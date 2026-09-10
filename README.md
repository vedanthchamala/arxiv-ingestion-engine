# arXiv Ingestion Engine

[![ci](https://github.com/vedanthchamala/arxiv-ingestion-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/vedanthchamala/arxiv-ingestion-engine/actions/workflows/ci.yml)

Ingests new arXiv papers in **cs.LG, cs.CV, cs.RO** within minutes of each daily announcement,
embeds and summarizes them, and serves semantic search over full text.

```
poller (Rust) ─▶ papers.new ─▶ fetcher (Rust) ─▶ papers.chunked ─▶ worker (Python) ─▶ Postgres + pgvector
                    │                                                                      ▲
                    └──────────▶ worker-abstract (Python, fast path) ──────────────────────┘
                                                                        query-api (FastAPI) + Redis semantic cache
```
Redpanda carries the topics (6 partitions, key = arXiv id, at-least-once, idempotent upserts, dead-letter
topic). Every process that talks to arxiv.org (poller, fetcher, backfill) reserves its send slot from one
request budget in Redis, so the whole system honours arXiv's one-request-per-3-s rule no matter how many
processes run. vLLM on a DGX Spark serves embeddings (`bge-m3`) and summaries (`Qwen2.5-7B-Instruct`);
Ollama on the Mac serves the same OpenAI-compatible endpoints as a fallback. Every stage exports
Prometheus metrics; a provisioned Grafana dashboard shows throughput, latency, dead letters and lag.

`SPEC.md` has the design and the arXiv constraints behind it; `DECISIONS.md` records all 29 architecture
decisions with their reasoning; `STATUS.md` has progress and live evidence; `BENCHMARKS.md` has the
retrieval evaluation and load test.

## Quickstart (host)
```
cp .env.example .env            # defaults point at the compose stack and a local Ollama
./scripts/dev.sh                # Redpanda + Console, Postgres/pgvector, Redis 8; topics; lag metrics; migrations
ollama serve & ollama pull bge-m3 && ollama pull qwen2.5:7b-instruct   # local inference (or use the Spark)

scripts/pipeline.sh poller --once        # first run bootstraps ~2000 most recent per category
scripts/pipeline.sh worker-abstract      # papers become searchable within seconds (abstract as chunk 0)
scripts/pipeline.sh api                  # http://localhost:8000/docs
curl -X POST localhost:8000/search -H 'content-type: application/json' \
     -d '{"query":"diffusion policy for robot manipulation","k":5,"categories":["cs.RO"]}'
```
For full text and summaries, additionally run the fetcher (HTML → PDF fallback → 512-token chunks) and the
full-text worker (embeds every chunk, writes the summary, replaces the abstract-only row):
```
scripts/pipeline.sh fetcher
scripts/pipeline.sh worker
```
Steady state is all five at once; `scripts/run.sh start` launches them detached with logs in `data/logs/`
and `scripts/run.sh stop` shuts them down after the in-flight message commits.

## Run everything in Docker
The `pipeline` profile builds and runs the five services next to the infra (`docker compose up -d` alone is
unchanged):
```
docker compose --profile pipeline build && docker compose --profile pipeline up -d
```
Containers reach Redpanda, Postgres and Redis by service name. Inference defaults to Ollama on the host via
`host.docker.internal`; a `.env` written for host runs (`localhost:11434`) must be overridden for containers:
`EMBEDDING_URL=http://host.docker.internal:11434/v1 LLM_URL=http://host.docker.internal:11434/v1 docker compose --profile pipeline up -d`.
The Spark URLs work from both. `API_PORT` moves the query API off 8000. Never scale `fetcher`.

## Observability
```
docker compose --profile observability up -d     # Prometheus :9090, Grafana :3000 (dashboard "arXiv pipeline")
```
Metrics: poller `:9101`, fetcher `:9102`, worker `:9103`, worker-abstract `:9104`, API `/metrics`. Redpanda's
native consumer-lag metrics are enabled by `dev.sh`. Structured logs (`tracing`, `structlog`) carry the arXiv
id as the correlation key across stages; Redpanda Console (`:8080`) shows topics, groups and the DLQ.

## Measured (see BENCHMARKS.md and STATUS.md)
- Search: cold `POST /search` p50 74 ms end to end (≈43 ms query embedding + 29 ms SQL), semantic-cache
  hit 34 ms; ~83 req/s sustained at 8 and 32 concurrent clients with 0 errors (the ceiling is Ollama's
  embedding throughput on the laptop, not the API or Postgres). Disabling psycopg's server-side prepared
  statements halved the SQL time (60 → 29 ms unfiltered, 24 → 15 ms filtered).
- Retrieval: title → own paper recall@1 = 1.000 (n = 500); first abstract sentence → own paper
  recall@1 = 0.795, recall@10 = 0.945 (n = 200); category precision@5 = 0.987 over 15 natural-language
  queries. Self-retrieval is a sanity metric, not a relevance benchmark.
- Ingestion: bootstrap of 6,000 API entries → 5,630 unique papers in 128 s (30 requests at arXiv's 3 s
  spacing), 370 cross-listings deduplicated, partitions within 7 % of even; 5,630 abstracts embedded in
  ~9 min with 0 failures.
- Correctness: replaying processed messages under a fresh consumer group leaves every row count identical;
  poison messages and exhausted retries land on `papers.failed` with the original payload attached.

## Layout
```
schemas/messages.v1.json   the contract: paper / chunk / chunked / failed (validated in Rust and Python)
schemas/ratelimit.lua      the shared arXiv request budget (loaded by Rust and Python)
sql/001_init.sql           papers, authors, paper_authors, chunks(vector(1024), HNSW)
rust/common                models, schema validation, Kafka + rate-limit + metrics helpers
rust/poller                arXiv API → papers.new, Redis seen-set dedup
rust/fetcher               papers.new → full text → papers.chunked (single instance)
python/arxiv_common        models, schema, inference client, metrics, pgvector upserts + search
python/worker              papers.chunked (or papers.new) → inference → Postgres, retries, DLQ
python/query_api           /search (semantic cache), /papers/{id}, /stats, /metrics, /health
inference/spark            vLLM serve script + README for the DGX Spark
observability/             Prometheus config, Grafana provisioning and dashboard
scripts/                   dev.sh, pipeline.sh, run.sh, smoke.sh, backfill.py, eval_search.py, bench_api.py
.github/workflows/ci.yml   rustfmt + clippy + cargo test; ruff + pytest; schema and compose validation
```

## Verify
```
cd rust   && cargo test && cargo clippy --all-targets -- -D warnings
cd python && uv run pytest && uv run ruff check .
./scripts/smoke.sh          # seen-set == topic, no orphan/duplicate chunks, DLQ count, lag, live search
uv run --project python python scripts/eval_search.py     # recall@k / MRR / precision@5
uv run --project python python scripts/bench_api.py       # p50/p95/p99, throughput, cache hit rate
```
