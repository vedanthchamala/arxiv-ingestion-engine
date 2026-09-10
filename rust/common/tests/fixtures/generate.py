"""Writes python_failed.json and python_chunked.json next to this file, using the Python models."""

import json
from datetime import UTC, datetime
from pathlib import Path

from arxiv_common import schema
from arxiv_common.models import ChunkedPaper, Failed, Paper

here = Path(__file__).resolve().parent
now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
paper = Paper(
    arxiv_id="2609.01234",
    version=1,
    title="Python fixture",
    abstract="Produced by the Python models for the Rust cross-language schema test.",
    authors=["Ada Lovelace", "Grace Hopper"],
    primary_category="cs.LG",
    categories=["cs.LG", "cs.RO"],
    published_at=now,
    updated_at=now,
    pdf_url="https://arxiv.org/pdf/2609.01234v1",
    polled_at=now,
)
chunked = ChunkedPaper.from_abstract(paper, now)
failed = Failed(
    arxiv_id=paper.arxiv_id,
    stage="worker",
    error="embeddings 400: bad request",
    attempts=3,
    failed_at=now,
    payload=json.loads(schema.dumps("paper", paper)),
)
(here / "python_chunked.json").write_bytes(schema.dumps("chunked", chunked) + b"\n")
(here / "python_failed.json").write_bytes(schema.dumps("failed", failed) + b"\n")
print("wrote", here / "python_chunked.json", here / "python_failed.json")
