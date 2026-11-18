#!/usr/bin/env bash
# HE-133 / Runbook B6.3: point <name>.duckdns.org at this VM's current public IP.
# Reads DUCKDNS_TOKEN (and the name from APP_DOMAIN) from infra/.env, so the token is never
# in the crontab line, and passes the URL to curl on stdin, so it is not in `ps` output either.
#
#   crontab -e  ->  */5 * * * * ~/hackevents/infra/duckdns-update.sh >/dev/null 2>&1
#
# Exit 0 when DuckDNS answers OK; non-zero (message on stderr) otherwise.
set -euo pipefail

ENV_FILE=${ENV_FILE:-$(dirname "$0")/.env}
DUCKDNS_URL=${DUCKDNS_URL:-https://www.duckdns.org/update}  # override only for tests

value() { { grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2- | tr -d '"'"'"; }

[[ -r $ENV_FILE ]] || { echo "cannot read $ENV_FILE" >&2; exit 1; }
domain=$(value APP_DOMAIN)
token=$(value DUCKDNS_TOKEN)
name=${domain%.duckdns.org}
if [[ -z $domain || $name == "$domain" || $name == *.* ]]; then
  echo "APP_DOMAIN must be <name>.duckdns.org (got '${domain}')" >&2
  exit 1
fi
[[ -n $token ]] || { echo "DUCKDNS_TOKEN is empty in $ENV_FILE" >&2; exit 1; }

# Empty ip= lets DuckDNS use the address the request comes from.
answer=$(printf 'url = "%s?domains=%s&token=%s&ip="\n' "$DUCKDNS_URL" "$name" "$token" |
  curl -fsS --max-time 30 -K -) || { echo "DuckDNS request failed" >&2; exit 1; }
[[ $answer == OK ]] || { echo "DuckDNS answered '${answer}' (bad token or name?)" >&2; exit 1; }
