import time
import uuid

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from arxiv_common.config import Settings
from arxiv_common.ratelimit import LocalMinInterval, SharedMinInterval, script_path


@pytest.fixture
def client():
    c = redis.Redis.from_url(Settings().redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        c.ping()
    except redis.exceptions.RedisError:
        pytest.skip("redis unreachable")
    return c


def test_script_is_the_shared_file():
    assert script_path().name == "ratelimit.lua"
    assert "redis.call('TIME')" in script_path().read_text()


def test_lua_contract_back_to_back(client):
    key = f"arxiv:ratelimit:test:{uuid.uuid4()}"
    lim = SharedMinInterval(client, 0.2, key)
    try:
        assert lim.reserve() == 0.0
        second = lim.reserve()
        assert 0.2 <= second <= 0.25, second
        third = lim.reserve()
        assert 0.45 <= third <= 0.5, third
        assert 0 < client.pttl(key) <= 3_600_000
    finally:
        client.delete(key)


def test_wait_sleeps_the_reserved_time(client):
    key = f"arxiv:ratelimit:test:{uuid.uuid4()}"
    lim = SharedMinInterval(client, 0.1, key)
    try:
        t0 = time.monotonic()
        first, second = lim.wait(), lim.wait()
        assert first == 0.0 and 0.1 <= second <= 0.15
        assert time.monotonic() - t0 >= 0.1
    finally:
        client.delete(key)


def test_falls_back_to_local_spacing_when_redis_is_down():
    # No client-side retries: redis-py would otherwise spend seconds backing off before each
    # fallback, which is safe (slower, never faster) but hides the spacing this test measures.
    dead = redis.Redis(
        host="127.0.0.1",
        port=1,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
        retry=Retry(NoBackoff(), 0),
    )
    lim = SharedMinInterval(dead, 0.05, "arxiv:ratelimit:test:dead")
    t0 = time.monotonic()
    assert lim.wait() == 0.0
    assert lim.wait() > 0.0
    assert time.monotonic() - t0 >= 0.05


def test_local_min_interval_spaces_calls():
    lim = LocalMinInterval(0.05)
    t0 = time.monotonic()
    lim.wait()
    lim.wait()
    lim.wait()
    assert time.monotonic() - t0 >= 0.1
