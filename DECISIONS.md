# Architecture Decision Records

Every non-obvious choice in this system, with the reason and the cost. Dates are when the decision
was made. "Superseded" entries are kept so the reasoning trail stays intact.

| # | Decision | Status |
|---|---|---|
| [1](#1-poll-on-a-schedule-not-continuously) | Poll arXiv every 15 min, not continuously | accepted |
| [2](#2-one-process-owns-all-arxivorg-traffic) | One process owns all arxiv.org traffic | accepted (amended by 27) |
| [3](#3-page-by-lastupdateddate-until-seen) | Page by `lastUpdatedDate` until already-seen, not date ranges | accepted |
| [4](#4-redpanda-as-the-partitioned-log) | Redpanda (Kafka API), 6 partitions, key = arXiv id | accepted |
| [5](#5-fat-messages-workers-never-re-query-arxiv) | Fat messages: workers never re-query arXiv | accepted |
| [6](#6-one-json-schema-file-validated-in-both-languages) | One JSON Schema file, validated in both languages | accepted |
| [7](#7-pgvector-instead-of-chromadb) | pgvector instead of ChromaDB | accepted (supersedes original plan) |
| [8](#8-rust-and-python-split-by-strength-not-duplicated) | Rust for poller + fetcher, Python for worker + API | accepted (supersedes original plan) |
| [9](#9-at-least-once-everywhere-every-write-is-an-upsert) | At-least-once delivery; every write is an idempotent upsert | accepted |
| [10](#10-produce-first-then-mark-seen) | Poller produces first, then marks seen | accepted |
| [11](#11-dead-letter-topic-after-bounded-retries) | Dead-letter topic after 3 attempts; poison messages immediately | accepted |
| [12](#12-html-first-pdf-second-abstract-only-last) | Full text: HTML → PDF → abstract-only (not DLQ) | accepted (amends plan) |
| [13](#13-chunk-with-the-embedding-models-own-tokenizer) | 512-token chunks, 64 overlap, embedding model's tokenizer | accepted |
| [14](#14-two-consumers-of-papersnew-fast-path-and-full-text-path) | Two consumers of `papers.new`: fast abstract path + full-text path | accepted (emerged during build) |
| [15](#15-inference-behind-openai-compatible-http) | Inference behind OpenAI-compatible HTTP; vLLM on Spark, Ollama fallback | accepted |
| [16](#16-embedding-model-bge-m3-1024-d) | Embedding model `BAAI/bge-m3`, 1024-d, L2-normalized | accepted |
| [17](#17-summaries-from-a-self-hosted-7b-model) | Summaries from a self-hosted 7B instruct model | accepted |
| [18](#18-semantic-cache-on-the-query-side-scoped-by-filters) | Semantic cache on `/search`, scoped by filters | accepted (moves from ingestion side) |
| [19](#19-redis-does-exactly-two-jobs) | Redis: seen-set and semantic cache only | accepted (amended by 27) |
| [20](#20-new-versions-are-re-processed) | New paper versions are re-processed and replace old rows | accepted |
| [21](#21-search-is-chunk-level-then-best-chunk-per-paper) | Search: chunk-level ANN, best chunk per paper, SQL filters | accepted |
| [22](#22-backfill-through-oai-pmh) | Backfill through OAI-PMH, version-1 semantics | accepted |
| [23](#23-never-serve-pdfs) | Never redistribute PDFs | accepted |
| [24](#24-infra-in-compose-python-on-the-host-for-now) | Infra in docker-compose; Python services on the host via uv | accepted (amended by 29) |
| [25](#25-observability-logs--redpanda-console-no-prometheus-yet) | Structured logs + Redpanda Console; no Prometheus yet | accepted (amended by 28) |
| [26](#26-a-distributed-shape-for-a-workload-that-does-not-need-one) | Keep the distributed shape despite ~600 papers/day | accepted, eyes open |
| [27](#27-one-request-budget-in-redis-shared-by-every-arxiv-facing-process) | One arXiv request budget in Redis, shared by poller, fetcher and backfill | accepted (amends 2, 19, 22) |
| [28](#28-prometheus-metrics-on-every-stage) | Prometheus metrics on every stage; Grafana dashboard | accepted (amends 25) |
| [29](#29-containerized-pipeline-services) | Containerized pipeline services behind a compose profile | accepted (amends 24) |
| [30](#30-the-fast-path-never-downgrades-full-text-and-reads-never-open-transactions) | The fast path never downgrades full text; reads never open transactions | accepted (amends 9, 14) |

---

## 1. Poll on a schedule, not continuously
**Date** 2026-09-07 · **Context** The original plan said "constantly polls". arXiv announces new
papers once a day (~20:00 ET, Sun–Thu) and its docs say there is no reason to run the same query
more than once a day. There is no push channel. **Decision** The poller runs every 15 minutes per
category and deduplicates on `arxiv_id` + version in a Redis SET before producing. **Consequences**
"Real time" means minutes after the daily announcement, which is the best anyone can do. Cross-listed
papers (cs.LG + cs.CV is common) are seen once; 370 of the first 6000 entries were duplicates.

## 2. One process owns all arxiv.org traffic
**Date** 2026-09-07 · **Context** arXiv's terms allow one request every 3 s over a single connection,
counted across *all* machines you control, for the API, RSS, OAI-PMH and PDF downloads alike.
N parallel workers each downloading PDFs would get the project blocked. **Decision** Exactly one
fetcher instance performs every HTML/PDF download, behind a mutex-serialized minimum-interval limiter
(`common::ratelimit::MinInterval`). The poller and backfill script use the same spacing and must not
run concurrently with the fetcher. **Consequences** Full text arrives at ~4 s/paper: ~45 min for a
normal day, ~6 h for the 5600-paper bootstrap. Scaling fetchers would require a shared token bucket
and still would not raise the ceiling.

## 3. Page by `lastUpdatedDate` until seen
**Date** 2026-09-07 · **Context** `submittedDate:[a TO b]` range queries on the search API timed out
three times in a row. **Decision** Query `cat:X` sorted by `lastUpdatedDate` descending, 200 per
page, and stop at the first page that yields nothing new (bounded by `--max-pages`).
**Consequences** Steady state costs ~2 requests per category per cycle. Sorting by update date also
surfaces new *versions* of old papers, which decision 20 wants anyway.

## 4. Redpanda as the partitioned log
**Date** 2026-09-07 · **Context** The user asked for a partitioned queue. Kafka partitions exist for
ordering guarantees this workload does not need; a plain work queue (RabbitMQ, Redis Streams) would
be a closer fit functionally. **Decision** Redpanda (Kafka API, single arm64 binary, no ZooKeeper)
with 6 partitions per topic and key = `arxiv_id`, so all versions of a paper land on one partition and
load spreads evenly (observed: every partition within 7% of the mean). Redpanda Console gives lag and DLQ inspection for free.
**Consequences** Slow consumers need `max.poll.interval.ms` raised to 10 min and manual commits, or
the group rebalances forever. `rdkafka` pulls a C build (cmake) into the Rust toolchain.

## 5. Fat messages: workers never re-query arXiv
**Date** 2026-09-07 · **Context** The plan's payload was `{abstract, title, category, id}` and had
workers "pull the papers from arxiv" again. The API entry already carries authors, all categories,
primary category, dates, PDF link, DOI, journal ref and comment. **Decision** The `papers.new`
message carries everything the API returned. `papers.chunked` wraps that message unchanged and adds
`source` + `chunks`. **Consequences** One fewer arXiv round trip per paper (see decision 2), and the
DLQ payload is self-describing.

## 6. One JSON Schema file, validated in both languages
**Date** 2026-09-07 · **Context** Two languages producing and consuming the same topics need a
contract neither owns. The plan had two schema files; cross-file `$ref` needs a resolver in each
language. **Decision** `schemas/messages.v1.json` holds `$defs` for `paper`, `chunk`, `chunked`,
`failed` with local `$ref`s only. Rust embeds it at compile time (`include_str!`); Python loads it at
import. Every producer validates before sending; consumers validate on receipt. A test in each
language checks the other language's serialized shape. **Consequences** Breaking changes mean a new
`$defs` entry and a `schema_version` bump, not silent drift.

## 7. pgvector instead of ChromaDB
**Date** 2026-09-07 · **Context** ChromaDB next to Postgres gives two sources of truth with no
transaction between them: a chunk written to Chroma whose Postgres write then fails is an orphan
needing reconciliation. Chroma is single-node with weak metadata filtering. Scale is ~25K
vectors/day, ~9M/year. **Decision** Embeddings live in `chunks.embedding vector(1024)` with an HNSW
cosine index, in the same database and the same transaction as `papers`, `authors`, `paper_authors`.
**Consequences** Category/date filters and ANN combine in one SQL query; one fewer service. Qdrant
was the runner-up if a dedicated vector DB were the point. Changing embedding dimension is a
migration.

## 8. Rust and Python split by strength, not duplicated
**Date** 2026-09-07 · **Context** The plan had Python *and* Rust workers doing the identical job:
two PDF parsers, two chunkers, two DB layers, two sets of bugs. **Decision** Rust owns the
long-running I/O: poller and fetcher (rate limiting, HTTP, HTML/PDF extraction, tokenizer-based
chunking). Python owns the ML-adjacent work: embed/summarize/store worker and the query API. Two
topics connect them. **Consequences** Each language does what its ecosystem is good at, the fetcher
and worker scale independently (I/O-bound vs GPU-bound), and the polyglot story is "two stages" not
"two implementations".

## 9. At-least-once everywhere; every write is an upsert
**Date** 2026-09-07 · **Context** Kafka delivery is at-least-once; a worker can die after writing
and before committing. **Decision** Consumers use `enable.auto.commit=false` and commit after each
message. `papers` upserts on `arxiv_id`; chunks are deleted and re-inserted for the paper inside the
same transaction; authors upsert on a normalized name. **Consequences** Re-processing is safe by
construction. Verified: replaying 5 papers under a fresh consumer group left every row count
identical.

## 10. Produce first, then mark seen
**Date** 2026-09-07 · **Context** In the poller, `SADD` before `produce` risks marking a paper seen
that was never produced (lost forever). **Decision** Check `SISMEMBER`, produce, then `SADD`.
**Consequences** A crash between the two yields a duplicate message on the next cycle, which
decision 9 makes harmless. Lost papers are impossible; duplicates are cheap.

## 11. Dead-letter topic after bounded retries
**Date** 2026-09-07 · **Context** Network blips and inference hiccups are transient; malformed
messages never succeed. **Decision** Fetcher and worker retry 3× with backoff, then produce a
schema-valid `failed` record (stage, error, attempts, original payload) to `papers.failed` and
commit. Unparseable JSON or schema violations go straight to the DLQ with `attempts=1`.
**Consequences** No message blocks a partition. The DLQ is inspectable in Console and replayable
because it carries the original payload.

## 12. HTML first, PDF second, abstract-only last
**Date** 2026-09-07, amended 2026-09-08 · **Context** `arxiv.org/html/<id>` (LaTeXML) exists for most
LaTeX submissions since Dec 2023 and has real section structure; PDFs need `pdftotext` and lose
structure. The plan sent no-text (scanned) PDFs to the DLQ. **Decision** Try HTML, then PDF (skip
> 20 MB), then fall back to an abstract-only document with `source = "abstract"`. **Consequences**
A paper with no extractable text is a data limitation, not a system failure, and stays searchable;
the DLQ is reserved for real errors. Observed on the first 15 papers: 13 HTML, 2 PDF.

## 13. Chunk with the embedding model's own tokenizer
**Date** 2026-09-07 · **Context** `token_count` is only meaningful for the model that will embed
the chunk. **Decision** The fetcher loads `BAAI/bge-m3`'s tokenizer (HF `tokenizers` crate) and
slides a 512-token window with 64-token overlap over each section using byte offsets, so chunk text
is the original text, not a decode. Each chunk is prefixed with its section title. Chunk 0 is always
title + abstract. Bibliographies, math markup and tables are stripped. Max 120 chunks per paper.
**Consequences** Exact token counts for the server; abstract-only and full-text rows share a shape
(decision 14). First run downloads the tokenizer (~17 MB).

## 14. Two consumers of `papers.new`: fast path and full-text path
**Date** 2026-09-08 · **Context** The fetcher is throttled to ~4 s/paper by decision 2, so full-text
search would lag hours behind polling. The phase-2 abstract-only worker turned out not to be
throwaway. **Decision** `worker-abstract` consumes `papers.new` and embeds title + abstract as chunk
0 within seconds. `fetcher` → `worker` later replaces that row with full-text chunks and a summary.
Both write the same row shape. **Consequences** Papers are searchable minutes after announcement
and improve as the fetcher catches up. Each paper is embedded twice (once cheaply).

## 15. Inference behind OpenAI-compatible HTTP
**Date** 2026-09-07 · **Context** Embeddings and summaries are two different models, and the GPU
lives on a separate machine (DGX Spark). **Decision** Workers and the API talk only to
`/v1/embeddings` and `/v1/chat/completions`. vLLM on the Spark is the target; Ollama on the Mac
serves the same contract as a fallback and is what phases 2 and 4 were verified on.
**Consequences** The server is swappable by two env vars. **Amended 2026-09-08:** the Spark turned
out to be shared with seven other users and already runs someone's vLLM on port 8000 with the
`cu130-nightly` image. Ours uses that same image (proven on this GB10, no pull), ports 8001/8002,
binds only to localhost + Tailscale, and pins ~22 GB instead of the ~70 GB a dedicated box would
get (`--gpu-memory-utilization` 0.22 / 0.06). Bring it down with `serve.sh down` when idle for long.

## 16. Embedding model `bge-m3`, 1024-d
**Date** 2026-09-07 · **Context** Need a strong open embedding model available in both vLLM and
Ollama with a long context. **Decision** `BAAI/bge-m3` (1024-d, 8K context, multilingual). The
worker L2-normalizes vectors so the source server's normalization does not matter for cosine
search. `chunks.embedding` is `vector(1024)`. **Consequences** Switching models means re-embedding
and possibly a dimension migration.

## 17. Summaries from a self-hosted 7B model
**Date** 2026-09-07 · **Context** ~600 summaries/day. Claude Haiku 4.5 via the Batch API was
estimated at $1–2/day on abstract + intro input (about 5× more on full papers); the Spark makes
self-hosting free per token. **Decision** `Qwen/Qwen2.5-7B-Instruct` on the Spark (already in its
HF cache); `qwen2.5:7b-instruct` via Ollama locally. Input is title + abstract + introduction
excerpt, output ~150 tokens, `temperature 0.2`. **Consequences** Quality is adequate and free;
swapping to a hosted model is a URL change thanks to decision 15.

## 18. Semantic cache on the query side, scoped by filters
**Date** 2026-09-07 · **Context** The plan put "Redis semantic cache" in the ingestion pipeline,
where every input is unique and there is nothing to cache. Semantic caching pays off on *user
queries*. **Decision** `redisvl.SemanticCache` in front of `/search`, matched on the query embedding
(cosine distance ≤ 0.12), filtered by a tag that hashes `(k, categories, since)` so different filters
never share a result, TTL 1 h. **Consequences** Paraphrases hit (observed distance 0.03), filters
are respected, and new papers become visible within the TTL. `/stats` exposes the hit rate,
`DELETE /cache` clears it.

## 19. Redis does exactly two jobs
**Date** 2026-09-07 · **Context** "Use Redis" needed concrete roles. **Decision** (a) the poller's
seen-set `arxiv:seen` with members `IDvN`, persistent; (b) the semantic cache index (Redis 8 has the
vector index built in). A rate-limit token bucket is only needed if the fetcher is ever scaled
past one instance, which decision 2 argues against. **Consequences** Redis is not a source of truth
for anything Postgres holds; losing it costs one re-poll of duplicates.

## 20. New versions are re-processed
**Date** 2026-09-07 · **Context** Papers get v2/v3 with real content changes. **Decision** The
seen-set member includes the version, so a new version is produced again; the worker's upsert
replaces the paper row and all its chunks. **Consequences** Search reflects the latest version;
older versions are not kept.

## 21. Search is chunk-level, then best chunk per paper
**Date** 2026-09-07 · **Context** Papers have many chunks; results should be papers. **Decision**
ANN over chunks (top 200 candidates, filters applied in the same query), then `DISTINCT ON
(arxiv_id)` keeping the best-scoring chunk, ordered by score, limited to `k`. The winning chunk's
text and section are returned so the user sees *why* a paper matched. **Consequences** Category
and date filters are ordinary SQL; the candidate window bounds cost.

## 22. Backfill through OAI-PMH
**Date** 2026-09-07 · **Context** arXiv's sanctioned bulk path is OAI-PMH; the search API is capped
and slow for history. **Decision** `scripts/backfill.py` harvests set `cs` with `metadataPrefix
arXiv`, filters to the three categories, and produces the same `paper` messages. That format has no
version number, so backfilled papers are version 1 with unversioned URLs; the poller upgrades them if
a newer version is announced. **Consequences** One page (1300 records, ~600 matches) per request at
the same 3 s spacing; must not run alongside the fetcher.

## 23. Never serve PDFs
**Date** 2026-09-07 · **Context** arXiv's terms forbid redistributing e-prints from your own servers.
**Decision** PDFs are processed in a temp file and discarded; only extracted chunk text is stored.
The API links to `arxiv.org/abs/...`. **Consequences** Nothing to store or license.

## 24. Infra in compose; Python on the host for now
**Date** 2026-09-07 · **Context** Fast iteration during the build. **Decision** Redpanda, Console,
Postgres/pgvector and Redis 8 run in `docker-compose.yml`; Rust binaries and Python services run
via `cargo` and `uv` through `scripts/pipeline.sh`. **Consequences** No Dockerfiles for the Python
services yet; adding them is mechanical.

## 25. Observability: logs + Redpanda Console; no Prometheus yet
**Date** 2026-09-07 · **Context** Consumer lag is the metric that matters; structured logs with the
arXiv id as correlation key make a paper traceable across stages. **Decision** `tracing` (Rust) and
`structlog` JSON (Python) keyed by `arxiv_id`; lag and DLQ via Redpanda Console; `scripts/smoke.sh`
for invariants. **Consequences** Enough to debug the pipeline; Prometheus/Grafana deferred.

## 26. A distributed shape for a workload that does not need one
**Date** 2026-09-07 · **Context** ~600 papers/day is under an hour of work for one worker and one
GPU. **Decision** Keep the queue, staged polyglot pipeline, DLQ and idempotency anyway, because the
project is portfolio-first, the daily burst and backfill are real, and the user wants to keep using
it. Stage it so a usable search exists before the expensive pieces (phase 2 before 3–6).
**Consequences** More moving parts than strictly necessary, each justified above and each verified
end to end.

## 27. One request budget in Redis, shared by every arXiv-facing process
**Date** 2026-09-10 · **Context** Decision 2 gave each process its own in-memory `MinInterval`, so
the poller, the fetcher and `backfill.py` could each honour the 3 s spacing while jointly violating
it; the README's steady state (poller every 15 min *and* a fetcher) was therefore forbidden by
decisions 2 and 22, and the fetcher had to be stopped for every backfill. **Decision** The spacing
lives in Redis: `schemas/ratelimit.lua` atomically reserves the next send slot (`slot = max(last +
interval, now)` on the server clock, returns the sleep) under one key, `arxiv:ratelimit`. Rust
(`common::ratelimit::SharedMinInterval`) and Python (`arxiv_common.ratelimit`) load the same
script, so the Lua file is the contract the way `messages.v1.json` is. A Redis error logs a warning
and falls back to the local spacing for that call. `--local-ratelimit` opts out for offline tests.
Slots are reserved `interval + 50 ms` apart: a sleeper can wake a millisecond late while the next
process lands exactly on its slot, and without the guard consecutive sends measured 2998 ms.
**Consequences** Any number of arXiv-facing processes share one FIFO budget; measured with the
poller and fetcher running concurrently, 11 interleaved requests had a minimum gap of 3048 ms and
none below 3000 (`scripts/ratelimit_verify.py`). The guard costs 1.7 % of throughput. Redis now does three jobs (amends 19); the single fetcher remains the right shape
because the budget, not the process count, is the ceiling.

## 28. Prometheus metrics on every stage
**Date** 2026-09-10 · **Context** Decision 25 deferred Prometheus; logs answer "what happened to
paper X" but not "is the pipeline keeping up", and the two numbers that matter operationally,
consumer lag and the arXiv request rate, were only visible in Redpanda Console. **Decision** Each
stage exposes `/metrics`: poller 9101, fetcher 9102, worker 9103, worker-abstract 9104, the API on
its own port. Names are a cross-language contract (`arxiv_requests_total{process,kind,outcome}`,
`arxiv_ratelimit_wait_seconds{process}`, `poller_*`, `fetcher_*`, `worker_*{group}`,
`api_*{cached}`), defined once per language (`common::telemetry`, `arxiv_common.metrics`). Prometheus
and Grafana run under the compose `observability` profile and also scrape Redpanda's
`/public_metrics` with `enable_consumer_group_metrics` set to include `consumer_lag` (done by
`dev.sh`); one provisioned dashboard covers throughput per stage, p50/p95 latencies, dead letters,
cache hit rate and lag. Counters and histograms only; the arXiv id stays in the logs.
**Consequences** Lag and the 20 req/min ceiling are graphable and alertable; metric names must
change in both languages together; one exporter thread or task per process.

## 29. Containerized pipeline services
**Date** 2026-09-10 · **Context** Decision 24 kept the binaries and services on the host for fast
iteration; the pipeline is now feature-complete and a reader should be able to run it with one
command, and host assumptions (localhost ports, `uv`/`cargo` on `PATH`) had leaked into the
configuration. **Decision** `rust/Dockerfile` (rust 1.97 builder with BuildKit cache mounts for
the registry and the incremental `target/`; debian-slim runtime with `poppler-utils`, non-root,
`HF_HOME` on a volume for the tokenizer) and `python/Dockerfile` (python 3.13-slim with `uv` pinned
to the lockfile's version, `uv sync --frozen --all-packages`, `schemas/` copied in, non-root), both
built from the repo root so they can reach `schemas/`. Five services sit behind the compose profile
`pipeline`; without it `docker compose up -d` is exactly decision 24's four infra services. The
fetcher is pinned to one replica (decision 2) and depends on Redis for the shared budget (27).
**Consequences** A cold build takes about a minute on the M4 Pro, rebuilds seconds.
`scripts/pipeline.sh` and `scripts/run.sh` keep working for host-side runs, so 24 is amended, not
superseded. `.env` values written for the host (`localhost:11434`) are wrong inside a container:
override them on the command line or point at the Spark.

## 30. The fast path never downgrades full text, and reads never open transactions
**Date** 2026-09-11 · **Context** The first time both consumers of `papers.new` ran together for
real, the `worker-abstract` group started from the beginning of the topic (a legal at-least-once
replay of 7,449 messages) and its abstract-only upsert would have replaced every row the
full-text worker had already written: decision 14's two paths had only ever agreed by timing.
The first fix, a pre-check `SELECT`, ran outside a transaction block on a non-autocommit psycopg
connection; that opened an implicit transaction, every later `conn.transaction()` became a
savepoint inside it, nothing committed, row locks accumulated, the full-text worker blocked on
them past `max.poll.interval.ms`, Redpanda evicted both consumers, and each died on its next
commit. **Decision** (a) An abstract document never replaces a row whose `source` is `html` or
`pdf` for the same or a newer version: `db.has_full_text` skips before embedding and the upsert
carries the same predicate in `ON CONFLICT … DO UPDATE … WHERE … RETURNING`, reporting the
message as `skipped`. A newer version still replaces, and the fetcher replaces it again later.
(b) Connections are opened with `autocommit=True` and a 120 s `lock_timeout`; every write is an
explicit transaction block; a live test asserts a read leaves the connection `IDLE`. (c) Shared
author rows are locked in `name_norm` order so two writers cannot deadlock. (d) A failed offset
commit is logged and the message redelivered; it is not fatal. **Consequences** Either topic may
be replayed in any order. Verified by killing the full-text worker with SIGKILL mid-message: the
in-flight paper was redelivered and stored again, with no gap, duplicate or orphan in `chunks`
and every count consistent (STATUS.md). The cost of the pre-check is one indexed `SELECT` per
abstract message.
