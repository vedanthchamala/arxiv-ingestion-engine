//! Live check of the Redis-shared request budget. Needs a reachable Redis:
//!   REDIS_URL=redis://localhost:6379 cargo test -p common -- --ignored

use std::time::{Duration, Instant};

use common::ratelimit::SharedMinInterval;

const INTERVAL: Duration = Duration::from_millis(100);
const TASKS: usize = 3;
const WAITS_PER_TASK: usize = 3;

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "needs a live Redis; set REDIS_URL and run with --ignored"]
async fn concurrent_limiters_share_one_budget() {
    let Ok(url) = std::env::var("REDIS_URL") else {
        eprintln!("REDIS_URL unset; skipping");
        return;
    };
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let key = format!("arxiv:ratelimit:test:{}:{nanos}", std::process::id());

    // Each task owns its own limiter and connection, like three separate processes would.
    let mut handles = Vec::new();
    for _ in 0..TASKS {
        let lim = SharedMinInterval::connect(&url, &key, INTERVAL)
            .await
            .expect("connect to redis");
        handles.push(tokio::spawn(async move {
            let mut sends = Vec::with_capacity(WAITS_PER_TASK);
            for _ in 0..WAITS_PER_TASK {
                lim.wait().await;
                sends.push(Instant::now());
            }
            sends
        }));
    }
    let started = Instant::now();
    let mut sends = Vec::new();
    for h in handles {
        sends.extend(h.await.unwrap());
    }
    let total = started.elapsed();

    let mut conn = redis::Client::open(url.as_str())
        .unwrap()
        .get_multiplexed_async_connection()
        .await
        .unwrap();
    let _: () = redis::cmd("DEL").arg(&key).query_async(&mut conn).await.unwrap();

    sends.sort();
    assert_eq!(sends.len(), TASKS * WAITS_PER_TASK);
    let gaps: Vec<Duration> = sends.windows(2).map(|w| w[1] - w[0]).collect();
    let min_gap = gaps.iter().min().unwrap();
    eprintln!(
        "gaps (ms): {:?}, total {} ms",
        gaps.iter().map(|g| g.as_millis()).collect::<Vec<_>>(),
        total.as_millis()
    );
    assert!(*min_gap >= INTERVAL, "sends only {min_gap:?} apart");
    // Nine sends at 100 ms (+ guard) spacing take ~1.2 s; anything far beyond means slots were not reused.
    assert!(total < Duration::from_secs(3), "took {total:?}");
}
