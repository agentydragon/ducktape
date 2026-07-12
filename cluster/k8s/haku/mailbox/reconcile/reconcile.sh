#!/bin/sh
# Apply the desired plan to the running server with the dedicated Authentik
# machine JWT. Flux applies this layer only after bootstrap and production.
set -eu

export HOME=/tmp/stalwart-cli
export STALWART_URL=http://haku-mailbox:8080
mkdir -p "$HOME"

export STALWART_TOKEN
STALWART_TOKEN=$(cat /var/run/secrets/stalwart-token/jwt)
stalwart-cli apply --file /etc/stalwart-plan/mailbox-plan.ndjson --quiet
