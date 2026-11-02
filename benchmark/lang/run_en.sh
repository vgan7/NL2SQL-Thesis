#!/usr/bin/env bash
set -uo pipefail
LANG_DIR="$(cd "$(dirname "$0")" && pwd)"
set -a; . "$LANG_DIR/../../.env"; set +a
export PG_DSN="host=localhost port=5433 dbname=iot_staging user=postgres password=$DB_PASSWORD"
for s in broad mid; do
  q="$LANG_DIR/questions_en.yaml"; [[ $s == mid ]] && q="$LANG_DIR/questions_mid_en.yaml"
  echo "=== lang_en_$s $(date +%T)"
  python3 "$LANG_DIR/runner_frozen_copy.py" --label "lang_en_$s" --api http://localhost:8006 --questions "$q" | tail -3
done
echo "=== done $(date +%T)"
