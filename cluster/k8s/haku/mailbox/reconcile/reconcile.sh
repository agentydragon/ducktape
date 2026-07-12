#!/bin/sh
# Apply the desired plan to the running server with the dedicated Authentik
# machine JWT. Flux applies this layer only after bootstrap and production.
set -eu

export HOME=/tmp/stalwart-cli
export STALWART_URL=http://haku-mailbox:8080
mkdir -p "$HOME"

response=$(curl --fail --silent --show-error \
  --data-urlencode grant_type=client_credentials \
  --data-urlencode scope="openid profile email" \
  --data-urlencode client_id@/var/run/secrets/authentik/client_id \
  --data-urlencode username@/var/run/secrets/authentik/username \
  --data-urlencode password@/var/run/secrets/authentik/password \
  https://auth.allegedly.works/application/o/token/)
STALWART_TOKEN=$(printf '%s' "$response" | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
test -n "$STALWART_TOKEN"
export STALWART_TOKEN
stalwart-cli apply --file /etc/stalwart-plan/mailbox-plan.ndjson --quiet
