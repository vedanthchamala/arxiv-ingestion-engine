#!/usr/bin/env python3
"""Re-queue dead-lettered papers from papers.failed onto the topic they failed on.

    uv run --project python python scripts/replay_dlq.py              # list what would be replayed
    uv run --project python python scripts/replay_dlq.py --apply      # produce them
    uv run --project python python scripts/replay_dlq.py --apply --id 2604.14501

Reads the whole DLQ (it is small) without a consumer group, keeps the latest failure per paper, and
skips papers the database has since stored properly. A `chunked` payload goes back to papers.chunked
(the full-text worker re-embeds it, no arXiv traffic); a `paper` payload goes back to papers.new, where
the fetcher downloads it again through the shared arXiv budget. Payloads that were never valid JSON
cannot be replayed and are only listed.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaError, Producer, TopicPartition

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "arxiv_common" / "src"))
from arxiv_common import db, schema  # noqa: E402
from arxiv_common.config import Settings  # noqa: E402


def read_all(settings: Settings) -> list[dict]:
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_brokers,
            "group.id": f"replay-dlq-{uuid.uuid4()}",
            "enable.auto.commit": False,
            "enable.partition.eof": True,
        }
    )
    meta = consumer.list_topics(settings.topic_failed, timeout=10).topics[settings.topic_failed]
    if meta.error is not None:
        raise SystemExit(f"{settings.topic_failed}: {meta.error}")
    parts = [TopicPartition(settings.topic_failed, p, OFFSET_BEGINNING) for p in meta.partitions]
    consumer.assign(parts)
    pending = {p.partition for p in parts}
    records = []
    while pending:
        msg = consumer.poll(5.0)
        if msg is None:
            break
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                pending.discard(msg.partition())
                continue
            raise SystemExit(f"consumer error: {msg.error()}")
        try:
            obj = json.loads(msg.value())
            schema.validate("failed", obj)
        except (ValueError, schema.SchemaError) as e:
            print(f"skipping unreadable DLQ record at {msg.partition()}:{msg.offset()}: {e}", file=sys.stderr)
            continue
        records.append(obj)
    consumer.close()
    return records


def plan(records: list[dict], conn, settings: Settings) -> list[dict]:
    latest: dict[str, dict] = {}
    for r in records:
        if r["arxiv_id"] not in latest or r["failed_at"] > latest[r["arxiv_id"]]["failed_at"]:
            latest[r["arxiv_id"]] = r
    out = []
    for _, r in sorted(latest.items()):
        payload = r["payload"]
        if "chunks" in payload:
            kind, topic, version = (
                "chunked",
                settings.topic_chunked,
                payload.get("paper", {}).get("version", 1),
            )
        elif "title" in payload:
            kind, topic, version = "paper", settings.topic_new, payload.get("version", 1)
        else:
            out.append({**r, "action": "unreplayable", "topic": None})
            continue
        try:
            schema.validate(kind, payload)
        except schema.SchemaError as e:
            out.append({**r, "action": f"invalid payload ({e})", "topic": None})
            continue
        stored = conn.execute(
            "SELECT source, version FROM papers WHERE arxiv_id = %s", (payload_id(payload),)
        ).fetchone()
        abstract_ok = kind == "paper" and r["stage"] == "worker"
        if stored and stored["version"] >= version and (stored["source"] != "abstract" or abstract_ok):
            out.append({**r, "action": f"resolved (stored, source={stored['source']})", "topic": None})
            continue
        out.append({**r, "action": "replay", "topic": topic, "kind": kind})
    return out


def payload_id(payload: dict) -> str:
    return payload["paper"]["arxiv_id"] if "chunks" in payload else payload["arxiv_id"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="produce the replays (default: only list them)")
    ap.add_argument("--id", action="append", help="only these arXiv ids (repeatable)")
    ap.add_argument("--stage", choices=["fetcher", "worker"], help="only failures from this stage")
    args = ap.parse_args()

    settings = Settings()
    records = read_all(settings)
    if args.id:
        records = [r for r in records if r["arxiv_id"] in set(args.id)]
    if args.stage:
        records = [r for r in records if r["stage"] == args.stage]
    conn = db.connect(settings.database_url)
    rows = plan(records, conn, settings)
    conn.close()

    for r in rows:
        target = f" -> {r['topic']}" if r["topic"] else ""
        print(f"{r['arxiv_id']:<16} {r['stage']:<8} {r['action']}{target}   [{r['error'][:70]}]")
    todo = [r for r in rows if r["action"] == "replay"]
    print(f"\n{len(records)} DLQ records, {len(rows)} papers, {len(todo)} to replay", file=sys.stderr)
    if not todo:
        return
    if not args.apply:
        print("dry run: add --apply to produce them", file=sys.stderr)
        return
    if any(r["topic"] == settings.topic_new for r in todo):
        print(
            f"note: replays on {settings.topic_new} are fetched from arXiv again (shared budget)",
            file=sys.stderr,
        )
    producer = Producer(
        {"bootstrap.servers": settings.kafka_brokers, "acks": "all", "enable.idempotence": True}
    )
    for r in todo:
        producer.produce(
            r["topic"], key=payload_id(r["payload"]).encode(), value=json.dumps(r["payload"]).encode()
        )
    producer.flush(30)
    print(f"replayed {len(todo)}", file=sys.stderr)


if __name__ == "__main__":
    main()
