#!/bin/sh
# Seed an empty Stalwart datastore before the production Flux layer is applied.
set -eu

RUN=/tmp/stalwart-bootstrap
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

# Recovery mode exposes the management API but does not install Stalwart's
# built-in roles. Briefly boot normal mode first so upstream seeds those roles
# with their version-matched permission sets. The declarative plan can then
# match and reference them instead of creating empty roles by description.
/usr/local/bin/stalwart --config /etc/stalwart/config.json >"$RUN/defaults.log" 2>&1 &
server_pid=$!

tries=0
until curl --fail --silent http://127.0.0.1:8080/healthz/live >/dev/null; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    tail -c 4000 "$RUN/defaults.log" >&2 || true
    exit 1
  fi
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    tail -c 4000 "$RUN/defaults.log" >&2 || true
    exit 1
  fi
  sleep 2
done
stop_server

STALWART_RECOVERY_MODE=1 \
  STALWART_RECOVERY_ADMIN="admin:${STALWART_ADMIN_PASSWORD}" \
  /usr/local/bin/stalwart --config /etc/stalwart/config.json >"$RUN/recovery.log" 2>&1 &
server_pid=$!

export STALWART_URL=http://127.0.0.1:8080
export STALWART_USER=admin
export STALWART_PASSWORD="${STALWART_ADMIN_PASSWORD}"

tries=0
until stalwart-cli apply --file /etc/stalwart/mailbox-plan.ndjson --quiet >"$RUN/apply.log" 2>&1; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    tail -c 4000 "$RUN/recovery.log" >&2 || true
    exit 1
  fi
  tries=$((tries + 1))
  if [ "$tries" -ge 30 ]; then
    tail -c 2000 "$RUN/apply.log" >&2 || true
    tail -c 4000 "$RUN/recovery.log" >&2 || true
    exit 1
  fi
  sleep 2
done

cat "$RUN/apply.log"
