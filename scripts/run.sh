#!/usr/bin/env bash
# Host-side supervisor for the steady-state pipeline (poller loop, fetcher, both workers, API).
# Logs in data/logs/<name>.log, PIDs in data/run/<name>.pid. Usage:
#   scripts/run.sh start [name...]   # default: all five
#   scripts/run.sh stop  [name...]   # SIGINT/SIGTERM; in-flight message finishes and commits
#   scripts/run.sh status | logs <name>
# API_PORT=8001 scripts/run.sh start api   # move the API off :8000
set -euo pipefail
cd "$(dirname "$0")/.."
ALL=(poller fetcher worker worker-abstract api)
mkdir -p data/logs data/run

pid_of() { [[ -f "data/run/$1.pid" ]] && cat "data/run/$1.pid" || true; }
alive()  { local p; p=$(pid_of "$1"); [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

start() {
  for n in "$@"; do
    if alive "$n"; then echo "running  $n (pid $(pid_of "$n"))"; continue; fi
    ( nohup scripts/pipeline.sh "$n" >>"data/logs/$n.log" 2>&1 & echo $! >"data/run/$n.pid" )
    echo "started  $n (pid $(cat "data/run/$n.pid")) -> data/logs/$n.log"
  done
}
stop() {
  for n in "$@"; do
    if ! alive "$n"; then echo "stopped  $n"; rm -f "data/run/$n.pid"; continue; fi
    local p; p=$(pid_of "$n")
    pkill -INT -P "$p" 2>/dev/null || true; kill -INT "$p" 2>/dev/null || true
    for _ in $(seq 1 120); do alive "$n" || break; sleep 1; done
    alive "$n" && { echo "still up after 120 s, killing $n"; pkill -KILL -P "$p" 2>/dev/null || true; kill -KILL "$p" 2>/dev/null || true; }
    rm -f "data/run/$n.pid"; echo "stopped  $n"
  done
}
status() {
  for n in "${ALL[@]}"; do
    if alive "$n"; then
      local p; p=$(pid_of "$n"); local since; since=$(ps -o lstart= -p "$p" 2>/dev/null | sed 's/^ *//')
      echo "up       $n  pid $p  since $since"
    else echo "down     $n"; fi
  done
}

cmd="${1:-status}"; shift || true
case "$cmd" in
  start)  start "${@:-${ALL[@]}}" ;;
  stop)   stop "${@:-${ALL[@]}}" ;;
  status) status ;;
  logs)   tail -n 50 -f "data/logs/${1:?name}.log" ;;
  *) sed -n '2,6p' "$0"; exit 1 ;;
esac
