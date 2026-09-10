#!/usr/bin/env python3
"""Backfill papers.new from arXiv's OAI-PMH interface (the sanctioned bulk path).

    uv run --project python python scripts/backfill.py --from 2026-06-01 [--until 2026-06-30] [--dry-run]

Harvests set `cs` with metadataPrefix `arXiv`, keeps records whose categories intersect
ARXIV_CATEGORIES, emits schema-valid PaperV1 messages keyed by arxiv_id, and marks them in the
Redis seen-set. OAI-PMH records carry no version number, so backfilled papers are version 1 with
unversioned URLs; the poller re-emits them as vN if a newer version is announced later.
Draws on the arXiv request budget shared with the poller and fetcher (schemas/ratelimit.lua in
Redis), so it may run alongside them; --local-ratelimit keeps the 3 s spacing in-process instead.
"""

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import httpx
import redis
from confluent_kafka import Producer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "arxiv_common" / "src"))
from arxiv_common import schema  # noqa: E402
from arxiv_common.config import Settings  # noqa: E402
from arxiv_common.models import Paper  # noqa: E402
from arxiv_common.ratelimit import DEFAULT_KEY, LocalMinInterval, SharedMinInterval  # noqa: E402

OAI = "https://oaipmh.arxiv.org/oai"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "ax": "http://arxiv.org/OAI/arXiv/",
}
MIN_INTERVAL = float(os.environ.get("ARXIV_MIN_REQUEST_INTERVAL_SECS", "3"))


def text(el, path):
    node = el.find(path, NS)
    return " ".join(node.text.split()) if node is not None and node.text else None


def to_paper(record, polled_at: datetime) -> Paper | None:
    meta = record.find("oai:metadata/ax:arXiv", NS)
    if meta is None:
        return None
    arxiv_id = text(meta, "ax:id")
    title, abstract = text(meta, "ax:title"), text(meta, "ax:abstract")
    if not (arxiv_id and title and abstract):
        return None
    cats = (text(meta, "ax:categories") or "").split()
    if not cats:
        return None
    authors = []
    for a in meta.findall("ax:authors/ax:author", NS):
        key, fore = text(a, "ax:keyname"), text(a, "ax:forenames")
        name = f"{fore} {key}" if fore else key
        if name:
            authors.append(name)
    created = text(meta, "ax:created") or polled_at.date().isoformat()
    updated = text(meta, "ax:updated") or created
    return Paper(
        arxiv_id=arxiv_id,
        version=1,
        title=title,
        abstract=abstract,
        authors=authors,
        primary_category=cats[0],
        categories=cats,
        published_at=datetime.fromisoformat(created).replace(tzinfo=UTC),
        updated_at=datetime.fromisoformat(updated).replace(tzinfo=UTC),
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        html_url=f"https://arxiv.org/html/{arxiv_id}",
        doi=text(meta, "ax:doi"),
        journal_ref=text(meta, "ax:journal-ref"),
        comment=text(meta, "ax:comments"),
        polled_at=polled_at,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="from_", required=True, help="YYYY-MM-DD (by record datestamp)")
    ap.add_argument("--until", default=None)
    ap.add_argument("--set", default="cs")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument(
        "--local-ratelimit",
        action="store_true",
        help="space requests in-process instead of via the Redis budget",
    )
    args = ap.parse_args()

    settings = Settings()
    wanted = set(os.environ.get("ARXIV_CATEGORIES", "cs.LG,cs.CV,cs.RO").split(","))
    producer = None
    if not args.dry_run:
        producer = Producer({"bootstrap.servers": settings.kafka_brokers, "acks": "all"})
    r = None if args.dry_run else redis.Redis.from_url(settings.redis_url)
    seen_key = os.environ.get("REDIS_SEEN_KEY", "arxiv:seen")
    ua = os.environ.get("ARXIV_USER_AGENT", "arxiv-ingest/0.1")
    if args.local_ratelimit:
        limiter = LocalMinInterval(MIN_INTERVAL)
    else:
        rl_client = r if r is not None else redis.Redis.from_url(settings.redis_url)
        limiter = SharedMinInterval(
            rl_client, MIN_INTERVAL, os.environ.get("ARXIV_RATELIMIT_KEY", DEFAULT_KEY)
        )

    params = {
        "verb": "ListRecords",
        "metadataPrefix": "arXiv",
        "set": args.set,
        "from": args.from_,
    }
    if args.until:
        params["until"] = args.until
    stats = {"pages": 0, "records": 0, "matched": 0, "produced": 0, "seen": 0}
    with httpx.Client(timeout=120, headers={"User-Agent": ua}) as http:
        while True:
            for attempt in range(1, 5):
                limiter.wait()
                resp = http.get(OAI, params=params)
                if resp.status_code == 503:
                    delay = int(resp.headers.get("Retry-After", "20"))
                    print(f"503 retry-after {delay}s (attempt {attempt})", file=sys.stderr)
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                break
            root = ET.fromstring(resp.text)
            err = root.find("oai:error", NS)
            if err is not None:
                if err.get("code") == "noRecordsMatch":
                    break
                raise SystemExit(f"OAI error {err.get('code')}: {err.text}")
            stats["pages"] += 1
            polled_at = datetime.now(UTC)
            for rec in root.findall("oai:ListRecords/oai:record", NS):
                stats["records"] += 1
                paper = to_paper(rec, polled_at)
                if paper is None or not (set(paper.categories) & wanted):
                    continue
                stats["matched"] += 1
                member = paper.versioned_id
                if r is not None and r.sismember(seen_key, member):
                    stats["seen"] += 1
                    continue
                payload = schema.dumps("paper", paper)
                if producer is not None:
                    producer.produce(settings.topic_new, key=paper.arxiv_id.encode(), value=payload)
                    producer.poll(0)
                    r.sadd(seen_key, member)
                stats["produced"] += 1
            if producer is not None:
                producer.flush(30)
            print(json.dumps(stats), file=sys.stderr)
            token = root.find("oai:ListRecords/oai:resumptionToken", NS)
            if token is None or not (token.text or "").strip():
                break
            if args.max_pages and stats["pages"] >= args.max_pages:
                break
            params = {"verb": "ListRecords", "resumptionToken": token.text.strip()}
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
