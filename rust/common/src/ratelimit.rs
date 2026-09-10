use std::time::{Duration, Instant};

use tokio::sync::Mutex;
use tokio::time::sleep;

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

    /// Resolves when the caller may issue its request. The lock is held while sleeping on
    /// purpose so concurrent callers queue instead of racing.
    pub async fn wait(&self) {
        let mut last = self.last.lock().await;
        if let Some(t) = *last {
            let elapsed = t.elapsed();
            if elapsed < self.min {
                sleep(self.min - elapsed).await;
            }
        }
        *last = Some(Instant::now());
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
}
