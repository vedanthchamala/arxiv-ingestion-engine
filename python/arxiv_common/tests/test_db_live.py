"""Live-database checks; skipped when Postgres is unreachable."""

import os

import psycopg
import pytest

from arxiv_common import db

URL = os.environ.get("DATABASE_URL", "postgresql://arxiv:arxiv@localhost:5432/arxiv")


@pytest.fixture
def conn():
    try:
        c = db.connect(URL)
    except psycopg.OperationalError:
        pytest.skip("postgres not reachable")
    yield c
    c.close()


def test_reads_outside_a_block_leave_no_transaction_open(conn):
    db.has_full_text(conn, "0000.00000", 1)
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_hybrid_search_finds_a_coined_name(conn):
    row = conn.execute(
        "SELECT p.arxiv_id, split_part(p.title, ':', 1) AS name, c.embedding FROM papers p "
        "JOIN chunks c ON c.arxiv_id = p.arxiv_id AND c.idx = 0 "
        "WHERE p.title ~ '^[A-Za-z0-9-]{4,20}: ' ORDER BY p.arxiv_id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("no papers ingested")
    hits = db.search(conn, row["embedding"].to_list(), k=5, text=row["name"], ef_search=200)
    assert row["arxiv_id"] in [h.arxiv_id for h in hits]
    assert all(-1.0 <= h.score <= 1.0 and h.chunk_text for h in hits)
