#!/usr/bin/env bash
set -uo pipefail
TB="$(cd "$(dirname "$0")" && pwd)"
BENCH="$(dirname "$TB")"
set -a; . "$BENCH/../.env"; set +a
DSN_TB="host=localhost port=5433 dbname=iot_tb user=postgres password=$DB_PASSWORD"

for s in broad mid; do
  q="$TB/questions_opaque.yaml"; [[ $s == mid ]] && q="$TB/questions_mid_opaque.yaml"
  echo "=== tb_opaque_nodesc_$s $(date +%T)"
  PG_DSN="$DSN_TB" python3 "$TB/runner_frozen_copy.py" --label "tb_opaque_nodesc_$s" \
    --api http://localhost:8005 --questions "$q" | tail -3
done
echo "=== done $(date +%T)"
