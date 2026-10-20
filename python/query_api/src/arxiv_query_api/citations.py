"""Citation edges for papers on the reading trail (decision 38): Semantic Scholar's batch endpoint first
(references and citing papers, arXiv ids where known), the paper's arXiv HTML bibliography as a fallback
when Semantic Scholar does not know the paper or returns no references. Fetched once per paper and cached
in Postgres; a paper on the trail is fetched in the background when it joins."""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import httpx
import structlog
from psycopg_pool import ConnectionPool

from arxiv_common import db
from arxiv_common.config import Settings

from . import figures

log = structlog.get_logger()

S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_FIELDS = (
    "externalIds,title,year,references.externalIds,references.title,references.year,"
    "citations.externalIds,citations.title,citations.year"
)
S2_BATCH_SIZE = 500
S2_BACKOFF_S = (5, 15, 45)  # the keyless pool answers 429 often; a key gets its own quota
MAX_EDGES = 500  # per direction; a famous paper's citing list is long and only its newest matter here
_ARXIV_ID = re.compile(r"(?:arxiv[:\s]*|arxiv\.org/(?:abs|pdf)/|\babs/)(\d{4}\.\d{4,5})", re.IGNORECASE)
# Hand-written \\bibitem entries carry no title field; IEEE style quotes the title: “Diffusion policy: …,”
_QUOTED = re.compile(r"[“\"]([^”\"]{8,300}?)[,.]?[”\"]")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _last_year(text: str) -> re.Match | None:
    found = list(_YEAR.finditer(text))
    return found[-1] if found else None


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _key(arxiv_id: str | None, s2_id: str | None, title: str | None) -> str | None:
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    if s2_id:
        return f"s2:{s2_id}"
    if title:
        return f"title:{norm_title(title)[:120]}"
    return None


# -- Semantic Scholar ----------------------------------------------------------------------------


def fetch_s2(
    arxiv_ids: list[str], settings: Settings, client: httpx.Client | None = None
) -> list[dict | None]:
    """One batch call (<= 500 ids); the list is aligned with `arxiv_ids`, None for papers S2 lacks."""
    headers = {"x-api-key": settings.s2_api_key} if settings.s2_api_key else {}
    own = client is None
    client = client or httpx.Client(timeout=60)
    try:
        for attempt, pause in enumerate((*S2_BACKOFF_S, None), start=1):
            r = client.post(
                S2_BATCH,
                params={"fields": S2_FIELDS},
                json={"ids": [f"ARXIV:{i}" for i in arxiv_ids]},
                headers=headers,
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504) and pause is not None:
                log.warning(
                    "semantic scholar busy; retrying", status=r.status_code, attempt=attempt, wait_s=pause
                )
                time.sleep(pause)
                continue
            raise RuntimeError(f"Semantic Scholar HTTP {r.status_code}: {r.text[:200]}")
        raise RuntimeError("unreachable")
    finally:
        if own:
            client.close()


def edges_from_s2(record: dict) -> list[dict]:
    out = []
    for direction, field in (("cites", "references"), ("cited_by", "citations")):
        for other in (record.get(field) or [])[:MAX_EDGES]:
            arxiv_id = ((other.get("externalIds") or {}).get("ArXiv") or "").split("v")[0] or None
            key = _key(arxiv_id, other.get("paperId"), other.get("title"))
            if key:
                out.append(
                    {
                        "direction": direction,
                        "other_key": key,
                        "other_arxiv_id": arxiv_id,
                        "title": other.get("title"),
                        "year": other.get("year"),
                    }
                )
    return out


# -- arXiv HTML bibliography (fallback) ----------------------------------------------------------


class _BibParser(HTMLParser):
    """LaTeXML bibliography items: `li.ltx_bibitem` with `ltx_bib_title` / `ltx_bib_year` spans."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._item: dict | None = None
        self._field: str | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "li" and "ltx_bibitem" in classes:
            self._item = {"title": "", "year": "", "text": [], "hrefs": []}
            return
        if self._item is None:
            return
        if tag == "a" and a.get("href"):
            self._item["hrefs"].append(a["href"])
        for field in ("title", "year"):
            if f"ltx_bib_{field}" in classes:
                self._field, self._depth = field, 1
                return
        if self._field:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._item is None:
            return
        if self._field:
            self._depth -= 1
            if self._depth == 0:
                self._field = None
        if tag == "li":
            self.items.append(self._item)
            self._item = None

    def handle_data(self, data):
        if self._item is None:
            return
        self._item["text"].append(data)
        if self._field:
            self._item[self._field] += data


def references_from_html(html: str) -> list[dict]:
    parser = _BibParser()
    parser.feed(html)
    out = []
    for item in parser.items:
        text = " ".join("".join(item["text"]).split())
        m = _ARXIV_ID.search(" ".join(item["hrefs"]) + " " + text)
        title = " ".join(item["title"].split()).strip(" .") or None
        if title is None and (quoted := _QUOTED.search(text)):
            title = quoted.group(1).strip(" ,.")
        year = re.search(r"(19|20)\d{2}", item["year"]) or _last_year(text)
        arxiv_id = m.group(1) if m else None
        key = _key(arxiv_id, None, title)
        if key:
            out.append(
                {
                    "direction": "cites",
                    "other_key": key,
                    "other_arxiv_id": arxiv_id,
                    "title": title,
                    "year": int(year.group(0)) if year else None,
                }
            )
    return out


def resolve_titles(conn, edges: list[dict]) -> list[dict]:
    """Give title-only references an arXiv id when a paper in the index has exactly that title."""
    wanted = {norm_title(e["title"]): e for e in edges if not e["other_arxiv_id"] and e["title"]}
    if not wanted:
        return edges
    for row in db.papers_by_norm_title(conn, list(wanted)):
        e = wanted[row["norm"]]
        e["other_arxiv_id"] = row["arxiv_id"]
        e["other_key"] = f"arxiv:{row['arxiv_id']}"
    return edges


# -- fetching and caching ------------------------------------------------------------------------

_executor = ThreadPoolExecutor(1, "citations")


def schedule(pool: ConnectionPool, settings: Settings, arxiv_ids: list[str]) -> None:
    _executor.submit(ensure, pool, settings, arxiv_ids)


def ensure(pool: ConnectionPool, settings: Settings, arxiv_ids: list[str]) -> None:
    """Fetch citation edges for the papers that have none cached yet (S2 batch, then HTML fallback)."""
    try:
        with pool.connection() as conn:
            todo = [i for i in arxiv_ids if i not in db.citation_fetched(conn, arxiv_ids)]
        for start in range(0, len(todo), S2_BATCH_SIZE):
            batch = todo[start : start + S2_BATCH_SIZE]
            try:
                records: list[dict | None] = fetch_s2(batch, settings)
                s2_ok = True
            except (httpx.HTTPError, RuntimeError) as e:
                log.warning("semantic scholar unavailable; trying the HTML fallback", error=str(e)[:200])
                records, s2_ok = [None] * len(batch), False
            for arxiv_id, record in zip(batch, records, strict=True):
                edges = edges_from_s2(record) if record else []
                if any(e["direction"] == "cites" for e in edges):
                    with pool.connection() as conn:
                        db.store_citations(conn, arxiv_id, "s2", "done", resolve_titles(conn, edges), None)
                    continue
                _fallback(pool, settings, arxiv_id, edges, s2_ok)
    except Exception as e:  # noqa: BLE001 - background work: log, never take the API down
        log.warning("citation fetch failed", error=str(e)[:300])


def _fallback(
    pool: ConnectionPool, settings: Settings, arxiv_id: str, s2_edges: list[dict], s2_ok: bool
) -> None:
    """Statuses: done (complete), partial (S2 was unavailable: retried after a day so citing papers can
    still arrive), missing (nothing to find), failed (the HTML fetch failed: retried after a day)."""
    with pool.connection() as conn:
        paper = db.get_paper(conn, arxiv_id)
    html, error, fetch_failed = None, None, False
    if paper and paper.get("html_url"):
        status, html, error, _ = figures.fetch_html(paper["html_url"], settings)
        fetch_failed = status == "failed"
    edges = s2_edges + (references_from_html(html) if html else [])
    if fetch_failed:
        result = "failed"
    elif not s2_ok:
        result = "partial"
    else:
        result = "done" if edges else "missing"
    with pool.connection() as conn:
        db.store_citations(
            conn, arxiv_id, "html" if html else "s2", result, resolve_titles(conn, edges), error
        )
