#!/usr/bin/env bash
# One markdown row of live-run evidence for STATUS.md: poller cycles, produced papers, fetcher and
# worker totals, DB counts, consumer lag, DLQ size. Reads the Prometheus exporters and the compose stack.
set -euo pipefail
cd "$(dirname "$0")/.."
m() { { curl -sf "localhost:$1/metrics" 2>/dev/null || true; } | { grep -E "^$2" || true; } | awk '{s+=$NF} END {printf "%d", s}'; }
pg() { docker compose exec -T postgres psql -qtA -U arxiv -d arxiv -c "$1" | tr -d '[:space:]'; }
lag() { docker compose exec -T redpanda rpk group describe "$1" 2>/dev/null | awk '$1=="'"$2"'" {l+=$6} END {print l+0}'; }
hw() { docker compose exec -T redpanda rpk topic describe "$1" -p 2>/dev/null | awk 'NR>1 {s+=$6} END {print s+0}'; }
cycles=$(m 9101 'poller_cycles_total'); produced=$(m 9101 'poller_papers_total\{.*result="produced"')
fhtml=$(m 9102 'fetcher_papers_total\{result="html"'); fpdf=$(m 9102 'fetcher_papers_total\{result="pdf"')
fabs=$(m 9102 'fetcher_papers_total\{result="abstract"'); ffail=$(m 9102 'fetcher_papers_total\{result="failed"')
wst=$(m 9103 'worker_messages_total\{.*result="stored"'); wfl=$(m 9103 'worker_messages_total\{.*result="failed"')
ast=$(m 9104 'worker_messages_total\{.*result="stored"')
papers=$(pg "select count(*) from papers"); chunks=$(pg "select count(*) from chunks")
full=$(pg "select count(*) from papers where source in ('html','pdf')"); summ=$(pg "select count(*) from papers where summary is not null")
echo "| $(date -u +%Y-%m-%dT%H:%MZ) | poller cycles $cycles, produced $produced (session) | fetcher html $fhtml / pdf $fpdf / abstract $fabs / failed $ffail | worker stored $wst, failed $wfl; abstract worker stored $ast | DB: $papers papers, $chunks chunks, $full full-text, $summ summarized | lag: fetcher $(lag fetcher papers.new), worker $(lag worker papers.chunked), worker-abstract $(lag worker-abstract papers.new) | DLQ $(hw papers.failed) |"
