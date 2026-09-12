#!/usr/bin/env python3
"""Retrieval-quality evaluation of pgvector search, bypassing the API and its semantic cache.

    uv run --project python python scripts/eval_search.py [--n 500] [--paraphrase-n 200] [--seed 42] [--k 10]

Self-retrieval (title -> own paper, unfiltered and filtered to the paper's primary category), paraphrase
robustness (first abstract sentence -> own paper), category precision@5 on a fixed query set, and raw
embedding / SQL latency. Prints markdown tables to stdout; writes data/eval_search.json.
"""

import argparse
import json
import math
import random
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from arxiv_common import db
from arxiv_common.config import Settings
from arxiv_common.inference import InferenceClient

CATEGORY_QUERIES = [
    ("diffusion policy for robot manipulation", "cs.RO"),
    ("SLAM for legged robots", "cs.RO"),
    ("reinforcement learning for quadruped locomotion", "cs.RO"),
    ("grasp planning with tactile sensing", "cs.RO"),
    ("motion planning for autonomous driving in dense traffic", "cs.RO"),
    ("vision transformer pretraining on ImageNet", "cs.CV"),
    ("text-to-image generation with diffusion models", "cs.CV"),
    ("3D Gaussian splatting for novel view synthesis", "cs.CV"),
    ("semantic segmentation of medical images", "cs.CV"),
    ("object detection in aerial drone imagery", "cs.CV"),
    ("gradient descent convergence in overparameterized networks", "cs.LG"),
    ("graph neural networks for molecular property prediction", "cs.LG"),
    ("federated learning with differential privacy", "cs.LG"),
    ("time series forecasting with transformers", "cs.LG"),
    ("scaling laws for large language model pretraining", "cs.LG"),
]

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def first_sentence(abstract: str, min_chars: int = 40, max_chars: int = 300) -> str:
    out = ""
    for piece in _SENTENCE_BREAK.split(abstract.strip()):
        out = f"{out} {piece}".strip()
        if len(out) >= min_chars:
            break
    return out[:max_chars]


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


def dist(xs: list[float]) -> dict:
    return {
        "n": len(xs),
        "p50": pct(xs, 50),
        "p95": pct(xs, 95),
        "p99": pct(xs, 99),
        "mean": statistics.fmean(xs) if xs else None,
        "max": max(xs) if xs else None,
    }


def rank_of(hits: list[db.Hit], arxiv_id: str) -> int | None:
    for i, h in enumerate(hits, 1):
        if h.arxiv_id == arxiv_id:
            return i
    return None


def retrieval_metrics(ranks: list[int | None], k: int) -> dict:
    n = len(ranks)
    found = [r for r in ranks if r is not None]
    return {
        "n": n,
        "recall@1": sum(r == 1 for r in found) / n,
        "recall@5": sum(r <= 5 for r in found) / n,
        f"recall@{k}": sum(r <= k for r in found) / n,
        "mrr": sum(1 / r for r in found) / n,
    }


def fmt(x: float | None, digits: int = 3) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def corpus_info(conn) -> dict:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM papers) AS papers, (SELECT count(*) FROM chunks) AS chunks, "
        "(SELECT count(*) FROM papers WHERE source = 'abstract') AS abstract_only, "
        "(SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector"
    ).fetchone()
    info = dict(row)
    try:
        info["hnsw_ef_search"] = conn.execute("SHOW hnsw.ef_search").fetchone()["hnsw.ef_search"]
    except Exception:
        info["hnsw_ef_search"] = None
    return info


def main() -> None:
    defaults = Settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=500, help="papers sampled for self-retrieval")
    ap.add_argument("--paraphrase-n", type=int, default=200, help="subset of the sample used for paraphrase")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--ef-search", type=int, default=None, help="pgvector hnsw.ef_search for the run")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--database-url", default=defaults.database_url)
    ap.add_argument("--embedding-url", default=defaults.embedding_url)
    ap.add_argument("--embedding-model", default=defaults.embedding_model)
    ap.add_argument("--out", type=Path, default=Path("data/eval_search.json"))
    args = ap.parse_args()

    settings = Settings(embedding_url=args.embedding_url, embedding_model=args.embedding_model)
    client = InferenceClient(settings, timeout_s=120)
    conn = psycopg.connect(args.database_url, row_factory=dict_row, prepare_threshold=None)
    register_vector(conn)
    started = datetime.now(UTC)

    papers = conn.execute(
        "SELECT arxiv_id, title, abstract, primary_category, categories FROM papers ORDER BY arxiv_id"
    ).fetchall()
    sample = random.Random(args.seed).sample(papers, min(args.n, len(papers)))
    log(f"corpus {len(papers)} papers, sample {len(sample)}; {args.embedding_model} @ {args.embedding_url}")
    for _ in range(3):
        client.embed(["warm-up query"])

    embed_ms: list[float] = []
    sql_ms: list[float] = []
    sql_filtered_ms: list[float] = []

    def timed_search(text: str, k: int, categories: list[str] | None = None):
        t0 = time.perf_counter()
        vec = client.embed([text])[0]
        t1 = time.perf_counter()
        hits = db.search(conn, vec, k=k, categories=categories, ef_search=args.ef_search)
        t2 = time.perf_counter()
        embed_ms.append((t1 - t0) * 1000)
        (sql_filtered_ms if categories else sql_ms).append((t2 - t1) * 1000)
        return vec, hits

    t_phase = time.perf_counter()
    self_rows = []
    for p in sample:
        vec, hits = timed_search(p["title"], args.k)
        t0 = time.perf_counter()
        hits_f = db.search(conn, vec, k=args.k, categories=[p["primary_category"]], ef_search=args.ef_search)
        sql_filtered_ms.append((time.perf_counter() - t0) * 1000)
        self_rows.append(
            {
                "arxiv_id": p["arxiv_id"],
                "primary_category": p["primary_category"],
                "rank": rank_of(hits, p["arxiv_id"]),
                "rank_filtered": rank_of(hits_f, p["arxiv_id"]),
                "n_hits_filtered": len(hits_f),
            }
        )
    log(f"self-retrieval done in {time.perf_counter() - t_phase:.0f}s")

    t_phase = time.perf_counter()
    para_rows = []
    for p in sample[: args.paraphrase_n]:
        query = first_sentence(p["abstract"])
        _, hits = timed_search(query, args.k)
        para_rows.append({"arxiv_id": p["arxiv_id"], "query": query, "rank": rank_of(hits, p["arxiv_id"])})
    log(f"paraphrase done in {time.perf_counter() - t_phase:.0f}s")

    cat_rows = []
    for query, expected in CATEGORY_QUERIES:
        _, hits = timed_search(query, 5)
        in_cat = sum(expected in h.categories for h in hits)
        cat_rows.append(
            {
                "query": query,
                "expected": expected,
                "hits": len(hits),
                "in_category": in_cat,
                "precision": in_cat / len(hits) if hits else 0.0,
                "top": [(h.arxiv_id, h.primary_category, round(h.score, 3)) for h in hits],
            }
        )

    info = corpus_info(conn)
    self_unf = retrieval_metrics([r["rank"] for r in self_rows], args.k)
    self_fil = retrieval_metrics([r["rank_filtered"] for r in self_rows], args.k)
    para = retrieval_metrics([r["rank"] for r in para_rows], args.k)
    short_filtered = sum(r["n_hits_filtered"] < args.k for r in self_rows)
    cat_mean = statistics.fmean(r["precision"] for r in cat_rows)
    latency = {
        "embed_ms": dist(embed_ms),
        "sql_unfiltered_ms": dist(sql_ms),
        "sql_filtered_ms": dist(sql_filtered_ms),
    }

    rk = f"Recall@{args.k}"
    lines = [
        f"Corpus: {info['papers']} papers, {info['chunks']} chunks ({info['abstract_only']} abstract-only); "
        f"pgvector {info['pgvector']}, hnsw.ef_search={info['hnsw_ef_search']}; "
        f"embeddings {args.embedding_model} via {args.embedding_url}; seed={args.seed}",
        "",
        f"### Self-retrieval (paper title -> its own paper), n={self_unf['n']}, k={args.k}",
        "",
        f"| Setting | Recall@1 | Recall@5 | {rk} | MRR |",
        "|---|---|---|---|---|",
    ]
    for name, m in (("unfiltered", self_unf), ("categories=[primary_category]", self_fil)):
        lines.append(
            f"| {name} | {fmt(m['recall@1'])} | {fmt(m['recall@5'])} | {fmt(m[f'recall@{args.k}'])} | "
            f"{fmt(m['mrr'])} |"
        )
    lines += [
        "",
        f"Filtered searches returning fewer than k hits: {short_filtered}/{len(self_rows)}",
        "",
        f"### Paraphrase (first abstract sentence -> its own paper), n={para['n']}, k={args.k}",
        "",
        f"| Setting | Recall@1 | Recall@5 | {rk} | MRR |",
        "|---|---|---|---|---|",
        f"| unfiltered | {fmt(para['recall@1'])} | {fmt(para['recall@5'])} | "
        f"{fmt(para[f'recall@{args.k}'])} | {fmt(para['mrr'])} |",
        "",
        "### Category precision@5 (unfiltered; hit counts if its `categories` contain the expected one)",
        "",
        "| Query | Expected | In category |",
        "|---|---|---|",
    ]
    lines += [f"| {r['query']} | {r['expected']} | {r['in_category']}/{r['hits']} |" for r in cat_rows]
    lines += [
        f"| **mean precision@5** | | **{fmt(cat_mean)}** |",
        "",
        "### Raw search latency (ms; one query at a time, no cache)",
        "",
        "| Stage | n | p50 | p95 | mean |",
        "|---|---|---|---|---|",
    ]
    for name, key in (
        (f"query embedding ({args.embedding_model})", "embed_ms"),
        (f"SQL k={args.k} unfiltered", "sql_unfiltered_ms"),
        (f"SQL k={args.k} filtered by category", "sql_filtered_ms"),
    ):
        d = latency[key]
        lines.append(f"| {name} | {d['n']} | {fmt(d['p50'], 1)} | {fmt(d['p95'], 1)} | {fmt(d['mean'], 1)} |")
    print("\n".join(lines))

    result = {
        "started_at": started.isoformat(),
        "config": {
            "n": args.n,
            "paraphrase_n": args.paraphrase_n,
            "k": args.k,
            "seed": args.seed,
            "embedding_url": args.embedding_url,
            "embedding_model": args.embedding_model,
        },
        "corpus": info,
        "self_retrieval": {"unfiltered": self_unf, "filtered": self_fil, "short_filtered": short_filtered},
        "paraphrase": para,
        "category_precision": {"mean": cat_mean, "queries": cat_rows},
        "latency_ms": latency,
        "self_rows": self_rows,
        "paraphrase_rows": para_rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, default=str))
    log(f"wrote {args.out}")
    client.close()
    conn.close()


if __name__ == "__main__":
    main()
