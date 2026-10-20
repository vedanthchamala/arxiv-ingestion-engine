"""The reading trail: papers the reader actually read (10 visible seconds on a paper page, or an explicit
"Add to path"), grouped into one web per research area (decision 38).

A paper read from a suggestion joins the web the suggestion came from: that is the branch. Any other
paper joins the web where it is among the NEIGHBORS_K nearest papers (title + abstract embedding) of one
of that web's RECENT_N latest papers, choosing the closest match; otherwise it starts a new web. The rule
is rank-based because bge-m3 similarities are compressed: a paper's 50th neighbour sits near 0.63 cosine
while one random same-category pair in ten scores 0.58 or more, so a fixed cut-off checked against every
paper of a growing web would absorb unrelated papers almost half the time."""

from dataclasses import dataclass

import psycopg

from arxiv_common import db

NEIGHBORS_K = 50
RECENT_N = 5
DWELL_SECONDS = 10
KINDS = ("builds_on", "followed_by", "similar", "search", "manual")


@dataclass
class Read:
    arxiv_id: str
    web_id: int
    web_name: str
    new_web: bool
    already: bool


def web_name(paper: dict, query: str | None) -> str:
    """The search that started the web, else the paper's coined name ("CGS-SLAM: ..." -> "CGS-SLAM"),
    else its first words. The reader can rename it."""
    if query and query.strip():
        return query.strip()[:48]
    title = " ".join(paper["title"].split())
    head = title.split(":")[0].strip()
    if head != title and 2 <= len(head) <= 40:
        return head
    words = title.split()
    return " ".join(words[:6]) + (" …" if len(words) > 6 else "")


def assign_web(conn: psycopg.Connection, arxiv_id: str, via: str | None, web_hint: int | None) -> int | None:
    if web_hint is not None and db.get_web(conn, web_hint):
        return web_hint
    if via:
        entry = db.trail_entry(conn, via)
        if entry:
            return entry["web_id"]
    best: tuple[float, int] | None = None
    for web_id in db.web_ids_by_activity(conn):
        for p in db.recent_trail(conn, web_id, RECENT_N):
            for n in db.neighbors(conn, p, NEIGHBORS_K):
                if n["arxiv_id"] == arxiv_id and (best is None or n["sim"] > best[0]):
                    best = (n["sim"], web_id)
    return best[1] if best else None


def record_read(
    conn: psycopg.Connection,
    paper: dict,
    via: str | None = None,
    kind: str | None = None,
    query: str | None = None,
    web_hint: int | None = None,
) -> Read:
    arxiv_id = paper["arxiv_id"]
    existing = db.trail_entry(conn, arxiv_id)
    if existing:
        return Read(arxiv_id, existing["web_id"], existing["web_name"], new_web=False, already=True)
    kind = kind if kind in KINDS else ("search" if query else None)
    web_id = assign_web(conn, arxiv_id, via, web_hint)
    new_web = web_id is None
    if new_web:
        web_id = db.create_web(conn, web_name(paper, query))
    added = db.add_to_trail(conn, arxiv_id, web_id, via, kind, query)
    web = db.get_web(conn, web_id)
    return Read(arxiv_id, web_id, web["name"], new_web=new_web and added, already=not added)
