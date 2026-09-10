"""Cross-process arXiv request budget: one request every N seconds across everything we run.

`SharedMinInterval` reserves send slots in Redis through schemas/ratelimit.lua, the same script the
Rust poller and fetcher use, so a Python producer (backfill) can run alongside them. `LocalMinInterval`
is the in-process fallback and the `--local-ratelimit` option.
"""

import os
import time
from functools import lru_cache
from pathlib import Path

import redis
import structlog

DEFAULT_KEY = "arxiv:ratelimit"
# Slots are reserved min + guard apart (same constant as rust/common/src/ratelimit.rs): a sleeper can
# wake a few ms late while the next process lands exactly on its slot, so without a guard consecutive
# sends measure min - jitter. With it the configured interval is a true floor; cost 1.7% at 3 s.
SLOT_GUARD_MS = 50
log = structlog.get_logger()


def script_path() -> Path:
    override = os.environ.get("ARXIV_RATELIMIT_SCRIPT_PATH")
    if override:
        return Path(override)
    # <repo>/python/arxiv_common/src/arxiv_common/ratelimit.py -> <repo>/schemas/ratelimit.lua
    return Path(__file__).resolve().parents[4] / "schemas" / "ratelimit.lua"


@lru_cache(maxsize=1)
def script_source() -> str:
    return script_path().read_text()


class LocalMinInterval:
    """Minimum spacing between requests inside this process only."""

    def __init__(self, min_interval_s: float) -> None:
        self.min = min_interval_s
        self._last: float | None = None

    def wait(self) -> float:
        """Block until this process may send; returns the seconds slept."""
        waited = 0.0
        if self._last is not None:
            waited = max(0.0, self._last + self.min - time.monotonic())
            if waited > 0:
                time.sleep(waited)
        self.mark_sent()
        return waited

    def mark_sent(self) -> None:
        self._last = time.monotonic()


class SharedMinInterval:
    """Reserves the next free slot on a Redis key shared by every arXiv-facing process.

    Reservation is atomic and FIFO (see the script header), so callers never race. If Redis is
    unreachable the call warns and falls back to in-process spacing: a Redis blip must never stop
    a harvest, but must never skip the spacing either.
    """

    def __init__(self, client: redis.Redis, min_interval_s: float, key: str = DEFAULT_KEY) -> None:
        self.client = client
        self.key = key
        self.interval_ms = int(round(min_interval_s * 1000)) + SLOT_GUARD_MS
        self._script = client.register_script(script_source())
        self._fallback = LocalMinInterval(min_interval_s)

    def reserve(self) -> float:
        """Claim the next slot without sleeping; returns the seconds to wait before sending."""
        ms = self._script(keys=[self.key], args=[self.interval_ms])
        return int(ms) / 1000

    def wait(self) -> float:
        """Block until the reserved slot; returns the seconds slept."""
        try:
            waited = self.reserve()
        except redis.exceptions.RedisError as e:
            log.warning("shared rate limiter unavailable; in-process spacing for this request", error=str(e))
            return self._fallback.wait()
        if waited > 0:
            time.sleep(waited)
        self._fallback.mark_sent()
        return waited
