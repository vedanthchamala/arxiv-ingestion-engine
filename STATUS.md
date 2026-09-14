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
| 11. Evaluation + load test | **done** 2026-09-10 | `BENCHMARKS.md`: self-retrieval recall@1 1.000 (n=500), paraphrase recall@1 0.795 / recall@10 0.945 (n=200), precision@5 0.987 (15 queries); API cold p50 74 ms (105 before the prepared-statement fix), cache hit 34 ms, ~83 req/s at 8 and 32 clients with 0 errors; 15 API unit tests added (30 Python tests total) |
| 12. CI + GitHub | **done** 2026-09-11 | pushed to `github.com/vedanthchamala/arxiv-ingestion-engine`; `.github/workflows/ci.yml`: rustfmt, clippy `-D warnings`, cargo test; ruff, pytest; JSON Schema and compose validation. First run failed on runner setup only (librdkafka needs libcurl/SASL/SSL headers; `uv sync` needs `--all-packages` in a workspace), fixed in the next commit |
| 13. Continuous operation | **running** since 2026-09-10 16:59 UTC | poller: 211 cycles in 95 h without a crash before a deploy restart on 2026-09-14; picked up both announcement days in its window (2026-09-10: 437 papers; 2026-09-13: 347 papers, ingested 01:49–10:31 UTC); fetcher drained the backlog in 10.4 h (7,881 papers, 235,503 chunks); full-text worker drained its 10k queue in 12.6 h on one instance; every lag 0; DB 2026-09-14: 8,054 papers = every id in the seen-set, 239,912 chunks, 7,957 full-text, 8,019 summaries, 0 gappy chunk sets, 0 orphans. **Caveat:** since 2026-09-11 17:20 UTC arXiv's API answers 429 Too Many Requests on ~68 % of the poller's requests (893 of 1,321) even at the documented 3 s spacing, so cycles spend 3–7 min in retries; the announcements got through because retries eventually succeed |
| 14. Crash test | **done** 2026-09-11 | SIGKILL of the full-text worker mid-message, restart: the in-flight paper (2604.14501v2) was redelivered and stored again; 0 gappy chunk sets, 0 orphans, counts consistent. Found and fixed on the way: replay of `papers.new` could downgrade full-text rows, and a pre-check `SELECT` on a non-autocommit connection stopped every later transaction from committing (decision 30) |
| 15. Full-corpus tuning | **done** 2026-09-12 | at 229k chunks the planner uses HNSW and `ef_search` 40 lost 6 points of title recall@1; swept 40/100/200/400, default now 200 (`HNSW_EF_SEARCH`, `SET LOCAL` per query): recall@1 0.986, SQL p50 20 ms; API cold p50 58 ms, hit 36 ms, ~86 req/s, 0 errors (BENCHMARKS.md) |

## Live run log
| When | What |
|---|---|
| 2026-09-10 16:59Z | started poller, fetcher and API via `scripts/run.sh`; workers at 17:06Z |
| 2026-09-10 17:5xZ | both workers crashed (decision 30); fixed and restarted 17:48Z; full-text worker stopped again at 17:51Z by a SIGTERM from an interrupted shell and stayed down overnight (10,032 chunked papers queued, nothing lost) |
| 2026-09-11 03:20Z | fetcher finished the backlog: 7,881 papers, 235,503 chunks |
| 2026-09-11 17:30Z | poller 63 cycles / 25 h; DB 7,347 papers, 97 with full text (worker down), 0 gappy chunk sets, 0 orphans; DLQ 35 records (33 fetcher connect errors incl. 1 probe, 2 worker poison probes) |

| 2026-09-11 19:15Z | GitHub repo created and pushed; CI red on runner setup, fixed, green on the second run; two full-text workers started (partitions split 3/3) |
| 2026-09-11 ~20:00Z | both full-text workers died on `_MAX_POLL_EXCEEDED`: Ollama on the laptop stalled under two concurrent 7B generations plus the abstract worker's embeddings, one message exceeded the 10-min poll interval, and the worker treated that consumer error as fatal. Fixed: only `fatal()` consumer errors abort (librdkafka rejoins after an eviction), `max.poll.interval.ms` 30 min, inference timeout 120 s; one worker restarted — scaling workers needs the GPU box, not the laptop |

| 2026-09-12 11:27Z | full-text worker finished the queue: 7,732 stored since its 22:50Z restart, 5 dead-lettered (`PostgreSQL text fields cannot contain NUL`, fixed: NUL stripped in the Rust extractors and the Python sink); all 7,707 papers present, 7,610 with full text |
| 2026-09-12 17:30Z | full-corpus eval + load test; `ef_search` 200 made the default; API restarted; smoke test 7/7 |

| 2026-09-14 01:49–10:31Z | Sunday's announcement (347 papers) picked up, fetched, embedded and summarized by the running pipeline; nothing restarted |
| 2026-09-14 16:20Z | investigation: the poller's `produced=0` cycles since Sep 11 were an under-reporting bug (a failed later page discarded the category's partial stats and metrics) and the hidden error was arXiv **429 Too Many Requests** (logged only as `arxiv request` because the error chain was not printed). Fixed both; poller restarted on the new binary (its 95-h run ended here) |

Append a row with `scripts/snapshot.sh`.

## Known gaps / next
- PDF section detection is coarse (usually one "Body" section) because `pdftotext` reflow merges heading lines; HTML papers get real sections. Improve with `-layout` parsing or `pdfplumber` if it matters.
- Summary prompt still yields "The paper …" openers; tighten if desired.
- Retrieval eval is self-retrieval plus category precision, not a human-labelled relevance set; no BM25 baseline yet.
- **arXiv 429s.** From 2026-09-11 17:20 UTC the export API rejects most requests with 429 even though the shared budget keeps us at one request per 3.05 s; a probe through the same budget got 429 in 0.3 s. Cause unknown (possibly a longer-window quota tripped by the 8,348-request full-text run). The poller now backs off 30/60/120/240 s on 429 and counts them as `outcome="throttled"`; a cool-down experiment (pause polling for hours, then resume) is the next step.
- No auth or TLS on the query API.
- Full-text worker throughput on the laptop (~6 s/paper with local summaries) is below the fetcher's (~4.7 s/paper); a second laptop worker made Ollama stall, so keeping up on a big day needs the Spark vLLM servers.
- Five papers are dead-lettered with NUL bytes in their text; the fix is in, they can be replayed from `papers.failed`.

## Tests
```
cd rust   && cargo test && cargo clippy --all-targets -- -D warnings   # 17 tests (+1 Redis integration test: REDIS_URL=... cargo test -- --ignored)
cd python && uv run pytest && uv run ruff check .                        # 33 tests (2 skip without the live stack)
./scripts/smoke.sh                                                       # live stack consistency
```
