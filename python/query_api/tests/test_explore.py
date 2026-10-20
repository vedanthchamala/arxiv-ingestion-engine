import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from arxiv_common import db
from arxiv_query_api import citations, recommend, ui
from arxiv_query_api.main import app, state

CONN = object()
NOW = datetime.now(UTC)
GRAPH = {
    "web": {"id": 2, "name": "SLAM"},
    "nodes": [
        {
            "id": "2608.26868",
            "kind": "path",
            "order": 1,
            "title": "CGS-SLAM: Collaborative SLAM",
            "category": "cs.CV",
            "year": 2026,
            "summary": "s",
            "in_index": True,
        },
        {
            "id": "2609.17387",
            "kind": "suggestion",
            "in_index": True,
            "arxiv_id": "2609.17387",
            "key": "arxiv:2609.17387",
            "title": "PanoGS-SLAM",
            "category": "cs.CV",
            "year": 2026,
            "summary": "x",
            "score": 1.2,
            "reasons": [{"kind": "similar", "from": "2608.26868", "weight": 1.0}],
        },
        {
            "id": "title:3d gaussian splatting",
            "kind": "suggestion",
            "in_index": False,
            "arxiv_id": None,
            "key": "title:3d gaussian splatting",
            "title": "3D Gaussian Splatting </script>",
            "category": None,
            "year": 2023,
            "summary": None,
            "score": 0.35,
            "reasons": [{"kind": "builds_on", "from": "2608.26868", "weight": 0.35}],
        },
        {
            "id": "2610.00001",
            "kind": "suggestion",
            "in_index": True,
            "arxiv_id": "2610.00001",
            "key": "arxiv:2610.00001",
            "title": "A Follow-up",
            "category": "cs.RO",
            "year": 2026,
            "summary": None,
            "score": 0.6,
            "reasons": [{"kind": "follow_up", "from": "2608.26868", "weight": 0.6}],
        },
    ],
    "edges": [{"source": "2608.26868", "target": "2609.17387", "type": "similar"}],
    "pending_citations": 1,
}


@contextmanager
def fake_connection():
    yield CONN


@pytest.fixture
def client():
    ui._stats = None
    state.pool = SimpleNamespace(connection=fake_connection)
    state.inference = MagicMock()
    state.cache = None
    with (
        patch.object(db, "corpus_stats", return_value={"papers": 1, "full_text": 1, "newest": NOW}),
        patch.object(
            db,
            "list_webs",
            return_value=[
                {"id": 2, "name": "SLAM", "papers": 1, "last_read": NOW},
                {"id": 5, "name": "LoRA", "papers": 1, "last_read": NOW},
            ],
        ),
        patch.object(citations, "schedule") as schedule,
    ):
        c = TestClient(app)
        c.schedule = schedule
        yield c


def test_explore_redirects_to_the_latest_web(client):
    r = client.get("/explore", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == "/explore/2"


def test_explore_empty_state(client):
    with patch.object(db, "list_webs", return_value=[]):
        r = client.get("/explore")

    assert r.status_code == 200 and "Nothing here yet" in r.text
    assert "cytoscape" not in r.text


def test_explore_page_renders_graph_lists_and_vendored_scripts(client):
    with patch.object(recommend, "graph", return_value=GRAPH) as graph:
        r = client.get("/explore/2", params={"limit": 20})

    assert r.status_code == 200
    graph.assert_called_once_with(CONN, 2, 20)
    t = r.text
    for lib in (
        "cytoscape-3.34.3.min.js",
        "layout-base-2.0.1.js",
        "cose-base-2.2.0.js",
        "cytoscape-fcose-2.2.0.js",
        "explore.js",
    ):
        assert f"/static/vendor/{lib}" in t or f"/static/{lib}" in t
    assert 'href="/explore/5"' in t and 'aria-current="true">SLAM' in t
    assert 'href="/paper/2609.17387?via=2608.26868&kind=similar&web=2"' in t
    assert 'href="/paper/2610.00001?via=2608.26868&kind=followed_by&web=2"' in t
    assert "similar to CGS-SLAM" in t
    assert "arxiv.org/search/?searchtype=title&query=3D%20Gaussian%20Splatting%20%3C/script%3E" in t
    assert "still being fetched" in t
    assert 'href="/explore/2?limit=28"' in t
    data = re.search(r'<script id="graph-data" type="application/json">(.*?)</script>', t, re.S).group(1)
    assert "</script>" not in data
    assert json.loads(data)["web"] == {"id": 2, "name": "SLAM"}
    client.schedule.assert_called_once()


def test_explore_unknown_web_is_404(client):
    with patch.object(recommend, "graph", return_value=None):
        assert client.get("/explore/9").status_code == 404


def test_explore_limit_is_bounded(client):
    with patch.object(recommend, "graph", return_value=GRAPH):
        assert client.get("/explore/2", params={"limit": 999}).status_code == 422


def test_static_explore_script_is_served(client):
    r = client.get("/static/explore.js")

    assert r.status_code == 200 and "fixedNodeConstraint" in r.text and "#paper=" in r.text


def test_vendored_libraries_have_their_licenses(client):
    r = client.get("/static/vendor/LICENSES.md")

    assert r.status_code == 200 and r.text.count("Permission is hereby granted") == 4


def test_nav_links_to_explore(client):
    with patch.object(recommend, "graph", return_value=GRAPH):
        assert '<a href="/explore">Explore</a>' in client.get("/explore/2").text
