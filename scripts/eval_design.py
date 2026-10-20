#!/usr/bin/env python3
"""Compare design writers (the local model and Claude) on the same papers.

    uv run --project python python scripts/eval_design.py --providers local --n 8
    uv run --project python python scripts/eval_design.py --providers local,claude --n 8   # ANTHROPIC_API_KEY

Writes each paper's design synchronously and stores it exactly as the web page does, so every result can
be opened at /paper/<id>/design?model=<provider> and compared with the toggle. Existing designs for the
same model and prompt are reused unless --fresh. Prints, per provider: how many succeeded, time, steps,
claims the checks removed (by kind), evidence quotes kept, and cost; then one line per paper.
Each new paper's figures cost one arXiv request through the shared budget; Claude costs money per paper
(about $0.05-0.10 with claude-opus-5-5), so it only runs when named in --providers.
"""

import argparse
import statistics
import sys
from collections import Counter

from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arxiv_common import db
from arxiv_common.config import Settings
from arxiv_query_api import design


def sample(pool: ConnectionPool, n: int, seed: int, ids: list[str] | None) -> list[dict]:
    with pool.connection() as conn:
        if ids:
            return [p for p in (db.get_paper(conn, i) for i in ids) if p]
        rows = conn.execute(
            "SELECT arxiv_id FROM papers WHERE source = 'html' ORDER BY md5(arxiv_id || %s) LIMIT %s",
            (str(seed), n),
        ).fetchall()
        return [db.get_paper(conn, r["arxiv_id"]) for r in rows]


def produce(pool: ConnectionPool, settings: Settings, paper: dict, provider: str, fresh: bool) -> dict:
    with pool.connection() as conn:
        if fresh:
            db.reset_design(conn, paper["arxiv_id"], provider)
        claimed = db.claim_design(
            conn,
            paper["arxiv_id"],
            provider,
            paper["version"],
            design.model_name(provider, settings),
            design.PROMPT_VERSION,
            0,
        )
    if claimed:
        design.run(pool, settings, paper, provider)
    with pool.connection() as conn:
        return db.get_design(conn, paper["arxiv_id"], provider)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--providers", default="local", help="comma list of local, claude")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ids", nargs="*", help="specific arXiv ids instead of a sample")
    ap.add_argument("--fresh", action="store_true", help="regenerate even if a design exists")
    args = ap.parse_args()

    settings = Settings()
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    if "claude" in providers and not design.claude_available():
        sys.exit("claude needs ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the environment")
    pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": dict_row, "autocommit": True},
        configure=register_vector,
        open=True,
    )
    papers = sample(pool, args.n, args.seed, args.ids)
    rows: dict[str, list[tuple[dict, dict]]] = {p: [] for p in providers}
    for paper in papers:
        for provider in providers:
            print(f"{paper['arxiv_id']} {provider} ...", file=sys.stderr, flush=True)
            rows[provider].append((paper, produce(pool, settings, paper, provider, args.fresh)))

    print(
        "| Provider | Model | Done | Median s | Steps | Results kept | Terms kept | Removed (by kind) "
        "| Evidence kept | Cost |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for provider, items in rows.items():
        done = [r for _, r in items if r and r["status"] == "done"]
        removed = Counter(x["kind"] for r in done for x in r["rejected"])
        steps = [s for r in done for s in r["design"]["steps"]]
        evidence = sum(1 for s in steps if s.get("evidence"))
        quoted = sum(1 for r in done for x in r["rejected"] if x["kind"] == "evidence") + evidence
        cost = sum(float(r["cost_usd"] or 0) for r in done)
        print(
            f"| {provider} | {done[0]['model'] if done else '-'} | {len(done)}/{len(items)} | "
            f"{statistics.median(r['seconds'] for r in done) if done else 0:.1f} | "
            f"{sum(len(r['design']['steps']) for r in done) / max(len(done), 1):.1f} | "
            f"{sum(len(r['design']['results']) for r in done)} | "
            f"{sum(len(r['design']['terms']) for r in done)} | "
            f"{', '.join(f'{k} {v}' for k, v in sorted(removed.items())) or 'none'} | "
            f"{f'{evidence}/{quoted}' if quoted else '-'} | {f'${cost:.3f}' if cost else '-'} |"
        )
    print()
    for paper in papers:
        print(f"- **{paper['arxiv_id']}** {paper['title'][:80]}")
        for provider in providers:
            r = next(r for p, r in rows[provider] if p["arxiv_id"] == paper["arxiv_id"])
            if r and r["status"] == "done":
                d = r["design"]
                n_steps, n_removed = len(d["steps"]), len(r["rejected"])
                print(f"  - {provider}: {n_steps} steps, {n_removed} removed. {d['one_liner'][:140]}")
            else:
                print(f"  - {provider}: {r['status'] if r else 'missing'} {(r or {}).get('error') or ''}")
    pool.close()


if __name__ == "__main__":
    main()
