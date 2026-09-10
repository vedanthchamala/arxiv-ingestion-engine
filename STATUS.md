# Status

Plan approved 2026-09-07. Last update: 2026-09-10.

| Phase | State | Evidence |
|---|---|---|
| 1. Skeleton + poller | **done** | bootstrap: 6000 fetched, 5630 produced, 370 cross-list dups skipped, 0 invalid, 128 s; 6 partitions within 7% of even; `SCARD arxiv:seen` == messages; 0 duplicate keys |
| 2. Abstract-only search | **done** | 5630 papers embedded via Ollama `bge-m3` in ~9 min, 0 failures; `/search` p50 ≈ 65 ms; cs.RO query returns 5/5 cs.RO hits |
| 3. Spark inference | **done** 2026-09-08 | two vLLM containers on the Spark (`cu130-nightly`, ports 8001/8002, ~22 GB pinned on a shared box); ready in 150 s; 32 chunks embed in 0.36 s, 13.8 tok/s summaries; bge-m3 vectors match Ollama's (cosine 0.99999) so the existing index stays valid |
| 4. Full text (Rust fetcher) | **done** | live run on 15 papers: 13 via arXiv HTML (real section titles), 2 via PDF, 0 failures; 5–42 chunks each (median ~25), all ≤ 512 tokens; worker re-upserted them with summaries (~6.7 s/paper on the Spark, ~5.5 s locally) |
| 5. Hardening | **done** | DLQ (`papers.failed`) exercised with a poison message; re-processing under a fresh consumer group leaves every row count unchanged; `scripts/smoke.sh` checks seen-set, orphans, duplicate chunk ids, authors, lag, search; `scripts/backfill.py` (OAI-PMH) dry-run: 1 page = 1300 records, 616 matched |
| 6. Semantic cache | **done** | redisvl `SemanticCache` on `/search`, scoped by filters; paraphrase hit at distance 0.03; same query with different filters misses; `/stats` reports hit rate; `DELETE /cache` |
| 7. Shared arXiv budget | **done** 2026-09-10 | `schemas/ratelimit.lua` slot reservation used by poller, fetcher and backfill; poller + fetcher run concurrently against arXiv: 11 interleaved requests, min gap 3048 ms, 0 below 3000 (`scripts/ratelimit_verify.py`); Redis-down fallback and Lua contract unit-tested in both languages |
| 8. Fetcher DLQ, live | **done** 2026-09-10 | paper with an unresolvable host → 3 attempts (10 s, 20 s backoff) → `papers.failed` record `stage=fetcher, attempts=3` with the original payload; Rust test deserializes Python-produced `failed`/`chunked` fixtures (both directions of the contract now tested) |
| 9. Metrics + dashboard | **done** 2026-09-10 | Prometheus exporters on all four stages + API `/metrics`; compose `observability` profile (Prometheus, Grafana, provisioned "arXiv pipeline" dashboard, 13 panels); Redpanda native consumer-lag metrics enabled |
| 10. Containers | **done** 2026-09-10 | `rust/Dockerfile` + `python/Dockerfile`, compose `pipeline` profile (5 services); cold build 67 s, rebuild 9 s; images 236 MB (Rust) / 507 MB (Python); `--help`/import checks pass in-container |
| 11. Evaluation + load test | **done** 2026-09-10 | `BENCHMARKS.md`: self-retrieval recall@1 1.000 (n=500), paraphrase recall@1 0.795 / recall@10 0.945 (n=200), precision@5 0.987 (15 queries); API cold p50 105 ms, cache hit 34 ms, ~77 req/s at 8 and 32 clients with 0 errors; 15 API unit tests added (30 Python tests total) |
| 12. CI + GitHub | **done** 2026-09-10 | `.github/workflows/ci.yml`: rustfmt, clippy `-D warnings`, cargo test; ruff, pytest; JSON Schema and compose validation |
| 13. Continuous operation | **running** since 2026-09-10 | poller loop + fetcher + both workers + API via `scripts/run.sh`; the poller picked up 1819 papers from the Sep 8–9 announcements on its first steady-state cycles; full-text backlog (5615 abstract-only papers, ~6 h at the arXiv budget) in progress — see the table below |

## Live run log
| When | What |
|---|---|
| 2026-09-10 | started `scripts/run.sh start` (all five); see BENCHMARKS.md "Continuous run" for cycle counts, papers/day, backlog progress |

## Known gaps / next
- PDF section detection is coarse (usually one "Body" section) because `pdftotext` reflow merges heading lines; HTML papers get real sections. Improve with `-layout` parsing or `pdfplumber` if it matters.
- Summary prompt still yields "The paper …" openers; tighten if desired.
- Retrieval eval is self-retrieval plus category precision, not a human-labelled relevance set; no BM25 baseline yet.
- The HNSW index is not chosen by the planner at ~6K chunks (sequential exact scan is cheaper); re-measure once the full-text backlog multiplies the chunk count.
- No auth or TLS on the query API.

## Tests
```
cd rust   && cargo test && cargo clippy --all-targets -- -D warnings   # 17 tests (+1 Redis integration test: REDIS_URL=... cargo test -- --ignored)
cd python && uv run pytest && uv run ruff check .                        # 30 tests
./scripts/smoke.sh                                                       # live stack consistency
```
