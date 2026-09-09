# arXiv Ingestion Engine

Ingests new arXiv papers in **cs.LG, cs.CV, cs.RO** within minutes of each daily announcement,
embeds and summarizes them, and serves semantic search.

```
poller (Rust) ─▶ papers.new ─▶ fetcher (Rust) ─▶ papers.chunked ─▶ worker (Python) ─▶ Postgres + pgvector
                    │                                                                      ▲
                    └──────────▶ worker-abstract (Python, fast path) ──────────────────────┘
                                                                        query-api (FastAPI) + Redis semantic cache
```
Redpanda carries the topics (6 partitions, key = arXiv id). vLLM on a DGX Spark serves embeddings
(`bge-m3`) and summaries; Ollama on the Mac serves the same OpenAI-compatible endpoints as a
fallback. `SPEC.md` has the design and the arXiv constraints behind it; `DECISIONS.md` records every
architecture decision with its reasoning; `STATUS.md` has progress.

## Quickstart
```
cp .env.example .env            # defaults point at the compose stack and a local Ollama
./scripts/dev.sh                # Redpanda + Console, Postgres/pgvector, Redis 8; topics; migrations
ollama serve & ollama pull bge-m3 && ollama pull qwen2.5:7b-instruct   # local inference (or use the Spark)

scripts/pipeline.sh poller --once        # first run bootstraps ~2000 most recent per category
scripts/pipeline.sh worker-abstract      # papers become searchable within seconds (abstract as chunk 0)
scripts/pipeline.sh api                  # http://localhost:8000/docs
curl -X POST localhost:8000/search -H 'content-type: application/json' \
     -d '{"query":"diffusion policy for robot manipulation","k":5,"categories":["cs.RO"]}'
```
For full text and summaries, additionally run (one fetcher only, it owns all arxiv.org traffic):
```
scripts/pipeline.sh fetcher              # HTML → PDF fallback → 512-token chunks → papers.chunked
scripts/pipeline.sh worker               # embeds every chunk, writes the summary, replaces the abstract-only row
```
Steady state: `scripts/pipeline.sh poller` (every 15 min) + the three consumers above.

## Layout
```
schemas/messages.v1.json   the contract: paper / chunk / chunked / failed (validated in Rust and Python)
sql/001_init.sql           papers, authors, paper_authors, chunks(vector(1024), HNSW)
rust/common                models, schema validation, Kafka + rate-limit helpers
rust/poller                arXiv API → papers.new, Redis seen-set dedup
rust/fetcher               papers.new → full text → papers.chunked (single instance, 3 s spacing)
python/arxiv_common        models, schema, inference client, pgvector upserts + search
python/worker              papers.chunked (or papers.new) → inference → Postgres, retries, DLQ
python/query_api           /search (semantic cache), /papers/{id}, /stats, /health
inference/spark            vLLM serve script + README for the DGX Spark
scripts/                   dev.sh, pipeline.sh, smoke.sh, backfill.py (OAI-PMH)
```

## Verify
```
./scripts/smoke.sh          # seen-set == topic, no orphan/duplicate chunks, DLQ count, lag, live search
```
