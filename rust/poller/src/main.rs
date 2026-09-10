mod arxiv;

use std::time::Duration;

use anyhow::{Context, Result};
use arxiv::ArxivClient;
use clap::Parser;
use common::kafka;
use common::schema::{Validators, validate_and_serialize};
use rdkafka::producer::FutureProducer;
use redis::AsyncCommands;
use tokio::time::sleep;
use tracing::{error, info, warn};

/// Polls arXiv categories and produces one `papers.new` message per unseen paper version.
#[derive(Parser, Debug)]
#[command(version, about)]
struct Args {
    #[arg(long, env = "KAFKA_BROKERS", default_value = "localhost:19092")]
    brokers: String,
    #[arg(long, env = "REDIS_URL", default_value = "redis://localhost:6379")]
    redis_url: String,
    #[arg(long, env = "REDIS_SEEN_KEY", default_value = "arxiv:seen")]
    seen_key: String,
    #[arg(long, env = "TOPIC_PAPERS_NEW", default_value = "papers.new")]
    topic: String,
    #[arg(
        long,
        env = "ARXIV_CATEGORIES",
        default_value = "cs.LG,cs.CV,cs.RO",
        value_delimiter = ','
    )]
    categories: Vec<String>,
    #[arg(long, env = "ARXIV_POLL_INTERVAL_SECS", default_value_t = 900)]
    interval_secs: u64,
    #[arg(long, env = "ARXIV_PAGE_SIZE", default_value_t = 200)]
    page_size: usize,
    /// Upper bound on pages per category per cycle; bounds the very first (bootstrap) run.
    #[arg(long, env = "ARXIV_MAX_PAGES", default_value_t = 10)]
    max_pages: usize,
    #[arg(long, env = "ARXIV_MIN_REQUEST_INTERVAL_SECS", default_value_t = 3)]
    min_request_interval_secs: u64,
    #[arg(long, env = "ARXIV_USER_AGENT", default_value = "arxiv-ingest/0.1")]
    user_agent: String,
    /// Run one cycle and exit.
    #[arg(long)]
    once: bool,
    /// Parse and count, but neither produce nor mark anything as seen.
    #[arg(long)]
    dry_run: bool,
}

struct Ctx {
    args: Args,
    arxiv: ArxivClient,
    producer: FutureProducer,
    redis: redis::aio::MultiplexedConnection,
    validators: Validators,
}

#[derive(Debug, Default)]
struct CycleStats {
    pages: usize,
    fetched: usize,
    produced: usize,
    skipped_seen: usize,
    invalid: usize,
}

#[tokio::main]
async fn main() -> Result<()> {
    common::telemetry::init();
    let args = Args::parse();
    info!(?args.categories, interval_secs = args.interval_secs, once = args.once, dry_run = args.dry_run, "poller starting");

    let arxiv = ArxivClient::new(&args.user_agent, Duration::from_secs(args.min_request_interval_secs))?;
    let producer = kafka::producer(&args.brokers)?;
    let redis = redis::Client::open(args.redis_url.as_str())
        .context("redis url")?
        .get_multiplexed_async_connection()
        .await
        .context("redis connect")?;
    let validators = Validators::load()?;
    let mut ctx = Ctx {
        args,
        arxiv,
        producer,
        redis,
        validators,
    };

    loop {
        let started = std::time::Instant::now();
        let mut total = CycleStats::default();
        let categories = ctx.args.categories.clone();
        for category in &categories {
            match poll_category(&mut ctx, category).await {
                Ok(s) => {
                    info!(
                        category,
                        pages = s.pages,
                        fetched = s.fetched,
                        produced = s.produced,
                        seen = s.skipped_seen,
                        invalid = s.invalid,
                        "category done"
                    );
                    total.pages += s.pages;
                    total.fetched += s.fetched;
                    total.produced += s.produced;
                    total.skipped_seen += s.skipped_seen;
                    total.invalid += s.invalid;
                }
                Err(e) => error!(category, error = %e, "category failed; continuing"),
            }
        }
        info!(
            produced = total.produced,
            fetched = total.fetched,
            pages = total.pages,
            elapsed_s = started.elapsed().as_secs(),
            "cycle done"
        );

        if ctx.args.once {
            break;
        }
        tokio::select! {
            _ = sleep(Duration::from_secs(ctx.args.interval_secs)) => {}
            _ = tokio::signal::ctrl_c() => { info!("shutting down"); break; }
        }
    }
    Ok(())
}

/// Walks a category newest-first and stops at the first page that yields nothing new.
async fn poll_category(ctx: &mut Ctx, category: &str) -> Result<CycleStats> {
    let mut stats = CycleStats::default();
    let mut start = 0usize;
    while stats.pages < ctx.args.max_pages {
        let page = fetch_page_with_retry(ctx, category, start).await?;
        stats.pages += 1;
        if page.entries.is_empty() {
            break;
        }
        let n = page.entries.len();
        let mut new_in_page = 0usize;
        for paper in page.entries {
            stats.fetched += 1;
            let member = paper.seen_member();
            let seen: bool = ctx
                .redis
                .sismember(&ctx.args.seen_key, &member)
                .await
                .context("redis sismember")?;
            if seen {
                stats.skipped_seen += 1;
                continue;
            }
            let payload = match validate_and_serialize(&ctx.validators.paper, &paper) {
                Ok(p) => p,
                Err(e) => {
                    warn!(id = %member, error = %e, "dropping invalid paper");
                    stats.invalid += 1;
                    continue;
                }
            };
            if !ctx.args.dry_run {
                // Produce first, then mark seen: a crash in between yields a duplicate (workers
                // upsert), never a lost paper.
                kafka::send(&ctx.producer, &ctx.args.topic, paper.key(), &payload).await?;
                let _: i64 = ctx
                    .redis
                    .sadd(&ctx.args.seen_key, &member)
                    .await
                    .context("redis sadd")?;
            }
            stats.produced += 1;
            new_in_page += 1;
        }
        if new_in_page == 0 {
            break;
        }
        start += n;
        if start >= page.total_results {
            break;
        }
    }
    Ok(stats)
}

/// arXiv occasionally returns a transient empty page (or a 5xx); retry a few times before giving up.
async fn fetch_page_with_retry(ctx: &Ctx, category: &str, start: usize) -> Result<arxiv::Page> {
    let mut last_err = None;
    for attempt in 1..=4u32 {
        match ctx.arxiv.category_page(category, start, ctx.args.page_size).await {
            Ok(page) if page.entries.is_empty() && start < page.total_results && attempt < 4 => {
                warn!(
                    category,
                    start,
                    attempt,
                    total = page.total_results,
                    "empty page from arxiv; retrying"
                );
            }
            Ok(page) => return Ok(page),
            Err(e) => {
                warn!(category, start, attempt, error = %e, "arxiv request failed; retrying");
                last_err = Some(e);
            }
        }
        sleep(Duration::from_secs(5 * attempt as u64)).await;
    }
    Err(last_err.unwrap_or_else(|| anyhow::anyhow!("arxiv returned empty pages for {category} at start={start}")))
}
