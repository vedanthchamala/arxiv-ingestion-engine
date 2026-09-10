//! arXiv request spacing: one request every N seconds across every process we run.
//!
//! `MinInterval` spaces requests inside one process. `SharedMinInterval` reserves send slots in
//! Redis through `schemas/ratelimit.lua`, so the poller, the fetcher and the backfill script draw
//! on one budget and may run at the same time. `Limiter` lets a client take either.

use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use redis::Script;
use redis::aio::MultiplexedConnection;
use tokio::sync::Mutex;
use tokio::time::{sleep, timeout};
use tracing::warn;

/// The reservation script, embedded so the binaries agree with Python on the exact contract.
pub const RATELIMIT_LUA: &str = include_str!("../../../schemas/ratelimit.lua");
pub const DEFAULT_KEY: &str = "arxiv:ratelimit";

const REDIS_TIMEOUT: Duration = Duration::from_secs(5);
/// Slots are reserved `min + SLOT_GUARD` apart. A sleeper can wake a few ms late while the next
/// process lands exactly on its slot, so without a guard consecutive sends measure `min - jitter`;
/// with it the configured interval is a true floor (observed jitter ~1 ms; cost 1.7% at 3 s).
pub const SLOT_GUARD: Duration = Duration::from_millis(50);

/// Enforces a minimum spacing between requests *and* serializes callers, which is exactly
/// arXiv's rule: one request every N seconds over a single connection.
pub struct MinInterval {
    min: Duration,
    last: Mutex<Option<Instant>>,
}

impl MinInterval {
    pub fn new(min: Duration) -> Self {
        Self {
            min,
            last: Mutex::new(None),
        }
    }

    /// Resolves when the caller may issue its request and returns how long it slept. The lock is
    /// held while sleeping on purpose so concurrent callers queue instead of racing.
    pub async fn wait(&self) -> Duration {
        let mut last = self.last.lock().await;
        let mut waited = Duration::ZERO;
        if let Some(t) = *last {
            let elapsed = t.elapsed();
            if elapsed < self.min {
                waited = self.min - elapsed;
                sleep(waited).await;
            }
        }
        *last = Some(Instant::now());
        waited
    }

    /// Records a send that was spaced by someone else (the shared limiter), so a later fallback to
    /// in-process spacing still knows when this process last talked to arXiv.
    async fn mark_sent(&self) {
        *self.last.lock().await = Some(Instant::now());
    }
}

/// Redis-backed limiter: every call reserves the next free slot on the shared key (FIFO across all
/// processes, Redis server time as the single clock) and sleeps until it.
pub struct SharedMinInterval {
    client: redis::Client,
    conn: Mutex<Option<MultiplexedConnection>>,
    script: Script,
    key: String,
    min: Duration,
    fallback: MinInterval,
}

impl SharedMinInterval {
    /// Connects eagerly so a misconfigured Redis fails at startup, not on the first request.
    pub async fn connect(redis_url: &str, key: &str, min: Duration) -> Result<Self> {
        let client = redis::Client::open(redis_url).context("redis url")?;
        let conn = open(&client).await?;
        Ok(Self {
            client,
            conn: Mutex::new(Some(conn)),
            script: Script::new(RATELIMIT_LUA),
            key: key.to_string(),
            min,
            fallback: MinInterval::new(min),
        })
    }

    pub fn key(&self) -> &str {
        &self.key
    }

    /// Reserves the next slot and sleeps until it; returns the time slept. If Redis is unreachable
    /// it warns and uses in-process spacing for this call: a Redis blip must never stop ingestion,
    /// but must never skip the spacing either.
    pub async fn wait(&self) -> Duration {
        match self.reserve().await {
            Ok(ms) => {
                let waited = Duration::from_millis(ms);
                if !waited.is_zero() {
                    sleep(waited).await;
                }
                self.fallback.mark_sent().await;
                waited
            }
            Err(e) => {
                warn!(error = %e, key = %self.key, "shared rate limiter unavailable; in-process spacing for this request");
                self.fallback.wait().await
            }
        }
    }

    /// Runs the reservation script (EVALSHA, loading it on NOSCRIPT) and returns the ms to sleep.
    /// The connection is dropped on any error so the next call reconnects: `MultiplexedConnection`
    /// does not reconnect on its own.
    async fn reserve(&self) -> Result<u64> {
        let mut guard = self.conn.lock().await;
        if guard.is_none() {
            *guard = Some(open(&self.client).await?);
        }
        let conn = guard.as_mut().expect("connection was just set");
        let mut invocation = self.script.prepare_invoke();
        invocation
            .key(&self.key)
            .arg((self.min + SLOT_GUARD).as_millis() as u64);
        let result = match timeout(REDIS_TIMEOUT, invocation.invoke_async::<u64>(conn)).await {
            Ok(Ok(ms)) => return Ok(ms),
            Ok(Err(e)) => Err(e).context("ratelimit script"),
            Err(_) => Err(anyhow!("ratelimit script timed out after {REDIS_TIMEOUT:?}")),
        };
        *guard = None;
        result
    }
}

async fn open(client: &redis::Client) -> Result<MultiplexedConnection> {
    timeout(REDIS_TIMEOUT, client.get_multiplexed_async_connection())
        .await
        .map_err(|_| anyhow!("redis connect timed out after {REDIS_TIMEOUT:?}"))?
        .context("redis connect")
}

/// What an arXiv client spaces its requests with: the shared budget by default, in-process spacing
/// for tests and offline runs (`--local-ratelimit`).
pub enum Limiter {
    Local(MinInterval),
    Shared(Box<SharedMinInterval>),
}

impl Limiter {
    pub fn local(min: Duration) -> Self {
        Self::Local(MinInterval::new(min))
    }

    pub async fn shared(redis_url: &str, key: &str, min: Duration) -> Result<Self> {
        Ok(Self::Shared(Box::new(
            SharedMinInterval::connect(redis_url, key, min).await?,
        )))
    }

    /// Resolves when the caller may send; returns the time slept.
    pub async fn wait(&self) -> Duration {
        match self {
            Self::Local(l) => l.wait().await,
            Self::Shared(s) => s.wait().await,
        }
    }

    pub fn kind(&self) -> &'static str {
        match self {
            Self::Local(_) => "local",
            Self::Shared(_) => "shared",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn spaces_requests() {
        let lim = MinInterval::new(Duration::from_millis(50));
        let t0 = Instant::now();
        lim.wait().await;
        lim.wait().await;
        lim.wait().await;
        assert!(t0.elapsed() >= Duration::from_millis(100));
    }

    #[tokio::test]
    async fn reports_time_slept() {
        let lim = MinInterval::new(Duration::from_millis(30));
        assert_eq!(lim.wait().await, Duration::ZERO);
        let second = lim.wait().await;
        assert!(second > Duration::ZERO && second <= Duration::from_millis(30));
    }
}
