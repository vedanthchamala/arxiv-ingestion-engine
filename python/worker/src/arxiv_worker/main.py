import argparse
import json
import signal
import time
from datetime import UTC, datetime
from typing import Literal

import psycopg
import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException, Message, Producer

from arxiv_common import db, metrics, schema
from arxiv_common.config import Settings
from arxiv_common.inference import InferenceClient, InferenceError
from arxiv_common.logging import configure
from arxiv_common.models import ChunkedPaper, Failed, Paper

log = structlog.get_logger()

MAX_ATTEMPTS = 3
BACKOFF_S = (2, 8, 20)

DeadLetterReason = Literal["poison", "retries_exhausted", "db_unavailable"]


class Poison(Exception):
    """Message can never succeed (bad JSON, schema violation): dead-letter without retrying."""


class Worker:
    def __init__(self, settings: Settings, topic: str, group: str, summary: bool) -> None:
        self.settings = settings
        self.topic = topic
        self.group = group
        self.summary_enabled = summary
        self.consumer = Consumer(
            {
                "bootstrap.servers": settings.kafka_brokers,
                "group.id": group,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                "max.poll.interval.ms": 1_800_000,
                "session.timeout.ms": 45_000,
                "partition.assignment.strategy": "cooperative-sticky",
            }
        )
        self.producer = Producer(
            {
                "bootstrap.servers": settings.kafka_brokers,
                "acks": "all",
                "enable.idempotence": True,
                "compression.type": "gzip",
                "message.timeout.ms": 30_000,
            }
        )
        self.inference = InferenceClient(settings, timeout_s=120)
        self.conn = db.connect(settings.database_url)
        self.stop = False
        self.processed = 0
        self.failed = 0
        metrics.init_worker_labels(group)

    # -- message handling -------------------------------------------------------------------

    def parse(self, msg: Message) -> ChunkedPaper:
        try:
            obj = json.loads(msg.value())
        except (TypeError, ValueError) as e:
            raise Poison(f"invalid json: {e}") from e
        try:
            if isinstance(obj, dict) and "chunks" in obj:
                schema.validate("chunked", obj)
                return ChunkedPaper.model_validate(obj)
            schema.validate("paper", obj)
            return ChunkedPaper.from_abstract(Paper.model_validate(obj), datetime.now(UTC))
        except schema.SchemaError as e:
            raise Poison(str(e)) from e

    def process(self, doc: ChunkedPaper) -> bool:
        if doc.source == "abstract" and db.has_full_text(self.conn, doc.paper.arxiv_id, doc.paper.version):
            return False
        with metrics.WORKER_EMBED_SECONDS.labels(self.group).time():
            embeddings = self.inference.embed([c.text for c in doc.chunks])
        summary = None
        model = None
        if self.summary_enabled:
            intro = next((c.text for c in doc.chunks if c.section and "intro" in c.section.lower()), None)
            with metrics.WORKER_SUMMARIZE_SECONDS.labels(self.group).time():
                summary = self.inference.summarize(doc.paper.title, doc.paper.abstract, intro)
            model = self.settings.llm_model
        if not db.upsert_paper(self.conn, doc, embeddings, summary, model):
            return False
        metrics.WORKER_CHUNKS.labels(self.group).inc(len(doc.chunks))
        return True

    def handle(self, msg: Message) -> None:
        with metrics.WORKER_PROCESS_SECONDS.labels(self.group).time():
            self._handle(msg)

    def _handle(self, msg: Message) -> None:
        key = (msg.key() or b"?").decode(errors="replace")
        started = time.monotonic()
        try:
            doc = self.parse(msg)
        except Poison as e:
            self.dead_letter(key, msg, str(e), attempts=1, reason="poison")
            return
        blog = log.bind(arxiv_id=doc.paper.versioned_id, chunks=len(doc.chunks), source=doc.source)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            metrics.WORKER_ATTEMPTS.labels(self.group).inc()
            try:
                outcome = "stored" if self.process(doc) else "skipped"
                self.processed += 1
                metrics.WORKER_MESSAGES.labels(self.group, outcome).inc()
                blog.info(outcome, attempt=attempt, ms=int((time.monotonic() - started) * 1000))
                return
            except psycopg.OperationalError as e:
                blog.warning("db connection lost; reconnecting", error=str(e), attempt=attempt)
                self.reconnect_db()
            except (InferenceError, psycopg.Error, OSError, ValueError) as e:
                blog.warning("processing failed", error=str(e)[:300], attempt=attempt)
                last = str(e)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_S[attempt - 1])
                    continue
                self.dead_letter(key, msg, last, attempts=attempt, reason="retries_exhausted")
                return
        self.dead_letter(key, msg, "database unavailable", attempts=MAX_ATTEMPTS, reason="db_unavailable")

    def dead_letter(
        self, key: str, msg: Message, error: str, attempts: int, reason: DeadLetterReason
    ) -> None:
        self.failed += 1
        metrics.WORKER_MESSAGES.labels(self.group, "failed").inc()
        metrics.WORKER_DEAD_LETTERS.labels(self.group, reason).inc()
        try:
            payload = json.loads(msg.value())
            if not isinstance(payload, dict):
                payload = {"raw": payload}
        except (TypeError, ValueError):
            payload = {"raw": msg.value().decode(errors="replace") if msg.value() else None}
        failed = Failed(
            arxiv_id=key,
            stage="worker",
            error=error[:2000],
            attempts=attempts,
            failed_at=datetime.now(UTC),
            payload=payload,
        )
        self.producer.produce(
            self.settings.topic_failed, key=key.encode(), value=schema.dumps("failed", failed)
        )
        self.producer.flush(10)
        log.error("dead-lettered", arxiv_id=key, error=error[:300], attempts=attempts, reason=reason)

    def reconnect_db(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        for delay in (1, 3, 10):
            try:
                self.conn = db.connect(self.settings.database_url)
                return
            except psycopg.OperationalError:
                time.sleep(delay)
        self.conn = db.connect(self.settings.database_url)

    # -- loop -------------------------------------------------------------------------------

    def run(self, max_messages: int | None = None) -> None:
        self.consumer.subscribe([self.topic])
        log.info(
            "worker started",
            topic=self.topic,
            group=self.group,
            summary=self.summary_enabled,
            embedding=self.settings.embedding_url,
            llm=self.settings.llm_url,
        )
        last_report = time.monotonic()
        try:
            while not self.stop:
                msg = self.consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    err = msg.error()
                    if err.fatal():
                        raise KafkaException(err)
                    if err.code() != KafkaError._PARTITION_EOF:
                        log.warning("consumer error; continuing", error=str(err)[:200])
                    continue
                self.handle(msg)
                try:
                    self.consumer.commit(message=msg, asynchronous=False)
                except KafkaException as e:
                    log.warning("commit failed; message will be redelivered", error=str(e)[:200])
                if max_messages and self.processed + self.failed >= max_messages:
                    break
                if time.monotonic() - last_report > 30:
                    log.info("progress", processed=self.processed, failed=self.failed)
                    last_report = time.monotonic()
        finally:
            log.info("worker stopping", processed=self.processed, failed=self.failed)
            self.consumer.close()
            self.producer.flush(10)
            self.inference.close()
            self.conn.close()


def main() -> None:
    settings = Settings()
    ap = argparse.ArgumentParser(description="Embed, summarize and store papers from a Kafka topic.")
    ap.add_argument("--topic", default=settings.topic_chunked, help="papers.chunked (default) or papers.new")
    ap.add_argument("--group", default="worker")
    ap.add_argument("--max-messages", type=int, default=None, help="exit after N messages (testing)")
    ap.add_argument("--no-summary", action="store_true", help="skip the LLM summary step")
    ap.add_argument(
        "--metrics-port", type=int, default=settings.metrics_port, help="Prometheus /metrics port; 0 disables"
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    configure(args.log_level)
    metrics.start_metrics_server(args.metrics_port)

    summary = settings.summary_enabled and not args.no_summary
    worker = Worker(settings, args.topic, args.group, summary=summary)

    def _stop(signum, _frame):
        log.info("signal received; finishing current message", signal=signum)
        worker.stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    worker.run(max_messages=args.max_messages)
