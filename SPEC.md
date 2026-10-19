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

---

# Part 2 — Research landscape agent

**Status:** proposed 2026-09-15; deferred 2026-10-19 in favour of making Part 1 usable (web UI,
watchlists, quickstart). Nothing below is built.

## Goal
Input: a research area in plain words ("diffusion policies for robot manipulation"). Output: a map of
the field instead of a ranked list:
- **Threads**: the main lines of work, each with a one-sentence thesis and its member papers.
- **Origins**: the paper(s) that started each thread, with dates.
- **Motivation**: what each thread is responding to (a problem, or a limitation of an earlier thread).
- **Tensions**: where threads or papers disagree, with a paper on each side.
- **Open problems**: what is still unsolved, and which papers say so.

Every claim links to the papers and passages it rests on.

## What the data can and cannot support (measured 2026-09-15)
- **The local corpus is a recent slice, not a field history.** 8,054 papers; 6,177 (77 %) published
  Aug–Sep 2026, 189 before 2025. For "diffusion policy for robot manipulation" the top 12 of 50 local
  hits are all 2024–2026 follow-ups. The papers that *started* a thread are mostly absent, so the
  corpus alone cannot answer the core question.
- **Semantic Scholar (S2) fills the gap.** Without a key it resolved a 2017 paper and a local paper
  published 2026-09-03 (22 references), so the reference graph of local papers is available. Citation
  *contexts* are sparse (1 of 5 sampled citations of Diffusion Policy had one; `intents` empty):
  supporting evidence, not the backbone. OpenAlex returned 404 for the arXiv-DOI form tried; not pursued.
- **Edges cannot come from local text.** Bibliographies are stripped at ingest (decision 13) and
  in-text citations are numeric (`REAP [15]`); S2 references supply the edges.
- **Local full text is the best evidence for relations.** 18,113 chunks sit in Related Work /
  Background / Prior Work sections. That asymmetry is the design: recent papers (local, full text)
  explain how they relate to older ones (external, abstract + graph).
- **Known thin spot.** Papers from 2024 to mid-2026 that are not in the corpus are known only by
  abstract, so the middle of a lineage is thinner than its ends. Backfilling them through the existing
  pipeline (OAI-PMH, shared budget) is a later option once the 429 issue is understood.
- **Zero arXiv traffic.** The agent never calls arxiv.org or export.arxiv.org; the shared request budget
  and the open 429 investigation are untouched.

## Shape: a journaled pipeline with Claude inside the steps, not a free-running agent
A tool-use loop that picks its own next action is the wrong backbone here: its state is a growing
conversation, so resuming means replaying it, and its cost has no natural bound. Instead the run is a
fixed pipeline of named steps, Claude's judgment lives inside steps, and each step's output is a small
JSON document in Postgres. Resume becomes exact (skip every step already done) and cost predictable.
Model-driven exploration enters in one bounded place (gap-filling, stage 3), where each iteration is
itself a journaled step.

```
browser ──▶ landscape-web  (FastAPI + Jinja2 + HTMX, :8010)   start / pause / resume / cancel, progress, map
                │  inserts runs, reads the journal
                ▼
           Postgres ── landscape_runs · landscape_steps · llm_calls · s2_cache   (+ papers, chunks)
                ▲
           landscape-runner  (claims runs by lease, walks the pipeline, heartbeats)
                ├─▶ arxiv_common: bge-m3 embeddings + db.search over the local corpus
                ├─▶ Semantic Scholar Graph API (every response cached in s2_cache)
                └─▶ Claude API (every request journaled in llm_calls before it is sent)
```
Web and runner are separate processes so either can die without affecting the other; both join
`scripts/run.sh`. New workspace member `python/landscape` (package `arxiv_landscape`) and migration
`sql/002_landscape.sql` (applied by `dev.sh` like 001). Reused: `arxiv_common` config, connection,
`search`, embedding client, logging, metrics. Not used: Redpanda and the Rust stages. A run is a small
DAG whose state the UI must query, and a message log is the wrong store for that.

## Pipeline
| Step key | Kind | What it does |
|---|---|---|
| `scope` | Claude | area → definition, include/exclude criteria, 3–6 facet queries |
| `retrieve.local` | code | each facet → bge-m3 → `db.search` (k 40) → union |
| `retrieve.s2` | code | each facet → S2 paper search → older and not-yet-ingested papers |
| `screen` (stage 3) | Claude | keep/drop candidates against the criteria, 25 per call |
| `graph` | code | S2 references (citations from stage 3) of kept papers; in-topic in-degree ranks root candidates, which join the set |
| `extract.<paper>` | Claude, fan-out | per paper: problem, approach, key claims, builds-on / responds-to (linked to its S2 references), limitations, stated disagreements, each with evidence. Topic-independent, so reusable across runs |
| `threads` | Claude | records + in-set citation edges → threads with origins and members |
| `synthesize.<thread>` | Claude | lineage in date order, what it responds to, current state |
| `tensions` | Claude | cross-thread disagreements (both sides) and open problems |
| `validate` | code | every paper reference resolves in the run's set; from stage 2 every quote is found in its source and every builds-on link is a real reference edge; failures leave the map and are listed |
| `assemble` | code | versioned landscape document (pydantic) stored on the run |

Evidence per paper: local papers → chunks (intro, related work, discussion, conclusion) plus the S2
reference list; external papers → S2 abstract, TLDR and any citation contexts. The map shows which
kind of evidence each claim has.

## Journal and resume
Tables:
- `landscape_runs`: id, topic, params, status (`queued|running|paused|done|failed|cancelled`),
  budget_usd, spent_usd, lease_owner, lease_expires_at, result, error, timestamps.
- `landscape_steps`: PK (run_id, step_key); status (`running|done|failed`), input_hash, attempt,
  output jsonb, cost_usd, error, started_at, finished_at.
- `llm_calls`: PK call_key = sha256 of the canonical request (model, system, messages, output schema,
  params); run_id, step_key, status (`sent|done|failed`), send_count, response, usage, cost_usd.
- `s2_cache`: PK request key; response, fetched_at (references kept; citations refreshed after 14 days).

Rules:
1. **Write-ahead.** Before a step runs, its row is upserted to `running` with attempt + 1 and its
   input_hash, and committed. Output, `done` and cost commit together in one transaction afterwards.
2. **Resume.** The runner walks the pipeline from the top: a step that is `done` with the same
   input_hash is loaded, not run; anything else runs. input_hash covers the step's inputs, its prompt
   version and the model, so a prompt change re-runs exactly the steps downstream of it.
3. **Every Claude request is journaled on its own.** It is written as `sent` (send_count + 1) before it
   goes out, then completed with the response. A re-run step finds its finished calls by key and pays
   nothing for them; a crash re-pays only for requests in flight (≤ fan-out concurrency, default 4).
   Prompts are pure functions of step inputs (sorted ids, no timestamps), so keys survive restarts,
   and an identical request in another run reuses the stored answer.
4. **Leases.** A runner claims a run with `FOR UPDATE SKIP LOCKED`, heartbeats every 10 s with a 60 s
   lease; a run whose lease lapsed is reclaimed on the next runner start.
5. **Budget.** Each call's cost (from `usage`) is added to the run. Once spent ≥ budget the run moves
   to `paused` (reason: budget) before the next call, overshooting by at most the calls already in
   flight. The UI can raise the cap and resume.
6. **Pause and cancel** are flags checked between steps and before every call.

Alternative considered: DBOS Transact (durable Python workflows on Postgres) covers rules 1, 2 and 4.
Not chosen: the journal is a few hundred lines, and the parts specific to this workload (per-request LLM
journal, budget pause, batch-id resume in stage 4) would be custom either way.

## Model and cost
Claude Opus 5 (`claude-opus-5`, $5 / $25 per MTok) for every Claude step, with adaptive thinking,
structured outputs validated by pydantic, and the server-side refusal fallback on. Embeddings stay on
local bge-m3. Estimates per run, replaced by measured journal numbers in stage 1: skeleton (≤ 30
papers) $2–3; full pipeline (~80 papers) $6–9. `extract` dominates, and the Batch API (stage 4) halves
it. Running `extract` on Sonnet 5 ($2 / $10) would cut that share by ~60 %; that trade of quality for
money is the user's call, not a default.

## Interface
Server-rendered pages with no JS build: FastAPI + Jinja2 + HTMX (one vendored file).
- `/`: research-area box, budget cap, optional category/date filters; list of runs (status, cost, age).
- `/runs/{id}`: live step list (status, attempts, cost; refreshes every 2 s) with pause / resume /
  cancel / raise budget. When done, the map: a short field overview; one card per thread (thesis,
  origin papers with year, lineage timeline, what it responds to, members); tensions (claim, side A,
  side B, evidence); open problems. Every paper chip links to `arxiv.org/abs` (or S2) and shows its
  summary. Markdown and JSON export.
- From stage 2, clicking any claim shows its evidence quotes and where they came from.

## Stages
A stage is done when its pass conditions are checked and recorded in STATUS.md; the next one starts
after that.

**Stage 0: data probe (no Claude spend).** S2 client (rate-limited, 429 back-off, cached), the journal
migration, and the `graph` step run on its own for T1.
Pass:
1. ≥ 80 % of T1's local hits resolve in S2 with ≥ 1 reference.
2. The top 15 root candidates by in-topic in-degree contain ≥ 60 % of the T1 anchor list (below).
3. Citation-context coverage of T1's edges measured and recorded (informational).

If 1 or 2 fails, stop and rethink the data source before spending anything on Claude.

**Stage 1: walking skeleton on T1.** Steps `scope`, `retrieve.local`, `retrieve.s2`, `graph`
(references only), `extract.<paper>` (≤ 30 papers), one combined `synthesize` call (threads, lineage,
tensions, open problems), `validate` (paper references only), `assemble`. Full journal, runner, leases,
budget cap, UI.
Pass:
1. A run started from the browser finishes on T1 with ≥ 3 threads (each ≥ 1 origin, ≥ 3 members),
   ≥ 1 tension, ≥ 3 open problems, and 0 references to papers outside the run's set.
2. SIGKILL the runner during the `extract` fan-out and restart: the run finishes, no `done` step runs
   again (attempt counts unchanged), and Σ(send_count − 1) over `llm_calls` ≤ 4.
3. Kill and restart the web process mid-run: the run is unaffected and the page shows current progress.
4. A run capped below its cost pauses, and finishes after the cap is raised, with no repeated calls.
5. ≥ 60 % of the anchor list appears as origin or key papers.
6. Measured cost and wall time recorded (targets < $5, < 20 min).
7. Journal unit tests (skip done, re-run running, input-hash invalidation, call reuse, budget pause,
   lease reclaim), pytest and ruff green.

**Stage 2: evidence you can check.** Every claim (thesis, lineage link, responds-to, tension, open
problem) carries ≥ 1 evidence item {paper, source (chunk id | S2 abstract | S2 citation context),
quote}. `validate` accepts a quote only if it is a normalized substring of that source, and a builds-on
link only if it is an S2 reference edge; rejected claims leave the map and appear in a "rejected" panel
with the reason. Claim → evidence view in the UI.
Pass:
1. 0 unverifiable quotes reach the rendered map (enforced, and tested with planted fabrications).
2. ≥ 85 % of claims survive validation (a lower rate means the prompts need work, not a looser validator).
3. The user audits 20 random surviving claims: ≥ 18 are supported by their evidence.

**Stage 3: depth.** Split `synthesize` into `threads`, `synthesize.<thread>` and `tensions`; add
`screen`; add citations of root papers (recent responses to them); community detection on the in-set
citation + similarity graph as a prior for `threads` (Claude may merge or split, with a stated reason).
Bounded gap-filling: after `threads`, Claude lists gaps (a thread without an origin in the set, a method
named in evidence but missing) and may request ≤ 10 S2 lookups per iteration, ≤ 2 iterations, each a
journaled step. Optional "review scope" pause after `scope`.
Pass:
1. Anchor recall ≥ 80 % on T1 and on a second topic T2 from another category.
2. Every tension has validated evidence from ≥ 1 paper on each side.
3. Stability: two T1 runs with the call cache bypassed; threads matched by membership Jaccard average ≥ 0.6.
4. Cost per run recorded.

**Stage 4: cost.** The `extract` fan-out goes through the Message Batches API (50 % price); the batch id
is journaled at submission, and a restarted step polls that batch instead of resubmitting. Prompt
caching on the shared system prompt. Cross-run reuse of topic-independent extractions shown in the UI.
Pass:
1. T1 cold-run cost ≥ 40 % below stage 3, with anchor recall within one anchor.
2. A second, overlapping topic reuses ≥ 50 % of its extraction calls.
3. SIGKILL while a batch is pending, then restart: the same batch id is polled; 0 duplicate batches.

**Stage 5: living map (optional).** Saved landscapes update after each announcement night: new local
papers close to a thread's centroid are extracted and placed in a thread, or flagged as a possible new
one; the page shows what changed since the last visit.
Pass: after one real announcement night every saved landscape shows its new papers placed, at
≤ $0.50 per landscape per night, with no paper extracted twice.

## Skeleton topic and anchors
T1 = **diffusion policies for robot manipulation**: cs.RO is well covered (2,084 papers with primary
category cs.RO) and all 50 local hits score 0.63–0.72. Draft anchors, to be fixed by the user before the
first run so the eval is not fitted to the output: DDPM (Ho et al. 2020), Implicit Behavioral Cloning
(Florence et al. 2021), Behavior Transformers (Shafiullah et al. 2022), Diffuser (Janner et al. 2022),
Flow Matching (Lipman et al. 2022), Diffusion Policy (Chi et al. 2023), ACT / ALOHA (Zhao et al. 2023),
3D Diffusion Policy (Ze et al. 2024), Consistency Policy (Prasad et al. 2024), Octo (2024), π0 (2024).

## Out of scope
Any arXiv traffic; full text of papers outside the corpus; chat over the map; multi-user or auth; a
Kafka topic for runs; writing prose literature reviews.

## Open questions for the user
1. Anthropic credentials for the runner, and the default per-run cap (proposed $5).
2. A free Semantic Scholar API key (semanticscholar.org/product/api). Without one we share the
   unauthenticated pool; the cache limits the damage, but 429s are likely during `graph`.
3. T1 and the anchor list above.
