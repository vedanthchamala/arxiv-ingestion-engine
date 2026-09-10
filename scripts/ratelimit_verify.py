#!/usr/bin/env python3
"""Check the arXiv request spacing across concurrently running processes from their logs.

    RUST_LOG=info,poller=debug,fetcher=debug rust/target/release/poller --once  > data/verify_poller.log
    RUST_LOG=info,poller=debug,fetcher=debug rust/target/release/fetcher ...    > data/verify_fetcher.log
    python3 scripts/ratelimit_verify.py data/verify_poller.log data/verify_fetcher.log

Every outgoing request logs one `arxiv request` line (kind, wait_ms) the moment the limiter releases
it. This merges those lines from all given logs, sorts by timestamp, and reports the gaps between
consecutive requests. Exit status is non-zero if any gap is below --min-gap-ms.
"""

import argparse
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\dT[\d:.]+Z)\s+\w+\s+arxiv request\s+(?P<fields>kind=.*)$")
FIELD = re.compile(r'(\w+)=("[^"]*"|\S+)')


def requests(path: Path, process: str) -> list[tuple[datetime, str, str, int, str]]:
    out = []
    for raw in path.read_text().splitlines():
        m = LINE.match(ANSI.sub("", raw).strip())
        if not m:
            continue
        f = {k: v.strip('"') for k, v in FIELD.findall(m["fields"])}
        ts = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
        target = f.get("url") or f"{f.get('category')} start={f.get('start')}"
        out.append((ts, process, f.get("kind", "?"), int(f.get("wait_ms", "0")), target))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--min-gap-ms", type=int, default=3000)
    ap.add_argument("--out", type=Path, default=Path("data/ratelimit_verify.txt"))
    args = ap.parse_args()

    rows = []
    for log in args.logs:
        rows.extend(requests(log, re.sub(r"^verify\d*_", "", log.stem)))
    rows.sort()
    if len(rows) < 2:
        print("fewer than two requests found", file=sys.stderr)
        return 2

    lines = ["timestamp                    process  gap_ms  wait_ms  kind  target"]
    gaps = []
    prev = None
    for ts, process, kind, wait_ms, target in rows:
        gap = int((ts - prev).total_seconds() * 1000) if prev else 0
        if prev:
            gaps.append(gap)
        lines.append(f"{ts.isoformat()}  {process:8} {gap:6}  {wait_ms:7}  {kind:5} {target}")
        prev = ts

    per_process = {p: sum(1 for r in rows if r[1] == p) for p in sorted({r[1] for r in rows})}
    violations = [g for g in gaps if g < args.min_gap_ms]
    summary = (
        f"requests={len(rows)} {per_process} span_s={(rows[-1][0] - rows[0][0]).total_seconds():.1f} "
        f"min_gap_ms={min(gaps)} median_gap_ms={statistics.median(gaps):.0f} max_gap_ms={max(gaps)} "
        f"gaps_below_{args.min_gap_ms}ms={len(violations)}"
    )
    lines += ["", summary]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n")
    print(summary)
    print(f"timeline written to {args.out}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
