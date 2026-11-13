#!/usr/bin/env bash
# Runbook Part D smoke test. Run from infra/ after `docker compose up -d`.
# BASE_URL defaults to https://APP_DOMAIN as Caddy sees it. With APP_DOMAIN=localhost
# the certificate is checked against Caddy's internal root CA instead of being skipped.
# The test-push check arrives with the notify tickets.
set -euo pipefail
cd "$(dirname "$0")"

TIMEOUT="${SMOKE_TIMEOUT:-180}"
MIN_CERT_DAYS=14
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

caddy_env() { docker compose exec -T caddy printenv "$1" 2>/dev/null | tr -d '\r' || true; }
APP_DOMAIN="$(caddy_env APP_DOMAIN)"
[[ -n "$APP_DOMAIN" ]] || fail "APP_DOMAIN not set in the caddy container"
HSTS="$(caddy_env HSTS)"

https_suffix=""
[[ "${HTTPS_PORT:-443}" == "443" ]] || https_suffix=":${HTTPS_PORT}"
BASE_URL="${BASE_URL:-https://${APP_DOMAIN}${https_suffix}}"

hostport="${BASE_URL#*://}"; hostport="${hostport%%/*}"
host="${hostport%%:*}"
port="${hostport##*:}"
[[ "$port" == "$hostport" ]] && port=$([[ "$BASE_URL" == https://* ]] && echo 443 || echo 80)

CURL=(curl -sS --max-time 15)
internal_ca=false
[[ "$host" == "localhost" ]] && internal_ca=true

if [[ "$BASE_URL" == https://* ]]; then
  if $internal_ca; then
    ca_file="$(mktemp)"
    trap 'rm -f "$ca_file"' EXIT
    docker compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$ca_file" \
      || fail "cannot read Caddy internal root CA"
    CURL+=(--cacert "$ca_file")
  fi

  # s_client's exit code varies by OpenSSL/LibreSSL build; parsing the leaf cert is the test.
  cert=$( { openssl s_client -connect "${host}:${port}" -servername "$host" </dev/null 2>/dev/null || true; } \
    | openssl x509 2>/dev/null) || fail "no TLS certificate served on ${host}:${port}"
  not_after=$(openssl x509 -noout -enddate <<<"$cert" | cut -d= -f2)
  if $internal_ca; then
    # Internal-CA leaf certs live 12h and auto-renew, so only require "not expired".
    openssl x509 -noout -checkend 0 <<<"$cert" >/dev/null || fail "TLS cert expired ($not_after)"
    echo "ok   TLS cert (Caddy internal CA) valid until $not_after"
  else
    openssl x509 -noout -checkend $((MIN_CERT_DAYS * 86400)) <<<"$cert" >/dev/null \
      || fail "TLS cert expires within ${MIN_CERT_DAYS} days ($not_after)"
    echo "ok   TLS cert valid > ${MIN_CERT_DAYS} days (until $not_after)"
  fi

  http_suffix=""
  [[ "${HTTP_PORT:-80}" == "80" ]] || http_suffix=":${HTTP_PORT}"
  read -r rcode rloc < <(curl -sS --max-time 15 -o /dev/null -w '%{http_code} %{redirect_url}\n' \
    "http://${host}${http_suffix}/")
  [[ "$rcode" == "308" && "$rloc" == "https://${host}"* ]] || fail "HTTP->HTTPS redirect: $rcode $rloc"
  echo "ok   http:// -> $rcode $rloc"
fi

# A single request supplies status and headers; curl verifies the chain (no -k).
headers=$("${CURL[@]}" -D - -o /dev/null "$BASE_URL/" | tr -d '\r') || fail "GET / failed (TLS or connection)"
code=$(awk 'NR==1 {print $2}' <<<"$headers")
[[ "$code" == "200" ]] || fail "GET / returned $code"
echo "ok   GET $BASE_URL/ -> 200"

need_header() { grep -qiE "^$1: $2" <<<"$headers" || fail "header $1 missing or not '$2'"; }
need_header "x-content-type-options" "nosniff"
need_header "referrer-policy" "strict-origin-when-cross-origin"
need_header "permissions-policy" ".+"
grep -qiE "^x-frame-options: deny|^content-security-policy: .*frame-ancestors 'none'" <<<"$headers" \
  || fail "no X-Frame-Options DENY / frame-ancestors 'none'"
need_header "content-security-policy" ".*default-src 'self'"
grep -qiE "^(server|x-powered-by|via):" <<<"$headers" && fail "Server/X-Powered-By/Via header exposed"
if [[ "$HSTS" == "on" && "$BASE_URL" == https://* ]]; then
  need_header "strict-transport-security" "max-age=31536000"
fi
echo "ok   security headers (nosniff, Referrer-Policy, Permissions-Policy, frame-ancestors, CSP, HSTS=${HSTS:-off})"

body=$("${CURL[@]}" "$BASE_URL/api/v1/health")
[[ "$body" == '{"db":"ok","redis":"ok","workers":"ok"}' ]] || fail "health: $body"
echo "ok   GET /api/v1/health -> $body"

if [[ "$BASE_URL" == https://* ]]; then
  ntfy_url="https://ntfy.${host}${https_suffix}/v1/health"
  resolve=()
  $internal_ca && resolve=(--resolve "ntfy.${host}:${port}:127.0.0.1")
  ntfy=$("${CURL[@]}" ${resolve[@]+"${resolve[@]}"} "$ntfy_url") || fail "ntfy unreachable at $ntfy_url"
  grep -q '"healthy":true' <<<"$ntfy" || fail "ntfy health: $ntfy"
  echo "ok   GET $ntfy_url -> $ntfy"
fi

beat=$(docker compose exec -T redis redis-cli get he:scheduler:heartbeat | tr -d '\r')
[[ -n "$beat" ]] || fail "no scheduler heartbeat"
age=$(( $(date +%s) - beat ))
(( age < 120 )) || fail "scheduler heartbeat ${age}s old"
echo "ok   scheduler heartbeat ${age}s old"

echo "SMOKE PASSED"
