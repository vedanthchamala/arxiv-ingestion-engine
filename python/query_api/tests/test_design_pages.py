from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from arxiv_common import db
from arxiv_query_api import design, ui
from arxiv_query_api.main import app, state

CONN = object()
NOW = datetime.now(UTC)
PAPER = {
    "arxiv_id": "2608.26868",
    "version": 1,
    "title": "CGS-SLAM: Collaborative SLAM",
    "abstract": "We present CGS-SLAM.",
    "summary": "CGS-SLAM maps.",
    "authors": ["Ada Lovelace"],
    "categories": ["cs.RO"],
    "primary_category": "cs.RO",
    "published_at": datetime(2026, 8, 27, tzinfo=UTC),
    "updated_at": datetime(2026, 8, 27, tzinfo=UTC),
    "source": "html",
    "chunk_count": 28,
    "html_url": "https://arxiv.org/html/2608.26868v1",
    "doi": None,
    "comment": None,
    "journal_ref": None,
    "summary_model": "qwen",
}
DESIGN = {
    "one_liner": "CGS-SLAM builds one map from many phones.",
    "problem": "Phones lack depth sensors.",
    "key_idea": "Map locally, merge on a server.",
    "steps": [
        {
            "name": "Local tracking",
            "input": "RGB + IMU",
            "does": "Tracks the camera",
            "output": "Local map",
            "why": "Scale",
            "section": "3 Method",
            "detail": "",
            "evidence": "",
        },
        {
            "name": "Merge <maps>",
            "input": "Local maps",
            "does": "Aligns with VGGT",
            "output": "Global map",
            "why": "Consistency",
            "section": "",
            "detail": "Uses co-visible keyframes.",
            "evidence": "The server merges them",
        },
    ],
    "results": [{"value": "130 KB/s", "label": "uplink per agent"}],
    "terms": [{"term": "VGGT", "meaning": "a view alignment model"}],
    "limitations": [],
    "overview_figure": 3,
}
FIGURES = [
    {"n": 1, "caption": "Overview of the system.", "images": ["https://arxiv.org/html/2608.26868v1/a.png"]},
    {"n": 3, "caption": "Submap alignment.", "images": ["https://arxiv.org/html/2608.26868v1/c.png"]},
]


def design_row(status="done", **over) -> dict:
    base = {
        "status": status,
        "model": "qwen2.5:7b-instruct",
        "design": DESIGN if status == "done" else None,
        "rejected": [
            {"kind": "term", "claim": "ATE = Average Trajectory Error", "reason": "acronym expansion"}
        ],
        "error": "model server down" if status == "failed" else None,
        "seconds": 47.6,
        "cost_usd": None,
        "started_at": NOW,
    }
    return base | over


@contextmanager
def fake_connection() -> Iterator[object]:
    yield CONN


@pytest.fixture
def dbm():
    with (
        patch.object(db, "corpus_stats", return_value={"papers": 1, "full_text": 1, "newest": NOW}),
        patch.object(db, "get_paper", return_value=PAPER) as get_paper,
        patch.object(db, "get_design", return_value=design_row()) as get_design,
        patch.object(db, "get_figures", return_value={"figures": FIGURES, "status": "done"}),
        patch.object(db, "list_designs", return_value=[{"provider": "local", "status": "done"}]),
        patch.object(db, "reset_design") as reset,
        patch.object(db, "trail_entry", return_value=None),
        patch.object(db, "sections", return_value=[]),
        patch.object(db, "paper_vector", return_value=None),
        patch.object(design, "start", return_value=False) as start,
    ):
        yield SimpleNamespace(get_paper=get_paper, get_design=get_design, reset=reset, start=start)


@pytest.fixture
def client(dbm, monkeypatch) -> TestClient:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    ui._stats = None
    state.pool = SimpleNamespace(connection=fake_connection)
    state.inference = MagicMock()
    state.cache = None
    return TestClient(app)


def test_paper_page_links_to_its_design(client):
    r = client.get("/paper/2608.26868")

    assert 'href="/paper/2608.26868/design">High-level design' in r.text


def test_design_page_renders_flow_figure_results_and_provenance(client, dbm):
    r = client.get("/paper/2608.26868/design")

    assert r.status_code == 200
    t = r.text
    assert "CGS-SLAM builds one map from many phones." in t
    assert t.count('class="flow-step"') == 2 and t.count('role="tabpanel"') == 2
    assert "Merge &lt;maps&gt;" in t
    assert "§ 3 Method" in t and "Uses co-visible keyframes." in t and "“The server merges them”" in t
    assert 'src="https://arxiv.org/html/2608.26868v1/c.png"' in t
    assert "<b>Figure 3.</b> Submap alignment." in t and "1 more figure from the paper" in t
    assert "130 KB/s" in t and "<dt>VGGT</dt>" in t
    assert "qwen2.5:7b-instruct" in t and "in 48 s" in t and "Numbers and terms were checked" in t
    assert "1 that could not be found were left out" in t and "ATE = Average Trajectory Error" in t
    assert 'http-equiv="refresh"' not in t
    assert 'aria-label="Written by"' not in t
    dbm.start.assert_called_once()


def test_design_page_shows_progress_while_running(client, dbm):
    dbm.get_design.return_value = design_row("running")

    r = client.get("/paper/2608.26868/design")

    assert "Reading the paper and sketching its design" in r.text
    assert '<meta http-equiv="refresh" content="3">' in r.text


def test_first_visit_starts_generation(client, dbm):
    dbm.get_design.side_effect = [None, design_row("running")]
    dbm.start.return_value = True

    r = client.get("/paper/2608.26868/design")

    args = dbm.start.call_args.args
    assert args[2]["arxiv_id"] == "2608.26868" and args[3] == "local"
    assert "Reading the paper" in r.text


def test_failed_design_offers_retry_without_restarting(client, dbm):
    dbm.get_design.return_value = design_row("failed")

    r = client.get("/paper/2608.26868/design")

    assert "The design could not be written: model server down" in r.text
    assert 'action="/paper/2608.26868/design/regenerate?model=local"' in r.text
    assert 'http-equiv="refresh"' not in r.text
    dbm.start.assert_not_called()


def test_regenerate_resets_and_retries(client, dbm):
    r = client.post("/paper/2608.26868/design/regenerate?model=local", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == "/paper/2608.26868/design?model=local"
    dbm.reset.assert_called_once_with(CONN, "2608.26868", "local")
    assert dbm.start.call_args.kwargs == {"retry_failed": True}


def test_regenerate_refuses_cross_origin(client, dbm):
    r = client.post("/paper/2608.26868/design/regenerate", headers={"origin": "https://evil.example"})

    assert r.status_code == 403
    dbm.reset.assert_not_called()


def test_claude_requested_without_a_key_falls_back_to_local(client, dbm):
    client.get("/paper/2608.26868/design?model=claude")

    assert dbm.get_design.call_args.args[1:] == ("2608.26868", "local")


def test_model_toggle_appears_with_a_claude_key(client, dbm, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    dbm.get_design.return_value = design_row(model="claude-opus-5-5", cost_usd=0.061)

    r = client.get("/paper/2608.26868/design?model=claude")

    assert dbm.get_design.call_args.args[1:] == ("2608.26868", "claude")
    assert 'aria-label="Written by"' in r.text and "Local model ✓" in r.text
    assert "for $0.061" in r.text and "Numbers, terms and quotes were checked" in r.text


def test_unknown_paper_is_404(client, dbm):
    dbm.get_paper.return_value = None

    assert client.get("/paper/9999.99999/design").status_code == 404


def test_design_json(client, dbm):
    r = client.get("/papers/2608.26868/design")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done" and body["design"]["steps"][0]["name"] == "Local tracking"
    assert body["figures"][0]["n"] == 1


def test_design_json_starts_generation_when_missing(client, dbm):
    dbm.get_design.return_value = None

    r = client.get("/papers/2608.26868/design")

    assert r.status_code == 202 and r.json()["status"] == "running"
    dbm.start.assert_called_once()
