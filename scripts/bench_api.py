#!/usr/bin/env python3
"""HTTP load test of the running query API. Latency is the full round trip a client sees.

    uv run --project python python scripts/bench_api.py [--base-url http://localhost:8000] [--duration 20] \
        [--concurrency 8,32]

Phases: cold (cache cleared, one sequential pass over the query pool), warm (same pass again, expect cache
hits), paraphrase (10 rewordings of cached queries), concurrency sweep (cache cleared before each level,
workers cycle through the pool for --duration seconds). Prints markdown tables; writes data/bench_api.json.
"""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

QUERIES = [
    "diffusion policy for robot manipulation",
    "SLAM for legged robots",
    "reinforcement learning for quadruped locomotion",
    "grasp planning with tactile sensing",
    "motion planning for autonomous driving in dense traffic",
    "sim-to-real transfer for robotic manipulation",
    "vision language action models for robot control",
    "humanoid robot whole-body control",
    "multi-robot task allocation",
    "aerial drone navigation in cluttered environments",
    "imitation learning from human demonstrations",
    "model predictive control for mobile robots",
    "dexterous in-hand manipulation with reinforcement learning",
    "LiDAR odometry and mapping",
    "robot navigation with large language models",
    "soft robot gripper design",
    "trajectory optimization for autonomous vehicles",
    "collision avoidance for unmanned aerial vehicles",
    "teleoperation of robotic arms",
    "active perception for object search",
    "vision transformer pretraining on ImageNet",
    "text-to-image generation with diffusion models",
    "3D Gaussian splatting for novel view synthesis",
    "semantic segmentation of medical images",
    "object detection in aerial drone imagery",
    "video object tracking with transformers",
    "monocular depth estimation",
    "neural radiance fields for scene reconstruction",
    "face recognition under occlusion",
    "open-vocabulary object detection",
    "image super-resolution with diffusion",
    "human pose estimation from a single image",
    "multimodal large language models for visual question answering",
    "anomaly detection in industrial images",
    "point cloud segmentation for autonomous driving",
    "video generation with diffusion transformers",
    "few-shot image classification",
    "remote sensing image change detection",
    "adversarial attacks on image classifiers",
    "optical flow estimation",
    "gradient descent convergence in overparameterized networks",
    "graph neural networks for molecular property prediction",
    "federated learning with differential privacy",
    "time series forecasting with transformers",
    "scaling laws for large language model pretraining",
    "contrastive self-supervised representation learning",
    "mixture of experts routing",
    "reinforcement learning from human feedback for language models",
    "low-rank adaptation for fine-tuning large models",
    "uncertainty quantification with conformal prediction",
    "knowledge distillation for model compression",
    "out-of-distribution generalization",
    "physics-informed neural networks for partial differential equations",
    "causal inference with machine learning",
    "quantization of large language models",
    "tabular data learning with deep networks",
    "neural network pruning and sparsity",
    "Bayesian optimization for hyperparameter tuning",
    "continual learning without catastrophic forgetting",
    "in-context learning in transformers",
]

PARAPHRASES = [
    ("diffusion policy for robot manipulation", "diffusion policies for robotic manipulation"),
    ("SLAM for legged robots", "simultaneous localization and mapping on legged robots"),
    ("vision transformer pretraining on ImageNet", "pretraining vision transformers on ImageNet"),
    ("text-to-image generation with diffusion models", "generating images from text using diffusion models"),
    (
        "gradient descent convergence in overparameterized networks",
        "convergence of gradient descent for overparameterized neural networks",
    ),
    (
        "graph neural networks for molecular property prediction",
        "predicting molecular properties with graph neural networks",
    ),
    ("federated learning with differential privacy", "differentially private federated learning"),
    ("3D Gaussian splatting for novel view synthesis", "novel view synthesis using 3D Gaussian splatting"),
    (
        "reinforcement learning for quadruped locomotion",
        "quadruped robot locomotion via reinforcement learning",
    ),
    ("time series forecasting with transformers", "transformer models for time series forecasting"),
]


@dataclass
class Sample:
    query: str
    latency_ms: float
    status: int
    took_ms: int | None = None
    cached: bool | None = None
    error: str | None = None


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


def fmt(x: float | None, digits: int = 0) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def one(client: httpx.AsyncClient, query: str, k: int) -> Sample:
    t0 = time.perf_counter()
    try:
        r = await client.post("/search", json={"query": query, "k": k})
        ms = (time.perf_counter() - t0) * 1000
    except httpx.HTTPError as e:
        return Sample(query, (time.perf_counter() - t0) * 1000, 0, error=repr(e)[:200])
    if r.status_code != 200:
        return Sample(query, ms, r.status_code, error=r.text[:200])
    body = r.json()
    return Sample(query, ms, 200, took_ms=body["took_ms"], cached=body["cached"])


async def sequential(client: httpx.AsyncClient, queries: list[str], k: int) -> list[Sample]:
    return [await one(client, q, k) for q in queries]


async def sweep(
    client: httpx.AsyncClient, queries: list[str], k: int, concurrency: int, duration: float
) -> tuple[list[Sample], float]:
    samples: list[Sample] = []
    deadline = time.perf_counter() + duration

    async def worker(i: int) -> None:
        j = i * len(queries) // concurrency
        while time.perf_counter() < deadline:
            samples.append(await one(client, queries[j % len(queries)], k))
            j += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    return samples, time.perf_counter() - t0


def summarize(samples: list[Sample], concurrency: int, wall: float | None = None) -> dict:
    ok = [s for s in samples if s.status == 200]
    hit = [s for s in ok if s.cached]
    miss = [s for s in ok if not s.cached]
    return {
        "requests": len(samples),
        "ok": len(ok),
        "errors": len(samples) - len(ok),
        "first_error": next((s.error for s in samples if s.error), None),
        "concurrency": concurrency,
        "wall_s": wall,
        "rps": len(ok) / wall if wall else None,
        "cached_rate": len(hit) / len(ok) if ok else None,
        "latency_ms": dist([s.latency_ms for s in ok]),
        "took_ms": dist([s.took_ms for s in ok]),
        "cached_latency_ms": dist([s.latency_ms for s in hit]),
        "uncached_latency_ms": dist([s.latency_ms for s in miss]),
    }


async def clear_cache(client: httpx.AsyncClient) -> None:
    (await client.delete("/cache")).raise_for_status()


async def run(args: argparse.Namespace) -> dict:
    limits = httpx.Limits(max_connections=max(args.concurrency) + 4)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout, limits=limits) as client:
        (await client.get("/health")).raise_for_status()
        stats_before = (await client.get("/stats")).json()
        phases: dict[str, dict] = {}

        await clear_cache(client)
        cold = await sequential(client, QUERIES, args.k)
        phases["cold"] = summarize(cold, 1)
        log(f"cold: {phases['cold']['ok']}/{len(cold)} ok, p50 {fmt(phases['cold']['latency_ms']['p50'])} ms")
        if phases["cold"]["errors"] > len(cold) / 2:
            log(f"aborting: most cold requests failed, first error: {phases['cold']['first_error']}")
            return {"phases": phases, "aborted": True}

        warm = await sequential(client, QUERIES, args.k)
        phases["warm"] = summarize(warm, 1)
        log(f"warm: cached rate {fmt(phases['warm']['cached_rate'], 2)}")

        para = await sequential(client, [p for _, p in PARAPHRASES], args.k)
        phases["paraphrase"] = summarize(para, 1)
        phases["paraphrase"]["pairs"] = [
            {"original": o, "paraphrase": p, "cached": s.cached, "latency_ms": s.latency_ms}
            for (o, p), s in zip(PARAPHRASES, para, strict=True)
        ]
        log(f"paraphrase: cached rate {fmt(phases['paraphrase']['cached_rate'], 2)}")

        for c in args.concurrency:
            await clear_cache(client)
            samples, wall = await sweep(client, QUERIES, args.k, c, args.duration)
            phases[f"sweep_{c}"] = summarize(samples, c, wall)
            s = phases[f"sweep_{c}"]
            log(f"sweep c={c}: {s['ok']} ok, {s['errors']} errors, {fmt(s['rps'], 1)} req/s")
            if s["errors"] > s["requests"] * 0.2:
                log(f"stopping sweep: >20% errors at concurrency {c}, first error: {s['first_error']}")
                s["aborted_after"] = True
                break

        stats_after = (await client.get("/stats")).json()
    return {
        "phases": phases,
        "stats_before": stats_before,
        "stats_after": stats_after,
        "raw": {
            "cold": [asdict(s) for s in cold],
            "warm": [asdict(s) for s in warm],
            "paraphrase": [asdict(s) for s in para],
        },
    }


def render(result: dict, args: argparse.Namespace) -> str:
    phases = result["phases"]
    totals = result.get("stats_after", {}).get("totals", {})
    labels = {
        "cold": "cold (cache cleared, sequential)",
        "warm": "warm (same queries again)",
        "paraphrase": "paraphrases of cached queries",
    }
    for c in args.concurrency:
        labels[f"sweep_{c}"] = f"sweep {c} workers x {args.duration:.0f} s (cache cleared first)"
    lines = [
        f"Target {args.base_url}; {len(QUERIES)} distinct queries, k={args.k}; corpus {totals.get('papers')} "
        f"papers / {totals.get('chunks')} chunks; latency = full HTTP round trip",
        "",
        "| Phase | Conc. | Requests | Errors | req/s | Cached | p50 ms | p95 ms | p99 ms | mean ms "
        "| max ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, s in phases.items():
        d = s["latency_ms"]
        cached = "-" if s["cached_rate"] is None else f"{100 * s['cached_rate']:.0f}%"
        lines.append(
            f"| {labels.get(key, key)} | {s['concurrency']} | {s['requests']} | {s['errors']} | "
            f"{fmt(s['rps'], 1)} | {cached} | {fmt(d['p50'])} | {fmt(d['p95'])} | {fmt(d['p99'])} | "
            f"{fmt(d['mean'])} | {fmt(d['max'])} |"
        )
    lines += [
        "",
        "Server-side `took_ms` (embed + cache lookup or SQL; excludes HTTP and serialization):",
        "",
        "| Phase | p50 ms | p95 ms | p99 ms | mean ms |",
        "|---|---|---|---|---|",
    ]
    for key, s in phases.items():
        d = s["took_ms"]
        lines.append(
            f"| {labels.get(key, key)} | {fmt(d['p50'])} | {fmt(d['p95'])} | {fmt(d['p99'])} | "
            f"{fmt(d['mean'])} |"
        )
    lines += [
        "",
        "Sweep latency split by cache outcome (the pool is only 60 queries, so most sweep traffic hits):",
        "",
        "| Phase | uncached n | uncached p50 | uncached p95 | cached n | cached p50 | cached p95 |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, s in phases.items():
        if not key.startswith("sweep_"):
            continue
        u, h = s["uncached_latency_ms"], s["cached_latency_ms"]
        lines.append(
            f"| {labels[key]} | {u['n']} | {fmt(u['p50'])} | {fmt(u['p95'])} | {h['n']} | {fmt(h['p50'])} | "
            f"{fmt(h['p95'])} |"
        )
    if "paraphrase" in phases:
        pairs = phases["paraphrase"]["pairs"]
        lines += [
            "",
            "Semantic-cache hits for paraphrased queries:",
            "",
            "| Cached query | Paraphrase | Hit |",
            "|---|---|---|",
        ]
        lines += [
            f"| {p['original']} | {p['paraphrase']} | {'yes' if p['cached'] else 'no'} |" for p in pairs
        ]
        lines.append(f"| **hit rate** | | **{sum(bool(p['cached']) for p in pairs)}/{len(pairs)}** |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--duration", type=float, default=20.0, help="seconds per concurrency level")
    ap.add_argument("--concurrency", default="8,32", help="comma-separated worker counts")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=Path("data/bench_api.json"))
    args = ap.parse_args()
    args.concurrency = [int(c) for c in args.concurrency.split(",") if c]

    started = datetime.now(UTC)
    result = asyncio.run(run(args))
    result["started_at"] = started.isoformat()
    result["config"] = {
        "base_url": args.base_url,
        "duration": args.duration,
        "concurrency": args.concurrency,
        "k": args.k,
        "queries": len(QUERIES),
    }
    print(render(result, args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, default=str))
    log(f"wrote {args.out}")
    if result.get("aborted"):
        sys.exit(1)


if __name__ == "__main__":
    main()
