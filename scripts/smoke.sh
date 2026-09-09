#!/usr/bin/env bash
# End-to-end consistency checks against the running stack. Exits non-zero on any mismatch.
set -euo pipefail
cd "$(dirname "$0")/.."
API="${API:-http://localhost:8000}"
fail=0
ok()   { echo "ok    $*"; }
bad()  { echo "FAIL  $*"; fail=1; }

pg() { docker compose exec -T postgres psql -qtA -U arxiv -d arxiv -c "$1"; }
hw() { docker compose exec -T redpanda rpk topic describe "$1" -p | awk 'NR>1 {s+=$6} END {print s+0}'; }

new=$(hw papers.new); seen=$(docker compose exec -T redis redis-cli SCARD arxiv:seen | tr -d '[:space:]')
[[ "$new" == "$seen" ]] && ok "papers.new messages ($new) == seen-set ($seen)" || bad "papers.new $new != seen $seen"

papers=$(pg "select count(*) from papers"); chunks=$(pg "select count(*) from chunks")
orphans=$(pg "select count(*) from chunks c where not exists (select 1 from papers p where p.arxiv_id=c.arxiv_id)")
dupidx=$(pg "select count(*) from (select arxiv_id, idx from chunks group by 1,2 having count(*)>1) d")
[[ "$orphans" == "0" ]] && ok "no orphan chunks (papers=$papers chunks=$chunks)" || bad "$orphans orphan chunks"
[[ "$dupidx" == "0" ]] && ok "no duplicate (arxiv_id, idx)" || bad "$dupidx duplicate chunk indexes"
noauth=$(pg "select count(*) from papers p where not exists (select 1 from paper_authors a where a.arxiv_id=p.arxiv_id)")
[[ "$noauth" == "0" ]] && ok "every paper has authors" || bad "$noauth papers without authors"

failed=$(hw papers.failed)
echo "info  papers.failed messages: $failed"
echo "info  consumer lag:"; docker compose exec -T redpanda rpk group describe worker 2>/dev/null | awk '/papers/ {lag+=$6} END {print "      worker lag =", lag+0}'

if curl -sf "$API/health" >/dev/null; then
  res=$(curl -sf -X POST "$API/search" -H 'content-type: application/json' \
        -d '{"query":"diffusion policy for robot manipulation","k":5,"categories":["cs.RO"]}')
  n=$(echo "$res" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(sum(1 for h in r["hits"] if "cs.RO" in h["categories"]))')
  [[ "$n" -ge 1 ]] && ok "search returns cs.RO hits ($n/5)" || bad "search returned no cs.RO hits"
  echo "$res" | python3 - <<'PY'
import json, sys
for h in json.load(sys.stdin)["hits"][:3]:
    print(f"      {h['score']:.3f} {h['primary_category']:6} {h['title'][:70]}")
PY
else
  echo "skip  query API not running at $API"
fi
exit $fail
