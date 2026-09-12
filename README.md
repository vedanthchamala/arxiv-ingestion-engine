# arXiv Ingestion Engine

[![ci](https://github.com/vedanthchamala/arxiv-ingestion-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/vedanthchamala/arxiv-ingestion-engine/actions/workflows/ci.yml)

A pipeline that watches arXiv for new machine learning, computer vision and robotics papers,
pulls their full text, embeds and summarizes them, and lets you search them by meaning.

arXiv publishes a few hundred papers a night in these three categories. A few minutes after the
announcement you can already search that night's batch by abstract; over the following hours the
full text and a short summary arrive for each one. Everything runs on one laptop plus, optionally,
a GPU box for inference.

## How it works

```
                 every 15 min                     one instance, all arxiv.org traffic
  arXiv API ──▶  poller (Rust)  ──▶ papers.new ──▶  fetcher (Rust)  ──▶ papers.chunked ──▶ worker (Python) ──▶ Postgres + pgvector
                                        │                                                                            ▲
                                        └──▶ worker-abstract (Python): title + abstract, searchable in seconds ──────┘
                                                                                                                     │
                                                                                     query API (FastAPI) + Redis semantic cache
```

1. **Poller.** Every 15 minutes it asks the arXiv API for the newest papers in each category and
   walks the results newest-first until it reaches papers it has already seen (a Redis set keyed
   by paper id and version). Each new paper becomes one JSON message on the `papers.new` topic.
   Papers cross-listed in several categories are seen once.
2. **Fast path.** `worker-abstract` reads `papers.new`, embeds the title and abstract, and writes
   the paper to Postgres. From this point the paper shows up in search.
3. **Fetcher.** Also reads `papers.new`. For each paper it tries the HTML rendering first
   (`arxiv.org/html/…`, which has real section headings), falls back to the PDF via `pdftotext`,
   and if neither exists keeps just the abstract. The text is split into 512-token chunks with the
   embedding model's own tokenizer and sent on as one `papers.chunked` message per paper.
4. **Worker.** Reads `papers.chunked`, embeds every chunk, asks a 7B instruct model for a
   three-sentence summary, and replaces the abstract-only row with the full set of chunks in one
   transaction.
5. **Search.** `POST /search` embeds the query, finds the nearest chunks in pgvector (category and
   date filters run in the same SQL), keeps the best chunk per paper, and returns the papers with
   the passage that matched. A Redis semantic cache answers paraphrases of recent queries without
   touching the database.

The topics live in Redpanda (Kafka API), six partitions each, keyed by paper id. Delivery is
at-least-once and every database write is an upsert, so a replayed or duplicated message is
harmless. Messages that fail after three attempts go to a `papers.failed` topic with their
original payload, so they can be inspected and replayed.

## Architecture

**Components**

| Stage | Language | Reads | Writes | Job |
|---|---|---|---|---|
| poller | Rust | arXiv API | `papers.new` | Polls each category every 15 min, dedups against a Redis set, emits one message per new paper version |
| fetcher | Rust | `papers.new` | `papers.chunked`, `papers.failed` | Downloads full text (HTML, then PDF), splits it into 512-token chunks; the only process that downloads from arxiv.org |
| worker-abstract | Python | `papers.new` | Postgres | Embeds title + abstract so the paper is searchable within seconds |
| worker | Python | `papers.chunked` | Postgres, `papers.failed` | Embeds every chunk, writes a summary, replaces the abstract-only row |
| query API | Python (FastAPI) | HTTP | — | `/search`, `/papers/{id}`, `/stats`, `/metrics`; semantic cache in front of the vector search |

**Data**

- **Redpanda** (Kafka API): `papers.new`, `papers.chunked`, `papers.failed`. Six partitions each,
  keyed by arXiv id. Consumers commit offsets manually after each message; delivery is
  at-least-once.
- **Postgres + pgvector**: `papers`, `authors`, `paper_authors`, `chunks` (`vector(1024)` with an
  HNSW cosine index). One transaction per paper; every write is an upsert, so replays converge.
- **Redis**: `arxiv:seen` (which paper versions have been produced), `arxiv:ratelimit` (the shared
  request budget), and the vector index behind the semantic cache.

**Contracts shared by both languages**

- `schemas/messages.v1.json` defines `paper`, `chunk`, `chunked` and `failed`. Rust compiles it in,
  Python loads it; every message is validated on the way out and on the way in, and each side's
  tests check messages produced by the other.
- `schemas/ratelimit.lua` is the arXiv request budget: an atomic slot reservation on the Redis
  clock that returns how long to sleep. Poller, fetcher and backfill all call it before every
  request, so the system as a whole never sends faster than one request per three seconds, which is
  arXiv's rule across all of a user's machines.

**Inference**

Embeddings (`BAAI/bge-m3`, 1024-d) and summaries (`Qwen2.5-7B-Instruct`) come from an
OpenAI-compatible HTTP endpoint. vLLM on a DGX Spark is the intended server; Ollama on the laptop
serves the same two endpoints and is what the defaults point at. Chunks are sized with the embedding
model's own tokenizer so token counts are exact.

**Failure handling**

- Transient errors retry three times with backoff, then the message goes to `papers.failed` with its
  original payload attached; malformed messages go there immediately.
- A paper with no extractable text is stored abstract-only, not failed.
- An abstract-only write never replaces stored full text for the same version, so the two consumers
  of `papers.new` can run in any order and either topic can be replayed from the start.
- Database connections are autocommit with every write in an explicit transaction block, and shared
  author rows are locked in a fixed order, so concurrent writers neither lose commits nor deadlock.

**Operations**

Every stage exports Prometheus metrics; a compose profile runs Prometheus and Grafana with one
provisioned dashboard (throughput per stage, arXiv request rate, latency percentiles, dead letters,
cache hit rate, consumer lag). Another profile runs the five services as containers. CI runs
rustfmt, clippy, cargo test, ruff, pytest, and validates the schema and the compose file.

The reasoning behind each choice, including the ones that came out of running the system for a day,
is in [`DECISIONS.md`](DECISIONS.md). [`SPEC.md`](SPEC.md) has the design, [`STATUS.md`](STATUS.md)
the live-run log, [`BENCHMARKS.md`](BENCHMARKS.md) the measurements.

## Running it

```
cp .env.example .env               # defaults: the compose stack below and a local Ollama
./scripts/dev.sh                   # Redpanda + Console, Postgres/pgvector, Redis; topics; migrations
ollama pull bge-m3 && ollama pull qwen2.5:7b-instruct

scripts/pipeline.sh poller --once  # first run collects the ~2000 most recent papers per category
scripts/pipeline.sh worker-abstract
scripts/pipeline.sh api            # http://localhost:8000/docs
curl -X POST localhost:8000/search -H 'content-type: application/json' \
     -d '{"query":"diffusion policy for robot manipulation","k":5,"categories":["cs.RO"]}'
```

Add `scripts/pipeline.sh fetcher` and `scripts/pipeline.sh worker` for full text and summaries.
`scripts/run.sh start` launches all five detached with logs in `data/logs/`; `scripts/run.sh stop`
lets each finish its current message first. Or run the whole thing in containers:

```
docker compose --profile pipeline build && docker compose --profile pipeline up -d
docker compose --profile observability up -d    # Prometheus :9090, Grafana :3000
```

Grafana comes with one dashboard: papers per minute at each stage, arXiv request rate, worker and
API latency, dead letters, cache hit rate, consumer lag.

## What it has done so far

- Ran unattended for over a day: 70 poll cycles, no crashes, and the next night's announcement
  picked up within one cycle.
- Fetched and chunked the full text of 7,881 papers (95 % from arXiv's HTML) into 235,000 chunks
  in about ten hours, which is exactly arXiv's rate limit.
- Search answers in about 74 ms cold and 34 ms from the cache, and holds roughly 83 requests per
  second on the laptop with no errors; the ceiling is the local embedding model.
- Survived a `kill -9` mid-message: the paper in flight was redelivered and stored again with no
  gaps, duplicates or orphans.
- Found two real bugs in its first day of operation (a replay that could overwrite full text, and
  a database connection that had quietly stopped committing). Both are fixed and written up.

## Layout

```
schemas/        messages.v1.json (the contract), ratelimit.lua (the shared request budget)
sql/            tables and the HNSW index
rust/           common crate, poller, fetcher
python/         arxiv_common (models, schema, inference client, database), worker, query_api
observability/  Prometheus config, Grafana dashboard
inference/      vLLM serve script for the DGX Spark
scripts/        dev, pipeline, run, smoke test, backfill, retrieval eval, load test
```

Tests: `cd rust && cargo test`, `cd python && uv run pytest`. `./scripts/smoke.sh` checks the
running stack end to end.
