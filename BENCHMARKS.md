# Benchmarks

Measured 2026-09-10, everything on one Apple M4 Pro MacBook:

- Corpus (from `/stats` at run time): **5630 papers, 6046 chunks**, 5614 abstract-only (chunk 0 = title + abstract),
  14 HTML + 2 PDF full-text papers. Categories polled: cs.LG, cs.CV, cs.RO (cross-lists included).
- Query embedding: Ollama `bge-m3` (1024-d) at `http://localhost:11434/v1`. The DGX Spark vLLM endpoint was not reachable.
- Store: PostgreSQL 17.11 + pgvector 0.8.6 in Docker, `chunks_embedding_hnsw` (`vector_cosine_ops`, `hnsw.ef_search=40`).
- Semantic cache: Redis 8 via redisvl, cosine distance threshold 0.12, scoped by filters.
- API under test: a fresh `arxiv_query_api` instance on `:8001` running the current code (connection pool with
  `prepare_threshold=None`, `GET /metrics`), embedding through Ollama. An older instance on `:8000`, configured for the
  unreachable Spark, was left alone and ignored. The Rust poller and fetcher were running on the host during the
  load test; neither uses Ollama or the query API.

The retrieval eval is **self-retrieval**, a sanity metric (does the index return the paper its own text came from),
not a human-relevance benchmark.

## Retrieval quality (`scripts/eval_search.py`, direct `db.search`, no cache)

Corpus: 5630 papers, 6046 chunks (5614 abstract-only); pgvector 0.8.6, hnsw.ef_search=40; embeddings bge-m3 via http://localhost:11434/v1; seed=42

### Self-retrieval (paper title -> its own paper), n=500, k=10

| Setting | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| unfiltered | 1.000 | 1.000 | 1.000 | 1.000 |
| categories=[primary_category] | 1.000 | 1.000 | 1.000 | 1.000 |

Filtered searches returning fewer than k hits: 6/500

### Paraphrase (first abstract sentence -> its own paper), n=200, k=10

| Setting | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---|---|---|---|
| unfiltered | 0.795 | 0.930 | 0.945 | 0.852 |

### Category precision@5 (unfiltered; hit counts if its `categories` contain the expected one)

| Query | Expected | In category |
|---|---|---|
| diffusion policy for robot manipulation | cs.RO | 5/5 |
| SLAM for legged robots | cs.RO | 5/5 |
| reinforcement learning for quadruped locomotion | cs.RO | 5/5 |
| grasp planning with tactile sensing | cs.RO | 5/5 |
| motion planning for autonomous driving in dense traffic | cs.RO | 4/5 |
| vision transformer pretraining on ImageNet | cs.CV | 5/5 |
| text-to-image generation with diffusion models | cs.CV | 5/5 |
| 3D Gaussian splatting for novel view synthesis | cs.CV | 5/5 |
| semantic segmentation of medical images | cs.CV | 5/5 |
| object detection in aerial drone imagery | cs.CV | 5/5 |
| gradient descent convergence in overparameterized networks | cs.LG | 5/5 |
| graph neural networks for molecular property prediction | cs.LG | 5/5 |
| federated learning with differential privacy | cs.LG | 5/5 |
| time series forecasting with transformers | cs.LG | 5/5 |
| scaling laws for large language model pretraining | cs.LG | 5/5 |
| **mean precision@5** | | **0.987** |

### Raw search latency (ms; one query at a time, no cache, connection with `prepare_threshold=None`)

| Stage | n | p50 | p95 | mean |
|---|---|---|---|---|
| query embedding (bge-m3) | 715 | 42.6 | 48.4 | 42.6 |
| SQL k=10 unfiltered | 715 | 29.1 | 31.1 | 29.1 |
| SQL k=10 filtered by category | 500 | 14.8 | 17.5 | 13.6 |

What these numbers do and do not say:

- Self-retrieval at 1.000 is expected, not impressive: the title is verbatim inside the embedded chunk. It shows the
  index is intact and the category filter does not lose the target; it says nothing about relevance to real queries.
- Paraphrase recall is the weaker, more honest number: 11/200 first sentences did not retrieve their paper in the top 10
  and 30 ranked below 1. Misses are generic openers ("With the rapid advancement of large language models, ...").
- The corpus is abstract-only for 5614/5630 papers, so full-text chunk retrieval is effectively not benchmarked.
- The 6 filtered searches with < 10 hits are papers whose primary category (math.AG, math.AC, q-fin.GN, econ.GN) has
  fewer than 10 papers in the corpus; not a retrieval failure.
- `EXPLAIN ANALYZE` shows the planner does **not** use the HNSW index at this size: both queries are a sequential exact
  scan over all chunks, hash join, top-N sort. Forcing the index (`enable_seqscan=off`) did not change latency.
- Prepared statements used to double the SQL time. The first run of this eval (default psycopg `prepare_threshold=5`)
  measured unfiltered SQL at p50 59.7 ms and filtered at 24.1 ms: on a fresh connection the first five executions took
  ~24 ms, from the sixth the prepared statement's generic plan could not fold the `%(cats)s IS NULL OR ...` shortcuts
  and took ~55 ms. The API's `ConnectionPool` now passes `prepare_threshold=None` (and this script's connection does the
  same), which brought unfiltered SQL to 29.1 ms and filtered to 14.8 ms, and the API's cold `/search` p50 from
  105 ms to 74 ms (server-side `took_ms` 100 -> 69) between the two load-test runs.
- Embedding p50 of ~43 ms is Ollama with idle gaps between calls (what a single user sees); back-to-back it is ~29 ms.

## Query API load test (`scripts/bench_api.py`, full HTTP round trip)

Target http://127.0.0.1:8001; 60 distinct queries, k=10; corpus 5630 papers / 6046 chunks; latency = full HTTP round trip

| Phase | Conc. | Requests | Errors | req/s | Cached | p50 ms | p95 ms | p99 ms | mean ms | max ms |
|---|---|---|---|---|---|---|---|---|---|---|
| cold (cache cleared, sequential) | 1 | 60 | 0 | - | 0% | 74 | 79 | 105 | 73 | 105 |
| warm (same queries again) | 1 | 60 | 0 | - | 100% | 34 | 39 | 40 | 35 | 40 |
| paraphrases of cached queries | 1 | 10 | 0 | - | 90% | 32 | 65 | 65 | 35 | 65 |
| sweep 8 workers x 20 s (cache cleared first) | 8 | 1662 | 0 | 82.7 | 96% | 92 | 99 | 237 | 96 | 338 |
| sweep 32 workers x 20 s (cache cleared first) | 32 | 1710 | 0 | 83.9 | 95% | 371 | 388 | 1053 | 378 | 1104 |

Server-side `took_ms` (embed + cache lookup or SQL; excludes HTTP and serialization):

| Phase | p50 ms | p95 ms | p99 ms | mean ms |
|---|---|---|---|---|
| cold (cache cleared, sequential) | 69 | 74 | 103 | 69 |
| warm (same queries again) | 31 | 34 | 35 | 31 |
| paraphrases of cached queries | 30 | 62 | 62 | 33 |
| sweep 8 workers x 20 s (cache cleared first) | 89 | 96 | 231 | 93 |
| sweep 32 workers x 20 s (cache cleared first) | 369 | 385 | 1044 | 374 |

Sweep latency split by cache outcome (the pool is only 60 queries, so most sweep traffic hits):

| Phase | uncached n | uncached p50 | uncached p95 | cached n | cached p50 | cached p95 |
|---|---|---|---|---|---|---|
| sweep 8 workers x 20 s (cache cleared first) | 60 | 227 | 292 | 1602 | 92 | 97 |
| sweep 32 workers x 20 s (cache cleared first) | 78 | 950 | 1090 | 1632 | 371 | 381 |

Semantic-cache hits for paraphrased queries:

| Cached query | Paraphrase | Hit |
|---|---|---|
| diffusion policy for robot manipulation | diffusion policies for robotic manipulation | yes |
| SLAM for legged robots | simultaneous localization and mapping on legged robots | no |
| vision transformer pretraining on ImageNet | pretraining vision transformers on ImageNet | yes |
| text-to-image generation with diffusion models | generating images from text using diffusion models | yes |
| gradient descent convergence in overparameterized networks | convergence of gradient descent for overparameterized neural networks | yes |
| graph neural networks for molecular property prediction | predicting molecular properties with graph neural networks | yes |
| federated learning with differential privacy | differentially private federated learning | yes |
| 3D Gaussian splatting for novel view synthesis | novel view synthesis using 3D Gaussian splatting | yes |
| reinforcement learning for quadruped locomotion | quadruped robot locomotion via reinforcement learning | yes |
| time series forecasting with transformers | transformer models for time series forecasting | yes |
| **hit rate** | | **9/10** |

What these numbers do and do not say:

- A cold search is ~74 ms end to end (≈ 43 ms embed + ≈ 29 ms SQL); a cache hit is ~34 ms, all of it the query
  embedding plus a Redis vector lookup. Every request embeds, so the cache cannot go below Ollama's per-call time.
- Throughput saturates at **~83 req/s at 8 workers and does not rise at 32**; that is Ollama's embedding throughput on
  this Mac (measured directly: ~77 embeds/s). 32 workers only add queueing (p50 92 -> 371 ms). No errors at either level.
- The sweep is 95-96% cache hits because the pool has 60 queries and is exhausted within a second; the uncached rows
  (60 and 78 requests) are the honest under-load numbers for a miss: p50 227 ms at 8 workers, 950 ms at 32.
- The 32-worker tail was worse in this run than in the first (p99 1053 vs 691 ms, uncached p50 950 vs 590 ms) while
  p50 improved; the poller and fetcher were running on the host this time, so treat the 32-worker tail as noisy.
- Cache paraphrase hits: 9/10. The miss is an acronym expansion (SLAM -> simultaneous localization and mapping), whose
  bge-m3 distance exceeds the 0.12 threshold.

## Full corpus: 229,487 chunks (2026-09-12)

After the full-text worker drained the backlog the corpus is **7,707 papers, 229,487 chunks, 7,610 with
full text (7,294 HTML, 316 PDF), 7,672 summaries**. Two things changed versus the 6K-chunk runs above.

**The planner now uses the HNSW index** (`Index Scan using chunks_embedding_hnsw`, ~5 ms for the ANN
step), and approximate search has a recall cost that pgvector's default `hnsw.ef_search = 40` makes
visible. Same `eval_search.py` protocol (seed 42, n = 500 titles / 200 first sentences, 15 category
queries), sweeping `ef_search` per query:

| `ef_search` | Title → own paper R@1 | R@5 | Paraphrase R@1 | R@10 | precision@5 | SQL p50 unfiltered | SQL p50 filtered |
|---|---|---|---|---|---|---|---|
| 40 (pgvector default) | 0.936 | 0.942 | 0.705 | 0.885 | 0.960 | 10.6 ms | 6.0 ms |
| 100 | 0.976 | 0.982 | 0.720 | 0.905 | 0.960 | 15.2 ms | 6.5 ms |
| **200 (chosen)** | **0.986** | **0.992** | **0.725** | **0.910** | **0.960** | **20.4 ms** | **9.1 ms** |
| 400 | 0.994 | 1.000 | 0.725 | 0.915 | 0.960 | 26.8 ms | 13.1 ms |

The misses at 40 are total (the paper is absent from the 200-candidate window, not merely ranked low),
i.e. HNSW recall, not ranking. 200 is the default (`HNSW_EF_SEARCH`), applied with `SET LOCAL` per
query; it buys back 5 points of recall for ~10 ms. Paraphrase recall is lower than on the abstract-only
corpus (0.795 → 0.725 at R@1) because body chunks from other papers now compete with the target's
abstract chunk; precision@5 is 0.960 (was 0.987).

**Query API on the full corpus** (`bench_api.py`, `ef_search` 200, same 60-query pool, one API on :8000):

| Phase | Conc. | Requests | Errors | req/s | Cached | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|---|---|---|
| cold (cache cleared, sequential) | 1 | 60 | 0 | - | 0 % | 58 | 72 | 134 |
| warm (same queries again) | 1 | 60 | 0 | - | 100 % | 36 | 42 | 45 |
| paraphrases of cached queries | 1 | 10 | 0 | - | 90 % | 39 | 68 | 68 |
| sweep 8 workers × 20 s | 8 | 1738 | 0 | 86.5 | 97 % | 92 | 100 | 103 |
| sweep 32 workers × 20 s | 32 | 1742 | 0 | 85.6 | 97 % | 371 | 386 | 392 |

Cold search got faster with 38× more chunks (74 → 58 ms p50) because the index replaced the sequential
scan. The throughput ceiling is unchanged and is still Ollama's embedding rate on the laptop.

## How to reproduce

```
docker compose up -d && ollama serve                     # Postgres/pgvector, Redis, Redpanda; Ollama with `ollama pull bge-m3`
uv run --project python python -m uvicorn arxiv_query_api.main:app --port 8001   # query API on Ollama defaults, if none is running (do not source .env: it points at the Spark)
uv run --project python python scripts/eval_search.py --n 500 --paraphrase-n 200 --seed 42 [--ef-search 200]   # -> data/eval_search.json
uv run --project python python scripts/bench_api.py --base-url http://127.0.0.1:8001 --duration 20 --concurrency 8,32   # -> data/bench_api.json
uv run --project python ruff check --config python/pyproject.toml scripts/
```
