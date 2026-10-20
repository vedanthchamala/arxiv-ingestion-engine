"""Reading-trail endpoints: the dwell beacon from paper pages, the manual "Add to path" button, and the
page that lists each web's path with rename / move / remove."""

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from arxiv_common import db
from arxiv_common.text import parse_arxiv_id

from . import citations, recommend, trail
from .service import settings, state
from .ui import render
from .watches import same_origin

router = APIRouter(include_in_schema=False)


class ReadEvent(BaseModel):
    arxiv_id: str = Field(max_length=64)
    via: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=32)
    q: str | None = Field(default=None, max_length=500)
    web: int | None = None
    seconds: int = 0


def _record(arxiv_id: str, via: str | None, kind: str | None, q: str | None, web: int | None) -> trail.Read:
    canonical = parse_arxiv_id(arxiv_id)
    via = parse_arxiv_id(via) if via else None
    with state.pool.connection() as conn:
        paper = db.get_paper(conn, canonical) if canonical else None
        if paper is None:
            raise HTTPException(404, f"{arxiv_id} not ingested")
        r = trail.record_read(conn, paper, via=via, kind=kind, query=q, web_hint=web)
    if not r.already:
        citations.schedule(state.pool, settings, [r.arxiv_id])
    return r


@router.post("/trail/read")
def read(request: Request, event: ReadEvent) -> dict:
    """Sent by a paper page after DWELL_SECONDS of visible time."""
    same_origin(request)
    r = _record(event.arxiv_id, event.via, event.kind, event.q, event.web)
    return {
        "arxiv_id": r.arxiv_id,
        "web_id": r.web_id,
        "web_name": r.web_name,
        "new_web": r.new_web,
        "already": r.already,
    }


@router.post("/trail/add")
def add(request: Request, arxiv_id: str = Form(max_length=64)):
    same_origin(request)
    r = _record(arxiv_id, None, "manual", None, None)
    return RedirectResponse(f"/paper/{r.arxiv_id}", status_code=303)


@router.post("/trail/{arxiv_id:path}/remove")
def remove(request: Request, arxiv_id: str, next: str = Form(default="/trail", max_length=300)):
    same_origin(request)
    with state.pool.connection() as conn:
        db.remove_from_trail(conn, arxiv_id)
    return RedirectResponse(
        next if next.startswith("/") and not next.startswith("//") else "/trail", status_code=303
    )


@router.post("/trail/{arxiv_id:path}/move")
def move(request: Request, arxiv_id: str, web_id: int = Form()):
    same_origin(request)
    with state.pool.connection() as conn:
        if db.get_web(conn, web_id) is None or db.trail_entry(conn, arxiv_id) is None:
            raise HTTPException(404, "no such web or paper on the trail")
        db.move_to_web(conn, arxiv_id, web_id)
    return RedirectResponse(f"/trail#web-{web_id}", status_code=303)


@router.post("/webs/{web_id}/rename")
def rename(request: Request, web_id: int, name: str = Form(min_length=1, max_length=80)):
    same_origin(request)
    with state.pool.connection() as conn:
        db.rename_web(conn, web_id, name.strip())
    return RedirectResponse(f"/trail#web-{web_id}", status_code=303)


@router.post("/webs/{web_id}/delete")
def delete(request: Request, web_id: int):
    same_origin(request)
    with state.pool.connection() as conn:
        db.delete_web(conn, web_id)
    return RedirectResponse("/trail", status_code=303)


@router.get("/trail", response_class=HTMLResponse)
def trail_page(request: Request):
    with state.pool.connection() as conn:
        webs = db.list_webs(conn)
        paths = {w["id"]: db.web_trail(conn, w["id"]) for w in webs}
    return render(request, "trail.html", webs=webs, paths=paths, dwell=trail.DWELL_SECONDS)


@router.get("/webs/{web_id}/graph.json")
def web_graph(web_id: int, limit: int = Query(default=recommend.LIMIT, ge=4, le=40)):
    with state.pool.connection() as conn:
        g = recommend.graph(conn, web_id, limit)
    if g is None:
        return JSONResponse({"detail": "no such web"}, status_code=404)
    citations.schedule(state.pool, settings, [n["id"] for n in g["nodes"] if n["kind"] == "path"])
    return g


class Dismiss(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@router.post("/suggestions/dismiss")
def dismiss(request: Request, body: Dismiss) -> dict:
    """'Not interested': the suggestion never comes back (arXiv id, or an s2:/title: key)."""
    same_origin(request)
    key = parse_arxiv_id(body.key) or body.key
    with state.pool.connection() as conn:
        db.dismiss(conn, key)
    return {"dismissed": key}


@router.get("/explore", response_class=HTMLResponse)
def explore_latest(request: Request):
    with state.pool.connection() as conn:
        webs = db.list_webs(conn)
    if webs:
        return RedirectResponse(f"/explore/{webs[0]['id']}", status_code=303)
    return render(request, "explore.html", webs=[], web=None, graph=None, dwell=trail.DWELL_SECONDS)


@router.get("/explore/{web_id}", response_class=HTMLResponse)
def explore(request: Request, web_id: int, limit: int = Query(default=recommend.LIMIT, ge=4, le=40)):
    with state.pool.connection() as conn:
        webs = db.list_webs(conn)
        g = recommend.graph(conn, web_id, limit)
    if g is None:
        raise HTTPException(404, "no such web")
    citations.schedule(state.pool, settings, [n["id"] for n in g["nodes"] if n["kind"] == "path"])
    titles = {n["id"]: n["title"].split(":")[0] for n in g["nodes"] if n["kind"] == "path"}
    return render(
        request,
        "explore.html",
        webs=webs,
        web=g["web"],
        graph=g,
        titles=titles,
        limit=limit,
        dwell=trail.DWELL_SECONDS,
    )
