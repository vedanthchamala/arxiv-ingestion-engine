use tracing_subscriber::EnvFilter;

/// `RUST_LOG` controls the filter; defaults to `info` for our crates and `warn` for librdkafka noise.
pub fn init() {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,rdkafka=warn,librdkafka=warn"));
    tracing_subscriber::fmt().with_env_filter(filter).with_target(false).init();
}
