#!/bin/sh
# Startup wrapper for the Stalwart mailserver: render the provisioning plan,
# apply it against a temporary recovery-mode instance (idempotent upserts, per
# Stalwart's declarative-deployments workflow), then exec the normal server.
# TODO: this dance is ugly but currently irreducible — see the README's
# "Future" section for the revisit gates (tofu provider coverage, upstream
# declarative bootstrap, native ACME).
# Running on every pod start makes the plan the reconcile loop — a
# reloader-triggered restart after cert-manager renews mx-allegedly-works-tls
# re-pushes the fresh certificate.
set -eu

RUN=/tmp/stalwart-run
mkdir -p "$RUN"
# stalwart-cli caches the server schema under $HOME.
export HOME="$RUN"

# JSON-escape a PEM file as a single \n-joined line. The PEM alphabet
# (base64 + header dashes/spaces) contains no character that needs further
# JSON escaping and none special to the sed replacement below. awk emits
# doubled backslashes because GNU sed collapses \\ -> \ in the replacement
# (a single \n there would become a real newline and break the NDJSON).
pem_json() {
  awk 'BEGIN { ORS = "\\\\n" } { print }' "$1"
}

sed -e "s|@@TLS_CERT@@|$(pem_json /tls/tls.crt)|" \
  -e "s|@@TLS_KEY@@|$(pem_json /tls/tls.key)|" \
  /etc/stalwart/mailbox-plan.ndjson.tmpl >"$RUN/plan.ndjson"

STALWART_RECOVERY_MODE=1 \
  STALWART_RECOVERY_ADMIN="admin:${STALWART_ADMIN_PASSWORD}" \
  stalwart --config /etc/stalwart/config.json &
recovery_pid=$!

# stalwart-cli doubles as the readiness probe: apply fails (and is retried)
# while the recovery listener is still coming up.
export STALWART_URL=http://127.0.0.1:8080
export STALWART_USER=admin
export STALWART_PASSWORD="${STALWART_ADMIN_PASSWORD}"
tries=0
until /cli/stalwart-cli apply --file "$RUN/plan.ndjson" --quiet; do
  tries=$((tries + 1))
  if [ "$tries" -ge 30 ]; then
    echo "provisioning apply did not succeed after ${tries} attempts" >&2
    exit 1
  fi
  sleep 2
done
unset STALWART_URL STALWART_USER STALWART_PASSWORD

kill "$recovery_pid"
wait "$recovery_pid" || true
rm -f "$RUN/plan.ndjson"

exec stalwart --config /etc/stalwart/config.json
