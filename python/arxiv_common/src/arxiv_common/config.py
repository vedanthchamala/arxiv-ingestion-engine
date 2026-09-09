import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    kafka_brokers: str = field(default_factory=lambda: _env("KAFKA_BROKERS", "localhost:19092"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379"))
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", "postgresql://arxiv:arxiv@localhost:5432/arxiv")
    )
    topic_new: str = field(default_factory=lambda: _env("TOPIC_PAPERS_NEW", "papers.new"))
    topic_chunked: str = field(default_factory=lambda: _env("TOPIC_PAPERS_CHUNKED", "papers.chunked"))
    topic_failed: str = field(default_factory=lambda: _env("TOPIC_PAPERS_FAILED", "papers.failed"))
    embedding_url: str = field(default_factory=lambda: _env("EMBEDDING_URL", "http://localhost:11434/v1"))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "bge-m3"))
    embedding_dim: int = field(default_factory=lambda: int(_env("EMBEDDING_DIM", "1024")))
    llm_url: str = field(default_factory=lambda: _env("LLM_URL", "http://localhost:11434/v1"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "qwen3:8b"))
    summary_enabled: bool = field(default_factory=lambda: _env("SUMMARY_ENABLED", "true").lower() == "true")
    schema_path: str | None = field(default_factory=lambda: os.environ.get("ARXIV_SCHEMA_PATH"))
    cache_enabled: bool = field(default_factory=lambda: _env("CACHE_ENABLED", "true").lower() == "true")
    cache_ttl_secs: int = field(default_factory=lambda: int(_env("CACHE_TTL_SECS", "3600")))
    cache_distance: float = field(default_factory=lambda: float(_env("CACHE_DISTANCE", "0.12")))
