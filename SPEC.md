# arXiv Ingestion Engine — Spec

Continuously ingest new arXiv papers in **cs.LG, cs.CV, cs.RO**, produce chunk embeddings and a short
summary per paper, and serve semantic search over them. Portfolio project first, daily tool second.

## Constraints that shape the design (arXiv terms of use, verified 2026-09-07)
- **One request every 3 s, single connection, across all machines you control.** Applies to the API,
  RSS, OAI-PMH and PDF downloads. Therefore every arXiv-facing process (poller, fetcher, backfill)
  reserves its send slot from one shared budget in Redis (`schemas/ratelimit.lua`), and only one
  fetcher instance ever runs.
- New papers are announced once a day (~20:00 ET, Sun–Thu). "Real time" means minutes after that.
- The API returns full metadata (authors, categories, dates, PDF link, DOI); nothing needs re-fetching.
- `submittedDate` range queries are unreliable (time out); the poller pages by `lastUpdatedDate`
  descending until it hits already-seen ids.

## Architecture
```
poller (Rust)   ── papers.new ──▶ fetcher (Rust) ── papers.chunked ──▶ worker (Python) ──▶ Postgres+pgvector
 every 15 min                     1 instance, owns                    N instances,           ▲
 dedup via Redis SET              all arxiv.org I/O                   embeds + summarizes   query-api (FastAPI)
                                                                      via vLLM on DGX Spark  + Redis semantic cache
                                  failures after retries ──▶ papers.failed (DLQ)
```
Redpanda (Kafka API) with 6 partitions per topic, key = `arxiv_id`. Delivery is at-least-once;
every database write is an upsert keyed on `arxiv_id` / `arxiv_id:chunk_idx`.

## Decisions (2026-09-07)
| Choice | Decision | Why |
|---|---|---|
| Vector store | pgvector in the same Postgres | one transaction per paper; SQL filters + ANN in one query; one fewer service |
| Languages | Rust: poller + fetcher. Python: worker + query API | Rust owns I/O and rate limiting; Python owns ML-adjacent work. Not two implementations of one job |
| Queue | Redpanda | user asked for a partitioned log; single arm64 binary; Console for lag/DLQ |
| Inference | vLLM on DGX Spark, OpenAI-compatible | embeddings `BAAI/bge-m3` (1024-d), summaries `Qwen2.5-7B-Instruct` |
| Redis | seen-set (`arxiv:seen`, members `IDvN`) + shared arXiv request budget (`arxiv:ratelimit`) + semantic cache on `/search` | ingestion has nothing to semantically cache; the budget must be cross-process |
| Full text | prefer `arxiv.org/html/{id}`, fall back to PDF via `pdftotext`, else abstract-only | HTML has section structure; PDFs > 20 MB or without text degrade to abstract-only (a data limitation, not a failure); the DLQ is for real errors after retries |

## Message contracts
`schemas/messages.v1.json` is the single source of truth (`$defs.paper`, `chunk`, `chunked`, `failed`);
one file with local `$ref`s so neither language needs a cross-file schema resolver.
Rust embeds it at compile time (`common::schema`); Python loads it at import. Every producer validates
before sending. Bump `schema_version` and add a new `$defs` entry for breaking changes.

## Two consumers of `papers.new`
`worker-abstract` embeds title+abstract as chunk 0 within seconds of polling; `fetcher` → `worker`
later replaces that row with full-text chunks and a summary. Both paths write the same shape, so
search works immediately and improves as the fetcher catches up at arXiv's pace.

## Storage
`sql/001_init.sql`: `papers`, `authors`, `paper_authors`, `chunks(embedding vector(1024), HNSW cosine)`.
Re-processing a new version deletes that paper's chunks and re-inserts inside the same transaction.

## Observability
Every stage exports Prometheus metrics (`poller_*`, `fetcher_*`, `worker_*`, `api_*`, `arxiv_requests_total`,
`arxiv_ratelimit_wait_seconds`); Prometheus + Grafana run under the compose `observability` profile with one
provisioned dashboard. Logs are structured and keyed by `arxiv_id`.

## Non-goals (for now)
Serving PDFs (arXiv ToU forbids redistribution), multi-node Kafka, auth on the query API.
