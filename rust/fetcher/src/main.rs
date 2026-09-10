mod chunk;
mod fetch;
mod html;
mod pdf;

use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::Utc;
use chunk::Chunker;
use clap::Parser;
use common::kafka;
use common::models::{ChunkedPaper, Failed, Paper, SCHEMA_VERSION, Stage, TextSource};
use common::ratelimit::Limiter;
use common::schema::{Validators, validate_and_serialize};
use fetch::Fetcher;
use html::Section;
use metrics::{Unit, counter, describe_counter, describe_histogram, histogram};
use rdkafka::Message;
use rdkafka::consumer::{CommitMode, Consumer, StreamConsumer};
use rdkafka::message::BorrowedMessage;
use rdkafka::producer::FutureProducer;
use tokio::time::sleep;
use tracing::{error, info, warn};

/// Consumes `papers.new`, downloads full text (HTML, then PDF), chunks it, produces `papers.chunked`.
#[derive(Parser, Debug)]
#[command(version, about)]
struct Args {
    #[arg(long, env = "KAFKA_BROKERS", default_value = "localhost:19092")]
    brokers: String,
    #[arg(long, env = "FETCHER_GROUP", default_value = "fetcher")]
    group: String,
    #[arg(long, env = "TOPIC_PAPERS_NEW", default_value = "papers.new")]
    topic_in: String,
    #[arg(long, env = "TOPIC_PAPERS_CHUNKED", default_value = "papers.chunked")]
    topic_out: String,
    #[arg(long, env = "TOPIC_PAPERS_FAILED", default_value = "papers.failed")]
    topic_failed: String,
    #[arg(long, env = "ARXIV_USER_AGENT", default_value = "arxiv-ingest/0.1")]
    user_agent: String,
    #[arg(long, env = "ARXIV_MIN_REQUEST_INTERVAL_SECS", default_value_t = 3)]
    min_request_interval_secs: u64,
    #[arg(long, env = "REDIS_URL", default_value = "redis://localhost:6379")]
    redis_url: String,
    /// Redis key of the request budget shared with the poller and backfill script.
    #[arg(long, env = "ARXIV_RATELIMIT_KEY", default_value = common::ratelimit::DEFAULT_KEY)]
    ratelimit_key: String,
    /// Space requests in-process only instead of through the shared Redis budget (tests/offline).
    #[arg(long)]
    local_ratelimit: bool,
    /// Prometheus `/metrics` port; 0 disables the exporter.
    #[arg(long, env = "METRICS_PORT", default_value_t = 9102)]
    metrics_port: u16,
    /// Hugging Face id of the embedding model whose tokenizer sizes the chunks.
    #[arg(long, env = "TOKENIZER_MODEL", default_value = "BAAI/bge-m3")]
    tokenizer: String,
    #[arg(long, env = "CHUNK_TOKENS", default_value_t = 512)]
    chunk_tokens: usize,
    #[arg(long, env = "CHUNK_OVERLAP", default_value_t = 64)]
    chunk_overlap: usize,
    #[arg(long, env = "MAX_CHUNKS", default_value_t = 120)]
    max_chunks: usize,
    #[arg(long, env = "MAX_PDF_MB", default_value_t = 20)]
    max_pdf_mb: usize,
    /// Skip the HTML attempt and go straight to PDF.
    #[arg(long)]
    no_html: bool,
    /// Never download PDFs (HTML or abstract only).
    #[arg(long)]
    no_pdf: bool,
    /// Exit after this many messages (testing).
    #[arg(long)]
    max_messages: Option<usize>,
}

struct Ctx {
    args: Args,
    fetcher: Fetcher,
    chunker: Chunker,
    consumer: StreamConsumer,
    producer: FutureProducer,
    validators: Validators,
}

#[derive(Default)]
struct Stats {
    html: usize,
    pdf: usize,
    abstract_only: usize,
    failed: usize,
}

impl Stats {
    fn total(&self) -> usize {
        self.html + self.pdf + self.abstract_only + self.failed
    }
}

const MAX_ATTEMPTS: u32 = 3;

#[tokio::main]
async fn main() -> Result<()> {
    common::telemetry::init();
    let args = Args::parse();
    if let Err(e) = common::telemetry::init_metrics(args.metrics_port) {
        warn!(error = %e, "metrics exporter not started; continuing without it");
    }
    describe_metrics();
    pdf::ensure_pdftotext().await?;
    info!(tokenizer = %args.tokenizer, "loading tokenizer (downloads on first run)");
    let chunker = Chunker::from_pretrained(&args.tokenizer, args.chunk_tokens, args.chunk_overlap, args.max_chunks)?;
    let min_interval = Duration::from_secs(args.min_request_interval_secs);
    let limiter = if args.local_ratelimit {
        Limiter::local(min_interval)
    } else {
        Limiter::shared(&args.redis_url, &args.ratelimit_key, min_interval)
            .await
            .context("shared rate limiter (pass --local-ratelimit to run without Redis)")?
    };
    info!(limiter = limiter.kind(), key = %args.ratelimit_key, interval_secs = args.min_request_interval_secs, "arxiv request budget");
    let fetcher = Fetcher::new(&args.user_agent, limiter, args.max_pdf_mb * 1024 * 1024)?;
    let consumer = kafka::consumer(&args.brokers, &args.group)?;
    consumer.subscribe(&[args.topic_in.as_str()]).context("subscribe")?;
    let producer = kafka::producer(&args.brokers)?;
    let validators = Validators::load()?;
    info!(topic_in = %args.topic_in, topic_out = %args.topic_out, group = %args.group, "fetcher started");

    let ctx = Ctx {
        args,
        fetcher,
        chunker,
        consumer,
        producer,
        validators,
    };
    let mut stats = Stats::default();
    let mut last_report = Instant::now();

    loop {
        let msg = tokio::select! {
            r = ctx.consumer.recv() => match r {
                Ok(m) => m,
                Err(e) => { error!(error = %e, "consumer error"); sleep(Duration::from_secs(2)).await; continue; }
            },
            _ = tokio::signal::ctrl_c() => { info!("shutting down"); break; }
        };
        handle(&ctx, &msg, &mut stats).await;
        if let Err(e) = ctx.consumer.commit_message(&msg, CommitMode::Sync) {
            error!(error = %e, "commit failed");
        }
        if last_report.elapsed() > Duration::from_secs(30) {
            info!(
                html = stats.html,
                pdf = stats.pdf,
                abstract_only = stats.abstract_only,
                failed = stats.failed,
                "progress"
            );
            last_report = Instant::now();
        }
        if ctx.args.max_messages.is_some_and(|n| stats.total() >= n) {
            break;
        }
    }
    info!(
        html = stats.html,
        pdf = stats.pdf,
        abstract_only = stats.abstract_only,
        failed = stats.failed,
        "fetcher stopped"
    );
    Ok(())
}

async fn handle(ctx: &Ctx, msg: &BorrowedMessage<'_>, stats: &mut Stats) {
    let key = msg
        .key()
        .map(|k| String::from_utf8_lossy(k).into_owned())
        .unwrap_or_else(|| "?".into());
    let payload = msg.payload().unwrap_or_default();
    let paper: Paper = match serde_json::from_slice(payload) {
        Ok(p) => p,
        Err(e) => {
            stats.failed += 1;
            counter!("fetcher_papers_total", "result" => "failed").increment(1);
            dead_letter(ctx, &key, payload, &format!("unparseable message: {e}"), 1).await;
            return;
        }
    };
    let started = Instant::now();
    let mut last_err = String::new();
    for attempt in 1..=MAX_ATTEMPTS {
        counter!("fetcher_attempts_total").increment(1);
        match process(ctx, &paper).await {
            Ok((source, n_chunks)) => {
                match source {
                    TextSource::Html => stats.html += 1,
                    TextSource::Pdf => stats.pdf += 1,
                    TextSource::Abstract => stats.abstract_only += 1,
                }
                counter!("fetcher_papers_total", "result" => source.as_str()).increment(1);
                counter!("fetcher_chunks_total").increment(n_chunks as u64);
                histogram!("fetcher_process_seconds", "result" => source.as_str())
                    .record(started.elapsed().as_secs_f64());
                info!(id = %paper.versioned_id(), ?source, chunks = n_chunks, attempt, ms = started.elapsed().as_millis() as u64, "chunked");
                return;
            }
            Err(e) => {
                last_err = format!("{e:#}");
                warn!(id = %paper.versioned_id(), attempt, error = %last_err, "fetch failed");
                if attempt < MAX_ATTEMPTS {
                    sleep(Duration::from_secs(10 * attempt as u64)).await;
                }
            }
        }
    }
    stats.failed += 1;
    counter!("fetcher_papers_total", "result" => "failed").increment(1);
    histogram!("fetcher_process_seconds", "result" => "failed").record(started.elapsed().as_secs_f64());
    dead_letter(ctx, &key, payload, &last_err, MAX_ATTEMPTS).await;
}

fn describe_metrics() {
    describe_counter!(
        "fetcher_papers_total",
        "Papers finished, by result (html|pdf|abstract|failed)"
    );
    describe_counter!("fetcher_chunks_total", "Chunks produced to papers.chunked");
    describe_histogram!(
        "fetcher_process_seconds",
        Unit::Seconds,
        "Time from first attempt to result, by result"
    );
    describe_counter!(
        "fetcher_attempts_total",
        "Processing attempts, one per try (up to 3 per paper)"
    );
}

async fn process(ctx: &Ctx, paper: &Paper) -> Result<(TextSource, usize)> {
    let (source, sections) = full_text(ctx, paper).await?;
    let chunks = ctx.chunker.chunk(paper, &sections)?;
    let n = chunks.len();
    let doc = ChunkedPaper {
        schema_version: SCHEMA_VERSION,
        paper: paper.clone(),
        source,
        chunks,
        fetched_at: Utc::now(),
    };
    let bytes = validate_and_serialize(&ctx.validators.chunked, &doc)?;
    kafka::send(&ctx.producer, &ctx.args.topic_out, paper.key(), &bytes).await?;
    Ok((source, n))
}

/// HTML first (one request, structured), then PDF, then abstract-only. Only network and tool
/// failures are errors; "no full text exists" is a normal outcome.
async fn full_text(ctx: &Ctx, paper: &Paper) -> Result<(TextSource, Vec<Section>)> {
    if !ctx.args.no_html
        && let Some(url) = &paper.html_url
        && let Some(body) = ctx.fetcher.html(url).await?
    {
        let sections = html::extract_sections(&body);
        if !sections.is_empty() {
            return Ok((TextSource::Html, sections));
        }
        warn!(id = %paper.versioned_id(), "html page had no usable sections");
    }
    if !ctx.args.no_pdf
        && let Some(bytes) = ctx.fetcher.pdf(&paper.pdf_url).await?
    {
        let tmp = tempfile::Builder::new().suffix(".pdf").tempfile().context("tempfile")?;
        tokio::fs::write(tmp.path(), &bytes).await.context("write pdf")?;
        let raw = pdf::pdftotext(tmp.path()).await?;
        let sections = pdf::into_sections(&pdf::clean(&raw));
        if !sections.is_empty() {
            return Ok((TextSource::Pdf, sections));
        }
        warn!(id = %paper.versioned_id(), "pdf had no extractable text (scanned?)");
    }
    Ok((TextSource::Abstract, Vec::new()))
}

async fn dead_letter(ctx: &Ctx, key: &str, payload: &[u8], error: &str, attempts: u32) {
    let original = serde_json::from_slice(payload)
        .unwrap_or_else(|_| serde_json::json!({ "raw": String::from_utf8_lossy(payload) }));
    let failed = Failed {
        schema_version: SCHEMA_VERSION,
        arxiv_id: key.to_string(),
        stage: Stage::Fetcher,
        error: error.chars().take(2000).collect(),
        attempts,
        failed_at: Utc::now(),
        payload: original,
    };
    match validate_and_serialize(&ctx.validators.failed, &failed) {
        Ok(bytes) => {
            if let Err(e) = kafka::send(&ctx.producer, &ctx.args.topic_failed, key, &bytes).await {
                error!(key, error = %e, "could not write to dead-letter topic");
            } else {
                error!(key, error, attempts, "dead-lettered");
            }
        }
        Err(e) => error!(key, error = %e, "dead-letter message invalid"),
    }
}
