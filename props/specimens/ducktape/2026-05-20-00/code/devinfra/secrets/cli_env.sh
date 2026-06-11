#!/usr/bin/env bash
# Laptop environment: common dev secrets only.
# Usage: source devinfra/secrets/cli_env.sh
#
# Does NOT export GITHUB_TOKEN or K8S_TOKEN — preserves personal tokens
# from home-manager / NixOS.
#
# Age recipients: admin + all user keys (via _common.sh secrets)
#
# Consumed by:
#   - Root .envrc (direnv)
#   - Session start hook (CLI profile)

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# Bootstrap note: after the first deploy of the Alloy JWT rotator, the SOPS
# file may be absent until the first successful rotation job writes it.
try_export DUCKTAPE_OTEL_BEARER_TOKEN "$REPO_ROOT/secrets/alloy-otlp-bearer-token.yaml" '["token"]' "OTEL bearer token — traces to Grafana Alloy"
