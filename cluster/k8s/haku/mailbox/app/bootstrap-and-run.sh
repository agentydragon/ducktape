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

# The upstream image setcaps cap_net_bind_service on the server binary (for
# :25/:443 as non-root). Under this pod's no-new-privs securityContext
# (allowPrivilegeEscalation: false) the kernel refuses to exec ANY
# capability-bearing file — EPERM before main() ("Operation not permitted").
# We bind only unprivileged ports (2525/8080), so run a cap-less copy: cp
# does not preserve the security.capability xattr.
cp /usr/local/bin/stalwart "$RUN/stalwart"

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

# The recovery server's output goes to a file, not straight to stdout: on
# failure it is printed LAST so it survives the 2KiB terminationMessage tail
# (pods/log here is operator-only; that tail is the agent-visible channel).
STALWART_RECOVERY_MODE=1 \
  STALWART_RECOVERY_ADMIN="admin:${STALWART_ADMIN_PASSWORD}" \
  "$RUN/stalwart" --config /etc/stalwart/config.json >"$RUN/recovery.log" 2>&1 &
recovery_pid=$!

fail() {
  {
    echo "$1"
    echo "--- recovery server log (tail) ---"
    # Non-fatal + explicit newline: a tail failure or a cut-off last line must
    # not truncate the failure report this function exists to emit.
    tail -c 1600 "$RUN/recovery.log" || true
    echo
  } >&2
  exit 1
}

# stalwart-cli doubles as the readiness probe: apply fails (and is retried)
# while the recovery listener is still coming up.
export STALWART_URL=http://127.0.0.1:8080
export STALWART_USER=admin
export STALWART_PASSWORD="${STALWART_ADMIN_PASSWORD}"
tries=0
until stalwart-cli apply --file "$RUN/plan.ndjson" --quiet >"$RUN/apply.log" 2>&1; do
  kill -0 "$recovery_pid" 2>/dev/null || fail "recovery server exited before provisioning completed"
  tries=$((tries + 1))
  if [ "$tries" -ge 30 ]; then
    {
      echo "--- last apply output (tail) ---"
      tail -c 400 "$RUN/apply.log" || true
      echo
    } >&2
    fail "provisioning apply did not succeed after ${tries} attempts"
  fi
  sleep 2
done
cat "$RUN/apply.log"
unset STALWART_URL STALWART_USER STALWART_PASSWORD

kill "$recovery_pid"
wait "$recovery_pid" || true
rm -f "$RUN/plan.ndjson"

exec "$RUN/stalwart" --config /etc/stalwart/config.json
