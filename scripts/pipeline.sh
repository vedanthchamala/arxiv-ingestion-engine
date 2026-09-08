#!/usr/bin/env bash
# Run one pipeline component with the repo's .env. Usage:
#   scripts/pipeline.sh poller            # poll every 15 min (add --once for one cycle)
#   scripts/pipeline.sh fetcher           # full text: papers.new -> papers.chunked (one instance only!)
#   scripts/pipeline.sh worker-abstract   # fast path: papers.new -> pgvector (abstract as chunk 0)
#   scripts/pipeline.sh worker            # full-text path: papers.chunked -> pgvector (+ summaries)
#   scripts/pipeline.sh api               # query API on :8000
# Extra args are passed through.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a
cmd="${1:-}"; shift || true
case "$cmd" in
  poller)          exec cargo run --release --manifest-path rust/Cargo.toml -q -p poller -- "$@" ;;
  fetcher)         exec cargo run --release --manifest-path rust/Cargo.toml -q -p fetcher -- "$@" ;;
  worker-abstract) exec uv run --project python -q arxiv-worker --topic "${TOPIC_PAPERS_NEW:-papers.new}" --group worker-abstract --no-summary "$@" ;;
  worker)          exec uv run --project python -q arxiv-worker --topic "${TOPIC_PAPERS_CHUNKED:-papers.chunked}" --group worker "$@" ;;
  api)             exec uv run --project python -q arxiv-query-api "$@" ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
