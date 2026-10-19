import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pgvector import Vector

from arxiv_common import db
from arxiv_query_api import ui
from arxiv_query_api.main import app, state
from arxiv_query_api.service import watch_matches

VEC = [0.03125] * 1024
CONN = object()
NOW = datetime.now(UTC)
SEEN = NOW - timedelta(days=2)
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def watch(**over) -> dict:
    base = {
        "id": 7,
        "name": "Diffusion policies",
        "query": "diffusion policy",
        "categories": ["cs.RO"],
        "embedding": Vector(VEC),
        "created_at": NOW - timedelta(days=30),
        "seen_at": SEEN,
    }
    return base | over


def hit(i: int, score: float, keyword: bool = False) -> db.Hit:
    return db.Hit(
        arxiv_id=f"2609.0000{i}",
        version=1,
        title=f"Hit {i}",
        abstract="An abstract.",
        summary="A summary.",
        primary_category="cs.RO",
        categories=["cs.RO"],
        published_at=datetime(2026, 9, i, tzinfo=UTC),
        authors=["Grace Hopper"],
        score=score,
        chunk_idx=0,
        chunk_section="abstract",
        chunk_text="Hit\n\nAn abstract.",
        keyword=keyword,
    )


@contextmanager
def fake_connection() -> Iterator[object]:
    yield CONN


@pytest.fixture
def dbm() -> Iterator[SimpleNamespace]:
    hits = [hit(1, 0.70), hit(2, 0.40, keyword=True), hit(3, 0.30), hit(4, 0.60)]
    ingested = {
        "2609.00001": SEEN - timedelta(days=1),
        "2609.00002": SEEN + timedelta(hours=1),
        "2609.00004": NOW,
    }
    with (
        patch.object(db, "corpus_stats", return_value={"papers": 3, "full_text": 2, "newest": NOW}),
        patch.object(db, "search", return_value=hits) as search,
        patch.object(db, "ingested_at", return_value=ingested),
        patch.object(db, "list_watches", return_value=[watch()]) as list_watches,
        patch.object(db, "get_watch", return_value=watch()) as get_watch,
        patch.object(db, "create_watch", return_value=7) as create_watch,
        patch.object(db, "delete_watch") as delete_watch,
        patch.object(db, "mark_watch_seen") as mark_watch_seen,
        patch.object(db, "find_author", return_value=None),
    ):
        yield SimpleNamespace(
            search=search,
            list_watches=list_watches,
            get_watch=get_watch,
            create_watch=create_watch,
            delete_watch=delete_watch,
            mark_watch_seen=mark_watch_seen,
        )


@pytest.fixture
def client(dbm) -> TestClient:
    ui._stats = None
    state.inference = MagicMock()
    state.inference.embed.return_value = [VEC]
    state.pool = SimpleNamespace(connection=fake_connection)
    state.cache = None
    state.hybrid = True
    return TestClient(app)


def test_matches_keep_keyword_hits_and_confident_vector_hits_new_first(client, dbm):
    since = NOW - timedelta(days=7)

    matches = watch_matches(watch(), since)

    assert [(m.hit.arxiv_id, m.new) for m in matches] == [
        ("2609.00002", True),
        ("2609.00004", True),
        ("2609.00001", False),
    ]
    args, kwargs = dbm.search.call_args
    assert args == (CONN, VEC)
    assert (
        kwargs["categories"] == ["cs.RO"]
        and kwargs["since"] == since
        and kwargs["text"] == "diffusion policy"
    )
    state.inference.embed.assert_not_called()


def test_matches_use_vector_only_when_hybrid_is_off(client, dbm):
    state.hybrid = False

    watch_matches(watch(), None)

    assert dbm.search.call_args.kwargs["text"] is None


def test_create_watch_from_form(client, dbm):
    r = client.post(
        "/watches",
        data={"query": "  diffusion policy ", "name": "", "cat": ["cs.RO"]},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/watches/7"
    state.inference.embed.assert_called_once_with(["diffusion policy"])
    dbm.create_watch.assert_called_once_with(CONN, "diffusion policy", "diffusion policy", ["cs.RO"], VEC)


def test_create_watch_without_categories_stores_none(client, dbm):
    client.post("/watches", data={"query": "LoRA", "name": "Adapters"}, follow_redirects=False)

    dbm.create_watch.assert_called_once_with(CONN, "Adapters", "LoRA", None, VEC)


def test_create_watch_rejects_cross_origin_posts(client, dbm):
    r = client.post("/watches", data={"query": "x"}, headers={"origin": "https://evil.example"})

    assert r.status_code == 403
    dbm.create_watch.assert_not_called()


def test_create_watch_accepts_same_origin(client, dbm):
    r = client.post(
        "/watches", data={"query": "x"}, headers={"origin": "http://testserver"}, follow_redirects=False
    )

    assert r.status_code == 303


def test_create_watch_reports_embedding_server_down(client, dbm):
    state.inference.embed.side_effect = httpx.ConnectError("refused")

    r = client.post("/watches", data={"query": "x"})

    assert r.status_code == 503
    dbm.create_watch.assert_not_called()


def test_watches_overview(client, dbm):
    r = client.get("/watches", params={"within": "month"})

    assert r.status_code == 200
    assert "Diffusion policies" in r.text
    assert "<b>2 new</b> · 3 matching" in r.text
    assert "Hit 2" in r.text
    dbm.mark_watch_seen.assert_not_called()
    since = dbm.search.call_args.kwargs["since"]
    assert abs(since - (NOW - timedelta(days=30))) < timedelta(minutes=1)


def test_watches_overview_empty(client, dbm):
    dbm.list_watches.return_value = []

    assert "No watches yet" in client.get("/watches").text


def test_watch_page_marks_seen_after_showing_new(client, dbm):
    r = client.get("/watches/7")

    assert r.status_code == 200
    assert r.text.count('<span class="new">new</span>') == 2
    assert "2 new since" in r.text
    assert "/watches/7/feed.xml" in r.text
    dbm.mark_watch_seen.assert_called_once_with(CONN, 7)


def test_watch_page_all_time_window(client, dbm):
    client.get("/watches/7", params={"within": ""})

    assert dbm.search.call_args.kwargs["since"] is None


def test_watch_page_404(client, dbm):
    dbm.get_watch.return_value = None

    assert client.get("/watches/99").status_code == 404


def test_delete_watch(client, dbm):
    r = client.post("/watches/7/delete", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == "/watches"
    dbm.delete_watch.assert_called_once_with(CONN, 7)


def test_atom_feed(client, dbm):
    dbm.get_watch.return_value = watch(name="Ampersands & <brackets>")

    r = client.get("/watches/7/feed.xml")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/atom+xml")
    root = ET.fromstring(r.content)
    assert root.find("a:title", ATOM).text == "Ampersands & <brackets> · arXiv search"
    entries = root.findall("a:entry", ATOM)
    assert [e.find("a:id", ATOM).text for e in entries] == [
        "https://arxiv.org/abs/2609.00004",
        "https://arxiv.org/abs/2609.00002",
        "https://arxiv.org/abs/2609.00001",
    ]
    assert entries[0].find("a:author/a:name", ATOM).text == "Grace Hopper"
    assert root.find("a:updated", ATOM).text.startswith("2026-09-04")
    dbm.mark_watch_seen.assert_not_called()


def test_search_results_offer_to_watch_the_search(client, dbm):
    r = client.get("/", params={"q": "diffusion policy", "cat": "cs.RO"})

    assert 'action="/watches"' in r.text
    assert 'name="query" value="diffusion policy"' in r.text
    assert 'name="cat" value="cs.RO"' in r.text
