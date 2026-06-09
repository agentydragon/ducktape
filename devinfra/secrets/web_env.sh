#!/usr/bin/env bash
# Web agent environment: common secrets + machine-user identity + k8s access.
# Usage: source devinfra/secrets/web_env.sh
#
# Age recipients: claude-web (+ admin via _common.sh)
#   github-pat-agentydragon-agent.yaml: admin, all user keys, claude-web, ci
#   claude-web-k8s-jwt.yaml:          admin, all user keys, claude-web
#   alloy-otlp-bearer-token.yaml:     admin, all user keys, claude-web
#   public-s3/claude-reader-credentials.sops.yaml: admin, cluster-secrets, claude-web
#
# Consumed by:
#   - web_setup.sh: eval'd to populate shell env, then written to
#     .claude/settings.local.json for Claude Code (MCP servers etc.)
#   - hook daemon startup_env_script: eval'd into daemon os.environ,
#     vars flow into the session env file for hook subprocesses
#
# Side effects: none beyond printing export lines. Kubeconfig is written by
# the SessionStart handler.

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# Machine-user GitHub PAT (agentydragon-agent)
try_export GITHUB_TOKEN "$REPO_ROOT/secrets/github-pat-agentydragon-agent.yaml" '["github_token"]' "GitHub PAT for agentydragon-agent bot — used by gh CLI automatically."

# OTEL bearer token (Grafana Alloy via Authentik) — dedicated client_credentials
# JWT rotated in-cluster and committed SOPS-encrypted to git. Session startup
# decrypts the file directly, avoiding kubectl reachability during daemon init.
# Bootstrap note: right after the TF module first creates the Authentik client
# credentials, the SOPS file may not exist until the first successful
# alloy-otlp-jwt-rotation run; that warning is expected.
try_export DUCKTAPE_OTEL_BEARER_TOKEN "$REPO_ROOT/secrets/alloy-otlp-bearer-token.yaml" '["token"]' "OTEL bearer token — traces to Grafana Alloy"

# CI read-only fine-grained PAT (personal, agentydragon — read GHA runs/artifacts)
try_export DUCKTAPE_CI_READ_GITHUB_TOKEN "$REPO_ROOT/secrets/github-ci-read-pat.yaml" '["github_token"]' "CI read PAT (agentydragon) — read GHA runs and artifacts"

# Read-only SeaweedFS S3 access via the public gateway (s3.allegedly.works). The
# claude-reader identity has Read+List on attic, drivefs-artifacts, vm-images,
# augur-assets, listing-monitor-captures. Exported as standard AWS_* so `aws s3`
# and boto3 work with no flags (e.g. `aws s3 ls s3://attic/`). NOTE: this makes
# every AWS SDK call in the session default to these read-only creds — override
# AWS_* if you ever need real AWS access. SeaweedFS ignores the region but SigV4
# requires one to be set.
_s3_reader="$REPO_ROOT/cluster/k8s/seaweedfs/public-s3/claude-reader-credentials.sops.yaml"
try_export AWS_ACCESS_KEY_ID "$_s3_reader" '["stringData"]["claudeReaderAccessKey"]' "SeaweedFS claude-reader access key (read-only)"
try_export AWS_SECRET_ACCESS_KEY "$_s3_reader" '["stringData"]["claudeReaderSecretKey"]' "SeaweedFS claude-reader secret key (read-only)"
export AWS_ENDPOINT_URL="https://s3.allegedly.works"
export AWS_DEFAULT_REGION="us-east-1"
unset _s3_reader

# loom-gym S3 writer (forecasting-gym eval-run results). Scoped to the
# loom-gym bucket only; consumed by //loom/gym:baseline_eval --upload.
_s3_loom="$REPO_ROOT/cluster/k8s/seaweedfs/public-s3/loom-gym-credentials.sops.yaml"
try_export LOOM_GYM_S3_ACCESS_KEY_ID "$_s3_loom" '["stringData"]["loomGymWriterAccessKey"]' "SeaweedFS loom-gym writer access key"
try_export LOOM_GYM_S3_SECRET_ACCESS_KEY "$_s3_loom" '["stringData"]["loomGymWriterSecretKey"]' "SeaweedFS loom-gym writer secret key"
unset _s3_loom

# Restore the caller's shell options (do not leak our `set -euo pipefail`; see _common.sh).
_secrets_restore_shell_opts
