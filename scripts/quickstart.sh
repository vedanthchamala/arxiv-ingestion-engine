#!/usr/bin/env bash
# From a fresh clone to a running pipeline and web UI. Everything runs in containers except Ollama,
# which stays on the host for its GPU / Metal access. Safe to re-run: it rebuilds and restarts.
#   scripts/quickstart.sh                  # poll arXiv, fetch full text, embed, summarize, serve the UI
#   scripts/quickstart.sh --no-summaries   # skip the 4.7 GB summary model; search works the same
#   scripts/quickstart.sh --no-ingest      # UI + workers on the existing database, no arXiv traffic
set -euo pipefail
cd "$(dirname "$0")/.."

SUMMARIES=true
INGEST=true
for a in "$@"; do
  case "$a" in
    --no-summaries) SUMMARIES=false ;;
    --no-ingest) INGEST=false ;;
    -h | --help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (see --help)" >&2; exit 1 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
envval() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' || true; }

say "Checking prerequisites"
command -v docker >/dev/null || die "Docker is not installed: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop (or dockerd) and re-run."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose version)."
OLLAMA="${OLLAMA_URL:-http://localhost:11434}"
curl -sf "$OLLAMA/api/tags" >/dev/null || die "Ollama is not answering at $OLLAMA.
  Install it from https://ollama.com/download and start it. On Linux, start it with
  OLLAMA_HOST=0.0.0.0 ollama serve so the containers can reach it over the Docker bridge."
if pgrep -f 'target/release/(poller|fetcher)' >/dev/null || pgrep -f '\.venv/bin/arxiv-(worker|query-api)' >/dev/null; then
  die "the host-side pipeline (scripts/run.sh) is running. Stop it first with scripts/run.sh stop:
  two fetchers would break the one-request-per-3-s budget and both would fight over the same ports."
fi
echo "docker $(docker version -f '{{.Server.Version}}'), ollama at $OLLAMA"

say "Models"
have() { curl -sf "$OLLAMA/api/tags" | grep -q "\"name\":\"$1"; }
pull() {
  if have "$1"; then echo "have     $1"; return; fi
  echo "pulling  $1"
  if command -v ollama >/dev/null; then ollama pull "$1"; else curl -sf "$OLLAMA/api/pull" -d "{\"name\":\"$1\",\"stream\":false}" >/dev/null; fi
  have "$1" || die "could not pull $1"
}
pull bge-m3
$SUMMARIES && pull qwen2.5:7b-instruct

# Inside a container `localhost` is the container itself, so host URLs from .env are rewritten to the
# host's Ollama; any other server named in .env (a GPU box running vLLM) is kept as it is.
for v in EMBEDDING_URL LLM_URL; do
  val="${!v:-$(envval "$v")}"
  [[ -z "$val" || "$val" == *localhost* || "$val" == *127.0.0.1* ]] && val="http://host.docker.internal:11434/v1"
  export "$v=$val"
done
export SUMMARY_ENABLED="$SUMMARIES"

say "Infrastructure: Redpanda, Postgres + pgvector, Redis, topics, migrations"
scripts/dev.sh | sed -n '/^--- /p;/^created/p;/^applied/p'

SERVICES=(api worker-abstract worker)
$INGEST && SERVICES+=(poller fetcher)
say "Building and starting: ${SERVICES[*]} (the first build takes a few minutes)"
docker compose --profile pipeline up -d --build "${SERVICES[@]}"

PORT="${API_PORT:-$(envval API_PORT)}"
PORT="${PORT:-8000}"
say "Waiting for the web UI on :$PORT"
for _ in $(seq 1 90); do curl -sf "localhost:$PORT/health" >/dev/null && break; sleep 2; done
curl -sf "localhost:$PORT/health" >/dev/null || die "the API did not come up; see: docker compose logs api"

papers=$(docker compose exec -T postgres psql -qtA -U arxiv -d arxiv -c "select count(*) from papers" | tr -d '[:space:]')
cat <<MSG

  Ready: http://localhost:$PORT   ($papers papers indexed)

MSG
if $INGEST; then
  cat <<'MSG'
  The first poll collects the ~2,000 newest papers in each category. They are searchable by title and
  abstract within about ten minutes; full text and summaries follow over the next hours, paced by
  arXiv's limit of one request every three seconds.

MSG
fi
cat <<'MSG'
  docker compose --profile pipeline logs -f poller worker   # follow progress
  docker compose --profile pipeline stop                    # stop; data stays in Docker volumes
MSG
[[ -t 1 ]] && command -v open >/dev/null && open "http://localhost:$PORT" || true
