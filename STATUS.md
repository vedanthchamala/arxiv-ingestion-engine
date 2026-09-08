# Status

Plan approved 2026-09-07. Last update: 2026-09-07.

| Phase | State | Evidence |
|---|---|---|
| 1. Skeleton + poller | **done** | bootstrap: 6000 fetched, 5630 produced, 370 cross-list dups skipped, 0 invalid, 128 s; 6 partitions within 7% of even; `SCARD arxiv:seen` == messages; 0 duplicate keys |
| 2. Abstract-only search | planned | |
| 3. Spark inference | planned | |
| 4. Full text (Rust fetcher) | planned | |
| 5. Hardening | planned | |
| 6. Semantic cache | planned | |

## Tests
```
cd rust && cargo test && cargo clippy --all-targets
```
