"""Semantic cache for /search: a query whose embedding is within `distance` of a cached query
(with the same filters) reuses that response. Backed by Redis 8's vector index via redisvl."""

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime

import structlog
from redisvl.extensions.cache.llm import SemanticCache
from redisvl.query.filter import Tag
from redisvl.utils.vectorize import CustomTextVectorizer

log = structlog.get_logger()


def scope_key(k: int, categories: Sequence[str] | None, since: datetime | None) -> str:
    """Results depend on the filters, so the cache only matches within the same scope."""
    raw = f"k={k}|cats={sorted(categories or [])}|since={since.isoformat() if since else ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class SearchCache:
    def __init__(
        self, redis_url: str, embed: Callable[[str], list[float]], ttl: int, distance: float
    ) -> None:
        self._cache = SemanticCache(
            name="arxiv_search",
            redis_url=redis_url,
            distance_threshold=distance,
            ttl=ttl,
            vectorizer=CustomTextVectorizer(embed=embed),
            filterable_fields=[{"name": "scope", "type": "tag"}],
        )
        self.hits = 0
        self.misses = 0

    def get(self, vector: list[float], scope: str) -> dict | None:
        rows = self._cache.check(vector=vector, num_results=1, filter_expression=Tag("scope") == scope)
        if not rows:
            self.misses += 1
            return None
        self.hits += 1
        row = rows[0]
        log.info("cache hit", cached_query=row.get("prompt"), distance=row.get("vector_distance"))
        return json.loads(row["response"])

    def put(self, query: str, vector: list[float], scope: str, response: dict) -> None:
        self._cache.store(
            prompt=query, response=json.dumps(response), vector=vector, filters={"scope": scope}
        )

    def clear(self) -> None:
        self._cache.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        rate = round(self.hits / total, 3) if total else None
        return {"hits": self.hits, "misses": self.misses, "hit_rate": rate}
