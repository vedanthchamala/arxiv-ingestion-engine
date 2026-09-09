# Status

Plan approved 2026-09-07. Last update: 2026-09-09.

| Phase | State | Evidence |
|---|---|---|
| 1. Skeleton + poller | **done** | bootstrap: 6000 fetched, 5630 produced, 370 cross-list dups skipped, 0 invalid, 128 s; 6 partitions within 7% of even; `SCARD arxiv:seen` == messages; 0 duplicate keys |
| 2. Abstract-only search | **done** | 5630 papers embedded via Ollama `bge-m3` in ~9 min, 0 failures; `/search` p50 ≈ 65 ms; cs.RO query returns 5/5 cs.RO hits |
| 3. Spark inference | **done** 2026-09-08 | two vLLM containers on the Spark (`cu130-nightly`, ports 8001/8002, ~22 GB pinned on a shared box); ready in 150 s; 32 chunks embed in 0.36 s, 13.8 tok/s summaries; bge-m3 vectors match Ollama's (cosine 0.99999) so the existing index stays valid |
| 4. Full text (Rust fetcher) | **done** | live run on 5 papers: 3 via arXiv HTML (real section titles), 2 via PDF; 21–31 chunks each, all ≤ 512 tokens; worker re-upserted them with summaries (~5.5 s/paper locally) |
| 5. Hardening | **done** | DLQ (`papers.failed`) exercised with a poison message; re-processing under a fresh consumer group leaves every row count unchanged; `scripts/smoke.sh` checks seen-set, orphans, duplicate chunk ids, authors, lag, search; `scripts/backfill.py` (OAI-PMH) dry-run: 1 page = 1300 records, 616 matched |
| 6. Semantic cache | **done** | redisvl `SemanticCache` on `/search`, scoped by filters; paraphrase hit at distance 0.03; same query with different filters misses; `/stats` reports hit rate; `DELETE /cache` |

## Known gaps / next
- **Full-text backlog**: 5625 of the 5630 bootstrapped papers are abstract-only. `scripts/pipeline.sh fetcher` will grind through them at ~4 s/paper (arXiv's limit) ≈ 6 h, with `scripts/pipeline.sh worker` alongside. Run only one fetcher, and not while `backfill.py` runs.
- PDF section detection is coarse (usually one "Body" section) because `pdftotext` reflow merges heading lines; HTML papers get real sections. Improve with `-layout` parsing or `pdfplumber` if it matters.
- Summary prompt still yields "The paper …" openers; tighten if desired.
- No Dockerfiles for the Python services yet; they run via `uv` on the host.
- No Prometheus/Grafana; consumer lag is visible in Redpanda Console (http://localhost:8080).

## Tests
```
cd rust   && cargo test && cargo clippy --all-targets      # 15 tests
cd python && uv run pytest && uv run ruff check .           # 10 tests
./scripts/smoke.sh                                          # live stack consistency
```
