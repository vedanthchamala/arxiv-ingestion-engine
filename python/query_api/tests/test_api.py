from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from arxiv_common import db
from arxiv_query_api.cache import scope_key
from arxiv_query_api.main import app, state

VEC = [0.03125] * 1024
CONN = object()
HIT_KEYS = {
    "arxiv_id",
    "version",
    "title",
    "authors",
    "primary_category",
    "categories",
    "published_at",
    "score",
    "summary",
    "abstract",
    "chunk_idx",
    "chunk_section",
    "chunk_text",
    "url",
}


def hit(i: int, score: float) -> db.Hit:
    return db.Hit(
        arxiv_id=f"2609.0000{i}",
        version=i,
        title=f"Paper {i}",
        abstract="An abstract.",
        summary=None if i == 1 else "A summary.",
        primary_category="cs.RO",
        categories=["cs.RO", "cs.LG"],
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        authors=["Ada Lovelace", "Grace Hopper"],
        score=score,
        chunk_idx=i - 1,
        chunk_section="Introduction",
        chunk_text="Matched chunk.",
    )


HITS = [hit(1, 0.87654321), hit(2, 0.5), hit(3, 0.123456)]


class FakeCache:
    def __init__(self, stored: dict | None = None) -> None:
        self.stored = stored
        self.puts: list[tuple[str, str, dict]] = []

    def get(self, vector: list[float], scope: str) -> dict | None:
        return self.stored

    def put(self, query: str, vector: list[float], scope: str, response: dict) -> None:
        self.puts.append((query, scope, response))


@contextmanager
def fake_connection() -> Iterator[object]:
    yield CONN


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def search() -> Iterator[MagicMock]:
    with patch("arxiv_query_api.main.db.search", return_value=list(HITS)) as m:
        yield m


@pytest.fixture
def client(search: MagicMock) -> TestClient:
    state.inference = MagicMock()
    state.inference.embed.return_value = [VEC]
    state.pool = SimpleNamespace(connection=fake_connection)
    state.cache = None
    return TestClient(app)


def test_post_search_returns_hits_with_expected_shape(client, search):
    r = client.post("/search", json={"query": "diffusion policy", "k": 3})

    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "diffusion policy"
    assert body["cached"] is False
    assert isinstance(body["took_ms"], int) and body["took_ms"] >= 0
    assert len(body["hits"]) == 3
    assert set(body["hits"][0]) == HIT_KEYS
    assert [h["score"] for h in body["hits"]] == [0.8765, 0.5, 0.1235]
    assert body["hits"][0]["url"] == "https://arxiv.org/abs/2609.00001v1"
    assert body["hits"][0]["summary"] is None and body["hits"][1]["summary"] == "A summary."
    assert body["hits"][0]["published_at"].startswith("2026-09-01T00:00:00")
    state.inference.embed.assert_called_once_with(["diffusion policy"])
    args, kwargs = search.call_args
    assert args == (CONN, VEC) and kwargs == {"k": 3, "categories": None, "since": None, "ef_search": 200}


def test_get_search_passes_filters_to_db(client, search):
    r = client.get(
        "/search",
        params={"q": "grasping", "category": ["cs.RO", "cs.LG"], "k": 5, "since": "2026-09-01T00:00:00Z"},
    )

    assert r.status_code == 200
    search.assert_called_once()
    args, kwargs = search.call_args
    assert args == (CONN, VEC)
    assert kwargs == {
        "k": 5,
        "categories": ["cs.RO", "cs.LG"],
        "since": datetime(2026, 9, 1, tzinfo=UTC),
        "ef_search": 200,
    }


def test_get_search_single_category(client, search):
    assert client.get("/search", params={"q": "grasping", "category": "cs.RO"}).status_code == 200
    assert search.call_args.kwargs == {"k": 10, "categories": ["cs.RO"], "since": None, "ef_search": 200}


@pytest.mark.parametrize("body", [{}, {"query": ""}, {"query": "x", "k": 0}, {"query": "x", "k": 51}])
def test_post_search_rejects_invalid_input(client, search, body):
    assert client.post("/search", json=body).status_code == 422
    search.assert_not_called()


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "x", "k": 0}, {"q": "x", "k": 51}])
def test_get_search_rejects_invalid_input(client, search, params):
    assert client.get("/search", params=params).status_code == 422
    search.assert_not_called()


def test_cache_hit_returns_stored_response_without_db(client, search):
    stored = client.post("/search", json={"query": "diffusion policy"}).json()
    search.reset_mock()
    state.cache = FakeCache(stored=stored)
    hits_before = sample("api_cache_events_total", result="hit")

    r = client.post("/search", json={"query": "diffusion policies for robots"})

    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is True
    assert body["query"] == "diffusion policies for robots"
    assert body["hits"] == stored["hits"]
    search.assert_not_called()
    assert state.cache.puts == []
    assert sample("api_cache_events_total", result="hit") == hits_before + 1


def test_cache_miss_stores_response(client, search):
    state.cache = FakeCache()
    misses_before = sample("api_cache_events_total", result="miss")

    r = client.post("/search", json={"query": "diffusion policy", "k": 3, "categories": ["cs.LG", "cs.RO"]})

    assert r.json()["cached"] is False
    search.assert_called_once()
    [(query, scope, response)] = state.cache.puts
    assert query == "diffusion policy"
    assert scope == scope_key(3, ["cs.RO", "cs.LG"], None)
    assert [h["arxiv_id"] for h in response["hits"]] == ["2609.00001", "2609.00002", "2609.00003"]
    assert sample("api_cache_events_total", result="miss") == misses_before + 1


def test_scope_key_depends_only_on_filters():
    since = datetime(2026, 9, 1, tzinfo=UTC)
    base = scope_key(10, ["cs.LG", "cs.RO"], since)

    assert scope_key(10, ["cs.LG", "cs.RO"], since) == base
    assert scope_key(10, ["cs.RO", "cs.LG"], since) == base
    assert scope_key(5, ["cs.LG", "cs.RO"], since) != base
    assert scope_key(10, ["cs.LG"], since) != base
    assert scope_key(10, ["cs.LG", "cs.RO"], None) != base
    assert scope_key(10, ["cs.LG", "cs.RO"], datetime(2026, 9, 2, tzinfo=UTC)) != base
    assert scope_key(10, None, None) == scope_key(10, [], None)


def test_metrics_endpoint_exposes_api_metrics(client):
    client.post("/search", json={"query": "q"})

    r = client.get("/metrics")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "api_search_total" in r.text
    assert 'api_search_total{cached="false"}' in r.text
    assert "api_search_seconds_bucket" in r.text
    assert "api_embed_seconds_count" in r.text
    assert "api_db_search_seconds_count" in r.text
