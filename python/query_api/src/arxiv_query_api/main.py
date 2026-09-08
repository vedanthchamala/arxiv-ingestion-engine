import time
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from arxiv_common import db
from arxiv_common.config import Settings
from arxiv_common.inference import InferenceClient
from arxiv_common.logging import configure

settings = Settings()


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=10, ge=1, le=50)
    categories: list[str] | None = None
    since: datetime | None = None


class SearchHit(BaseModel):
    arxiv_id: str
    version: int
    title: str
    authors: list[str]
    primary_category: str
    categories: list[str]
    published_at: datetime
    score: float
    summary: str | None
    abstract: str
    chunk_idx: int
    chunk_section: str | None
    chunk_text: str
    url: str


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    took_ms: int


class State:
    pool: ConnectionPool
    inference: InferenceClient


state = State()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure()
    state.pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row},
        configure=_register_vector,
        open=True,
    )
    state.inference = InferenceClient(settings, timeout_s=60)
    try:
        yield
    finally:
        state.pool.close()
        state.inference.close()


def _register_vector(conn):
    from pgvector.psycopg import register_vector

    register_vector(conn)


app = FastAPI(title="arXiv semantic search", version="0.1.0", lifespan=lifespan)


def run_search(req: SearchRequest) -> SearchResponse:
    t0 = time.monotonic()
    qvec = state.inference.embed([req.query])[0]
    with state.pool.connection() as conn:
        hits = db.search(conn, qvec, k=req.k, categories=req.categories, since=req.since)
    return SearchResponse(
        query=req.query,
        took_ms=int((time.monotonic() - t0) * 1000),
        hits=[
            SearchHit(
                arxiv_id=h.arxiv_id,
                version=h.version,
                title=h.title,
                authors=h.authors,
                primary_category=h.primary_category,
                categories=h.categories,
                published_at=h.published_at,
                score=round(h.score, 4),
                summary=h.summary,
                abstract=h.abstract,
                chunk_idx=h.chunk_idx,
                chunk_section=h.chunk_section,
                chunk_text=h.chunk_text,
                url=f"https://arxiv.org/abs/{h.arxiv_id}v{h.version}",
            )
            for h in hits
        ],
    )


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    return run_search(req)


@app.get("/search", response_model=SearchResponse)
def search_get(
    q: str = Query(min_length=1, max_length=2000),
    k: int = Query(default=10, ge=1, le=50),
    category: list[str] | None = Query(default=None),
    since: datetime | None = None,
) -> SearchResponse:
    return run_search(SearchRequest(query=q, k=k, categories=category, since=since))


@app.get("/papers/{arxiv_id:path}")
def paper(arxiv_id: str) -> dict:
    with state.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT p.*, (SELECT count(*) FROM chunks c WHERE c.arxiv_id = p.arxiv_id) AS chunk_count,
                   COALESCE((SELECT array_agg(a.name ORDER BY pa.position)
                             FROM paper_authors pa JOIN authors a ON a.id = pa.author_id
                             WHERE pa.arxiv_id = p.arxiv_id), '{}') AS authors
            FROM papers p WHERE p.arxiv_id = %s
            """,
            (arxiv_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"{arxiv_id} not ingested")
    return dict(row)


@app.get("/stats")
def stats() -> dict:
    with state.pool.connection() as conn:
        totals = conn.execute(
            "SELECT (SELECT count(*) FROM papers) AS papers, (SELECT count(*) FROM chunks) AS chunks, "
            "(SELECT count(*) FROM authors) AS authors, (SELECT max(published_at) FROM papers) AS newest, "
            "(SELECT count(*) FROM papers WHERE summary IS NOT NULL) AS summarized"
        ).fetchone()
        by_cat = conn.execute(
            "SELECT primary_category, count(*) AS n FROM papers GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        ).fetchall()
        by_source = conn.execute("SELECT source, count(*) AS n FROM papers GROUP BY 1").fetchall()
    return {
        "totals": dict(totals),
        "by_primary_category": [dict(r) for r in by_cat],
        "by_source": [dict(r) for r in by_source],
    }


@app.get("/health")
def health() -> dict:
    with state.pool.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True}


def run() -> None:
    uvicorn.run("arxiv_query_api.main:app", host="0.0.0.0", port=8000, reload=False)
