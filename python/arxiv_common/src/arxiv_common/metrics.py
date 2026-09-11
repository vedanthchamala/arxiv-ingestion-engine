"""Prometheus metrics shared by the worker and the query API. The names are a contract with the
Rust stages and observability/grafana; change them there too."""

import structlog
from prometheus_client import Counter, Histogram, disable_created_metrics, start_http_server

disable_created_metrics()

_WORKER_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300)

WORKER_MESSAGES = Counter("worker_messages_total", "Messages handled by outcome", ["group", "result"])
WORKER_PROCESS_SECONDS = Histogram(
    "worker_process_seconds", "Whole-message handling time", ["group"], buckets=_WORKER_BUCKETS
)
WORKER_EMBED_SECONDS = Histogram(
    "worker_embed_seconds", "Embedding call time per message", ["group"], buckets=_WORKER_BUCKETS
)
WORKER_SUMMARIZE_SECONDS = Histogram(
    "worker_summarize_seconds", "Summary call time per message", ["group"], buckets=_WORKER_BUCKETS
)
WORKER_CHUNKS = Counter("worker_chunks_total", "Chunks embedded and stored", ["group"])
WORKER_DEAD_LETTERS = Counter(
    "worker_dead_letters_total", "Messages sent to papers.failed", ["group", "reason"]
)
WORKER_ATTEMPTS = Counter("worker_attempts_total", "Processing attempts including retries", ["group"])

API_SEARCH_SECONDS = Histogram("api_search_seconds", "Server-side /search time", ["cached"])
API_SEARCH = Counter("api_search_total", "/search requests", ["cached"])
API_CACHE_EVENTS = Counter("api_cache_events_total", "Semantic cache lookups", ["result"])
API_EMBED_SECONDS = Histogram("api_embed_seconds", "Query embedding call time")
API_DB_SEARCH_SECONDS = Histogram("api_db_search_seconds", "pgvector search time")


def init_worker_labels(group: str) -> None:
    """Expose every fixed label combination at 0 so rates and stat panels work before the first event."""
    for result in ("stored", "skipped", "failed"):
        WORKER_MESSAGES.labels(group, result)
    for reason in ("poison", "retries_exhausted", "db_unavailable"):
        WORKER_DEAD_LETTERS.labels(group, reason)
    WORKER_CHUNKS.labels(group)
    WORKER_ATTEMPTS.labels(group)


def init_api_labels() -> None:
    for cached in ("true", "false"):
        API_SEARCH.labels(cached)
    for result in ("hit", "miss"):
        API_CACHE_EVENTS.labels(result)


def start_metrics_server(port: int) -> None:
    """Serve /metrics on a background thread; port 0 disables it. A busy port is a warning, not a
    reason to stop ingesting."""
    if not port:
        return
    try:
        start_http_server(port)
    except OSError as e:
        structlog.get_logger().warning(
            "metrics port unavailable; continuing without /metrics", port=port, error=str(e)
        )
