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
