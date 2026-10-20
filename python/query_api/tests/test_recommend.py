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
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def cite(src, direction, key, arxiv_id=None, title=None, year=None):
    return {
        "arxiv_id": src,
        "direction": direction,
        "other_key": key,
        "other_arxiv_id": arxiv_id,
        "title": title,
        "year": year,
    }


CITES = [
    cite("p2", "cites", "arxiv:old1", "old1", "Foundational", 2023),
    cite("p1", "cites", "arxiv:old1", "old1", "Foundational", 2023),
    cite("p2", "cites", "s2:x", None, "Outside Paper", 2021),
    cite("p1", "cited_by", "arxiv:new1", "new1", "Follow-up", 2026),
]
NEIGHBORS = {
    "p2": [{"arxiv_id": "n1", "sim": 0.80}, {"arxiv_id": "p1", "sim": 0.85}, {"arxiv_id": "n2", "sim": 0.62}],
    "p1": [{"arxiv_id": "n1", "sim": 0.70}],
}


@pytest.fixture
def fake_db():
    with (
        patch.object(
            db, "citations_for", side_effect=lambda c, ids: [e for e in CITES if e["arxiv_id"] in ids]
        ),
        patch.object(db, "neighbors", side_effect=lambda c, p, k: NEIGHBORS.get(p, [])),
    ):
        yield


def test_similar_weight_maps_the_narrow_cosine_range():
    assert recommend.similar_weight(0.85) == 1.0
    assert recommend.similar_weight(0.70) == pytest.approx(0.5)
    assert recommend.similar_weight(0.50) == 0.1


def test_candidates_sum_evidence_with_recency_decay(fake_db):
    found = recommend.candidates(CONN, ["p1", "p2"])

    shared = found["arxiv:old1"]
    assert shared.score == pytest.approx(0.35 * 1 + 0.35 * recommend.DECAY)
    assert {r["from"] for r in shared.reasons} == {"p1", "p2"}
    assert found["arxiv:new1"].reasons[0]["kind"] == "follow_up"
    n1 = found["arxiv:n1"]
    assert n1.score == pytest.approx(1.0 * 1.0 + recommend.DECAY * 0.5)
    assert found["s2:x"].arxiv_id is None and found["s2:x"].title == "Outside Paper"


def test_candidates_use_only_the_recent_path(fake_db):
    path = [f"old{i}" for i in range(20)] + ["p1", "p2"]
    with patch.object(db, "citations_for", return_value=[]) as cites:
        recommend.candidates(CONN, path)

    assert cites.call_args.args[1] == list(reversed(path))[: recommend.RECENT_N]


def test_pick_excludes_trail_and_dismissed_and_caps_sources_and_externals():
    found = {}
    for i in range(10):
        c = recommend.Candidate(f"arxiv:a{i}", f"a{i}")
        c.add("similar", "p1", 1.0 - i / 100)
        found[c.key] = c
    for i in range(6):
        c = recommend.Candidate(f"s2:e{i}", None)
        c.add("builds_on", f"q{i}", 0.5)
        found[c.key] = c
    seen = recommend.Candidate("arxiv:p9", "p9")
    seen.add("similar", "q9", 5.0)
    found[seen.key] = seen

    chosen = recommend.pick(found, exclude={"p9", "arxiv:a0"}, limit=12)

    keys = [c.key for c in chosen]
    assert "arxiv:p9" not in keys and "arxiv:a0" not in keys
    assert sum(k.startswith("arxiv:a") for k in keys) == recommend.PER_SOURCE
    assert sum(k.startswith("s2:") for k in keys) == round(12 * recommend.MAX_EXTERNAL_SHARE)
    assert keys[0] == "arxiv:a1"


def test_graph_nodes_and_typed_edges(fake_db):
    path_rows = [
        {
            "arxiv_id": "p1",
            "title": "One",
            "primary_category": "cs.RO",
            "published_at": NOW,
            "summary": "s1",
            "abstract": "a1",
        },
        {
            "arxiv_id": "p2",
            "title": "Two",
            "primary_category": "cs.RO",
            "published_at": NOW,
            "summary": None,
            "abstract": "a2",
        },
    ]
    brief = [
        {
            "arxiv_id": i,
            "title": f"T {i}",
            "primary_category": "cs.CV",
            "published_at": NOW,
            "summary": None,
            "abstract": "x",
        }
        for i in ("n1", "n2", "new1", "old1")
    ]
    extra = CITES + [cite("p2", "cites", "arxiv:p1", "p1", "One", 2026)]
    with (
        patch.object(db, "get_web", return_value={"id": 3, "name": "SLAM"}),
        patch.object(db, "web_trail", return_value=path_rows),
        patch.object(db, "trail_ids", return_value={"p1", "p2"}),
        patch.object(db, "dismissed_keys", return_value={"n2"}),
        patch.object(db, "papers_brief", return_value=brief),
        patch.object(db, "citation_fetched", return_value={"p1"}),
        patch.object(
            db, "citations_for", side_effect=lambda c, ids: [e for e in extra if e["arxiv_id"] in ids]
        ),
    ):
        g = recommend.graph(CONN, 3)

    nodes = {n["id"]: n for n in g["nodes"]}
    assert g["web"] == {"id": 3, "name": "SLAM"}
    assert [n["order"] for n in g["nodes"] if n["kind"] == "path"] == [1, 2]
    assert nodes["p2"]["summary"] == "a2"
    assert "n2" not in nodes and "p1" in nodes and nodes["p1"]["kind"] == "path"
    assert nodes["s2:x"]["in_index"] is False and nodes["s2:x"]["title"] == "Outside Paper"
    assert nodes["old1"]["in_index"] is True and nodes["old1"]["title"] == "T old1"
    edges = {(e["source"], e["target"], e["type"]) for e in g["edges"]}
    assert ("p1", "p2", "trail") in edges
    assert ("p2", "old1", "builds_on") in edges and ("p1", "old1", "builds_on") in edges
    assert ("new1", "p1", "builds_on") in edges
    assert ("p2", "n1", "similar") in edges
    assert ("p2", "p1", "builds_on") in edges
    assert g["pending_citations"] == 1
    assert len(g["edges"]) == len(edges)


def test_graph_of_unknown_web_is_none():
    with patch.object(db, "get_web", return_value=None):
        assert recommend.graph(CONN, 99) is None


# -- endpoints -----------------------------------------------------------------------------------


@contextmanager
def fake_connection():
    yield CONN


@pytest.fixture
def client():
    ui._stats = None
    state.pool = SimpleNamespace(connection=fake_connection)
    state.inference = MagicMock()
    state.cache = None
    return TestClient(app)


def test_graph_endpoint_schedules_citations_for_the_path(client):
    g = {
        "web": {"id": 3, "name": "SLAM"},
        "nodes": [{"id": "p1", "kind": "path"}, {"id": "n1", "kind": "suggestion"}],
        "edges": [],
    }
    with patch.object(recommend, "graph", return_value=g), patch.object(citations, "schedule") as schedule:
        r = client.get("/webs/3/graph.json")

    assert r.status_code == 200 and r.json() == g
    assert schedule.call_args.args[2] == ["p1"]


def test_graph_endpoint_404(client):
    with patch.object(recommend, "graph", return_value=None):
        assert client.get("/webs/9/graph.json").status_code == 404


def test_dismiss_normalizes_ids_and_checks_origin(client):
    with patch.object(db, "dismiss") as dismiss:
        assert client.post("/suggestions/dismiss", json={"key": "2303.04137v2"}).json() == {
            "dismissed": "2303.04137"
        }
        assert client.post("/suggestions/dismiss", json={"key": "s2:abc"}).json() == {"dismissed": "s2:abc"}
        assert (
            client.post(
                "/suggestions/dismiss", json={"key": "s2:abc"}, headers={"origin": "https://evil.example"}
            ).status_code
            == 403
        )

    assert [c.args[1] for c in dismiss.call_args_list] == ["2303.04137", "s2:abc"]
