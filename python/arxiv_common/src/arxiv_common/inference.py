"""Client for OpenAI-compatible inference servers (vLLM on the Spark, Ollama locally)."""

import json
from dataclasses import dataclass

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings
from .text import l2_normalize

log = structlog.get_logger()

# The worked example matters: without it qwen2.5-7b opened 8 of 8 summaries with "The paper ...";
# with it, 1 of 16, and length fell from 65 to 40 words on average.
SUMMARY_SYSTEM = (
    "You summarize machine learning, computer vision and robotics papers for researchers skimming a feed. "
    "Write two or three plain sentences, at most 70 words in total: the problem, the method (by name if it "
    "has one) and the main result, with a number if the abstract gives one. Start with the method's name or "
    "the problem, never with 'The paper', 'This paper' or 'The authors'. No preamble, no bullet points, no "
    "hedging.\n\n"
    'Example of the style: "FlowBot learns articulated-object manipulation from 3D flow predicted on point '
    "clouds, avoiding per-object demonstrations. A single policy trained in simulation opens unseen drawers "
    'and doors on a real robot, succeeding in 78% of trials versus 41% for the best imitation baseline."'
)


class InferenceError(RuntimeError):
    pass


@dataclass
class InferenceClient:
    settings: Settings
    timeout_s: float = 300.0
    embed_batch: int = 64

    def __post_init__(self) -> None:
        self._embed = httpx.Client(base_url=self.settings.embedding_url, timeout=self.timeout_s)
        self._llm = httpx.Client(base_url=self.settings.llm_url, timeout=self.timeout_s)

    def close(self) -> None:
        self._embed.close()
        self._llm.close()

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.embed_batch):
            out.extend(self._embed_batch(texts[i : i + self.embed_batch]))
        return out

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = self._embed.post("/embeddings", json={"model": self.settings.embedding_model, "input": texts})
        if not _ok_or_retryable(resp):
            raise InferenceError(f"embeddings {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        if len(data) != len(texts):
            raise InferenceError(f"embeddings returned {len(data)} vectors for {len(texts)} inputs")
        vecs = [l2_normalize(d["embedding"]) for d in data]
        dim = self.settings.embedding_dim
        for v in vecs:
            if len(v) != dim:
                raise InferenceError(f"embedding dim {len(v)} != configured {dim}")
        return vecs

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def summarize(self, title: str, abstract: str, intro: str | None = None) -> str:
        user = f"Title: {title}\n\nAbstract: {abstract}"
        if intro:
            user += f"\n\nIntroduction (excerpt): {intro[:6000]}"
        body = {
            "model": self.settings.llm_model,
            "messages": [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}],
            "max_tokens": 220,
            "temperature": 0.2,
        }
        resp = self._llm.post("/chat/completions", json=body)
        if not _ok_or_retryable(resp):
            raise InferenceError(f"chat {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if not content:
            raise InferenceError("empty summary")
        return content

    def complete_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 2000
    ) -> tuple[dict, dict]:
        """One chat completion constrained to a JSON schema (`response_format`, supported by vLLM and
        Ollama). Returns (parsed object, usage). ValueError on truncated or unparseable output."""
        body = {
            "model": self.settings.llm_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
        }
        resp = self._llm.post("/chat/completions", json=body)
        if not _ok_or_retryable(resp):
            raise InferenceError(f"chat {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError(f"output truncated at max_tokens={max_tokens}")
        return json.loads(choice["message"]["content"]), data.get("usage") or {}


def _ok_or_retryable(resp: httpx.Response) -> bool:
    return resp.status_code < 400 or resp.status_code == 429 or resp.status_code >= 500
