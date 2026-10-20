import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from arxiv_common import db
from arxiv_common.config import Settings
from arxiv_common.inference import InferenceClient
from arxiv_query_api import design, figures
from arxiv_query_api.design import Design, DesignError, Source, build_source, validate

SETTINGS = Settings()

CHUNKS = [
    {
        "idx": 0,
        "section": "abstract",
        "text": "CGS-SLAM: Collaborative SLAM\n\nWe present CGS-SLAM, a hybrid system.",
    },
    {"idx": 1, "section": "1 Introduction", "text": "1 Introduction\nMapping without GNSS is hard."},
    {"idx": 2, "section": "2 Related Work", "text": "2 Related Work\nPrior SLAM systems [3] did X."},
    {
        "idx": 3,
        "section": "3 Method",
        "text": "3 Method\nEach agent runs a metric monocular depth estimator (MMDE). "
        "The server aligns submaps with VGGT and reaches 10 FPS at 130 KB/s per agent.",
    },
    {"idx": 4, "section": "Appendix A Proofs", "text": "Appendix A Proofs\nLemma 1."},
]
FIGS = [{"n": 1, "caption": "The global architecture of our framework.", "images": ["https://x/1.png"]}]


def source() -> Source:
    return build_source("CGS-SLAM: Collaborative SLAM", CHUNKS, FIGS, budget=10_000)


def raw(**over) -> dict:
    base = {
        "one_liner": "CGS-SLAM builds a shared 3D map from several phones.",
        "problem": "Mapping without GNSS.",
        "key_idea": "Agents map locally; a server merges.",
        "steps": [
            {
                "name": "Local mapping",
                "input": "RGB + IMU",
                "does": "Builds a submap",
                "output": "Submap",
                "why": "Scale",
                "section": "3 Method",
            },
            {
                "name": "Merge",
                "input": "Submaps",
                "does": "Aligns with VGGT",
                "output": "Global map",
                "why": "Consistency",
                "section": "Method",
            },
        ],
        "results": [{"value": "10 FPS", "label": "speed"}, {"value": "42 ms", "label": "latency"}],
        "terms": [
            {"term": "MMDE", "meaning": "Metric Monocular Depth Estimator"},
            {"term": "VGGT", "meaning": "View Geometry Guided Transformation"},
            {"term": "submap", "meaning": "an agent's local map"},
            {"term": "NeRF", "meaning": "a radiance field"},
        ],
        "overview_figure": 1,
    }
    return base | over


# -- source --------------------------------------------------------------------------------------


def test_build_source_keeps_method_drops_related_work_and_appendix():
    s = source()

    assert s.text.startswith("Title: CGS-SLAM")
    assert "Abstract:\nWe present CGS-SLAM" in s.text
    assert "## 3 Method\n\nEach agent runs" in s.text
    assert "Prior SLAM systems" not in s.text and "Lemma 1" not in s.text
    assert s.sections == ["1 Introduction", "3 Method"]
    assert s.text.endswith("Figure 1: The global architecture of our framework.")


def test_build_source_respects_the_budget():
    s = build_source("T", CHUNKS, [], budget=40)

    assert "Mapping without GNSS" in s.text
    assert "Each agent runs" not in s.text


# -- validation ----------------------------------------------------------------------------------


def test_validate_drops_unsupported_numbers_and_invented_acronym_expansions():
    checked = validate(raw(), source(), "fallback.")

    d = checked.design
    assert [r.value for r in d.results] == ["10 FPS"]
    assert [t.term for t in d.terms] == ["MMDE", "submap"]
    reasons = {(x["kind"], x["reason"]) for x in checked.rejected}
    assert ("result", "number not in the text") in reasons
    assert ("term", "acronym expansion not in the text") in reasons
    assert ("term", "term not in the text") in reasons
    assert len(checked.rejected) == 3


def test_validate_matches_sections_and_caps_steps():
    steps = [raw()["steps"][0] | {"name": f"Step {i}"} for i in range(9)]
    steps[1]["section"] = "Method"
    steps[2]["section"] = "7 Discussion"

    d = validate(raw(steps=steps), source(), "f.").design

    assert len(d.steps) == design.MAX_STEPS
    assert d.steps[0].section == "3 Method" and d.steps[1].section == "3 Method"
    assert d.steps[2].section == ""


def test_validate_checks_evidence_quotes():
    steps = raw()["steps"]
    steps[0]["evidence"] = "Each agent runs a metric monocular depth estimator"
    steps[1]["evidence"] = "The server uses a transformer to align everything perfectly"

    checked = validate(raw(steps=steps), source(), "f.")

    assert checked.design.steps[0].evidence.startswith("Each agent runs")
    assert checked.design.steps[1].evidence == ""
    assert any(x["kind"] == "evidence" for x in checked.rejected)


def test_validate_replaces_an_instruction_echo_one_liner():
    checked = validate(
        raw(one_liner="Design a research paper explaining LieSpline-DP."), source(), "Fallback."
    )

    assert checked.design.one_liner == "Fallback."
    assert checked.rejected[-1]["kind"] == "one_liner"


@pytest.mark.parametrize("bad", [{"steps": []}, {"steps": "nope"}, {"one_liner": None}])
def test_validate_rejects_unusable_answers(bad):
    with pytest.raises(DesignError):
        validate(raw(**bad), source(), "f.")


def test_acronym_expansion_detection():
    assert design._expansion("VGGT", "Visual Geometry Grounded Transformer, a 3D model") == (
        "Visual Geometry Grounded Transformer"
    )
    assert design._expansion("3DGS", "3D Gaussian Splatting") == "3D Gaussian Splatting"
    assert design._expansion("TB", "Trajectory balance objective for GFlowNets") == "Trajectory balance"
    assert design._expansion("submap", "an agent's local map") is None
    assert design._expansion("VLA", "a model that maps images and text to actions") is None


def test_expansion_only_needs_the_spelled_out_words_in_the_text():
    chunks = [*CHUNKS, {"idx": 5, "section": "3 Method", "text": "We train with trajectory balance (TB)."}]
    src = build_source("T", chunks, [], budget=10_000)
    terms = [
        {"term": "TB", "meaning": "Trajectory balance objective used for training GFlowNets"},
        {"term": "MMDE", "meaning": "Metric monocular depth estimator, here Depth Pro"},
    ]

    checked = validate(raw(terms=terms), src, "f.")

    assert [t.term for t in checked.design.terms] == ["TB", "MMDE"]


# -- providers -----------------------------------------------------------------------------------


def claude_response(stop_reason="end_turn", text=None, stop_details=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        model="claude-opus-5-5",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text or "{}"),
        ],
        usage=SimpleNamespace(input_tokens=7000, output_tokens=1500),
    )


def test_claude_request_shape_and_cost():
    client = MagicMock()
    client.beta.messages.create.return_value = claude_response(text=json.dumps(raw()))
    with patch("anthropic.Anthropic", return_value=client):
        data, usage = design.generate_claude(source(), SETTINGS)

    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["system"] == design.CLAUDE_SYSTEM
    assert kwargs["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": design.CLAUDE_SCHEMA},
    }
    assert kwargs["fallbacks"] == "default" and kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert data["one_liner"].startswith("CGS-SLAM")
    assert usage.input_tokens == 7000 and usage.cost_usd == pytest.approx(0.058)


def test_claude_haiku_gets_no_server_side_fallback():
    client = MagicMock()
    client.beta.messages.create.return_value = claude_response(text=json.dumps(raw()))
    with patch("anthropic.Anthropic", return_value=client):
        from dataclasses import replace

        design.generate_claude(source(), replace(SETTINGS, design_claude_model="claude-haiku-5-5"))

    assert "fallbacks" not in client.beta.messages.create.call_args.kwargs


@pytest.mark.parametrize(("stop", "message"), [("refusal", "declined"), ("max_tokens", "cut off")])
def test_claude_refusal_and_truncation_are_errors(stop, message):
    client = MagicMock()
    client.beta.messages.create.return_value = claude_response(
        stop_reason=stop, stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None
    )
    with patch("anthropic.Anthropic", return_value=client), pytest.raises(DesignError, match=message):
        design.generate_claude(source(), SETTINGS)


def test_claude_schema_is_strict_everywhere():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])

    walk(design.CLAUDE_SCHEMA)
    walk(design.LOCAL_SCHEMA)
    assert "evidence" in design.CLAUDE_SCHEMA["properties"]["steps"]["items"]["properties"]
    assert "evidence" not in design.LOCAL_SCHEMA["properties"]["steps"]["items"]["properties"]


def test_local_retries_once_on_bad_json():
    calls = iter([ValueError("truncated"), (raw(), {"prompt_tokens": 9, "completion_tokens": 3})])

    def fake(self, system, user, schema, max_tokens):
        assert system == design.LOCAL_SYSTEM and schema == design.LOCAL_SCHEMA
        nxt = next(calls)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    with patch.object(InferenceClient, "complete_json", fake):
        data, usage = design.generate_local(source(), SETTINGS)

    assert data["key_idea"] and usage.input_tokens == 9 and usage.cost_usd is None


def test_complete_json_sends_a_json_schema_response_format():
    client = InferenceClient(SETTINGS)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"a": 1}'}}],
        "usage": {"prompt_tokens": 5},
    }
    with patch.object(client._llm, "post", return_value=resp) as post:
        data, usage = client.complete_json("sys", "user", {"type": "object"}, max_tokens=50)

    body = post.call_args.kwargs["json"]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": {"type": "object"}},
    }
    assert data == {"a": 1} and usage == {"prompt_tokens": 5}
    resp.json.return_value["choices"][0]["finish_reason"] = "length"
    with patch.object(client._llm, "post", return_value=resp), pytest.raises(ValueError):
        client.complete_json("sys", "user", {}, max_tokens=50)


# -- jobs ----------------------------------------------------------------------------------------

PAPER = {
    "arxiv_id": "2608.26868",
    "version": 1,
    "title": "CGS-SLAM: Collaborative SLAM",
    "abstract": "We present CGS-SLAM. It maps.",
    "summary": None,
    "html_url": "https://arxiv.org/html/2608.26868v1",
}


@pytest.fixture
def pool():
    @contextmanager
    def connection():
        yield object()

    return SimpleNamespace(connection=connection)


def test_run_stores_a_checked_design(pool):
    with (
        patch.object(
            db, "get_figures", return_value={"version": 1, "status": "done", "figures": FIGS, "parser": 2}
        ),
        patch.object(db, "design_source", return_value=CHUNKS),
        patch.object(db, "finish_design") as finish,
        patch.dict(design.GENERATORS, {"local": lambda src, s: (raw(), design.Usage("m", 10, 5))}),
    ):
        design.run(pool, SETTINGS, PAPER, "local")

    kwargs = finish.call_args.kwargs
    assert kwargs["status"] == "done"
    assert kwargs["design"]["steps"][0]["name"] == "Local mapping"
    assert len(kwargs["rejected"]) == 3 and kwargs["input_tokens"] == 10


def test_run_records_failures(pool):
    def boom(src, s):
        raise DesignError("model server down")

    with (
        patch.object(
            db, "get_figures", return_value={"version": 1, "status": "none", "figures": [], "parser": 2}
        ),
        patch.object(db, "design_source", return_value=CHUNKS),
        patch.object(db, "finish_design") as finish,
        patch.dict(design.GENERATORS, {"local": boom}),
    ):
        design.run(pool, SETTINGS, PAPER, "local")

    assert finish.call_args.kwargs["status"] == "failed"
    assert "model server down" in finish.call_args.kwargs["error"]


def test_figures_are_fetched_once_per_paper_version(pool):
    with (
        patch.object(db, "get_figures", return_value=None),
        patch.object(db, "store_figures") as store,
        patch.object(figures, "fetch_figures", return_value=("done", FIGS, None)) as fetch,
    ):
        assert design.ensure_figures(pool, SETTINGS, PAPER) == FIGS

    fetch.assert_called_once_with("https://arxiv.org/html/2608.26868v1", SETTINGS)
    store.assert_called_once()
    cached = {"version": 1, "status": "done", "figures": FIGS, "parser": figures.PARSER_VERSION}
    with (
        patch.object(db, "get_figures", return_value=cached),
        patch.object(figures, "fetch_figures") as fetch,
    ):
        design.ensure_figures(pool, SETTINGS, PAPER)
    fetch.assert_not_called()


def test_figures_parsed_by_an_older_parser_are_fetched_again(pool):
    old = {"version": 1, "status": "done", "figures": [], "parser": figures.PARSER_VERSION - 1}
    with (
        patch.object(db, "get_figures", return_value=old),
        patch.object(db, "store_figures") as store,
        patch.object(figures, "fetch_figures", return_value=("done", FIGS, None)) as fetch,
    ):
        assert design.ensure_figures(pool, SETTINGS, PAPER) == FIGS

    fetch.assert_called_once()
    assert store.call_args.args[-1] == figures.PARSER_VERSION


def test_start_queues_only_when_claimed_and_never_loops_on_failure(pool):
    with (
        patch.object(db, "get_design", return_value=None),
        patch.object(db, "claim_design", return_value=True) as claim,
        patch.object(design, "_submit") as submit,
    ):
        assert design.start(pool, SETTINGS, PAPER, "local")
    assert claim.call_args.args[2:] == (
        "local",
        1,
        SETTINGS.llm_model,
        design.PROMPT_VERSION,
        design.STALE_AFTER_S,
    )
    submit.assert_called_once()

    with (
        patch.object(db, "get_design", return_value={"status": "failed"}),
        patch.object(db, "claim_design") as claim,
        patch.object(design, "_submit") as submit,
    ):
        assert not design.start(pool, SETTINGS, PAPER, "local")
        claim.assert_not_called()
        assert design.start(pool, SETTINGS, PAPER, "local", retry_failed=True) == claim.return_value


def test_claude_is_offered_only_with_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert design.providers(SETTINGS) == ["local"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert design.providers(SETTINGS) == ["local", "claude"]


# -- figures -------------------------------------------------------------------------------------

LATEXML = """
<figure id="S3.F1" class="ltx_figure"><img src="2608.26868v1/arch.png" class="ltx_graphics">
  <figcaption class="ltx_caption"><span class="ltx_tag ltx_tag_figure"><span>Figure 1</span>: </span>
  <span>The global architecture of our <b>framework</b>.</span></figcaption></figure>
<figure id="S4.T1" class="ltx_table"><img src="2608.26868v1/table.png">
  <figcaption>Table 1: numbers</figcaption></figure>
<figure id="S4.F2" class="ltx_figure">
  <figure class="ltx_figure ltx_figure_panel"><img src="2608.26868v1/a.png"></figure>
  <figure class="ltx_figure ltx_figure_panel"><img src="2608.26868v1/b.png"></figure>
  <figcaption><span class="ltx_tag ltx_tag_figure">Figure 2: </span>Two panels &amp; results.
  </figcaption></figure>
<figure id="S5.F4" class="ltx_figure"><img src="2608.26868v1/ieee.png">
  <figcaption><span class="ltx_tag ltx_tag_figure">Fig. 4: </span>IEEE style caption.</figcaption></figure>
<figure id="A1.F3" class="ltx_figure"><img src="2608.26868v1/s.png">
  <figcaption><span class="ltx_tag ltx_tag_figure">Figure S1: </span>Supplementary.</figcaption></figure>
"""


def test_parse_figures_from_latexml():
    found = figures.parse_figures(LATEXML, "https://arxiv.org/html/2608.26868v1")

    assert [f["n"] for f in found] == [1, 2, 4]
    assert found[2]["caption"] == "IEEE style caption."
    assert found[0]["images"] == ["https://arxiv.org/html/2608.26868v1/arch.png"]
    assert found[0]["caption"] == "The global architecture of our framework."
    assert found[1]["images"] == [
        "https://arxiv.org/html/2608.26868v1/a.png",
        "https://arxiv.org/html/2608.26868v1/b.png",
    ]
    assert found[1]["caption"] == "Two panels & results."


def test_overview_figure_choice():
    found = [
        {"n": 1, "caption": "Qualitative results on scene A.", "images": ["1"]},
        {"n": 2, "caption": "Overview of the proposed pipeline.", "images": ["2"]},
        {"n": 3, "caption": "Ablation.", "images": ["3"]},
    ]
    assert figures.overview_figure(found, 3)["n"] == 3
    assert figures.overview_figure(found, 9)["n"] == 2
    assert figures.overview_figure(found[::2])["n"] == 1
    assert figures.overview_figure([]) is None


def test_design_model_defaults():
    d = Design(one_liner="a", problem="b", key_idea="c", steps=[])
    assert d.results == [] and d.limitations == [] and d.overview_figure == 0


def test_a_result_with_a_unit_must_appear_with_that_unit():
    results = [
        {"value": "130 KB/s", "label": "uplink"},
        {"value": "130 ms", "label": "an example value copied into the wrong paper"},
        {"value": "10", "label": "a bare number"},
    ]

    checked = validate(raw(results=results), source(), "f.")

    assert [r.value for r in checked.design.results] == ["130 KB/s", "10"]
    assert checked.rejected[0]["reason"] == "number not in the text with this unit"


def test_a_range_needs_its_unit_on_the_number_that_carries_it():
    chunks = [*CHUNKS, {"idx": 5, "section": "3 Method", "text": "Final maps take between 25 and 75 MB."}]
    src = build_source("T", chunks, [], budget=10_000)
    results = [{"value": "25-75 MB", "label": "map size"}, {"value": "84.1%", "label": "not there"}]

    checked = validate(raw(results=results), src, "f.")

    assert [r.value for r in checked.design.results] == ["25-75 MB"]


def test_prompts_carry_no_example_values():
    assert "130" not in design.LOCAL_SYSTEM and "130" not in design.CLAUDE_SYSTEM
