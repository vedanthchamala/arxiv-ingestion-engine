"""Client for OpenAI-compatible inference servers (vLLM on the Spark, Ollama locally)."""

from dataclasses import dataclass

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings
from .text import l2_normalize

log = structlog.get_logger()

SUMMARY_SYSTEM = (
    "You summarize machine learning, computer vision and robotics papers for researchers skimming "
    "a feed. Write 2-3 plain sentences: what problem, what method, what result. No preamble, no "
    "bullet points, no hedging, no mention of 'this paper'."
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


def _ok_or_retryable(resp: httpx.Response) -> bool:
    return resp.status_code < 400 or resp.status_code == 429 or resp.status_code >= 500
