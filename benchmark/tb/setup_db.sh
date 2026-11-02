#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PG="docker exec -i datalake-postgres psql -U postgres -v ON_ERROR_STOP=1 -q"

if docker exec datalake-postgres psql -U postgres -Atc "SELECT 1 FROM pg_database WHERE datname='iot_tb'" | grep -q 1; then
  echo "iot_tb already exists — drop it first (DROP DATABASE iot_tb) to rebuild"; exit 1
fi

$PG -d postgres -c "CREATE DATABASE iot_tb"
docker exec datalake-postgres pg_dump -U postgres -d iot_staging --no-owner -t public.factory_telemetry_fixed \
  | $PG -d iot_tb
$PG -d iot_tb -c "CREATE SCHEMA raw; ALTER TABLE public.factory_telemetry_fixed SET SCHEMA raw;"
$PG -d iot_tb < view_hist_1m.sql

docker exec datalake-postgres psql -U postgres -d iot_tb -Atc \
  "SELECT 'rows in view: ' || count(*) FROM hist_1m;
   SELECT 'public objects: ' || string_agg(table_name || ' (' || table_type || ')', ', ')
     FROM information_schema.tables WHERE table_schema = 'public';"
