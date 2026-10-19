import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arxiv_common import db, metrics
from arxiv_common.inference import InferenceClient
from arxiv_common.logging import configure

from . import ui
from .cache import SearchCache
from .service import SearchHit, SearchRequest, SearchResponse, run_search, settings, similar, state

__all__ = ["SearchHit", "SearchRequest", "SearchResponse", "app", "run_search", "state"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure()
    metrics.init_api_labels()
    state.pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
        configure=_register_vector,
        open=True,
    )
    state.inference = InferenceClient(settings, timeout_s=60)
    if settings.cache_enabled:
        state.cache = SearchCache(
            settings.redis_url,
            embed=lambda text: state.inference.embed([text])[0],
            ttl=settings.cache_ttl_secs,
            distance=settings.cache_distance,
        )
    try:
        yield
    finally:
        state.pool.close()
        state.inference.close()


def _register_vector(conn):
    from pgvector.psycopg import register_vector

    register_vector(conn)


app = FastAPI(title="arXiv semantic search", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(ui.router)


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


@app.get("/papers/{arxiv_id:path}/similar", response_model=list[SearchHit])
def paper_similar(arxiv_id: str, k: int = Query(default=10, ge=1, le=50)) -> list[SearchHit]:
    hits = similar(arxiv_id, k)
    if hits is None:
        raise HTTPException(404, f"{arxiv_id} not ingested")
    return hits


@app.get("/papers/{arxiv_id:path}")
def paper(arxiv_id: str) -> dict:
    with state.pool.connection() as conn:
        row = db.get_paper(conn, arxiv_id)
    if row is None:
        raise HTTPException(404, f"{arxiv_id} not ingested")
    return row


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
        "cache": state.cache.stats() if state.cache is not None else None,
    }


@app.delete("/cache")
def clear_cache() -> dict:
    if state.cache is None:
        return {"cleared": False}
    state.cache.clear()
    return {"cleared": True}


@app.get("/health")
def health() -> dict:
    with state.pool.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def run() -> None:
    uvicorn.run(
        "arxiv_query_api.main:app", host="0.0.0.0", port=int(os.environ.get("API_PORT", "8000")), reload=False
    )
