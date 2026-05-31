#!/usr/bin/env bash
# CI environment: common secrets + registry/release credentials.
# Usage: source devinfra/secrets/ci_env.sh
#
# Age recipients: ci (+ admin via _common.sh)
#   secrets/ci/*.sops.yaml:             admin, ci
#
# Consumed by:
#   - .github/actions/setup-ci-secrets (GitHub Actions)
#
# GITHUB_TOKEN is NOT sourced from SOPS here. GHCR pushes and any other
# workflow-run-scoped GitHub API use `${{ secrets.GITHUB_TOKEN }}` (the
# workflow-scoped token GitHub Actions provisions per run) plumbed through
# `bb remote`'s `x-buildbuddy-platform.env-overrides`. See
# .github/workflows/push-images.yml for the wiring.
#
# TODO: secrets/ci/ghcr-credentials.sops.yaml is no longer consumed by any
# CI workflow after the migration to secrets.GITHUB_TOKEN. It can be deleted
# once no out-of-tree consumer depends on it; keeping it temporarily so the
# change is trivially revertible.

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# Attic binary cache writer token for `main` namespace.
# Auto-rotated by the attic-rotate-ducktape-ci-writer CronJob in the cluster
# (see cluster/k8s/agents/attic-jwt-rotation/).
# CLEANUP(2026-05-04): Delete secrets/ci/attic-token.sops.yaml once no
# out-of-tree consumer reads it.
try_export ATTIC_TOKEN "$REPO_ROOT/secrets/ci/attic-main-writer.sops.yaml" '["attic_token"]'

# Harbor CI robot
try_export PROPS_REGISTRY_USERNAME "$REPO_ROOT/secrets/ci/harbor-ci-robot.sops.yaml" '["username"]'
try_export PROPS_REGISTRY_PASSWORD "$REPO_ROOT/secrets/ci/harbor-ci-robot.sops.yaml" '["password"]'

# GitHub releases (agentydragon account, contents:write on ducktape)
try_export GH_RELEASE_PAT "$REPO_ROOT/secrets/ci/gh-release-pat.sops.yaml" '["token"]'

# Restore the caller's shell options (do not leak our `set -euo pipefail`; see _common.sh).
_secrets_restore_shell_opts
