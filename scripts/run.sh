#!/usr/bin/env bash
# Host-side supervisor for the steady-state pipeline (poller loop, fetcher, both workers, API).
# Logs in data/logs/<name>.log. Processes are found by command line, not by PID file, so a
# launcher wrapper (cargo run, uv run) dying never orphans the real process. Usage:
#   scripts/run.sh start [name...]   # default: all five
#   scripts/run.sh stop  [name...]   # SIGINT; the in-flight message finishes and commits
#   scripts/run.sh status | logs <name>
# API_PORT=8001 scripts/run.sh start api   # move the API off :8000
set -euo pipefail
cd "$(dirname "$0")/.."
ALL=(poller fetcher worker worker-abstract api)
mkdir -p data/logs

pattern() {
  case "$1" in
    poller)          echo 'target/release/poller' ;;
    fetcher)         echo 'target/release/fetcher' ;;
    worker)          echo '\.venv/bin/arxiv-worker --topic [^ ]* --group worker( |$)' ;;
    worker-abstract) echo '\.venv/bin/arxiv-worker .*--group worker-abstract' ;;
    api)             echo '\.venv/bin/arxiv-query-api' ;;
    *) echo "unknown component: $1" >&2; exit 1 ;;
  esac
}
pids() { pgrep -f "$(pattern "$1")" 2>/dev/null || true; }

start() {
  for n in "$@"; do
    if [[ -n "$(pids "$n")" ]]; then echo "running  $n (pid $(pids "$n" | tr '\n' ' '))"; continue; fi
    ( nohup scripts/pipeline.sh "$n" >>"data/logs/$n.log" 2>&1 & )
    echo "started  $n -> data/logs/$n.log"
  done
}
stop() {
  for n in "$@"; do
    local p; p=$(pids "$n")
    if [[ -z "$p" ]]; then echo "stopped  $n"; continue; fi
    kill -INT $p 2>/dev/null || true
    for _ in $(seq 1 120); do [[ -z "$(pids "$n")" ]] && break; sleep 1; done
    p=$(pids "$n"); [[ -n "$p" ]] && { echo "still up after 120 s, killing $n"; kill -KILL $p 2>/dev/null || true; }
    echo "stopped  $n"
  done
}
status() {
  for n in "${ALL[@]}"; do
    local p; p=$(pids "$n" | head -1)
    if [[ -n "$p" ]]; then
      echo "up       $n  pid $p  since $(ps -o lstart= -p "$p" | sed 's/^ *//')"
    else echo "down     $n"; fi
  done
}

cmd="${1:-status}"; shift || true
case "$cmd" in
  start)  start "${@:-${ALL[@]}}" ;;
  stop)   stop "${@:-${ALL[@]}}" ;;
  status) status ;;
  logs)   tail -n 50 -f "data/logs/${1:?name}.log" ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
