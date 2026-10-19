from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from arxiv_common import db
from arxiv_query_api import ui
from arxiv_query_api.main import app, state

VEC = [0.03125] * 1024
CONN = object()
NOW = datetime.now(UTC)


def row(i: int, **over) -> dict:
    base = {
        "arxiv_id": f"2609.0000{i}",
        "version": 1,
        "title": f"Paper {i}",
        "abstract": "An abstract.",
        "summary": "A summary.",
        "primary_category": "cs.RO",
        "categories": ["cs.RO"],
        "published_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC),
        "source": "html",
        "authors": ["Ada Lovelace"],
        "html_url": None,
        "doi": None,
        "comment": None,
        "journal_ref": None,
        "summary_model": "qwen",
        "chunk_count": 12,
    }
    return base | over


def hit(i: int, score: float, chunk_idx: int = 3) -> db.Hit:
    return db.Hit(
        arxiv_id=f"2609.0000{i}",
        version=1,
        title=f"Hit {i}",
        abstract="An abstract.",
        summary=None,
        primary_category="cs.RO",
        categories=["cs.RO"],
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        authors=["Grace Hopper"],
        score=score,
        chunk_idx=chunk_idx,
        chunk_section="3 Method",
        chunk_text="3 Method\nWe train a diffusion policy.",
    )


@contextmanager
def fake_connection() -> Iterator[object]:
    yield CONN


@pytest.fixture
def dbm() -> Iterator[SimpleNamespace]:
    with (
        patch.object(
            db,
            "corpus_stats",
            return_value={"papers": 3, "full_text": 2, "newest": NOW, "last_processed": NOW},
        ) as stats,
        patch.object(db, "latest", return_value=[row(1), row(2)]) as latest,
        patch.object(db, "search", return_value=[hit(1, 0.81), hit(2, 0.7, chunk_idx=0)]) as search,
        patch.object(db, "get_paper", return_value=row(1)) as get_paper,
        patch.object(db, "sections", return_value=["1 Introduction", "3 Method"]) as sections,
        patch.object(db, "paper_vector", return_value=VEC) as paper_vector,
        patch.object(db, "find_author", return_value=None) as find_author,
        patch.object(
            db, "author_papers", return_value=[row(3, authors=["Ada Lovelace", "Alan Turing"])]
        ) as author_papers,
    ):
        yield SimpleNamespace(
            stats=stats,
            latest=latest,
            search=search,
            get_paper=get_paper,
            sections=sections,
            paper_vector=paper_vector,
            find_author=find_author,
            author_papers=author_papers,
        )


@pytest.fixture
def client(dbm) -> TestClient:
    ui._stats = None
    state.inference = MagicMock()
    state.inference.embed.return_value = [VEC]
    state.pool = SimpleNamespace(connection=fake_connection)
    state.cache = None
    return TestClient(app)


def test_home_lists_latest_papers(client, dbm):
    r = client.get("/")

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Paper 1" in r.text and "Paper 2" in r.text
    assert "Latest papers" in r.text
    dbm.latest.assert_called_once_with(CONN, limit=31, offset=0, categories=None)
    state.inference.embed.assert_not_called()


def test_home_category_filter_and_paging(client, dbm):
    dbm.latest.return_value = [row(i) for i in range(31)]

    r = client.get("/", params={"cat": ["cs.RO"], "page": 2})

    dbm.latest.assert_called_once_with(CONN, limit=31, offset=60, categories=["cs.RO"])
    assert "page=3" in r.text and "page=1" in r.text
    assert "Latest papers in Robotics" in r.text


def test_search_renders_hits_with_matched_passage(client, dbm):
    r = client.get("/", params={"q": "diffusion policy", "cat": "cs.RO", "within": "week"})

    assert r.status_code == 200
    state.inference.embed.assert_called_once_with(["diffusion policy"])
    kwargs = dbm.search.call_args.kwargs
    assert kwargs["k"] == 20 and kwargs["categories"] == ["cs.RO"]
    assert abs(kwargs["since"] - (NOW - timedelta(days=7))) < timedelta(minutes=1)
    assert "2 results" in r.text
    assert "Matched in § 3 Method" in r.text
    assert "We train a diffusion policy." in r.text
    assert "3 Method\nWe train" not in r.text
    assert r.text.count("Matched in") == 1


def test_search_unknown_window_means_any_time(client, dbm):
    client.get("/", params={"q": "x", "within": "decade"})

    assert dbm.search.call_args.kwargs["since"] is None


def test_search_show_more_asks_for_50(client, dbm):
    dbm.search.return_value = [hit(i, 0.5) for i in range(20)]
    first = client.get("/", params={"q": "x"})
    assert "more=1" in first.text

    client.get("/", params={"q": "x", "more": "1"})
    assert dbm.search.call_args.kwargs["k"] == 50


def test_search_reports_embedding_server_down(client, dbm):
    state.inference.embed.side_effect = httpx.ConnectError("refused")

    r = client.get("/", params={"q": "x"})

    assert r.status_code == 503
    assert "did not answer" in r.text
    dbm.search.assert_not_called()


def test_html_is_escaped(client, dbm):
    dbm.latest.return_value = [row(1, title="<script>alert(1)</script>")]

    r = client.get("/")

    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_paper_page(client, dbm):
    dbm.search.return_value = [hit(1, 1.0), hit(5, 0.9), hit(6, 0.8)]

    r = client.get("/paper/2609.00001")

    assert r.status_code == 200
    assert "Paper 1" in r.text and "3 Method" in r.text and "12 passages" in r.text
    assert "Hit 5" in r.text and "Hit 6" in r.text
    assert 'href="/paper/2609.00001"' not in r.text.split('id="similar"')[1]
    dbm.paper_vector.assert_called_once_with(CONN, "2609.00001")
    assert dbm.search.call_args.kwargs["k"] == 9


@pytest.mark.parametrize("given", ["2609.00001v3", "arXiv:2609.00001", "https://arxiv.org/abs/2609.00001v2"])
def test_paper_page_redirects_to_canonical_id(client, given):
    r = client.get(f"/paper/{given}", follow_redirects=False)

    assert r.status_code == 301
    assert r.headers["location"] == "/paper/2609.00001"


def test_paper_page_missing(client, dbm):
    dbm.get_paper.return_value = None

    r = client.get("/paper/2609.99999")

    assert r.status_code == 404
    assert "not in the local index" in r.text
    assert "https://arxiv.org/abs/2609.99999" in r.text


def test_stale_notice(client, dbm):
    assert "days old" not in client.get("/").text

    ui._stats = None
    dbm.stats.return_value = {
        "papers": 3,
        "full_text": 2,
        "newest": NOW - timedelta(days=9),
        "last_processed": NOW,
    }

    assert "newest paper here is 9 days old" in client.get("/").text


def test_similar_api_excludes_the_paper_itself(client, dbm):
    dbm.search.return_value = [hit(1, 1.0), hit(5, 0.9), hit(6, 0.8)]

    r = client.get("/papers/2609.00001/similar", params={"k": 2})

    assert r.status_code == 200
    assert [h["arxiv_id"] for h in r.json()] == ["2609.00005", "2609.00006"]


def test_similar_api_404_when_not_ingested(client, dbm):
    dbm.paper_vector.return_value = None

    assert client.get("/papers/2609.99999/similar").status_code == 404


def test_paper_json_route_still_serves_json(client, dbm):
    r = client.get("/papers/2609.00001")

    assert r.status_code == 200
    assert r.json()["chunk_count"] == 12


def test_static_stylesheet_is_served(client):
    r = client.get("/static/style.css")

    assert r.status_code == 200
    assert "prefers-color-scheme" in r.text


@pytest.mark.parametrize("q", ["2609.00001", "2609.00001v2", "https://arxiv.org/pdf/2609.00001v2"])
def test_search_box_jumps_to_a_pasted_arxiv_id(client, dbm, q):
    r = client.get("/", params={"q": q}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/paper/2609.00001"
    state.inference.embed.assert_not_called()


def test_search_offers_author_page_for_an_exact_name(client, dbm):
    dbm.find_author.return_value = {"id": 7, "name": "Ada Lovelace", "papers": 4}

    r = client.get("/", params={"q": "ada lovelace"})

    dbm.find_author.assert_called_once_with(CONN, "ada lovelace")
    assert "4 papers by <b>Ada Lovelace</b>" in r.text
    assert 'href="/author?name=Ada%20Lovelace"' in r.text


def test_search_skips_author_lookup_for_long_queries(client, dbm):
    client.get("/", params={"q": "diffusion policy for robot manipulation"})

    dbm.find_author.assert_not_called()


def test_author_names_link_to_author_pages(client):
    r = client.get("/")

    assert '<a href="/author?name=Ada%20Lovelace">Ada Lovelace</a>' in r.text


def test_author_page(client, dbm):
    dbm.find_author.return_value = {"id": 7, "name": "Ada Lovelace", "papers": 1}

    r = client.get("/author", params={"name": "Ada Lovelace"})

    assert r.status_code == 200
    assert "Paper 3" in r.text and "Alan Turing" in r.text
    dbm.author_papers.assert_called_once_with(CONN, 7)


def test_author_page_unknown(client, dbm):
    r = client.get("/author", params={"name": "Nobody Here"})

    assert r.status_code == 404
    assert "No author named" in r.text
    dbm.author_papers.assert_not_called()
