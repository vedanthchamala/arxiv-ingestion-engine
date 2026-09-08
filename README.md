# arXiv Ingestion Engine

Ingests new arXiv papers in **cs.LG, cs.CV, cs.RO** within minutes of each daily announcement,
embeds them, and serves semantic search. `SPEC.md` has the design and the arXiv constraints behind
it; `STATUS.md` tracks progress phase by phase.

## Quickstart
```
cp .env.example .env            # defaults point at the compose stack
./scripts/dev.sh                # Redpanda + Console, Postgres/pgvector, Redis 8; topics; migrations
```
