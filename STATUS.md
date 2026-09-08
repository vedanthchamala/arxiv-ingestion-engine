# Status

Plan approved 2026-09-07. Last update: 2026-09-08.

| Phase | State | Evidence |
|---|---|---|
| 1. Skeleton + poller | **done** | bootstrap: 6000 fetched, 5630 produced, 370 cross-list dups skipped, 0 invalid, 128 s; 6 partitions within 7% of even; `SCARD arxiv:seen` == messages; 0 duplicate keys |
| 2. Abstract-only search | **done** | 5630 papers embedded via Ollama `bge-m3` in ~9 min, 0 failures; `/search` p50 ≈ 65 ms; cs.RO query returns 5/5 cs.RO hits |
| 3. Spark inference | **done** 2026-09-08 | two vLLM containers on the Spark (`cu130-nightly`, ports 8001/8002, ~22 GB pinned on a shared box); ready in 150 s; 32 chunks embed in 0.36 s, 13.8 tok/s summaries; bge-m3 vectors match Ollama's (cosine 0.99999) so the existing index stays valid |
| 4. Full text (Rust fetcher) | planned | |
| 5. Hardening | planned | |
| 6. Semantic cache | planned | |

## Tests
```
cd rust   && cargo test && cargo clippy --all-targets
cd python && uv run pytest && uv run ruff check .
```
