# Benchmarks

Measured 2026-09-10, everything on one Apple M4 Pro MacBook:

- Corpus (from `/stats` at run time): **5630 papers, 5992 chunks**, 5615 abstract-only (chunk 0 = title + abstract),
  13 HTML + 2 PDF full-text papers. Categories polled: cs.LG, cs.CV, cs.RO (cross-lists included).
- Query embedding: Ollama `bge-m3` (1024-d) at `http://localhost:11434/v1`. The DGX Spark vLLM endpoint was not reachable.
- Store: PostgreSQL 17.11 + pgvector 0.8.6 in Docker, `chunks_embedding_hnsw` (`vector_cosine_ops`, `hnsw.ef_search=40`).
- Semantic cache: Redis 8 via redisvl, cosine distance threshold 0.12, scoped by filters.
- The long-running query API on `:8000` was started with `EMBEDDING_URL` pointing at the Spark and returned HTTP 500 for
  every `/search`, so the load test ran against a second, identical `arxiv_query_api` process on `:8001` using the
  config defaults (Ollama). Same Postgres and Redis. It was stopped afterwards.

The retrieval eval is **self-retrieval**, a sanity metric (does the index return the paper its own text came from),
not a human-relevance benchmark.

## Retrieval quality (`scripts/eval_search.py`, direct `db.search`, no cache)

Corpus: 5630 papers, 5992 chunks (5615 abstract-only); pgvector 0.8.6, hnsw.ef_search=40; embeddings bge-m3 via http://localhost:11434/v1; seed=42

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

### Raw search latency (ms; one query at a time, no cache)

| Stage | n | p50 | p95 | mean |
|---|---|---|---|---|
| query embedding (bge-m3) | 715 | 51.7 | 64.3 | 51.6 |
| SQL k=10 unfiltered | 715 | 59.7 | 63.2 | 59.4 |
| SQL k=10 filtered by category | 500 | 24.1 | 28.6 | 22.1 |

What these numbers do and do not say:

- Self-retrieval at 1.000 is expected, not impressive: the title is verbatim inside the embedded chunk. It shows the
  index is intact and the category filter does not lose the target; it says nothing about relevance to real queries.
- Paraphrase recall is the weaker, more honest number: 11/200 first sentences did not retrieve their paper in the top 10
  and 30 ranked below 1. Misses are generic openers ("With the rapid advancement of large language models, ...").
- The corpus is abstract-only for 5615/5630 papers, so full-text chunk retrieval is effectively not benchmarked.
- The 6 filtered searches with < 10 hits are papers whose primary category (math.AG, math.AC, q-fin.GN, econ.GN) has
  fewer than 10 papers in the corpus; not a retrieval failure.
- `EXPLAIN ANALYZE` shows the planner does **not** use the HNSW index at this size: both queries are a sequential exact
  scan over all 5992 chunks, hash join, top-N sort. Forcing the index (`enable_seqscan=off`) does not change latency.
- The ~60 ms unfiltered SQL is mostly psycopg's prepared-statement generic plan: on a fresh connection the first five
  executions take ~24 ms, from the sixth (`prepare_threshold=5`) ~55 ms, because the generic plan cannot fold the
  `%(cats)s IS NULL OR ...` shortcuts. With `prepare_threshold=None`: unfiltered ~24 ms, filtered ~11 ms. The API's
  `ConnectionPool` uses the default, so it pays this after warm-up. Not changed here; it is a one-line fix.
- Embedding p50 of ~52 ms is Ollama with idle gaps between calls (what a single user sees); back-to-back it is ~29 ms.

## Query API load test (`scripts/bench_api.py`, full HTTP round trip)

Target http://127.0.0.1:8001; 60 distinct queries, k=10; corpus 5630 papers / 5992 chunks; latency = full HTTP round trip

| Phase | Conc. | Requests | Errors | req/s | Cached | p50 ms | p95 ms | p99 ms | mean ms | max ms |
|---|---|---|---|---|---|---|---|---|---|---|
| cold (cache cleared, sequential) | 1 | 60 | 0 | - | 0% | 105 | 119 | 143 | 102 | 143 |
| warm (same queries again) | 1 | 60 | 0 | - | 100% | 34 | 40 | 52 | 35 | 52 |
| paraphrases of cached queries | 1 | 10 | 0 | - | 90% | 33 | 108 | 108 | 41 | 108 |
| sweep 8 workers x 20 s (cache cleared first) | 8 | 1566 | 0 | 77.9 | 96% | 96 | 132 | 222 | 102 | 361 |
| sweep 32 workers x 20 s (cache cleared first) | 32 | 1561 | 0 | 76.5 | 96% | 408 | 474 | 691 | 414 | 994 |

Server-side `took_ms` (embed + cache lookup or SQL; excludes HTTP and serialization):

| Phase | p50 ms | p95 ms | p99 ms | mean ms |
|---|---|---|---|---|
| cold (cache cleared, sequential) | 100 | 114 | 134 | 97 |
| warm (same queries again) | 31 | 37 | 50 | 32 |
| paraphrases of cached queries | 31 | 104 | 104 | 38 |
| sweep 8 workers x 20 s (cache cleared first) | 93 | 129 | 218 | 99 |
| sweep 32 workers x 20 s (cache cleared first) | 405 | 471 | 660 | 410 |

Sweep latency split by cache outcome (the pool is only 60 queries, so most sweep traffic hits):

| Phase | uncached n | uncached p50 | uncached p95 | cached n | cached p50 | cached p95 |
|---|---|---|---|---|---|---|
| sweep 8 workers x 20 s (cache cleared first) | 65 | 210 | 306 | 1501 | 95 | 125 |
| sweep 32 workers x 20 s (cache cleared first) | 67 | 590 | 953 | 1494 | 407 | 463 |

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

- A cold search is ~105 ms end to end (≈ 50 ms embed + ≈ 55 ms SQL); a cache hit is ~34 ms, all of it the query
  embedding plus a Redis vector lookup. Every request embeds, so the cache cannot go below Ollama's per-call time.
- Throughput saturates at **~77 req/s at 8 workers and does not rise at 32**; that is Ollama's embedding throughput on
  this Mac (measured directly: ~77 embeds/s). 32 workers only add queueing (p50 96 → 408 ms). No errors at either level.
- The sweep is 96% cache hits because the pool has 60 queries and is exhausted within a second; the uncached rows
  (65 and 67 requests) are the honest under-load numbers for a miss: p50 210 ms at 8 workers, 590 ms at 32.
- Cache paraphrase hits: 9/10. The miss is an acronym expansion (SLAM → simultaneous localization and mapping), whose
  bge-m3 distance exceeds the 0.12 threshold.

## How to reproduce

```
docker compose up -d && ollama serve                     # Postgres/pgvector, Redis, Redpanda; Ollama with `ollama pull bge-m3`
uv run --project python python -m uvicorn arxiv_query_api.main:app --port 8001   # query API on Ollama defaults (do not source .env: it points at the Spark)
uv run --project python python scripts/eval_search.py --n 500 --paraphrase-n 200 --seed 42      # -> data/eval_search.json
uv run --project python python scripts/bench_api.py --base-url http://127.0.0.1:8001 --duration 20 --concurrency 8,32   # -> data/bench_api.json
uv run --project python ruff check --config python/pyproject.toml scripts/
```
