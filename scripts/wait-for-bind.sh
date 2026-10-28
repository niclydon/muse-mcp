#!/bin/bash
# wait-for-bind.sh — wait until the .env BIND_HOST address exists on a
# local interface before starting. Without this, a non-loopback
# address that is not up yet at boot is an instant EADDRNOTAVAIL
# crash. The env file defaults to ../.env relative to this script;
# override with MUSE_MCP_ENV_FILE.
set -u
ENV_FILE="${MUSE_MCP_ENV_FILE:-$(cd "$(dirname "$0")/.." && pwd)/.env}"
BIND_HOST="${BIND_HOST:-}"
if [ -z "${BIND_HOST}" ] && [ -f "${ENV_FILE}" ]; then
  BIND_HOST="$(grep -E '^BIND_HOST=' "${ENV_FILE}" | tail -1 | cut -d= -f2-)"
fi
case "${BIND_HOST}" in
  ""|127.0.0.1|localhost|0.0.0.0) exit 0 ;;
esac
for _ in $(seq 1 60); do
  if ip -o addr show | grep -qF " ${BIND_HOST}/"; then
    exit 0
  fi
  sleep 2
done
echo "wait-for-bind: ${BIND_HOST} not present after 120s; starting anyway" >&2
exit 0
