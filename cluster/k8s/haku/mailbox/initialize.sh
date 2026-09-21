#!/bin/sh
# Reconcile Stalwart's database before the production container starts.
set -eu

RUN=/tmp/stalwart-initialize
mkdir -p "$RUN"
export HOME="$RUN"

server_pid=
stop_server() {
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    server_pid=
  fi
}

cleanup() {
  stop_server
}
trap cleanup EXIT INT TERM

# Normal startup creates SQL tables and version-matched safe defaults before it
# binds listeners. The fallback admin is valid in normal mode and exists only in
# this init container; the production container neither mounts nor receives it.
STALWART_RECOVERY_ADMIN="admin:${STALWART_ADMIN_PASSWORD}" \
  /usr/local/bin/stalwart --config /etc/stalwart/config.json >"$RUN/server.log" 2>&1 &
server_pid=$!

tries=0
until curl --fail --silent http://127.0.0.1:8080/healthz/live >/dev/null; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    tail -c 4000 "$RUN/server.log" >&2 || true
    exit 1
  fi
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    tail -c 4000 "$RUN/server.log" >&2 || true
    exit 1
  fi
  sleep 2
done

export STALWART_URL=http://127.0.0.1:8080
export STALWART_USER=admin
export STALWART_PASSWORD="${STALWART_ADMIN_PASSWORD}"

if ! stalwart-cli apply --file /etc/stalwart/mailbox-plan.ndjson --quiet >"$RUN/apply.log" 2>&1; then
  tail -c 4000 "$RUN/apply.log" >&2 || true
  tail -c 4000 "$RUN/server.log" >&2 || true
  exit 1
fi

cat "$RUN/apply.log"
stop_server
