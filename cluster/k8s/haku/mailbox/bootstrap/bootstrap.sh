#!/bin/sh
# Seed an empty Stalwart datastore before the production Flux layer is applied.
set -eu

RUN=/tmp/stalwart-bootstrap
mkdir -p "$RUN"
export HOME="$RUN"
cp /usr/local/bin/stalwart "$RUN/stalwart"

STALWART_RECOVERY_MODE=1 \
  STALWART_RECOVERY_ADMIN="admin:${STALWART_ADMIN_PASSWORD}" \
  "$RUN/stalwart" --config /etc/stalwart/config.json >"$RUN/recovery.log" 2>&1 &
recovery_pid=$!

cleanup() {
  kill "$recovery_pid" 2>/dev/null || true
  wait "$recovery_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

export STALWART_URL=http://127.0.0.1:8080
export STALWART_USER=admin
export STALWART_PASSWORD="${STALWART_ADMIN_PASSWORD}"

tries=0
until stalwart-cli apply --file /etc/stalwart/mailbox-plan.ndjson --quiet >"$RUN/apply.log" 2>&1; do
  if ! kill -0 "$recovery_pid" 2>/dev/null; then
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
