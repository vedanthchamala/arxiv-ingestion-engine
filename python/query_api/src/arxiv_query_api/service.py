"""Search shared by the JSON API and the web pages: request/response models, process state, and the
cached vector search."""

import time
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from arxiv_common import db, metrics
from arxiv_common.config import Settings
from arxiv_common.inference import InferenceClient

from .cache import SearchCache, scope_key

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
    cached: bool = False


class State:
    pool: ConnectionPool
    inference: InferenceClient
    cache: SearchCache | None = None
    hybrid: bool = True


state = State()


def to_hit(h: db.Hit) -> SearchHit:
    return SearchHit(
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


def run_search(req: SearchRequest) -> SearchResponse:
    t0 = time.monotonic()
    with metrics.API_EMBED_SECONDS.time():
        qvec = state.inference.embed([req.query])[0]
    scope = scope_key(req.k, req.categories, req.since, req.query)
    if state.cache is not None:
        cached = state.cache.get(qvec, scope)
        metrics.API_CACHE_EVENTS.labels("hit" if cached is not None else "miss").inc()
        if cached is not None:
            resp = SearchResponse.model_validate(cached)
            resp.query = req.query
            resp.cached = True
            return _finish(resp, t0)
    with state.pool.connection() as conn, metrics.API_DB_SEARCH_SECONDS.time():
        hits = db.search(
            conn,
            qvec,
            k=req.k,
            categories=req.categories,
            since=req.since,
            ef_search=settings.hnsw_ef_search,
            text=req.query if state.hybrid else None,
        )
    resp = SearchResponse(
        query=req.query, took_ms=int((time.monotonic() - t0) * 1000), hits=[to_hit(h) for h in hits]
    )
    if state.cache is not None:
        state.cache.put(req.query, qvec, scope, resp.model_dump(mode="json"))
    return _finish(resp, t0)


def similar(arxiv_id: str, k: int) -> list[SearchHit] | None:
    """Papers nearest to this paper's title + abstract (chunk 0); None when it is not ingested."""
    with state.pool.connection() as conn:
        vec = db.paper_vector(conn, arxiv_id)
        if vec is None:
            return None
        hits = db.search(conn, vec, k=k + 1, ef_search=settings.hnsw_ef_search)
    return [to_hit(h) for h in hits if h.arxiv_id != arxiv_id][:k]


def _finish(resp: SearchResponse, t0: float) -> SearchResponse:
    took = time.monotonic() - t0
    resp.took_ms = int(took * 1000)
    cached = "true" if resp.cached else "false"
    metrics.API_SEARCH_SECONDS.labels(cached).observe(took)
    metrics.API_SEARCH.labels(cached).inc()
    return resp


@dataclass
class WatchMatch:
    hit: SearchHit
    new: bool


def watch_matches(watch: dict, since: datetime | None, k: int = 50) -> list[WatchMatch]:
    """Papers since `since` that match a saved search: a keyword match, or a best chunk at least
    WATCH_MIN_SCORE similar (a plain top-k would always return k papers, even on a quiet day).
    New ones (ingested after the last visit) first, relevance order within each group."""
    with state.pool.connection() as conn:
        hits = db.search(
            conn,
            watch["embedding"].to_list(),
            k=k,
            categories=watch["categories"],
            since=since,
            ef_search=settings.hnsw_ef_search,
            text=watch["query"] if state.hybrid else None,
        )
        hits = [h for h in hits if h.keyword or h.score >= settings.watch_min_score]
        ingested = db.ingested_at(conn, [h.arxiv_id for h in hits]) if hits else {}
    matches = [
        WatchMatch(to_hit(h), ingested.get(h.arxiv_id, watch["seen_at"]) > watch["seen_at"]) for h in hits
    ]
    return sorted(matches, key=lambda m: not m.new)
