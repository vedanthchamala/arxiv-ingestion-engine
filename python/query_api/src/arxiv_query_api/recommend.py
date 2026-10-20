"""Suggestions that branch off a web's path, and the graph the Explore page draws (decision 38).

Candidates come from the web's most recent path papers (newest weighted highest) along three edges:
  builds on  - papers a path paper cites (shared references count more: one citation in a 50-entry
               bibliography is weak evidence, three path papers citing the same work is strong)
  follow-up  - papers that cite a path paper (from Semantic Scholar; newest papers have few)
  similar    - nearest papers by title + abstract embedding
Papers on any path and dismissed suggestions are excluded; the list is capped per source paper so one
paper's bibliography cannot fill it, and papers outside the index are capped so most suggestions can be
opened (and so join the path) here. Every suggestion keeps the reasons it was picked."""

from collections import defaultdict
from dataclasses import dataclass, field

import psycopg

from arxiv_common import db

RECENT_N = 8
DECAY = 0.85
SIMILAR_K = 12
PER_SOURCE = 4
LIMIT = 12
MAX_EXTERNAL_SHARE = 0.34
WEIGHT = {"builds_on": 0.35, "follow_up": 0.6}


def similar_weight(sim: float) -> float:
    """bge-m3 cosine to a 0.1..1 weight: a paper's nearest neighbour sits near 0.70-0.83, its 50th near
    0.63 (measured on this corpus), so the useful range is narrow."""
    return min(1.0, max(0.1, (sim - 0.60) / 0.20))


@dataclass
class Candidate:
    key: str
    arxiv_id: str | None
    title: str | None = None
    year: int | None = None
    score: float = 0.0
    reasons: list[dict] = field(default_factory=list)

    def add(self, kind: str, source: str, weight: float) -> None:
        self.score += weight
        if not any(r["kind"] == kind and r["from"] == source for r in self.reasons):
            self.reasons.append({"kind": kind, "from": source, "weight": round(weight, 3)})


def candidates(conn: psycopg.Connection, path: list[str]) -> dict[str, Candidate]:
    recent = list(reversed(path))[:RECENT_N]
    edges = defaultdict(list)
    for e in db.citations_for(conn, recent):
        edges[e["arxiv_id"]].append(e)
    found: dict[str, Candidate] = {}

    def get(key: str, arxiv_id: str | None, title: str | None = None, year: int | None = None) -> Candidate:
        c = found.setdefault(key, Candidate(key, arxiv_id))
        c.title = c.title or title
        c.year = c.year or year
        return c

    for i, p in enumerate(recent):
        w = DECAY**i
        for e in edges[p]:
            kind = "builds_on" if e["direction"] == "cites" else "follow_up"
            get(e["other_key"], e["other_arxiv_id"], e["title"], e["year"]).add(kind, p, w * WEIGHT[kind])
        for n in db.neighbors(conn, p, SIMILAR_K):
            get(f"arxiv:{n['arxiv_id']}", n["arxiv_id"]).add("similar", p, w * similar_weight(n["sim"]))
    return found


def pick(found: dict[str, Candidate], exclude: set[str], limit: int = LIMIT) -> list[Candidate]:
    ranked = sorted(
        (c for c in found.values() if c.key not in exclude and (c.arxiv_id or "") not in exclude),
        key=lambda c: (-c.score, c.key),
    )
    per_source: dict[str, int] = defaultdict(int)
    external_cap = max(1, round(limit * MAX_EXTERNAL_SHARE))
    chosen: list[Candidate] = []
    externals = 0
    for c in ranked:
        source = max(c.reasons, key=lambda r: r["weight"])["from"]
        external = c.arxiv_id is None
        if per_source[source] >= PER_SOURCE or (external and externals >= external_cap):
            continue
        chosen.append(c)
        per_source[source] += 1
        externals += external
        if len(chosen) == limit:
            break
    return chosen


def graph(conn: psycopg.Connection, web_id: int, limit: int = LIMIT) -> dict | None:
    """The web as nodes and edges: its path in reading order, the suggestions, and why each is there."""
    web = db.get_web(conn, web_id)
    if web is None:
        return None
    path = db.web_trail(conn, web_id)
    path_ids = [p["arxiv_id"] for p in path]
    exclude = db.trail_ids(conn) | db.dismissed_keys(conn)
    chosen = pick(candidates(conn, path_ids), exclude, limit)

    brief = {p["arxiv_id"]: p for p in db.papers_brief(conn, [c.arxiv_id for c in chosen if c.arxiv_id])}
    nodes = [
        {
            "id": p["arxiv_id"],
            "kind": "path",
            "order": i + 1,
            "title": p["title"],
            "category": p["primary_category"],
            "year": p["published_at"].year,
            "summary": p["summary"] or p["abstract"],
            "in_index": True,
        }
        for i, p in enumerate(path)
    ]
    edges = [
        {"source": a, "target": b, "type": "trail"} for a, b in zip(path_ids, path_ids[1:], strict=False)
    ]
    for c in chosen:
        info = brief.get(c.arxiv_id) if c.arxiv_id else None
        node_id = c.arxiv_id if info else c.key
        nodes.append(
            {
                "id": node_id,
                "kind": "suggestion",
                "in_index": info is not None,
                "arxiv_id": c.arxiv_id,
                "key": c.key,
                "title": info["title"] if info else (c.title or c.key),
                "category": info["primary_category"] if info else None,
                "year": info["published_at"].year if info else c.year,
                "summary": (info["summary"] or info["abstract"]) if info else None,
                "score": round(c.score, 3),
                "reasons": sorted(c.reasons, key=lambda r: -r["weight"]),
            }
        )
        for r in c.reasons:
            if r["kind"] == "similar":
                edges.append({"source": r["from"], "target": node_id, "type": "similar"})
            elif r["kind"] == "builds_on":
                edges.append({"source": r["from"], "target": node_id, "type": "builds_on"})
            else:
                edges.append({"source": node_id, "target": r["from"], "type": "builds_on"})
    # citations among the reader's own papers, so the path shows its internal structure too
    on_path = set(path_ids)
    for e in db.citations_for(conn, path_ids):
        if e["direction"] == "cites" and e["other_arxiv_id"] in on_path:
            edges.append({"source": e["arxiv_id"], "target": e["other_arxiv_id"], "type": "builds_on"})
    unique_edges = list({(e["source"], e["target"], e["type"]): e for e in edges}.values())
    pending = len(set(path_ids) - db.citation_fetched(conn, path_ids))
    return {
        "web": {"id": web["id"], "name": web["name"]},
        "nodes": nodes,
        "edges": unique_edges,
        "pending_citations": pending,
    }
