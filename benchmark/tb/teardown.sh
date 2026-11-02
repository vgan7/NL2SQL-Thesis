#!/usr/bin/env bash
set -euo pipefail
TB="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$(dirname "$TB")")"

docker compose -f "$TB/docker-compose.tb.yml" --env-file "$ROOT/.env" down
docker exec datalake-postgres psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS iot_tb"

if [[ "${1:-}" == "--delete-files" ]]; then
  rm -rf "$TB"
  echo "benchmark/tb/ deleted"
fi
echo "experiment removed; main system unchanged"
