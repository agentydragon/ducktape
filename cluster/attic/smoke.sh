#!/usr/bin/env bash
set -euo pipefail

config="$(tar -xOf result manifest.json | jq -er '.[0].Config')"
tar -xOf result "$config" | jq -e \
  '.config.Entrypoint == ["/bin/atticd"] and .config.User == "1000:1000"' >/dev/null

# Check the deployed configuration with synthetic credentials, before publishing the image.
export ATTIC_SERVER_DATABASE_URL='sqlite::memory:'
export ATTIC_SERVER_TOKEN_HS256_SECRET_BASE64='MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY='
nix shell .#attic-server --command atticd \
  --config cluster/k8s/nix-cache/server.toml --mode check-config
nix shell .#attic-server --command atticadm --help >/dev/null
