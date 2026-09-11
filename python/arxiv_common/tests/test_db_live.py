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
