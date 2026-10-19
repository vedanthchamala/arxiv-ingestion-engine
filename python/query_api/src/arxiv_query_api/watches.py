"""Saved searches: a watch keeps a query (and its embedding) and shows the papers that match it in a
recent window, marking the ones ingested since the last visit. Each watch also has an Atom feed."""

from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from arxiv_common import db
from arxiv_common.inference import InferenceError

from .service import settings, state, watch_matches
from .ui import WINDOWS, render, templates

router = APIRouter(include_in_schema=False)

WATCH_WINDOWS = [("week", "Week"), ("month", "Month"), ("year", "Year"), ("", "All")]
FEED_WINDOW = "month"
LIMIT = 50


def same_origin(request: Request) -> None:
    """Refuse cross-site form posts: there is no login, so a foreign page must not edit watches."""
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request")


def since_for(within: str) -> datetime | None:
    return datetime.now(UTC) - WINDOWS[within] if within in WINDOWS else None


def load(watch_id: int) -> dict:
    with state.pool.connection() as conn:
        watch = db.get_watch(conn, watch_id)
    if watch is None:
        raise HTTPException(404, "no such watch")
    return watch


@router.get("/watches", response_class=HTMLResponse)
def watches_page(request: Request, within: str = "week"):
    within = within if within in WINDOWS or within == "" else "week"
    with state.pool.connection() as conn:
        watches = db.list_watches(conn)
    rows = [{"watch": w, "matches": watch_matches(w, since_for(within), k=LIMIT)} for w in watches]
    return render(
        request,
        "watches.html",
        rows=rows,
        within=within,
        windows=WATCH_WINDOWS,
        categories=settings.categories,
        limit=LIMIT,
    )


@router.post("/watches")
def create_watch(
    request: Request,
    query: str = Form(min_length=1, max_length=500),
    name: str = Form(default="", max_length=120),
    cat: list[str] | None = Form(default=None),
):
    same_origin(request)
    query = query.strip()
    try:
        vec = state.inference.embed([query])[0]
    except (httpx.HTTPError, InferenceError):
        raise HTTPException(503, f"embedding server at {settings.embedding_url} did not answer") from None
    with state.pool.connection() as conn:
        watch_id = db.create_watch(
            conn, name.strip() or query, query, [c for c in cat or [] if c] or None, vec
        )
    return RedirectResponse(f"/watches/{watch_id}", status_code=303)


@router.get("/watches/{watch_id}", response_class=HTMLResponse)
def watch_page(request: Request, watch_id: int, within: str = "week"):
    within = within if within in WINDOWS or within == "" else "week"
    watch = load(watch_id)
    matches = watch_matches(watch, since_for(within), k=LIMIT)
    with state.pool.connection() as conn:
        db.mark_watch_seen(conn, watch_id)
    return render(
        request, "watch.html", watch=watch, matches=matches, within=within, windows=WATCH_WINDOWS, limit=LIMIT
    )


@router.post("/watches/{watch_id}/delete")
def delete_watch(request: Request, watch_id: int):
    same_origin(request)
    with state.pool.connection() as conn:
        db.delete_watch(conn, watch_id)
    return RedirectResponse("/watches", status_code=303)


@router.get("/watches/{watch_id}/feed.xml")
def watch_feed(request: Request, watch_id: int, k: int = Query(default=30, ge=1, le=50)):
    watch = load(watch_id)
    hits = sorted(
        (m.hit for m in watch_matches(watch, since_for(FEED_WINDOW), k=k)), key=lambda h: h.published_at
    )
    return templates.TemplateResponse(
        request,
        "feed.xml",
        {
            "watch": watch,
            "hits": hits[::-1],
            "base": str(request.base_url).rstrip("/"),
            "updated": (hits[-1].published_at if hits else watch["created_at"]).isoformat(),
        },
        media_type="application/atom+xml",
    )
