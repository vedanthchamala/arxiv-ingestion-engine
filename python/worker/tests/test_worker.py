import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from arxiv_common import schema
from arxiv_common.models import Paper
from arxiv_worker.main import Poison, Worker


class FakeMessage:
    def __init__(self, value: bytes | None, key: bytes | None = b"2609.01234"):
        self._value = value
        self._key = key

    def value(self):
        return self._value

    def key(self):
        return self._key


def sample_paper_json() -> bytes:
    now = datetime.now(UTC)
    return schema.dumps(
        "paper",
        Paper(
            arxiv_id="2609.01234",
            version=1,
            title="T",
            abstract="A",
            authors=["X Y"],
            primary_category="cs.LG",
            categories=["cs.LG"],
            published_at=now,
            updated_at=now,
            pdf_url="https://arxiv.org/pdf/2609.01234v1",
            polled_at=now,
        ),
    )


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def worker():
    with (
        patch("arxiv_worker.main.Consumer"),
        patch("arxiv_worker.main.Producer") as producer,
        patch("arxiv_worker.main.InferenceClient"),
        patch("arxiv_worker.main.db") as dbm,
    ):
        dbm.has_full_text.return_value = False
        dbm.upsert_paper.return_value = True
        w = Worker.__new__(Worker)
        from arxiv_common.config import Settings

        w.settings = Settings()
        w.topic = "papers.new"
        w.group = "test"
        w.summary_enabled = False
        w.producer = producer.return_value
        w.inference = MagicMock()
        w.conn = MagicMock()
        w.stop = False
        w.processed = 0
        w.failed = 0
        yield w


def test_parse_wraps_paper_into_abstract_chunk(worker):
    doc = worker.parse(FakeMessage(sample_paper_json()))
    assert doc.source == "abstract"
    assert len(doc.chunks) == 1
    assert doc.chunks[0].text.startswith("T")


def test_parse_rejects_bad_json_and_schema(worker):
    with pytest.raises(Poison):
        worker.parse(FakeMessage(b"{not json"))
    with pytest.raises(Poison):
        worker.parse(FakeMessage(json.dumps({"schema_version": 1, "arxiv_id": "x"}).encode()))


def test_poison_goes_to_dlq_without_retry(worker):
    poison_before = sample("worker_dead_letters_total", group="test", reason="poison")
    worker.handle(FakeMessage(b"{not json", key=b"bad"))
    assert worker.failed == 1
    worker.producer.produce.assert_called_once()
    _, kwargs = worker.producer.produce.call_args
    failed = schema.loads("failed", kwargs["value"])
    assert failed["stage"] == "worker" and failed["attempts"] == 1 and failed["arxiv_id"] == "bad"
    assert sample("worker_dead_letters_total", group="test", reason="poison") == poison_before + 1
    assert sample("worker_attempts_total", group="test") == 0


def test_success_path_calls_embed_and_upsert(worker):
    stored_before = sample("worker_messages_total", group="test", result="stored")
    chunks_before = sample("worker_chunks_total", group="test")
    worker.inference.embed.return_value = [[0.1] * 1024]
    with patch("arxiv_worker.main.db.upsert_paper") as upsert:
        worker.handle(FakeMessage(sample_paper_json()))
    assert worker.processed == 1
    worker.inference.embed.assert_called_once()
    upsert.assert_called_once()
    doc = upsert.call_args.args[1]
    assert doc.paper.arxiv_id == "2609.01234"
    assert sample("worker_messages_total", group="test", result="stored") == stored_before + 1
    assert sample("worker_chunks_total", group="test") == chunks_before + 1
    assert sample("worker_process_seconds_count", group="test") >= 1


def test_persistent_failure_dead_letters_after_retries(worker):
    from arxiv_common.inference import InferenceError

    attempts_before = sample("worker_attempts_total", group="test")
    dlq_before = sample("worker_dead_letters_total", group="test", reason="retries_exhausted")
    worker.inference.embed.side_effect = InferenceError("embeddings 400: bad")
    with patch("arxiv_worker.main.time.sleep"):
        worker.handle(FakeMessage(sample_paper_json()))
    assert worker.processed == 0 and worker.failed == 1
    assert worker.inference.embed.call_count == 3
    assert sample("worker_attempts_total", group="test") == attempts_before + 3
    assert sample("worker_dead_letters_total", group="test", reason="retries_exhausted") == dlq_before + 1


def test_abstract_message_never_downgrades_full_text(worker):
    with (
        patch("arxiv_worker.main.db.has_full_text", return_value=True) as has_full,
        patch("arxiv_worker.main.db.upsert_paper") as upsert,
    ):
        worker.handle(FakeMessage(sample_paper_json()))
    has_full.assert_called_once_with(worker.conn, "2609.01234", 1)
    worker.inference.embed.assert_not_called()
    upsert.assert_not_called()
    assert worker.processed == 1 and worker.failed == 0


def test_guarded_upsert_result_is_reported_as_skipped(worker):
    worker.inference.embed.return_value = [[0.1] * 1024]
    with (
        patch("arxiv_worker.main.db.has_full_text", return_value=False),
        patch("arxiv_worker.main.db.upsert_paper", return_value=False) as upsert,
    ):
        worker.handle(FakeMessage(sample_paper_json()))
    upsert.assert_called_once()
    assert worker.processed == 1 and worker.failed == 0
