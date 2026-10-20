from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from arxiv_common import db
from arxiv_query_api import trail, ui
from arxiv_query_api.main import app, state

CONN = object()
NOW = datetime.now(UTC)


def paper(arxiv_id="2608.26868", title="CGS-SLAM: Collaborative Gaussian Splatting based SLAM") -> dict:
    return {
        "arxiv_id": arxiv_id,
        "version": 1,
        "title": title,
        "abstract": "We present it.",
        "summary": "It maps.",
        "authors": ["Ada Lovelace"],
        "categories": ["cs.RO"],
        "primary_category": "cs.RO",
        "published_at": datetime(2026, 8, 27, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 27, tzinfo=UTC),
        "source": "html",
        "chunk_count": 28,
        "html_url": None,
        "doi": None,
        "comment": None,
        "journal_ref": None,
        "summary_model": "qwen",
    }


# -- web assignment ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "query", "expected"),
    [
        ("CGS-SLAM: Collaborative Gaussian Splatting", None, "CGS-SLAM"),
        (
            "CGS-SLAM: Collaborative Gaussian Splatting",
            "  gaussian splatting slam ",
            "gaussian splatting slam",
        ),
        (
            "Adaptive teachers for amortized samplers in deceptive worlds",
            None,
            "Adaptive teachers for amortized samplers in …",
        ),
        ("Short title", None, "Short title"),
    ],
)
def test_web_name(title, query, expected):
    assert trail.web_name({"title": title}, query) == expected


def test_assign_web_prefers_explicit_web_then_the_branch_source():
    with patch.object(db, "get_web", return_value={"id": 4}), patch.object(db, "trail_entry") as entry:
        assert trail.assign_web(CONN, "x", via="y", web_hint=4) == 4
        entry.assert_not_called()

    with (
        patch.object(db, "get_web", return_value=None),
        patch.object(db, "trail_entry", return_value={"web_id": 9}),
    ):
        assert trail.assign_web(CONN, "x", via="2608.26868", web_hint=77) == 9


def test_assign_web_by_neighbour_rank_picks_the_closest_web():
    recent = {1: ["a1", "a2"], 2: ["b1"]}
    nbrs = {
        "a1": [{"arxiv_id": "z", "sim": 0.70}, {"arxiv_id": "new", "sim": 0.64}],
        "a2": [{"arxiv_id": "q", "sim": 0.80}],
        "b1": [{"arxiv_id": "new", "sim": 0.71}],
    }
    with (
        patch.object(db, "web_ids_by_activity", return_value=[1, 2]),
        patch.object(db, "recent_trail", side_effect=lambda c, w, n: recent[w]),
        patch.object(db, "neighbors", side_effect=lambda c, p, k: nbrs[p]) as neighbors,
    ):
        assert trail.assign_web(CONN, "new", via=None, web_hint=None) == 2
    assert neighbors.call_args.args[2] == trail.NEIGHBORS_K


def test_assign_web_returns_none_for_a_new_domain():
    with (
        patch.object(db, "web_ids_by_activity", return_value=[1]),
        patch.object(db, "recent_trail", return_value=["a1"]),
        patch.object(db, "neighbors", return_value=[{"arxiv_id": "other", "sim": 0.9}]),
    ):
        assert trail.assign_web(CONN, "new", via=None, web_hint=None) is None


def test_record_read_starts_a_new_web_named_after_the_search():
    with (
        patch.object(db, "trail_entry", return_value=None),
        patch.object(trail, "assign_web", return_value=None),
        patch.object(db, "create_web", return_value=5) as create,
        patch.object(db, "add_to_trail", return_value=True) as add,
        patch.object(db, "get_web", return_value={"id": 5, "name": "gaussian splatting slam"}),
    ):
        r = trail.record_read(CONN, paper(), query="gaussian splatting slam")

    create.assert_called_once_with(CONN, "gaussian splatting slam")
    add.assert_called_once_with(CONN, "2608.26868", 5, None, "search", "gaussian splatting slam")
    assert (r.web_id, r.new_web, r.already) == (5, True, False)


def test_record_read_is_idempotent():
    with (
        patch.object(db, "trail_entry", return_value={"web_id": 3, "web_name": "SLAM"}),
        patch.object(db, "add_to_trail") as add,
    ):
        r = trail.record_read(CONN, paper())

    add.assert_not_called()
    assert (r.web_id, r.web_name, r.already) == (3, "SLAM", True)


def test_record_read_keeps_the_branch():
    with (
        patch.object(db, "trail_entry", return_value=None),
        patch.object(trail, "assign_web", return_value=2) as assign,
        patch.object(db, "add_to_trail", return_value=True) as add,
        patch.object(db, "get_web", return_value={"id": 2, "name": "SLAM"}),
    ):
        r = trail.record_read(CONN, paper(), via="2601.00001", kind="builds_on", web_hint=2)

    assign.assert_called_once_with(CONN, "2608.26868", "2601.00001", 2)
    add.assert_called_once_with(CONN, "2608.26868", 2, "2601.00001", "builds_on", None)
    assert not r.new_web


def test_unknown_kinds_are_dropped():
    with (
        patch.object(db, "trail_entry", return_value=None),
        patch.object(trail, "assign_web", return_value=2),
        patch.object(db, "add_to_trail", return_value=True) as add,
        patch.object(db, "get_web", return_value={"id": 2, "name": "SLAM"}),
    ):
        trail.record_read(CONN, paper(), kind="<script>")

    assert add.call_args.args[4] is None


# -- routes --------------------------------------------------------------------------------------


@contextmanager
def fake_connection() -> Iterator[object]:
    yield CONN


@pytest.fixture
def dbm():
    with (
        patch.object(db, "corpus_stats", return_value={"papers": 1, "full_text": 1, "newest": NOW}),
        patch.object(db, "get_paper", return_value=paper()) as get_paper,
        patch.object(db, "sections", return_value=[]),
        patch.object(db, "paper_vector", return_value=None),
        patch.object(db, "trail_entry", return_value=None) as trail_entry,
        patch.object(db, "remove_from_trail") as remove,
        patch.object(db, "move_to_web") as move,
        patch.object(db, "rename_web") as rename,
        patch.object(db, "delete_web") as delete,
        patch.object(db, "get_web", return_value={"id": 2, "name": "SLAM"}) as get_web,
        patch.object(db, "list_webs", return_value=[]) as list_webs,
        patch.object(db, "web_trail", return_value=[]) as web_trail,
        patch.object(
            trail, "record_read", return_value=trail.Read("2608.26868", 2, "SLAM", True, False)
        ) as rec,
    ):
        yield SimpleNamespace(
            get_paper=get_paper,
            trail_entry=trail_entry,
            remove=remove,
            move=move,
            rename=rename,
            delete=delete,
            get_web=get_web,
            list_webs=list_webs,
            web_trail=web_trail,
            record=rec,
        )


@pytest.fixture
def client(dbm) -> TestClient:
    ui._stats = None
    state.pool = SimpleNamespace(connection=fake_connection)
    state.inference = MagicMock()
    state.cache = None
    return TestClient(app)


def test_beacon_records_a_read(client, dbm):
    r = client.post(
        "/trail/read",
        json={
            "arxiv_id": "2608.26868v1",
            "via": "arXiv:2601.00001",
            "kind": "similar",
            "q": "slam",
            "web": 2,
            "seconds": 11,
        },
    )

    assert r.status_code == 200
    assert r.json() == {
        "arxiv_id": "2608.26868",
        "web_id": 2,
        "web_name": "SLAM",
        "new_web": True,
        "already": False,
    }
    args, kwargs = dbm.record.call_args
    assert args[1]["arxiv_id"] == "2608.26868"
    assert kwargs == {"via": "2601.00001", "kind": "similar", "query": "slam", "web_hint": 2}
    dbm.get_paper.assert_called_once_with(CONN, "2608.26868")


def test_beacon_refuses_cross_origin_and_unknown_papers(client, dbm):
    assert (
        client.post(
            "/trail/read", json={"arxiv_id": "2608.26868"}, headers={"origin": "https://x.example"}
        ).status_code
        == 403
    )
    dbm.get_paper.return_value = None
    assert client.post("/trail/read", json={"arxiv_id": "2608.99999"}).status_code == 404
    assert client.post("/trail/read", json={"arxiv_id": "not an id"}).status_code == 404
    dbm.record.assert_not_called()


def test_manual_add(client, dbm):
    r = client.post("/trail/add", data={"arxiv_id": "2608.26868"}, follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == "/paper/2608.26868"
    assert dbm.record.call_args.kwargs["kind"] == "manual"


def test_remove_redirects_only_to_local_paths(client, dbm):
    r = client.post("/trail/2608.26868/remove", data={"next": "/paper/2608.26868"}, follow_redirects=False)
    assert r.headers["location"] == "/paper/2608.26868"
    dbm.remove.assert_called_once_with(CONN, "2608.26868")

    r = client.post("/trail/2608.26868/remove", data={"next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/trail"


def test_move_rename_delete(client, dbm):
    dbm.trail_entry.return_value = {"web_id": 1, "web_name": "A"}
    assert (
        client.post("/trail/2608.26868/move", data={"web_id": 2}, follow_redirects=False).status_code == 303
    )
    dbm.move.assert_called_once_with(CONN, "2608.26868", 2)

    assert (
        client.post("/webs/2/rename", data={"name": "  Gaussian SLAM "}, follow_redirects=False).status_code
        == 303
    )
    dbm.rename.assert_called_once_with(CONN, 2, "Gaussian SLAM")

    assert client.post("/webs/2/delete", follow_redirects=False).headers["location"] == "/trail"
    dbm.delete.assert_called_once_with(CONN, 2)


def test_move_to_unknown_web_is_404(client, dbm):
    dbm.get_web.return_value = None

    assert client.post("/trail/2608.26868/move", data={"web_id": 99}).status_code == 404
    dbm.move.assert_not_called()


def test_paper_page_runs_the_dwell_beacon_with_its_context(client, dbm):
    r = client.get(
        "/paper/2608.26868", params={"via": "2601.00001v2", "kind": "builds_on", "web": 2, "q": "slam"}
    )

    t = r.text
    assert "Reading this for 10 seconds adds it to your path." in t
    assert 'action="/trail/add"' in t
    assert "const need = 10000" in t
    assert (
        '"via": "2601.00001"' in t and '"kind": "builds_on"' in t and '"web": 2' in t and '"q": "slam"' in t
    )
    assert 'document.visibilityState === "visible"' in t


def test_paper_page_drops_unknown_kinds_and_escapes_the_query(client, dbm):
    t = client.get("/paper/2608.26868", params={"kind": "evil", "q": "</script><b>"}).text

    assert '"kind": null' in t
    assert "</script><b>" not in t


def test_paper_on_the_trail_shows_its_web_and_no_beacon(client, dbm):
    dbm.trail_entry.return_value = {"web_id": 2, "web_name": "SLAM"}

    t = client.get("/paper/2608.26868").text

    assert 'On your path in <a href="/trail#web-2">SLAM</a>' in t
    assert 'action="/trail/2608.26868/remove"' in t
    assert "/trail/read" not in t


def test_versioned_paper_url_keeps_the_trail_context(client):
    r = client.get(
        "/paper/2608.26868v1", params={"via": "2601.00001", "kind": "similar"}, follow_redirects=False
    )

    assert r.status_code == 301
    assert r.headers["location"] == "/paper/2608.26868?via=2601.00001&kind=similar"


def test_search_results_link_papers_with_their_query(client, dbm):
    hit = db.Hit(
        arxiv_id="2608.26868",
        version=1,
        title="CGS-SLAM",
        abstract="a",
        summary=None,
        primary_category="cs.RO",
        categories=["cs.RO"],
        published_at=NOW,
        authors=["A"],
        score=0.7,
        chunk_idx=0,
        chunk_section="abstract",
        chunk_text="t",
    )
    state.inference.embed.return_value = [[0.1] * 1024]
    with patch.object(db, "search", return_value=[hit]), patch.object(db, "find_author", return_value=None):
        t = client.get("/", params={"q": "gaussian slam"}).text

    assert 'href="/paper/2608.26868?q=gaussian%20slam"' in t


def test_trail_page(client, dbm):
    dbm.list_webs.return_value = [
        {"id": 2, "name": "SLAM", "papers": 2, "last_read": NOW},
        {"id": 3, "name": "LoRA", "papers": 1, "last_read": NOW},
    ]
    dbm.web_trail.side_effect = lambda c, w: (
        [
            {
                "arxiv_id": "2608.26868",
                "title": "CGS-SLAM",
                "primary_category": "cs.RO",
                "added_at": NOW,
                "via_kind": "similar",
                "via_arxiv_id": "2601.00001",
                "query": None,
            },
        ]
        if w == 2
        else []
    )

    t = client.get("/trail").text

    assert 'id="web-2"' in t and 'id="web-3"' in t
    assert "CGS-SLAM" in t and "similar to 2601.00001" in t
    assert 'name="web_id"' in t
    assert 'action="/webs/2/rename"' in t


def test_empty_trail(client, dbm):
    assert "Nothing on your trail yet" in client.get("/trail").text
