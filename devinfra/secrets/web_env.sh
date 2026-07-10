#!/usr/bin/env bash
# Web agent environment: common secrets + machine-user identity + k8s access.
# Usage: source devinfra/secrets/web_env.sh
#
# Age recipients: claude-web (+ admin via _common.sh)
#   github-pat-agentydragon-agent.yaml: admin, all user keys, claude-web, ci
#   claude-web-k8s-jwt.yaml:          admin, all user keys, claude-web
#   alloy-otlp-bearer-token.yaml:     admin, all user keys, claude-web
#   public-s3/claude-reader-credentials.sops.yaml: admin, cluster-secrets, claude-web
#   claude-web-attic.yaml:            admin, claude-web
#   haku-attic.yaml:                  admin, haku, all user keys
#
# Consumed by:
#   - web_setup.sh: eval'd to populate shell env, then written to
#     .claude/settings.local.json for Claude Code (MCP servers etc.)
#   - hook daemon startup_env_script: eval'd into daemon os.environ,
#     vars flow into the session env file for hook subprocesses
#
# Side effects: upserts the Attic reader token into /nix/var/determinate/netrc
# (see the attic block below); otherwise only prints export lines. Kubeconfig
# is written by the SessionStart handler.

# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# Machine-user GitHub PAT (agentydragon-agent)
try_export GITHUB_TOKEN "$REPO_ROOT/secrets/github-pat-agentydragon-agent.yaml" '["github_token"]' "GitHub PAT for agentydragon-agent bot — used by gh CLI automatically."

# OTEL bearer token (Grafana Alloy via Authentik) — dedicated client_credentials
# JWT rotated in-cluster and committed SOPS-encrypted to git. Session startup
# decrypts the file directly, avoiding kubectl reachability during daemon init.
# Bootstrap note: right after the TF module first creates the Authentik client
# credentials, the SOPS file may not exist until the first successful
# authentik-jwt-rotation run; that warning is expected.
try_export DUCKTAPE_OTEL_BEARER_TOKEN "$REPO_ROOT/secrets/alloy-otlp-bearer-token.yaml" '["token"]' "OTEL bearer token — traces to Grafana Alloy"

# CI read-only fine-grained PAT (personal, agentydragon — read GHA runs/artifacts)
try_export DUCKTAPE_CI_READ_GITHUB_TOKEN "$REPO_ROOT/secrets/github-ci-read-pat.yaml" '["github_token"]' "CI read PAT (agentydragon) — read GHA runs and artifacts"

# SeaweedFS S3 access via the public gateway (s3.allegedly.works). The
# claude-reader identity has Read+List on attic, drivefs-artifacts, vm-images,
# augur-assets, listing-monitor-captures, loom-gym — plus Write+Tagging on
# loom-gym only (eval-run uploads). Exported as standard AWS_* so `aws s3`
# and boto3 work with no flags (e.g. `aws s3 ls s3://attic/`). NOTE: this makes
# every AWS SDK call in the session default to these creds — override
# AWS_* if you ever need real AWS access. SeaweedFS ignores the region but SigV4
# requires one to be set.
_s3_reader="$REPO_ROOT/cluster/k8s/seaweedfs/public-s3/claude-reader-credentials.sops.yaml"
try_export AWS_ACCESS_KEY_ID "$_s3_reader" '["stringData"]["claudeReaderAccessKey"]' "SeaweedFS claude-reader access key (read-only)"
try_export AWS_SECRET_ACCESS_KEY "$_s3_reader" '["stringData"]["claudeReaderSecretKey"]' "SeaweedFS claude-reader secret key (read-only)"
export AWS_ENDPOINT_URL="https://s3.allegedly.works"
export AWS_DEFAULT_REGION="us-east-1"
unset _s3_reader

# Attic reader JWT → Nix netrc (a file write, not an env export: Nix reads
# substitution auth from netrc-file — /etc/nix/nix.conf points it at
# Determinate's /nix/var/determinate/netrc — not from the session env).
# web_setup.sh adds cache.allegedly.works/main (private) as an extra
# substituter alongside cache.allegedly.works/public (anonymous-readable, no
# token needed — carries the web bootstrap closures, see
# cluster/docs/nix_cache.md "Public bootstrap cache"). Nix re-reads the netrc
# per download, so upserting the token here at daemon startup enables
# authenticated substitution from `main`/`gaffer` for the current session and
# for the next boot's tool install — `public` already covers the core devtools
# bootstrap even before this runs, so a missing token here is a lesser
# concern than the warning below implies, not a full fallback to source.
# Tokens are per-principal, auto-rotated by the attic-jwt-rotation CronJob:
# claude-web sessions can decrypt claude-web-attic.yaml, Haku homes
# haku-attic.yaml — try both, first that decrypts wins (each principal's key
# opens exactly one, so per-file failures are expected and only the aggregate
# miss warns).
_attic_token=""
for _attic_file in "$REPO_ROOT/secrets/claude-web-attic.yaml" "$REPO_ROOT/secrets/haku-attic.yaml"; do
  [ -f "$_attic_file" ] || continue
  if _attic_token=$(sops -d --extract '["attic_token"]' "$_attic_file" 2>/dev/null) && [ -n "$_attic_token" ]; then
    break
  fi
  _attic_token=""
done
_netrc="/nix/var/determinate/netrc"
if [ -z "$_attic_token" ]; then
  echo "WARNING: secrets: attic: no reader token decryptable (tried claude-web-attic.yaml, haku-attic.yaml) — cache.allegedly.works/main+gaffer substitution stays anonymous (401s there fall back to /public or source)" >&2
elif mkdir -p "${_netrc%/*}" 2>/dev/null && _netrc_tmp=$(mktemp "${_netrc}.XXXXXX" 2>/dev/null); then
  {
    grep -v '^machine cache\.allegedly\.works ' "$_netrc" 2>/dev/null || true
    printf 'machine cache.allegedly.works password %s\n' "$_attic_token"
  } >"$_netrc_tmp"
  if chmod 600 "$_netrc_tmp" && mv "$_netrc_tmp" "$_netrc"; then
    echo "secrets: attic: OK — reader token in ${_netrc} (cache.allegedly.works substitution)" >&2
  else
    rm -f "$_netrc_tmp"
    echo "WARNING: secrets: attic: failed to move token into ${_netrc} — substitution stays anonymous" >&2
  fi
  unset _netrc_tmp
else
  echo "WARNING: secrets: attic: cannot write ${_netrc} — cache.allegedly.works substitution stays anonymous" >&2
fi
unset _attic_token _attic_file _netrc

# Restore the caller's shell options (do not leak our `set -euo pipefail`; see _common.sh).
_secrets_restore_shell_opts
