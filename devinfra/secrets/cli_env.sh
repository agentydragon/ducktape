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

# BuildBuddy remote cache/execution (bbr). Read from the shared cluster Secret,
# the only copy of this key. CI does not decrypt it: tofu reads the same Secret
# in-cluster and sets it as a GitHub Actions repository secret, so CI needs the
# BuildBuddy capability without the broader CI decryption identity.
try_export BUILDBUDDY_API_KEY "$REPO_ROOT/cluster/k8s/agents/shared-secrets/buildbuddy-api-key.sops.yaml" '["stringData"]["api-key"]' "BuildBuddy remote cache/execution (bbr)"

# Bootstrap note: after the first deploy of the Alloy JWT rotator, the SOPS
# file may be absent until the first successful rotation job writes it.
try_export DUCKTAPE_OTEL_BEARER_TOKEN "$REPO_ROOT/secrets/alloy-otlp-bearer-token.yaml" '["token"]' "OTEL bearer token — traces to Grafana Alloy"

# Restore the caller's shell options (do not leak our `set -euo pipefail`; see _common.sh).
_secrets_restore_shell_opts
