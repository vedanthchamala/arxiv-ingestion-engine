"""Server-rendered web pages over the same search the JSON API serves. Plain GET forms, so every
page is a bookmarkable URL and nothing needs a JavaScript build."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from arxiv_common import db
from arxiv_common.inference import InferenceError
from arxiv_common.text import parse_arxiv_id

from . import trail
from .service import SearchRequest, run_search, settings, similar, state

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

PAGE_SIZE = 30
SEARCH_K = 20
STALE_AFTER = timedelta(days=3)
WINDOWS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}
CATEGORY_NAMES = {
    "cs.AI": "Artificial Intelligence",
    "cs.CL": "Computation & Language",
    "cs.CR": "Cryptography & Security",
    "cs.CV": "Computer Vision",
    "cs.DC": "Distributed Computing",
    "cs.HC": "Human-Computer Interaction",
    "cs.IR": "Information Retrieval",
    "cs.LG": "Machine Learning",
    "cs.NE": "Neural & Evolutionary",
    "cs.RO": "Robotics",
    "cs.SE": "Software Engineering",
    "eess.IV": "Image & Video Processing",
    "eess.SY": "Systems & Control",
    "stat.ML": "Statistics: ML",
}


def fmt_date(d: datetime | None) -> str:
    return f"{d.day} {d:%b %Y}" if d else ""


def passage(text: str, section: str | None) -> str:
    """Chunk text minus the section title the fetcher prepends to each chunk."""
    first, _, rest = text.partition("\n")
    return rest.strip() if section and rest and first.strip() == section.strip() else text.strip()


templates.env.filters["date"] = fmt_date
templates.env.filters["category_name"] = templates.env.globals["category_name"] = lambda code: (
    CATEGORY_NAMES.get(code, code)
)
templates.env.globals["passage"] = passage

_stats: tuple[float, dict] | None = None


def corpus() -> dict:
    """Corpus counts for the footer and the staleness notice, refreshed at most once a minute."""
    global _stats
    if _stats is None or time.monotonic() - _stats[0] > 60:
        with state.pool.connection() as conn:
            row = dict(db.corpus_stats(conn))
        newest = row["newest"]
        row["stale"] = newest is not None and datetime.now(UTC) - newest > STALE_AFTER
        row["age_days"] = (datetime.now(UTC) - newest).days if newest else None
        _stats = (time.monotonic(), row)
    return _stats[1]


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, template, {"corpus": corpus(), **ctx}, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = Query(default="", max_length=2000),
    cat: list[str] | None = Query(default=None),
    within: str = "",
    page: int = Query(default=0, ge=0, le=1000),
    more: bool = False,
):
    q = q.strip()
    cats = [c for c in cat or [] if c]
    ctx = {
        "q": q,
        "selected": cats,
        "within": within if within in WINDOWS else "",
        "categories": settings.categories,
        "windows": [
            ("", "Any time"),
            ("day", "24 h"),
            ("week", "Week"),
            ("month", "Month"),
            ("year", "Year"),
        ],
    }
    if not q:
        with state.pool.connection() as conn:
            papers = db.latest(conn, limit=PAGE_SIZE + 1, offset=page * PAGE_SIZE, categories=cats or None)
        return render(
            request,
            "index.html",
            **ctx,
            papers=papers[:PAGE_SIZE],
            page=page,
            has_next=len(papers) > PAGE_SIZE,
        )

    arxiv_id = parse_arxiv_id(q)
    if arxiv_id:
        return RedirectResponse(f"/paper/{arxiv_id}", status_code=303)
    since = datetime.now(UTC) - WINDOWS[within] if within in WINDOWS else None
    k = 50 if more else SEARCH_K
    try:
        resp = run_search(SearchRequest(query=q, k=k, categories=cats or None, since=since))
    except (httpx.HTTPError, InferenceError) as e:
        return render(
            request,
            "index.html",
            status_code=503,
            **ctx,
            error=f"The embedding server at {settings.embedding_url} did not answer ({type(e).__name__}). "
            "Is Ollama (or vLLM) running?",
        )
    with state.pool.connection() as conn:
        author = db.find_author(conn, q) if len(q.split()) in (2, 3, 4) else None
    return render(request, "index.html", **ctx, resp=resp, k=k, more=more, author=author)


@router.get("/paper/{arxiv_id:path}", response_class=HTMLResponse)
def paper_page(
    request: Request,
    arxiv_id: str,
    via: str | None = Query(default=None, max_length=64),
    kind: str | None = Query(default=None, max_length=32),
    web: int | None = None,
    q: str | None = Query(default=None, max_length=500),
):
    canonical = parse_arxiv_id(arxiv_id)
    if canonical and canonical != arxiv_id:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/paper/{canonical}{query}", status_code=301)
    with state.pool.connection() as conn:
        paper = db.get_paper(conn, arxiv_id)
        sections = db.sections(conn, arxiv_id) if paper else []
        on_trail = db.trail_entry(conn, arxiv_id) if paper else None
    if paper is None:
        return render(
            request, "missing.html", status_code=404, arxiv_id=arxiv_id, valid=canonical is not None
        )
    related = similar(arxiv_id, 8) or []
    beacon = {
        "arxiv_id": arxiv_id,
        "via": parse_arxiv_id(via) if via else None,
        "kind": kind if kind in trail.KINDS else None,
        "q": q.strip() if q and q.strip() else None,
        "web": web,
    }
    return render(
        request,
        "paper.html",
        paper=paper,
        sections=sections,
        related=related,
        on_trail=on_trail,
        beacon=beacon,
        dwell=trail.DWELL_SECONDS,
    )


@router.get("/author", response_class=HTMLResponse)
def author_page(request: Request, name: str = Query(min_length=1, max_length=300)):
    with state.pool.connection() as conn:
        author = db.find_author(conn, name)
        papers = db.author_papers(conn, author["id"]) if author else []
    return render(
        request, "author.html", status_code=200 if author else 404, name=name, author=author, papers=papers
    )
