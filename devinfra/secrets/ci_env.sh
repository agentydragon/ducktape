#!/usr/bin/env bash
# CI environment: common secrets + registry/release credentials.
# Usage: eval "$(devinfra/secrets/ci_env.sh)"
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
# TODO: secrets/github-pat-agentydragon-agent.yaml is still read by non-CI
# consumers (devinfra/claude/ web sessions, devinfra/secrets/web_env.sh,
# wt/server/github_client.py). When those are migrated off the machine-user
# PAT, the SOPS file + PAT can be rotated out entirely.
#
# TODO: secrets/ci/ghcr-credentials.sops.yaml is no longer consumed by any
# CI workflow after the migration to secrets.GITHUB_TOKEN. It can be deleted
# once no out-of-tree consumer depends on it; keeping it temporarily so the
# change is trivially revertible.

# shellcheck source=_common.sh
source "$(dirname "$0")/_common.sh"

# Attic binary cache token
try_export ATTIC_TOKEN "$REPO_ROOT/secrets/ci/attic-token.sops.yaml" '["token"]'

# Harbor CI robot
try_export PROPS_REGISTRY_USERNAME "$REPO_ROOT/secrets/ci/harbor-ci-robot.sops.yaml" '["username"]'
try_export PROPS_REGISTRY_PASSWORD "$REPO_ROOT/secrets/ci/harbor-ci-robot.sops.yaml" '["password"]'

# GitHub releases (agentydragon account, contents:write on ducktape)
try_export GH_RELEASE_PAT "$REPO_ROOT/secrets/ci/gh-release-pat.sops.yaml" '["token"]'
