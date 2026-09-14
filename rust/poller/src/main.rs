mod arxiv;

use std::time::Duration;

use anyhow::{Context, Result};
use arxiv::ArxivClient;
use clap::Parser;
use common::kafka;
use common::ratelimit::Limiter;
use common::schema::{Validators, validate_and_serialize};
use metrics::{Unit, counter, describe_counter, describe_gauge, describe_histogram, gauge, histogram};
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
    /// Redis key of the request budget shared with the fetcher and backfill script.
    #[arg(long, env = "ARXIV_RATELIMIT_KEY", default_value = common::ratelimit::DEFAULT_KEY)]
    ratelimit_key: String,
    /// Space requests in-process only instead of through the shared Redis budget (tests/offline).
    #[arg(long)]
    local_ratelimit: bool,
    /// Prometheus `/metrics` port; 0 disables the exporter.
    #[arg(long, env = "METRICS_PORT", default_value_t = 9101)]
    metrics_port: u16,
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
    page_errors: usize,
}

#[tokio::main]
async fn main() -> Result<()> {
    common::telemetry::init();
    let args = Args::parse();
    info!(?args.categories, interval_secs = args.interval_secs, once = args.once, dry_run = args.dry_run, "poller starting");

    if let Err(e) = common::telemetry::init_metrics(args.metrics_port) {
        warn!(error = %e, "metrics exporter not started; continuing without it");
    }
    describe_metrics();

    let min_interval = Duration::from_secs(args.min_request_interval_secs);
    let limiter = if args.local_ratelimit {
        Limiter::local(min_interval)
    } else {
        Limiter::shared(&args.redis_url, &args.ratelimit_key, min_interval)
            .await
            .context("shared rate limiter (pass --local-ratelimit to run without Redis)")?
    };
    info!(limiter = limiter.kind(), key = %args.ratelimit_key, interval_secs = args.min_request_interval_secs, "arxiv request budget");
    let arxiv = ArxivClient::new(&args.user_agent, limiter)?;
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
                        page_errors = s.page_errors,
                        "category done"
                    );
                    for (result, n) in [
                        ("produced", s.produced),
                        ("seen", s.skipped_seen),
                        ("invalid", s.invalid),
                    ] {
                        counter!("poller_papers_total", "category" => category.clone(), "result" => result)
                            .increment(n as u64);
                    }
                    total.pages += s.pages;
                    total.fetched += s.fetched;
                    total.produced += s.produced;
                    total.skipped_seen += s.skipped_seen;
                    total.invalid += s.invalid;
                    total.page_errors += s.page_errors;
                }
                Err(e) => error!(category, error = %format!("{e:#}"), "category failed; continuing"),
            }
        }
        info!(
            produced = total.produced,
            fetched = total.fetched,
            pages = total.pages,
            page_errors = total.page_errors,
            elapsed_s = started.elapsed().as_secs(),
            "cycle done"
        );
        counter!("poller_cycles_total").increment(1);
        histogram!("poller_cycle_seconds").record(started.elapsed().as_secs_f64());
        gauge!("poller_last_cycle_timestamp_seconds").set(chrono::Utc::now().timestamp() as f64);

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

fn describe_metrics() {
    describe_counter!("poller_cycles_total", "Completed poll cycles");
    describe_histogram!(
        "poller_cycle_seconds",
        Unit::Seconds,
        "Wall time of one poll cycle over all categories"
    );
    describe_counter!(
        "poller_papers_total",
        "Papers per category and result (produced|seen|invalid)"
    );
    describe_gauge!(
        "poller_last_cycle_timestamp_seconds",
        Unit::Seconds,
        "Unix time the last cycle finished"
    );
}

/// Walks a category newest-first and stops at the first page that yields nothing new.
async fn poll_category(ctx: &mut Ctx, category: &str) -> Result<CycleStats> {
    let mut stats = CycleStats::default();
    let mut start = 0usize;
    while stats.pages < ctx.args.max_pages {
        let page = match fetch_page_with_retry(ctx, category, start).await {
            Ok(page) => page,
            Err(e) if stats.pages == 0 => return Err(e),
            Err(e) => {
                // Keep what earlier pages already produced; the next cycle resumes from the top.
                warn!(category, start, error = %format!("{e:#}"), "page failed after retries; keeping earlier pages");
                stats.page_errors += 1;
                break;
            }
        };
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
        let mut throttled = false;
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
                throttled = e.downcast_ref::<reqwest::Error>().and_then(|r| r.status())
                    == Some(reqwest::StatusCode::TOO_MANY_REQUESTS);
                warn!(category, start, attempt, throttled, error = %format!("{e:#}"), "arxiv request failed; retrying");
                last_err = Some(e);
            }
        }
        // 429 means arXiv wants us slower, not more persistent: exponential backoff from 30 s.
        let delay = if throttled {
            30 * 2u64.pow(attempt - 1)
        } else {
            5 * attempt as u64
        };
        sleep(Duration::from_secs(delay)).await;
    }
    Err(last_err.unwrap_or_else(|| anyhow::anyhow!("arxiv returned empty pages for {category} at start={start}")))
}
