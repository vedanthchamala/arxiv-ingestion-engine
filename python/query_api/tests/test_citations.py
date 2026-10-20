from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from arxiv_common import db
from arxiv_common.config import Settings
from arxiv_query_api import citations, figures

SETTINGS = Settings()

S2_RECORD = {
    "paperId": "p0",
    "externalIds": {"ArXiv": "2608.26868"},
    "references": [
        {
            "paperId": "r1",
            "externalIds": {"ArXiv": "2303.04137v2"},
            "title": "Diffusion Policy",
            "year": 2023,
        },
        {"paperId": "r2", "externalIds": {"DOI": "10.1/x"}, "title": "NetVLAD", "year": 2016},
        {"paperId": None, "externalIds": None, "title": "An Unindexed Report", "year": None},
        {"paperId": None, "externalIds": None, "title": None, "year": None},
    ],
    "citations": [
        {"paperId": "c1", "externalIds": {"ArXiv": "2610.00001"}, "title": "A Follow-up", "year": 2026}
    ],
}

STRUCTURED = """
<ul class="ltx_biblist">
<li id="bib.bib1" class="ltx_bibitem ltx_bib_article"><span class="ltx_tag">[1]</span>
  <span class="ltx_bibblock">
  <span class="ltx_text ltx_bib_author">J. Zheng</span>
  <span class="ltx_text ltx_bib_year"> (2019)</span>
  </span>
  <span class="ltx_bibblock">
  <span class="ltx_text ltx_bib_title">Structured3D: a <em>large</em> dataset</span>. </span>
  <span class="ltx_bibblock">
  <span class="ltx_text ltx_bib_journal">ArXiv</span> <span class="ltx_text
  ltx_bib_volume">abs/1908.00222</span>.</span>
  </li>
<li id="bib.bib2" class="ltx_bibitem"><span class="ltx_tag">[2]</span>
  <span class="ltx_bibblock"><span class="ltx_text ltx_bib_year">(2021)</span></span>
  <span class="ltx_bibblock"><span class="ltx_text ltx_bib_title">Habitat 3D</span></span>
  <a href="https://arxiv.org/abs/2109.08238">link</a></li>
</ul>
"""
IEEE = """
<ul class="ltx_biblist">
<li id="bib.bib1" class="ltx_bibitem"><span class="ltx_tag">[1]</span>
<span class="ltx_bibblock">J. Ho, A. Jain, and P. Abbeel, “Denoising diffusion probabilistic
  models,” in NeurIPS, 2020, pp. 6840–6851.</span>
  </li>
<li id="bib.bib2" class="ltx_bibitem"><span class="ltx_tag">[2]</span>
<span class="ltx_bibblock">C. Chi, “Diffusion policy: Visuomotor policy learning via action
  diffusion,” arXiv:2303.04137, 2023.</span>
  </li>
<li id="bib.bib3" class="ltx_bibitem">
  <span class="ltx_bibblock">No title or identifiers here, 2019.</span>
  </li>
</ul>
"""


def test_edges_from_s2_keys_and_directions():
    edges = citations.edges_from_s2(S2_RECORD)

    assert [(e["direction"], e["other_key"], e["other_arxiv_id"]) for e in edges] == [
        ("cites", "arxiv:2303.04137", "2303.04137"),
        ("cites", "s2:r2", None),
        ("cites", "title:an unindexed report", None),
        ("cited_by", "arxiv:2610.00001", "2610.00001"),
    ]
    assert edges[0]["title"] == "Diffusion Policy" and edges[0]["year"] == 2023


def test_edges_from_s2_caps_long_lists():
    record = {"references": [{"paperId": f"r{i}", "title": "t"} for i in range(citations.MAX_EDGES + 50)]}

    assert len(citations.edges_from_s2(record)) == citations.MAX_EDGES


def test_structured_bibliography():
    refs = citations.references_from_html(STRUCTURED)

    assert [(r["title"], r["year"], r["other_arxiv_id"]) for r in refs] == [
        ("Structured3D: a large dataset", 2019, "1908.00222"),
        ("Habitat 3D", 2021, "2109.08238"),
    ]


def test_hand_written_bibliography_uses_the_quoted_title():
    refs = citations.references_from_html(IEEE)

    assert [(r["title"], r["year"], r["other_arxiv_id"]) for r in refs] == [
        ("Denoising diffusion probabilistic models", 2020, None),
        ("Diffusion policy: Visuomotor policy learning via action diffusion", 2023, "2303.04137"),
    ]


def test_norm_title():
    assert citations.norm_title("Diffusion Policy: Visuomotor  Policy—Learning!") == (
        "diffusion policy visuomotor policy learning"
    )


def response(status, body=None):
    r = MagicMock(status_code=status, text="busy")
    r.json.return_value = body
    return r


def test_fetch_s2_retries_on_429_and_sends_the_key():
    client = MagicMock()
    client.post.side_effect = [response(429), response(200, [S2_RECORD, None])]
    with patch.object(citations.time, "sleep") as sleep:
        out = citations.fetch_s2(
            ["2608.26868", "9999.99999"], replace(SETTINGS, s2_api_key="k"), client=client
        )

    assert out == [S2_RECORD, None]
    sleep.assert_called_once_with(citations.S2_BACKOFF_S[0])
    kwargs = client.post.call_args.kwargs
    assert kwargs["json"] == {"ids": ["ARXIV:2608.26868", "ARXIV:9999.99999"]}
    assert kwargs["params"] == {"fields": citations.S2_FIELDS}
    assert kwargs["headers"] == {"x-api-key": "k"}


def test_fetch_s2_without_key_sends_no_header_and_gives_up_after_retries():
    client = MagicMock()
    client.post.return_value = response(429)
    with patch.object(citations.time, "sleep"), pytest.raises(RuntimeError, match="429"):
        citations.fetch_s2(["2608.26868"], replace(SETTINGS, s2_api_key=""), client=client)

    assert client.post.call_count == len(citations.S2_BACKOFF_S) + 1
    assert client.post.call_args.kwargs["headers"] == {}


# -- ensure ---------------------------------------------------------------------------------------

PAPER = {"arxiv_id": "2608.26868", "html_url": "https://arxiv.org/html/2608.26868v1"}


@pytest.fixture
def pool():
    @contextmanager
    def connection():
        yield object()

    return SimpleNamespace(connection=connection)


@pytest.fixture
def store():
    with (
        patch.object(db, "citation_fetched", return_value=set()),
        patch.object(db, "get_paper", return_value=PAPER),
        patch.object(db, "papers_by_norm_title", return_value=[]),
        patch.object(db, "store_citations") as store,
    ):
        yield store


def test_ensure_stores_s2_edges(pool, store):
    with (
        patch.object(citations, "fetch_s2", return_value=[S2_RECORD]),
        patch.object(figures, "fetch_html") as html,
    ):
        citations.ensure(pool, SETTINGS, ["2608.26868"])

    args = store.call_args.args
    assert args[1:4] == ("2608.26868", "s2", "done") and len(args[4]) == 4
    html.assert_not_called()


def test_ensure_falls_back_to_html_when_s2_lacks_the_paper(pool, store):
    with (
        patch.object(citations, "fetch_s2", return_value=[None]),
        patch.object(figures, "fetch_html", return_value=("done", IEEE, None, "u")),
    ):
        citations.ensure(pool, SETTINGS, ["2608.26868"])

    args = store.call_args.args
    assert args[2:4] == ("html", "done") and len(args[4]) == 2


def test_ensure_marks_partial_when_s2_is_unavailable(pool, store):
    with (
        patch.object(citations, "fetch_s2", side_effect=RuntimeError("Semantic Scholar HTTP 429")),
        patch.object(figures, "fetch_html", return_value=("done", STRUCTURED, None, "u")),
    ):
        citations.ensure(pool, SETTINGS, ["2608.26868"])

    assert store.call_args.args[2:4] == ("html", "partial")


def test_ensure_records_missing_and_failed(pool, store):
    with (
        patch.object(citations, "fetch_s2", return_value=[None]),
        patch.object(figures, "fetch_html", return_value=("done", "<p>no bibliography</p>", None, "u")),
    ):
        citations.ensure(pool, SETTINGS, ["2608.26868"])
    assert store.call_args.args[3] == "missing"

    with (
        patch.object(citations, "fetch_s2", return_value=[None]),
        patch.object(figures, "fetch_html", return_value=("failed", None, "HTTP 503", "u")),
    ):
        citations.ensure(pool, SETTINGS, ["2608.26868"])
    assert store.call_args.args[3] == "failed" and store.call_args.args[5] == "HTTP 503"


def test_ensure_skips_cached_papers(pool, store):
    with (
        patch.object(db, "citation_fetched", return_value={"2608.26868"}),
        patch.object(citations, "fetch_s2") as s2,
    ):
        citations.ensure(pool, SETTINGS, ["2608.26868"])

    s2.assert_not_called()
    store.assert_not_called()


def test_resolve_titles_links_references_to_indexed_papers():
    edges = [
        {"direction": "cites", "other_key": "title:habitat 3d", "other_arxiv_id": None, "title": "Habitat 3D"}
    ]
    with patch.object(
        db, "papers_by_norm_title", return_value=[{"arxiv_id": "2109.08238", "norm": "habitat 3d"}]
    ):
        out = citations.resolve_titles(object(), edges)

    assert out[0]["other_arxiv_id"] == "2109.08238" and out[0]["other_key"] == "arxiv:2109.08238"


def test_fetch_html_is_rate_limited_and_classifies_responses():
    limiter = MagicMock()
    with (
        patch.object(figures, "SharedMinInterval", return_value=limiter),
        patch.object(figures.redis.Redis, "from_url"),
        patch.object(figures.httpx, "get", return_value=MagicMock(status_code=404)),
    ):
        assert figures.fetch_html("https://arxiv.org/html/x", SETTINGS)[:3] == ("none", None, None)
    limiter.wait.assert_called_once()

    with (
        patch.object(figures, "SharedMinInterval", return_value=limiter),
        patch.object(figures.redis.Redis, "from_url"),
        patch.object(figures.httpx, "get", side_effect=httpx.ConnectError("down")),
    ):
        status, html, error, _ = figures.fetch_html("https://arxiv.org/html/x", SETTINGS)
    assert (status, html) == ("failed", None) and "ConnectError" in error
