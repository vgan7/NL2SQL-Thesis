#!/usr/bin/env bash
set -uo pipefail
TB="$(cd "$(dirname "$0")" && pwd)"
BENCH="$(dirname "$TB")"
set -a; . "$BENCH/../.env"; set +a
DSN_STAGING="host=localhost port=5433 dbname=iot_staging user=postgres password=$DB_PASSWORD"
DSN_TB="host=localhost port=5433 dbname=iot_tb user=postgres password=$DB_PASSWORD"

run() {  # label api dsn questions
  local label=$1 api=$2 dsn=$3 q=$4
  echo "=== $label ($api, $(basename "$q")) $(date +%T)"
  PG_DSN="$dsn" python3 "$TB/runner_frozen_copy.py" --label "$label" --api "$api" --questions "$q" | tail -3
}

run tb_opaque_layer_broad  http://localhost:8002 "$DSN_TB"      "$TB/questions_opaque.yaml"
run tb_opaque_layer_mid    http://localhost:8002 "$DSN_TB"      "$TB/questions_mid_opaque.yaml"
run tb_opaque_bare_broad   http://localhost:8003 "$DSN_TB"      "$TB/questions_opaque.yaml"
run tb_opaque_bare_mid     http://localhost:8003 "$DSN_TB"      "$TB/questions_mid_opaque.yaml"
run tb_clear_bare_broad    http://localhost:8004 "$DSN_STAGING" "$BENCH/questions.yaml"
run tb_clear_bare_mid      http://localhost:8004 "$DSN_STAGING" "$BENCH/questions_mid.yaml"
echo "=== all done $(date +%T)"
