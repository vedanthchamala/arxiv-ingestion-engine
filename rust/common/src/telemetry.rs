use std::net::{Ipv4Addr, SocketAddr};
use std::time::Duration;

use anyhow::{Context, Result};
use metrics::{Unit, counter, describe_counter, describe_histogram, histogram};
use metrics_exporter_prometheus::{Matcher, PrometheusBuilder};
use tracing::info;
use tracing_subscriber::EnvFilter;

/// `RUST_LOG` controls the filter; defaults to `info` for our crates and `warn` for librdkafka noise.
pub fn init() {
    let filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info,rdkafka=warn,librdkafka=warn"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .init();
}

/// Buckets for every `*_seconds` histogram: sub-second limiter waits up to multi-minute fetches.
const SECONDS_BUCKETS: &[f64] = &[
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
];

/// Serves Prometheus text format at `http://0.0.0.0:<port>/metrics`; `port == 0` disables the
/// exporter and every `metrics` macro becomes a no-op. Call inside the Tokio runtime: the HTTP
/// server is spawned onto it. Binding errors are returned so the caller can decide whether an
/// occupied port is fatal.
pub fn init_metrics(port: u16) -> Result<()> {
    if port == 0 {
        info!("metrics exporter disabled (port 0)");
        return Ok(());
    }
    let addr = SocketAddr::from((Ipv4Addr::UNSPECIFIED, port));
    PrometheusBuilder::new()
        .with_http_listener(addr)
        .set_buckets_for_metric(Matcher::Suffix("_seconds".into()), SECONDS_BUCKETS)
        .context("histogram buckets")?
        .install()
        .with_context(|| format!("prometheus exporter on {addr}"))?;
    describe_counter!(
        "arxiv_requests_total",
        "Requests sent to arxiv.org, by process, kind (api|html|pdf) and outcome (ok|not_found|error)"
    );
    describe_histogram!(
        "arxiv_ratelimit_wait_seconds",
        Unit::Seconds,
        "Time slept waiting for the shared arXiv request slot"
    );
    info!(%addr, "metrics exporter serving /metrics");
    Ok(())
}

/// One outgoing arxiv.org request finished. Label values are a contract shared with the Python
/// side; keep them to `kind` in {api, html, pdf} and `outcome` in {ok, not_found, error}.
pub fn record_arxiv_request(process: &'static str, kind: &'static str, outcome: &'static str) {
    counter!("arxiv_requests_total", "process" => process, "kind" => kind, "outcome" => outcome).increment(1);
}

/// Time a request spent waiting for its slot, as returned by `Limiter::wait`.
pub fn record_ratelimit_wait(process: &'static str, waited: Duration) {
    histogram!("arxiv_ratelimit_wait_seconds", "process" => process).record(waited.as_secs_f64());
}
