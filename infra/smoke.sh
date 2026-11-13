#!/usr/bin/env bash
# Runbook Part D smoke test. Run from infra/ after `docker compose up -d`.
# TLS-certificate and test-push checks arrive with HE-003 / the notify tickets.
set -euo pipefail
cd "$(dirname "$0")"

BASE_URL="${BASE_URL:-http://localhost:${HTTP_PORT:-80}}"
TIMEOUT="${SMOKE_TIMEOUT:-180}"
fail() { echo "FAIL: $*" >&2; exit 1; }

echo "Waiting up to ${TIMEOUT}s for all services to be healthy..."
deadline=$((SECONDS + TIMEOUT))
while :; do
  total=$(docker compose ps -a --format '{{.Service}}' | wc -l | tr -d ' ')
  healthy=$(docker compose ps --format '{{.Health}}' | grep -c '^healthy$' || true)
  [[ "$total" -gt 0 && "$healthy" == "$total" ]] && break
  (( SECONDS >= deadline )) && { docker compose ps; fail "only $healthy/$total healthy"; }
  sleep 5
done
echo "ok   docker compose ps: $healthy/$total healthy"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/")
[[ "$code" == "200" ]] || fail "GET / returned $code"
echo "ok   GET / -> 200"

body=$(curl -s "$BASE_URL/api/v1/health")
[[ "$body" == '{"db":"ok","redis":"ok","workers":"ok"}' ]] || fail "health: $body"
echo "ok   GET /api/v1/health -> $body"

beat=$(docker compose exec -T redis redis-cli get he:scheduler:heartbeat | tr -d '\r')
[[ -n "$beat" ]] || fail "no scheduler heartbeat"
age=$(( $(date +%s) - beat ))
(( age < 120 )) || fail "scheduler heartbeat ${age}s old"
echo "ok   scheduler heartbeat ${age}s old"

echo "SMOKE PASSED"
