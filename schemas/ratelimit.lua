-- arXiv request budget: one atomic send-slot reservation per call.
--
-- Contract (implemented by rust/common/src/ratelimit.rs and python/arxiv_common/ratelimit.py):
--   KEYS[1]  budget key, default "arxiv:ratelimit". Holds the most recently reserved send slot in
--            Redis-server milliseconds (redis.call('TIME')), so every process shares one clock and
--            no host clock ever matters.
--   ARGV[1]  minimum spacing between requests, in ms (3000 for arXiv).
--   ARGV[2]  optional key TTL in ms (default 3600000 = 1 h). The TTL is refreshed on every
--            reservation, so a key left behind by a crash expires instead of living forever, while
--            a queue of waiting callers (slot far ahead of now) is never cut short.
--   returns  integer ms the caller must sleep before sending (0 = send now).
--
-- This reserves rather than checks: each call claims slot = max(last + interval, now), stores it, and
-- returns slot - now. Concurrent callers in any process therefore get distinct slots in arrival order
-- (FIFO) and never race or retry. A caller that reserves and then does not send just leaves a gap.
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local interval = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2]) or 3600000
local last = tonumber(redis.call('GET', KEYS[1]))
local slot = now
if last and last + interval > slot then
  slot = last + interval
end
redis.call('SET', KEYS[1], slot, 'PX', ttl)
return slot - now
