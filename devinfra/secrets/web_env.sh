#!/usr/bin/env bash
# Web agent environment: common secrets + machine-user identity + k8s access.
# Usage: eval "$(devinfra/secrets/web_env.sh)"
#
# Age recipients: claude-web (+ admin via _common.sh)
#   github-pat-agentydragon-agent.yaml: admin, all user keys, claude-web, ci
#   claude-web-k8s-token.yaml:          admin, claude-web
#
# Consumed by:
#   - web_setup.sh: eval'd to populate shell env, then written to
#     .claude/settings.local.json for Claude Code (MCP servers etc.)
#   - hook daemon startup_env_script: eval'd into daemon os.environ,
#     vars flow into the session env file for hook subprocesses
#
# Side effects (in addition to printing export lines):
#   - Writes ~/.kube/config from the SOPS-encrypted k8s token so that kubectl
#     works without KUBECONFIG set. Web env has no pre-existing user kubeconfig.

# shellcheck source=_common.sh
source "$(dirname "$0")/_common.sh"

# Machine-user GitHub PAT (agentydragon-agent)
try_export GITHUB_TOKEN "$REPO_ROOT/secrets/github-pat-agentydragon-agent.yaml" '["github_token"]' "GitHub PAT for agentydragon-agent bot — used by gh CLI automatically. PR workflow: origin is a local proxy; PRs must come from a fork (fork remote + push/PR instructions delivered via mailbox)."

# K8s service account token (claude-code-web SA) — for session hook kubeconfig
try_export K8S_TOKEN "$REPO_ROOT/secrets/claude-web-k8s-token.yaml" '["k8s_token"]' "K8s service account token (claude-code-web SA)"

# Write ~/.kube/config from the SOPS token so kubectl works without KUBECONFIG.
# The MCP server (claude-sandbox-kubectl-mcp.sh) decrypts its own temp kubeconfig
# directly via kube_from_sops.sh — it does not read from here.
# Must run before any `try_export_from_k8s` below: the web session has no
# pre-existing kubeconfig, so kubectl has nothing to authenticate with until
# this block writes ~/.kube/config.
# Failures are non-fatal (e.g. SOPS_AGE_KEY not yet available at web_setup.sh time).
_kube_stderr="$(mktemp)"
if ! "$REPO_ROOT/devinfra/claude/kube_from_sops.sh" "$HOME/.kube/config" 2>"$_kube_stderr"; then
  echo "WARNING: kubeconfig: failed to write ~/.kube/config: $(cat "$_kube_stderr")" >&2
else
  echo "kubeconfig: wrote ~/.kube/config — ServiceAccount claude-code-web, claude-sandbox namespace (full CRUD: pods/log/exec, services, configmaps, secrets, PVCs, events, deployments, jobs, cronjobs; quota: 8 CPU, 16Gi, 20 pods); cluster-wide read on nodes, Flux, HelmReleases, cert-manager, CNPG, etc." >&2
fi
rm -f "$_kube_stderr"

# OTEL bearer token (Grafana Alloy via Authentik) — canonical source is Authentik,
# TF writes it into this K8s Secret (cluster/terraform/gitops/alloy-otlp-bearer-token).
#
# Gated because `kubectl get secret` blocks for ~30s per retry when the k8s API
# is unreachable (e.g. CCR v2 web sandboxes can only reach port 443; the API
# lives on :16443). Unconditional call → wedged daemon startup → dispatcher
# timeout loop → no secrets in the agent env at all. Default off.
# CLEANUP: mirror alloy-otlp-bearer-token into SOPS and `try_export` it instead;
# see devinfra/claude/TODO.md "OTEL bearer token: mirror into SOPS".
if [ "${DUCKTAPE_ENABLE_K8S_OTEL_BEARER_TOKEN:-0}" = "1" ]; then
  try_export_from_k8s DUCKTAPE_OTEL_BEARER_TOKEN claude-sandbox alloy-otlp-bearer-token token "OTEL bearer token — traces to Grafana Alloy"
else
  echo "secrets: DUCKTAPE_OTEL_BEARER_TOKEN: skipped (DUCKTAPE_ENABLE_K8S_OTEL_BEARER_TOKEN != 1)" >&2
fi

# CI read-only fine-grained PAT (personal, agentydragon — read GHA runs/artifacts)
try_export DUCKTAPE_CI_READ_GITHUB_TOKEN "$REPO_ROOT/secrets/github-ci-read-pat.yaml" '["github_token"]' "CI read PAT (agentydragon) — read GHA runs and artifacts"

# Add Nix default profile bin to PATH so hook subprocesses find Nix-installed tools
# (sops, bb, gh, etc.) without relying on /usr/local/bin symlinks.
if [ -d "/nix/var/nix/profiles/default/bin" ]; then
  echo "PATH: prepended /nix/var/nix/profiles/default/bin (Nix default profile)" >&2
  echo 'export PATH=/nix/var/nix/profiles/default/bin:$PATH'
fi
