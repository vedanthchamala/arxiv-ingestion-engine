"""High-level design walkthroughs: a paper's method as a short flow of steps (input, what happens, output,
why), plus its headline results, key terms and overview figure, written by a language model from the
stored full text.

The model only writes structured data; the page draws the diagram, so the picture is always well formed.
Every claim that can be checked against the text the model was shown is checked, and what fails is
removed and listed: result numbers, terms (an acronym expansion must appear in the text), evidence
quotes and section names. Designs are generated on first view, in the background, and cached per paper
and provider, so a local model's version and Claude's can be compared side by side (decision 37)."""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import structlog
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ValidationError

from arxiv_common import db
from arxiv_common.config import Settings
from arxiv_common.inference import InferenceClient

from . import figures as figs

log = structlog.get_logger()

PROMPT_VERSION = 2
STALE_AFTER_S = 900
MAX_STEPS = 6
SOURCE_CHARS = {"local": 24_000, "claude": 60_000}
SKIP_SECTION = re.compile(
    r"related work|prior work|acknowledg|references|bibliography|appendix|supplement", re.I
)
# $ per million tokens (input, output) for cost tracking; an unknown model is recorded without a cost.
CLAUDE_PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}


class Step(BaseModel):
    name: str
    input: str
    does: str
    output: str
    why: str
    section: str = ""
    detail: str = ""
    evidence: str = ""


class Result(BaseModel):
    value: str
    label: str


class Term(BaseModel):
    term: str
    meaning: str


class Design(BaseModel):
    one_liner: str
    problem: str
    key_idea: str
    steps: list[Step]
    results: list[Result] = []
    terms: list[Term] = []
    limitations: list[str] = []
    overview_figure: int = 0


class DesignError(RuntimeError):
    pass


# -- prompts and schemas -------------------------------------------------------------------------
# Two prompts on purpose: a 7B model does best with a short, literal brief and few fields; Claude can
# carry more (per-step detail, a verbatim evidence quote, stated limitations) and keep them grounded.

LOCAL_SYSTEM = """\
You explain how the system in a research paper works, for a reader who wants the big picture before \
reading it. Use only facts stated in the paper text you are given.
- one_liner: one sentence that starts with the method's name and says what it builds and what for.
- problem: one sentence, the problem it solves.
- key_idea: one sentence, the main idea that makes it work.
- steps: 3 to 6 steps in the order data flows through the method. name: 2 to 5 words. input, does, \
output, why: one short plain phrase or sentence each; the output of a step is usually the input of the \
next. section: the title of the section that describes the step, copied exactly.
- results: up to 3 headline numbers copied exactly from the text. value: only the number and its unit \
(for example "130 KB/s" or "+12.4%"); label: what it measures.
- terms: up to 4 terms a newcomer needs, with the meaning the text gives. Never expand an acronym \
unless the text spells the expansion out.
- overview_figure: the number of the figure that shows the overall design, or 0 if none does."""

CLAUDE_SYSTEM = """\
You are writing a step-by-step explanation of a research paper's design for a reader who wants to \
understand how the system works before reading the paper. The page draws your steps as a flow diagram \
with your detail beside it, so the steps must describe a real flow of data through the method.

Ground everything in the paper text provided. If the text does not state something, leave it out: \
numbers, acronym expansions and claims must all come from the text. Write plainly; prefer the paper's \
own names for components.

- one_liner: one sentence, starting with the method's name, saying what it builds and for what.
- problem, key_idea: one or two sentences each.
- steps: 3 to 6 stages in the order data flows. name: 2 to 5 words. input and output: the concrete \
artifact (for example "RGB frames + IMU readings", "per-agent Gaussian submaps"); consecutive steps \
should connect, the output of one feeding the next. does: one sentence. why: one sentence on what \
would go wrong without the step. detail: two or three sentences on how it works, for a reader who \
wants more. evidence: a short verbatim quote, under 30 words, from the text that supports the step. \
section: the title of the section the step comes from, copied exactly.
- results: up to 4 headline numbers copied exactly. value: only the number and its unit (for example \
"130 KB/s" or "+12.4%"); label: what it measures and, if the text says, the baseline it is compared with.
- terms: up to 5 terms a newcomer needs, defined the way the paper uses them.
- limitations: up to 3 limitations the paper itself states; empty if it states none.
- overview_figure: the number of the figure that best shows the overall architecture, or 0."""


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S = {"type": "string"}
_STEP_LOCAL = {k: _S for k in ("name", "input", "does", "output", "why", "section")}
_RESULT = _obj({"value": _S, "label": _S})
_TERM = _obj({"term": _S, "meaning": _S})
LOCAL_SCHEMA = _obj(
    {
        "one_liner": _S,
        "problem": _S,
        "key_idea": _S,
        "steps": {"type": "array", "items": _obj(_STEP_LOCAL)},
        "results": {"type": "array", "items": _RESULT},
        "terms": {"type": "array", "items": _TERM},
        "overview_figure": {"type": "integer"},
    }
)
CLAUDE_SCHEMA = _obj(
    {
        "one_liner": _S,
        "problem": _S,
        "key_idea": _S,
        "steps": {"type": "array", "items": _obj({**_STEP_LOCAL, "detail": _S, "evidence": _S})},
        "results": {"type": "array", "items": _RESULT},
        "terms": {"type": "array", "items": _TERM},
        "limitations": {"type": "array", "items": _S},
        "overview_figure": {"type": "integer"},
    }
)


# -- source text ---------------------------------------------------------------------------------


@dataclass
class Source:
    text: str
    sections: list[str]
    chars: int


def build_source(title: str, chunks: list[dict], figures: list[dict], budget: int) -> Source:
    """Title, abstract and the paper's sections in order, minus related work, references and
    appendices, up to `budget` characters; then the figure captions so the model can pick one."""
    parts = [f"Title: {title}"]
    sections: list[str] = []
    used = 0
    for c in chunks:
        section = (c["section"] or "").strip()
        body = c["text"]
        if c["idx"] == 0:
            parts.append(f"Abstract:\n{body.split(chr(10), 2)[-1].strip()}")
            continue
        if SKIP_SECTION.search(section):
            continue
        first, _, rest = body.partition("\n")
        if section and first.strip() == section:
            body = rest
        if used + len(body) > budget:
            break
        if section and section not in sections:
            sections.append(section)
            parts.append(f"\n## {section}")
        parts.append(body.strip())
        used += len(body)
    if figures:
        parts.append("\nFigures:")
        parts += [f"Figure {f['n']}: {f['caption'][:240]}" for f in figures]
    text = "\n\n".join(parts)
    return Source(text=text, sections=sections, chars=len(text))


# -- providers -----------------------------------------------------------------------------------


@dataclass
class Usage:
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def providers(settings: Settings) -> list[str]:
    return ["local", "claude"] if claude_available() else ["local"]


def model_name(provider: str, settings: Settings) -> str:
    return settings.design_claude_model if provider == "claude" else settings.llm_model


def generate_local(source: Source, settings: Settings) -> tuple[dict, Usage]:
    client = InferenceClient(settings, timeout_s=600)
    try:
        last: Exception | None = None
        for _ in range(2):  # a 7B model occasionally emits truncated JSON; one retry fixes most
            try:
                data, usage = client.complete_json(LOCAL_SYSTEM, source.text, LOCAL_SCHEMA, max_tokens=2000)
                return data, Usage(
                    settings.llm_model, usage.get("prompt_tokens"), usage.get("completion_tokens")
                )
            except (ValueError, KeyError) as e:
                last = e
        raise DesignError(f"local model returned unusable JSON: {last}")
    finally:
        client.close()


def generate_claude(source: Source, settings: Settings) -> tuple[dict, Usage]:
    import anthropic

    model = settings.design_claude_model
    client = anthropic.Anthropic(timeout=300.0, max_retries=2)
    extra = {}
    if not model.startswith("claude-haiku"):
        # Server-side fallback re-runs a declined request on Anthropic's recommended model, routed by
        # the refusal category; Haiku has no server-side fallback.
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": source.text}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CLAUDE_SCHEMA}},
            **extra,
        )
    except anthropic.AuthenticationError as e:
        raise DesignError("Anthropic rejected the API key (ANTHROPIC_API_KEY)") from e
    except anthropic.RateLimitError as e:
        raise DesignError("Anthropic rate limit reached; try again in a minute") from e
    except anthropic.APIStatusError as e:
        raise DesignError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise DesignError("could not reach the Anthropic API") from e
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        raise DesignError(f"Claude declined this paper (category: {category})")
    if response.stop_reason == "max_tokens":
        raise DesignError("Claude's answer was cut off at max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise DesignError("Claude returned no text block")
    u = response.usage
    price = CLAUDE_PRICES.get(model)
    cost = (u.input_tokens * price[0] + u.output_tokens * price[1]) / 1e6 if price else None
    return json.loads(text), Usage(response.model or model, u.input_tokens, u.output_tokens, cost)


GENERATORS = {"local": generate_local, "claude": generate_claude}


# -- validation ----------------------------------------------------------------------------------

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "×": "x", "’": "'", "“": '"', "”": '"'})
_INSTRUCTION_ECHO = re.compile(r"^(design|write|explain|describe|summari[sz]e|create)\b", re.I)


def norm(s: str) -> str:
    return " ".join(s.translate(_DASHES).lower().split())


def _numbers(s: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") or "0" for n in _NUM.findall(s.translate(_DASHES))}


def _expansion(term: str, meaning: str) -> str | None:
    """If `meaning` opens by spelling out the acronym `term` (VGGT -> "Visual Geometry Grounded
    Transformer ..."), the spelled-out words; else None. Only these words must appear in the text, so a
    longer description after them is fine."""
    acronym = [c for c in term if c.isupper()]
    if len(acronym) < 2:
        return None
    words = [w for w in re.findall(r"[A-Za-z0-9][\w-]*", meaning) if w.lower() not in _GLUE]
    initials = "".join(next((c for c in w if c.isalpha()), w[0]) for w in words[: len(acronym)]).upper()
    if len(words) < 2 or initials[:3] != "".join(acronym)[:3]:
        return None
    return " ".join(words[: len(acronym)])


_GLUE = {"of", "and", "for", "the", "a", "an", "in", "on", "to", "with"}


def _words_in(phrase: str, text: str) -> bool:
    """The phrase's words in order in the normalized text, ignoring the glue words an expansion skips."""
    words = [w for w in norm(phrase).split() if w not in _GLUE]
    return re.search(r"\b" + r"\W+(?:\w+\W+){0,2}?".join(map(re.escape, words)), text) is not None


def _match_section(name: str, sections: list[str]) -> str:
    n = norm(name)
    if not n:
        return ""
    for s in sections:
        if norm(s) == n:
            return s
    for s in sections:
        if n in norm(s) or norm(s) in n:
            return s
    return ""


@dataclass
class Checked:
    design: Design
    rejected: list[dict] = field(default_factory=list)


def validate(raw: dict, source: Source, fallback_one_liner: str) -> Checked:
    try:
        d = Design.model_validate(raw)
    except ValidationError as e:
        raise DesignError(f"answer did not match the schema: {e.errors()[0]['msg']}") from e
    text = norm(source.text)
    numbers = _numbers(source.text)
    rejected: list[dict] = []

    d.steps = [s for s in d.steps if s.name.strip() and s.does.strip()][:MAX_STEPS]
    if len(d.steps) < 2:
        raise DesignError("the model described fewer than two steps")
    for s in d.steps:
        s.section = _match_section(s.section, source.sections)
        if s.evidence and norm(s.evidence).strip(' ".') not in text:
            rejected.append(
                {"kind": "evidence", "claim": s.evidence, "reason": "quote not found in the text"}
            )
            s.evidence = ""

    kept = []
    for r in d.results:
        nums = _numbers(r.value)
        if not nums or not nums <= numbers:
            rejected.append(
                {"kind": "result", "claim": f"{r.value}: {r.label}", "reason": "number not in the text"}
            )
        else:
            kept.append(r)
    d.results = kept[:4]

    kept_terms = []
    for t in d.terms:
        if norm(t.term) not in text:
            rejected.append({"kind": "term", "claim": t.term, "reason": "term not in the text"})
        elif (expansion := _expansion(t.term, t.meaning)) and not _words_in(expansion, text):
            rejected.append(
                {
                    "kind": "term",
                    "claim": f"{t.term} = {t.meaning}",
                    "reason": "acronym expansion not in the text",
                }
            )
        else:
            kept_terms.append(t)
    d.terms = kept_terms[:5]
    d.limitations = [x for x in d.limitations if x.strip()][:3]

    if not d.one_liner.strip() or _INSTRUCTION_ECHO.match(d.one_liner.strip()):
        rejected.append(
            {"kind": "one_liner", "claim": d.one_liner, "reason": "not a description of the paper"}
        )
        d.one_liner = fallback_one_liner
    return Checked(d, rejected)


# -- jobs ----------------------------------------------------------------------------------------

_executors = {
    "local": ThreadPoolExecutor(1, "design-local"),
    "claude": ThreadPoolExecutor(2, "design-claude"),
}


def _submit(provider: str, fn, *args) -> None:
    _executors[provider].submit(fn, *args)


def start(
    pool: ConnectionPool, settings: Settings, paper: dict, provider: str, retry_failed: bool = False
) -> bool:
    """Queue a generation unless an up-to-date design exists or one is already running. Failed designs
    are retried only when asked (`retry_failed`), so a broken model server doesn't loop on reloads."""
    with pool.connection() as conn:
        row = db.get_design(conn, paper["arxiv_id"], provider)
        if row and row["status"] == "failed" and not retry_failed:
            return False
        claimed = db.claim_design(
            conn,
            paper["arxiv_id"],
            provider,
            paper["version"],
            model_name(provider, settings),
            PROMPT_VERSION,
            STALE_AFTER_S,
        )
    if claimed:
        _submit(provider, run, pool, settings, paper, provider)
    return claimed


def ensure_figures(pool: ConnectionPool, settings: Settings, paper: dict) -> list[dict]:
    with pool.connection() as conn:
        row = db.get_figures(conn, paper["arxiv_id"])
    fresh = row and row["version"] == paper["version"] and row.get("parser", 1) >= figs.PARSER_VERSION
    if fresh and row["status"] in ("done", "none"):
        return row["figures"]
    if not paper.get("html_url"):
        status, found, error = "none", [], None
    else:
        status, found, error = figs.fetch_figures(paper["html_url"], settings)
    with pool.connection() as conn:
        db.store_figures(conn, paper["arxiv_id"], paper["version"], status, found, error, figs.PARSER_VERSION)
    return found


def run(pool: ConnectionPool, settings: Settings, paper: dict, provider: str) -> None:
    arxiv_id = paper["arxiv_id"]
    t0 = time.monotonic()
    blog = log.bind(arxiv_id=arxiv_id, provider=provider)
    try:
        found = ensure_figures(pool, settings, paper)
        with pool.connection() as conn:
            chunks = db.design_source(conn, arxiv_id)
        source = build_source(paper["title"], chunks, found, SOURCE_CHARS[provider])
        raw, usage = GENERATORS[provider](source, settings)
        fallback = (paper.get("summary") or paper["abstract"]).split(". ")[0].rstrip(".") + "."
        checked = validate(raw, source, fallback)
        seconds = round(time.monotonic() - t0, 1)
        with pool.connection() as conn:
            db.finish_design(
                conn,
                arxiv_id,
                provider,
                status="done",
                design=checked.design.model_dump(),
                rejected=checked.rejected,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                seconds=seconds,
            )
        blog.info(
            "design done", seconds=seconds, steps=len(checked.design.steps), rejected=len(checked.rejected)
        )
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the row and shown on the page
        blog.warning("design failed", error=str(e)[:300])
        with pool.connection() as conn:
            db.finish_design(
                conn,
                arxiv_id,
                provider,
                status="failed",
                error=str(e)[:500],
                seconds=round(time.monotonic() - t0, 1),
            )
