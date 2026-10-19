"""Postgres + pgvector access. One transaction per paper; every write is an upsert."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.rows import dict_row

from .models import ChunkedPaper
from .text import author_norm, strip_nul


def connect(database_url: str) -> psycopg.Connection:
    """Autocommit on purpose: a bare SELECT must not open an implicit transaction, or the next
    `conn.transaction()` block becomes a savepoint inside it and nothing ever commits. Every write
    below runs in an explicit block."""
    conn = psycopg.connect(
        database_url, row_factory=dict_row, autocommit=True, options="-c lock_timeout=120000"
    )
    register_vector(conn)
    return conn


def has_keyword_index(conn: psycopg.Connection) -> bool:
    """True once sql/002_search.sql has added `papers.tsv` (hybrid search needs it)."""
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'papers' AND column_name = 'tsv'"
    ).fetchone()
    return row is not None


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
                "title": strip_nul(p.title),
                "abstract": strip_nul(p.abstract),
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
                "summary": strip_nul(summary) if summary else summary,
                "summary_model": summary_model,
                "now": now,
            },
        ).fetchone()
        if written is None:
            return False

        conn.execute("DELETE FROM paper_authors WHERE arxiv_id = %s", (p.arxiv_id,))
        # Lock shared author rows in one global order so two writers cannot deadlock on them.
        for position, name in sorted(enumerate(p.authors), key=lambda a: author_norm(a[1])):
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
                    (
                        f"{p.arxiv_id}:{c.idx}",
                        p.arxiv_id,
                        c.idx,
                        strip_nul(c.section) if c.section else c.section,
                        strip_nul(c.text),
                        c.token_count,
                        list(e),
                    )
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
    keyword: bool = False


def search(
    conn: psycopg.Connection,
    query_vec: Sequence[float],
    k: int = 10,
    categories: Sequence[str] | None = None,
    since: datetime | None = None,
    candidate_chunks: int = 200,
    ef_search: int | None = None,
    text: str | None = None,
) -> list[Hit]:
    """Nearest chunks, then best chunk per paper, ordered by cosine similarity.

    With `text`, the dense ranking is fused (reciprocal rank fusion) with a keyword ranking of
    title + abstract, so exact names and acronyms the embedding blurs still surface; `score` stays
    the cosine similarity of the paper's best chunk (chunk 0 for keyword-only hits).

    `ef_search` widens pgvector's HNSW candidate list for this query only (default 40 misses ~6 %
    of exact nearest neighbours at 230K chunks; see BENCHMARKS.md)."""
    with conn.transaction():
        if ef_search:
            conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(int(ef_search))))
        if text:
            rows = _hybrid_rows(conn, query_vec, text, k, categories, since, candidate_chunks)
        else:
            rows = _search_rows(conn, query_vec, k, categories, since, candidate_chunks)
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
            keyword=bool(r.get("keyword")),
        )
        for r in rows
    ]


def _search_rows(conn, query_vec, k, categories, since, candidate_chunks):
    return conn.execute(
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


RRF_K = 60
KEYWORD_CANDIDATES = 50


def _hybrid_rows(conn, query_vec, text, k, categories, since, candidate_chunks):
    return conn.execute(
        f"""
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
        ),
        dense AS (
            SELECT *, row_number() OVER (ORDER BY score DESC) AS r FROM best
        ),
        keyword AS (
            SELECT p.arxiv_id,
                   row_number() OVER (ORDER BY ts_rank_cd(p.tsv, tq, 1) DESC, p.published_at DESC) AS r
            FROM papers p, websearch_to_tsquery('english', %(text)s) tq
            WHERE p.tsv @@ tq
              AND (%(cats)s::text[] IS NULL OR p.categories && %(cats)s::text[])
              AND (%(since)s::timestamptz IS NULL OR p.published_at >= %(since)s::timestamptz)
            ORDER BY r
            LIMIT %(kw)s
        ),
        fused AS (
            SELECT arxiv_id, d.idx, d.section, d.text, d.score, w.r IS NOT NULL AS keyword,
                   COALESCE(1.0 / (%(rrf)s + d.r), 0) + COALESCE(1.0 / (%(rrf)s + w.r), 0) AS rrf
            FROM dense d FULL JOIN keyword w USING (arxiv_id)
            ORDER BY rrf DESC, arxiv_id
            LIMIT %(k)s
        )
        SELECT f.arxiv_id, COALESCE(f.idx, c0.idx) AS idx, COALESCE(f.section, c0.section) AS section,
               COALESCE(f.text, c0.text) AS chunk_text,
               COALESCE(f.score, 1 - (c0.embedding <=> %(q)s::vector)) AS score, f.keyword,
               p.version, p.title, p.abstract, p.summary, p.primary_category, p.categories, p.published_at,
               {_AUTHORS}
        FROM fused f
        JOIN papers p ON p.arxiv_id = f.arxiv_id
        LEFT JOIN chunks c0 ON f.idx IS NULL AND c0.arxiv_id = f.arxiv_id AND c0.idx = 0
        ORDER BY f.rrf DESC, f.arxiv_id
        """,
        {
            "q": list(query_vec),
            "text": text,
            "cats": list(categories) if categories else None,
            "since": since,
            "cand": candidate_chunks,
            "kw": KEYWORD_CANDIDATES,
            "rrf": RRF_K,
            "k": k,
        },
    ).fetchall()


_AUTHORS = """COALESCE((SELECT array_agg(a.name ORDER BY pa.position)
                  FROM paper_authors pa JOIN authors a ON a.id = pa.author_id
                  WHERE pa.arxiv_id = p.arxiv_id), '{}') AS authors"""


def get_paper(conn: psycopg.Connection, arxiv_id: str) -> dict | None:
    return conn.execute(
        f"""
        SELECT p.*, (SELECT count(*) FROM chunks c WHERE c.arxiv_id = p.arxiv_id) AS chunk_count, {_AUTHORS}
        FROM papers p WHERE p.arxiv_id = %s
        """,
        (arxiv_id,),
    ).fetchone()


def latest(
    conn: psycopg.Connection, limit: int = 30, offset: int = 0, categories: Sequence[str] | None = None
) -> list[dict]:
    """Newest papers first (by first-version date), optionally limited to categories."""
    return conn.execute(
        f"""
        SELECT p.arxiv_id, p.version, p.title, p.abstract, p.summary, p.primary_category, p.categories,
               p.published_at, p.source, {_AUTHORS}
        FROM papers p
        WHERE (%(cats)s::text[] IS NULL OR p.categories && %(cats)s::text[])
        ORDER BY p.published_at DESC, p.arxiv_id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"cats": list(categories) if categories else None, "limit": limit, "offset": offset},
    ).fetchall()


def sections(conn: psycopg.Connection, arxiv_id: str) -> list[str]:
    """Section titles of the stored full text, in document order (chunk 0, the abstract, excluded)."""
    rows = conn.execute(
        "SELECT section FROM chunks WHERE arxiv_id = %s AND idx > 0 AND section IS NOT NULL "
        "GROUP BY section ORDER BY min(idx)",
        (arxiv_id,),
    ).fetchall()
    return [r["section"] for r in rows]


def paper_vector(conn: psycopg.Connection, arxiv_id: str) -> list[float] | None:
    """Embedding of the paper's title + abstract (chunk 0 for every source)."""
    row = conn.execute("SELECT embedding FROM chunks WHERE arxiv_id = %s AND idx = 0", (arxiv_id,)).fetchone()
    return None if row is None else row["embedding"].to_list()


def corpus_stats(conn: psycopg.Connection) -> dict:
    return conn.execute(
        "SELECT (SELECT count(*) FROM papers) AS papers, "
        "(SELECT count(*) FROM papers WHERE source IN ('html', 'pdf')) AS full_text, "
        "(SELECT max(published_at) FROM papers) AS newest, "
        "(SELECT max(processed_at) FROM papers) AS last_processed"
    ).fetchone()


def find_author(conn: psycopg.Connection, name: str) -> dict | None:
    """The author whose normalized name equals `name`'s, with their paper count."""
    return conn.execute(
        "SELECT a.id, a.name, count(pa.arxiv_id) AS papers FROM authors a "
        "JOIN paper_authors pa ON pa.author_id = a.id WHERE a.name_norm = %s GROUP BY a.id",
        (author_norm(name),),
    ).fetchone()


def author_papers(conn: psycopg.Connection, author_id: int, limit: int = 100) -> list[dict]:
    return conn.execute(
        f"""
        SELECT p.arxiv_id, p.version, p.title, p.abstract, p.summary, p.primary_category, p.categories,
               p.published_at, p.source, {_AUTHORS}
        FROM paper_authors x JOIN papers p ON p.arxiv_id = x.arxiv_id
        WHERE x.author_id = %s
        ORDER BY p.published_at DESC
        LIMIT %s
        """,
        (author_id, limit),
    ).fetchall()


def create_watch(
    conn: psycopg.Connection,
    name: str,
    query: str,
    categories: Sequence[str] | None,
    embedding: Sequence[float],
) -> int:
    return conn.execute(
        "INSERT INTO watches (name, query, categories, embedding) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, query, list(categories) if categories else None, list(embedding)),
    ).fetchone()["id"]


def list_watches(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM watches ORDER BY created_at").fetchall()


def get_watch(conn: psycopg.Connection, watch_id: int) -> dict | None:
    return conn.execute("SELECT * FROM watches WHERE id = %s", (watch_id,)).fetchone()


def delete_watch(conn: psycopg.Connection, watch_id: int) -> None:
    conn.execute("DELETE FROM watches WHERE id = %s", (watch_id,))


def mark_watch_seen(conn: psycopg.Connection, watch_id: int) -> None:
    conn.execute("UPDATE watches SET seen_at = now() WHERE id = %s", (watch_id,))


def ingested_at(conn: psycopg.Connection, arxiv_ids: Sequence[str]) -> dict[str, datetime]:
    rows = conn.execute(
        "SELECT arxiv_id, ingested_at FROM papers WHERE arxiv_id = ANY(%s)", (list(arxiv_ids),)
    ).fetchall()
    return {r["arxiv_id"]: r["ingested_at"] for r in rows}
