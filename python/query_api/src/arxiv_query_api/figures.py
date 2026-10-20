"""Figures from a paper's arXiv HTML rendering (LaTeXML): number, caption and image URLs. Fetched once
per paper, on the first view of its design page, through the shared arXiv request budget; the images
themselves are loaded by the reader's browser straight from arxiv.org, never stored or re-served."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
import redis
import structlog

from arxiv_common.config import Settings
from arxiv_common.ratelimit import DEFAULT_KEY, SharedMinInterval

log = structlog.get_logger()

MAX_FIGURES = 16
PARSER_VERSION = 2  # bump when parsing changes: cached figure lists from older versions are fetched again
_FIGURE_NO = re.compile(r"^\s*(?:Figure|Fig\.?)\s*(\d+)\s*[:.]?\s*", re.IGNORECASE)
_OVERVIEW = re.compile(
    r"\b(overview|architecture|pipeline|framework|system design|method overview|our (method|approach|system)|"
    r"illustration of (our|the proposed))\b",
    re.IGNORECASE,
)


class _FigureParser(HTMLParser):
    """Collects top-level `<figure class="ltx_figure">` blocks; nested panels add their images to the
    enclosing figure. Tables and algorithms (also `<figure>` in LaTeXML) are skipped."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.figures: list[dict] = []
        self._depth = 0  # nesting depth of <figure> inside the current ltx_figure, 0 = outside
        self._current: dict | None = None
        self._in_caption = 0
        self._caption_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "figure":
            classes = (a.get("class") or "").split()
            if self._depth:
                self._depth += 1
            elif "ltx_figure" in classes and "ltx_table" not in classes:
                self._depth = 1
                self._current = {"images": [], "caption": ""}
                self._caption_parts = []
        elif self._depth and tag == "img" and a.get("src"):
            self._current["images"].append(urljoin(self.base_url, a["src"]))
        elif self._depth == 1 and tag == "figcaption":
            self._in_caption += 1
        elif self._in_caption and tag in ("math",):
            self._caption_parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "figcaption" and self._in_caption:
            self._in_caption -= 1
        elif tag == "figure" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._current is not None:
                caption = " ".join("".join(self._caption_parts).split())
                m = _FIGURE_NO.match(caption)
                if m and self._current["images"]:
                    self.figures.append(
                        {
                            "n": int(m.group(1)),
                            "caption": caption[m.end() :],
                            "images": self._current["images"][:6],
                        }
                    )
                self._current = None

    def handle_data(self, data):
        if self._in_caption:
            self._caption_parts.append(data)


def parse_figures(html: str, base_url: str) -> list[dict]:
    parser = _FigureParser(base_url)
    parser.feed(html)
    seen, out = set(), []
    for f in parser.figures:
        if f["n"] not in seen:
            seen.add(f["n"])
            out.append(f)
    return out[:MAX_FIGURES]


def overview_figure(figures: list[dict], chosen: int | None = None) -> dict | None:
    """The figure that shows the whole design: the model's choice if it names a real figure, else the
    first caption that says overview/architecture/pipeline/framework, else Figure 1."""
    by_n = {f["n"]: f for f in figures}
    if chosen in by_n:
        return by_n[chosen]
    for f in figures:
        if _OVERVIEW.search(f["caption"][:200]):
            return f
    return by_n.get(1)


def fetch_figures(html_url: str, settings: Settings) -> tuple[str, list[dict], str | None]:
    """(status, figures, error). One GET, after reserving a slot in the arXiv budget shared with the
    poller, fetcher and backfill (decision 27)."""
    limiter = SharedMinInterval(
        redis.Redis.from_url(settings.redis_url), settings.arxiv_min_interval_s + 0.05, DEFAULT_KEY
    )
    limiter.wait()
    try:
        r = httpx.get(
            html_url, headers={"User-Agent": settings.arxiv_user_agent}, timeout=60, follow_redirects=True
        )
    except httpx.HTTPError as e:
        return "failed", [], f"{type(e).__name__}: {e}"
    if r.status_code == 404:
        return "none", [], None
    if r.status_code != 200:
        return "failed", [], f"HTTP {r.status_code}"
    figures = parse_figures(r.text, str(r.url))
    log.info("figures fetched", url=html_url, figures=len(figures))
    return "done", figures, None
