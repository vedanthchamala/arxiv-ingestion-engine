#!/usr/bin/env bash
# Bring up the local stack, create topics, apply migrations. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d --wait

echo "--- topics"
for t in papers.new papers.chunked papers.failed; do
  if docker compose exec -T redpanda rpk topic list | awk '{print $1}' | grep -qx "$t"; then
    echo "exists  $t"
  else
    docker compose exec -T redpanda rpk topic create "$t" -p 6 -r 1 >/dev/null && echo "created $t"
  fi
done

echo "--- consumer-lag metrics"
docker compose exec -T redpanda rpk cluster config set enable_consumer_group_metrics '["group","partition","consumer_lag"]' >/dev/null && echo "enabled"

echo "--- migrations"
for f in sql/*.sql; do
  docker compose exec -T postgres psql -q -v ON_ERROR_STOP=1 -U arxiv -d arxiv -f "/docker-entrypoint-initdb.d/$(basename "$f")"
  echo "applied $f"
done

echo "--- ready"
echo "console   http://localhost:8080"
echo "kafka     localhost:19092"
echo "postgres  postgresql://arxiv:arxiv@localhost:5432/arxiv"
echo "redis     redis://localhost:6379"
echo "metrics   docker compose --profile observability up -d   # Prometheus :9090, Grafana :3000"
