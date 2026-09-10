"""Postgres + pgvector access. One transaction per paper; every write is an upsert."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .models import ChunkedPaper
from .text import author_norm


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, row_factory=dict_row)
    register_vector(conn)
    return conn


def has_full_text(conn: psycopg.Connection, arxiv_id: str, version: int) -> bool:
    """True when the stored row already carries full text for this version or a newer one."""
    row = conn.execute(
        "SELECT 1 FROM papers WHERE arxiv_id = %s AND source IN ('html', 'pdf') AND version >= %s",
        (arxiv_id, version),
    ).fetchone()
    return row is not None


def upsert_paper(
    conn: psycopg.Connection,
    doc: ChunkedPaper,
    embeddings: Sequence[Sequence[float]],
    summary: str | None,
    summary_model: str | None,
) -> bool:
    """Idempotent: re-running for the same arxiv_id (any version) converges to the same rows.

    Returns False, writing nothing, when an abstract-only document meets a row that already has
    full text for the same or a newer version: the fast path never downgrades the slow path's work,
    whatever order the two consumers happen to run in."""
    if len(embeddings) != len(doc.chunks):
        raise ValueError(f"{len(embeddings)} embeddings for {len(doc.chunks)} chunks")
    p = doc.paper
    now = datetime.now(UTC)
    with conn.transaction():
        written = conn.execute(
            """
            INSERT INTO papers (arxiv_id, version, title, abstract, primary_category, categories,
                                published_at, updated_at, pdf_url, html_url, doi, journal_ref, comment,
                                source, summary, summary_model, status, processed_at)
            VALUES (%(arxiv_id)s, %(version)s, %(title)s, %(abstract)s, %(primary_category)s, %(categories)s,
                    %(published_at)s, %(updated_at)s, %(pdf_url)s, %(html_url)s, %(doi)s, %(journal_ref)s,
                    %(comment)s, %(source)s, %(summary)s, %(summary_model)s, 'processed', %(now)s)
            ON CONFLICT (arxiv_id) DO UPDATE SET
                version = EXCLUDED.version, title = EXCLUDED.title, abstract = EXCLUDED.abstract,
                primary_category = EXCLUDED.primary_category, categories = EXCLUDED.categories,
                published_at = EXCLUDED.published_at, updated_at = EXCLUDED.updated_at,
                pdf_url = EXCLUDED.pdf_url, html_url = EXCLUDED.html_url, doi = EXCLUDED.doi,
                journal_ref = EXCLUDED.journal_ref, comment = EXCLUDED.comment, source = EXCLUDED.source,
                summary = COALESCE(EXCLUDED.summary, papers.summary),
                summary_model = COALESCE(EXCLUDED.summary_model, papers.summary_model),
                status = 'processed', processed_at = EXCLUDED.processed_at
            WHERE NOT (EXCLUDED.source = 'abstract'
                       AND papers.source IN ('html', 'pdf')
                       AND papers.version >= EXCLUDED.version)
            RETURNING arxiv_id
            """,
            {
                "arxiv_id": p.arxiv_id,
                "version": p.version,
                "title": p.title,
                "abstract": p.abstract,
                "primary_category": p.primary_category,
                "categories": p.categories,
                "published_at": p.published_at,
                "updated_at": p.updated_at,
                "pdf_url": p.pdf_url,
                "html_url": p.html_url,
                "doi": p.doi,
                "journal_ref": p.journal_ref,
                "comment": p.comment,
                "source": doc.source,
                "summary": summary,
                "summary_model": summary_model,
                "now": now,
            },
        ).fetchone()
        if written is None:
            return False

        conn.execute("DELETE FROM paper_authors WHERE arxiv_id = %s", (p.arxiv_id,))
        for position, name in enumerate(p.authors):
            row = conn.execute(
                """
                INSERT INTO authors (name, name_norm) VALUES (%s, %s)
                ON CONFLICT (name_norm) DO UPDATE SET name = authors.name
                RETURNING id
                """,
                (name, author_norm(name)),
            ).fetchone()
            conn.execute(
                "INSERT INTO paper_authors (arxiv_id, author_id, position) VALUES (%s, %s, %s) "
                "ON CONFLICT (arxiv_id, author_id) DO NOTHING",
                (p.arxiv_id, row["id"], position),
            )

        conn.execute("DELETE FROM chunks WHERE arxiv_id = %s", (p.arxiv_id,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (id, arxiv_id, idx, section, text, token_count, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (f"{p.arxiv_id}:{c.idx}", p.arxiv_id, c.idx, c.section, c.text, c.token_count, list(e))
                    for c, e in zip(doc.chunks, embeddings, strict=True)
                ],
            )
    return True


@dataclass
class Hit:
    arxiv_id: str
    version: int
    title: str
    abstract: str
    summary: str | None
    primary_category: str
    categories: list[str]
    published_at: datetime
    authors: list[str]
    score: float
    chunk_idx: int
    chunk_section: str | None
    chunk_text: str


def search(
    conn: psycopg.Connection,
    query_vec: Sequence[float],
    k: int = 10,
    categories: Sequence[str] | None = None,
    since: datetime | None = None,
    candidate_chunks: int = 200,
) -> list[Hit]:
    """Nearest chunks, then best chunk per paper, ordered by cosine similarity."""
    rows = conn.execute(
        """
        WITH nearest AS (
            SELECT c.arxiv_id, c.idx, c.section, c.text,
                   1 - (c.embedding <=> %(q)s::vector) AS score
            FROM chunks c
            JOIN papers p ON p.arxiv_id = c.arxiv_id
            WHERE (%(cats)s::text[] IS NULL OR p.categories && %(cats)s::text[])
              AND (%(since)s::timestamptz IS NULL OR p.published_at >= %(since)s::timestamptz)
            ORDER BY c.embedding <=> %(q)s::vector
            LIMIT %(cand)s
        ),
        best AS (
            SELECT DISTINCT ON (arxiv_id) * FROM nearest ORDER BY arxiv_id, score DESC
        )
        SELECT b.arxiv_id, b.idx, b.section, b.text AS chunk_text, b.score,
               p.version, p.title, p.abstract, p.summary, p.primary_category, p.categories, p.published_at,
               COALESCE((SELECT array_agg(a.name ORDER BY pa.position)
                         FROM paper_authors pa JOIN authors a ON a.id = pa.author_id
                         WHERE pa.arxiv_id = p.arxiv_id), '{}') AS authors
        FROM best b JOIN papers p ON p.arxiv_id = b.arxiv_id
        ORDER BY b.score DESC
        LIMIT %(k)s
        """,
        {
            "q": list(query_vec),
            "cats": list(categories) if categories else None,
            "since": since,
            "cand": candidate_chunks,
            "k": k,
        },
    ).fetchall()
    return [
        Hit(
            arxiv_id=r["arxiv_id"],
            version=r["version"],
            title=r["title"],
            abstract=r["abstract"],
            summary=r["summary"],
            primary_category=r["primary_category"],
            categories=list(r["categories"]),
            published_at=r["published_at"],
            authors=list(r["authors"]),
            score=float(r["score"]),
            chunk_idx=r["idx"],
            chunk_section=r["section"],
            chunk_text=r["chunk_text"],
        )
        for r in rows
    ]
