import math
import re
import unicodedata

_WS = re.compile(r"\s+")


def normalize_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def author_norm(name: str) -> str:
    """Key used to dedupe authors: accent-stripped, case-folded, single-spaced."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    return normalize_ws(ascii_only).casefold()


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def strip_nul(s: str) -> str:
    """Postgres text columns reject NUL bytes; extracted PDF/HTML text occasionally carries them."""
    return s.replace("\x00", "")


_ARXIV_ID = re.compile(
    r"^(?:https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/|arxiv:)?"
    r"(\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})(?:v\d+)?(?:\.pdf)?/?$",
    re.IGNORECASE,
)


def parse_arxiv_id(s: str) -> str | None:
    """Unversioned arXiv id from a bare id, `arXiv:` id or arxiv.org abs/pdf/html URL; else None."""
    m = _ARXIV_ID.match(s.strip())
    return m.group(1) if m else None
