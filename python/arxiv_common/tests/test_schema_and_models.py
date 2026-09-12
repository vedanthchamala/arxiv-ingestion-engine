from datetime import UTC, datetime

import pytest

from arxiv_common import schema
from arxiv_common.models import ChunkedPaper, Paper
from arxiv_common.text import author_norm, l2_normalize, strip_nul


def sample_paper() -> Paper:
    now = datetime.now(UTC)
    return Paper(
        arxiv_id="2609.01234",
        version=2,
        title="A title",
        abstract="An abstract.",
        authors=["Ada Lovelace", "Grace Hopper"],
        primary_category="cs.LG",
        categories=["cs.LG", "cs.CV"],
        published_at=now,
        updated_at=now,
        pdf_url="https://arxiv.org/pdf/2609.01234v2",
        html_url="https://arxiv.org/html/2609.01234v2",
        polled_at=now,
    )


def test_paper_roundtrip_validates():
    raw = schema.dumps("paper", sample_paper())
    back = Paper.model_validate(schema.loads("paper", raw))
    assert back.versioned_id == "2609.01234v2"
    assert back.doi is None


def test_schema_rejects_bad_id():
    p = sample_paper().model_copy(update={"arxiv_id": "nope"})
    with pytest.raises(schema.SchemaError):
        schema.dumps("paper", p)


def test_chunked_from_abstract_validates():
    doc = ChunkedPaper.from_abstract(sample_paper(), datetime.now(UTC))
    raw = schema.dumps("chunked", doc)
    assert schema.loads("chunked", raw)["source"] == "abstract"
    assert doc.chunks[0].text.startswith("A title")


def test_rust_produced_message_shape_is_accepted():
    """Field set must match what rust/common/src/models.rs serializes."""
    msg = {
        "schema_version": 1,
        "arxiv_id": "hep-th/9901001",
        "version": 1,
        "title": "T",
        "abstract": "A",
        "authors": ["X"],
        "primary_category": "hep-th",
        "categories": ["hep-th"],
        "published_at": "1999-01-04T00:00:00Z",
        "updated_at": "1999-01-04T00:00:00Z",
        "pdf_url": "https://arxiv.org/pdf/hep-th/9901001v1",
        "html_url": None,
        "doi": None,
        "journal_ref": None,
        "comment": None,
        "polled_at": "2026-09-07T00:00:00Z",
    }
    schema.validate("paper", msg)
    Paper.model_validate(msg)


def test_author_norm_and_l2():
    assert author_norm("  Grace   Hopper ") == "grace hopper"
    assert author_norm("José Ángel") == "jose angel"
    v = l2_normalize([3.0, 4.0])
    assert abs(v[0] - 0.6) < 1e-9 and abs(v[1] - 0.8) < 1e-9


def test_strip_nul_removes_only_nul_bytes():
    assert strip_nul("a\x00b\tc") == "ab\tc"
