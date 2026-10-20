"""The "High Level Design" page of a paper, and the same design as JSON."""

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from arxiv_common import db

from . import design
from .figures import overview_figure
from .service import settings, state
from .ui import render
from .watches import same_origin

router = APIRouter(include_in_schema=False)

PROVIDER_LABELS = {"local": "Local model", "claude": "Claude"}


def pick_provider(requested: str | None) -> str:
    available = design.providers(settings)
    if requested in available:
        return requested
    return settings.design_provider if settings.design_provider in available else "local"


def load(arxiv_id: str, provider: str) -> tuple[dict | None, dict | None, dict | None, list[dict]]:
    with state.pool.connection() as conn:
        paper = db.get_paper(conn, arxiv_id)
        if paper is None:
            return None, None, None, []
        row = db.get_design(conn, arxiv_id, provider)
        figures = db.get_figures(conn, arxiv_id)
        others = db.list_designs(conn, arxiv_id)
    return paper, row, figures, others


@router.get("/paper/{arxiv_id:path}/design", response_class=HTMLResponse)
def design_page(request: Request, arxiv_id: str, model: str | None = None):
    provider = pick_provider(model)
    paper, row, figures, others = load(arxiv_id, provider)
    if paper is None:
        return render(request, "missing.html", status_code=404, arxiv_id=arxiv_id, valid=True)
    if row is None or row["status"] != "failed":
        if design.start(state.pool, settings, paper, provider):
            paper, row, figures, others = load(arxiv_id, provider)
    ctx = {
        "paper": paper,
        "row": row,
        "provider": provider,
        "providers": [(p, PROVIDER_LABELS[p]) for p in design.providers(settings)],
        "done_providers": {o["provider"] for o in others if o["status"] == "done"},
        "model": design.model_name(provider, settings),
    }
    if row is None or row["status"] != "done":
        return render(request, "design_pending.html", **ctx)
    found = figures["figures"] if figures else []
    chosen = row["design"].get("overview_figure") or None
    main = overview_figure(found, chosen)
    return render(
        request,
        "design.html",
        **ctx,
        d=row["design"],
        figure=main,
        more_figures=[f for f in found if main is None or f["n"] != main["n"]],
    )


@router.post("/paper/{arxiv_id:path}/design/regenerate")
def regenerate(request: Request, arxiv_id: str, model: str | None = Query(default=None)):
    same_origin(request)
    provider = pick_provider(model)
    with state.pool.connection() as conn:
        paper = db.get_paper(conn, arxiv_id)
        if paper is not None:
            db.reset_design(conn, arxiv_id, provider)
    if paper is not None:
        design.start(state.pool, settings, paper, provider, retry_failed=True)
    return RedirectResponse(f"/paper/{arxiv_id}/design?model={provider}", status_code=303)


@router.get("/papers/{arxiv_id:path}/design")
def design_json(arxiv_id: str, model: str | None = None):
    provider = pick_provider(model)
    paper, row, figures, _ = load(arxiv_id, provider)
    if paper is None:
        return JSONResponse({"detail": f"{arxiv_id} not ingested"}, status_code=404)
    if row is None:
        design.start(state.pool, settings, paper, provider)
        return JSONResponse({"status": "running", "provider": provider}, status_code=202)
    body = {
        "status": row["status"],
        "provider": provider,
        "model": row["model"],
        "design": row["design"],
        "rejected": row["rejected"],
        "error": row["error"],
        "figures": figures["figures"] if figures else [],
    }
    return JSONResponse(body, status_code=202 if row["status"] == "running" else 200)
