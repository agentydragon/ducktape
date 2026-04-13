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

# OTEL bearer token (Grafana Alloy via Authentik)
try_export DUCKTAPE_OTEL_BEARER_TOKEN "$REPO_ROOT/secrets/alloy-otlp-bearer-token.yaml" '["token"]' "OTEL bearer token — traces to Grafana Alloy"

# CI read-only fine-grained PAT (personal, agentydragon — read GHA runs/artifacts)
try_export DUCKTAPE_CI_READ_GITHUB_TOKEN "$REPO_ROOT/secrets/github-ci-read-pat.yaml" '["github_token"]' "CI read PAT (agentydragon) — read GHA runs and artifacts"

# Write ~/.kube/config from the SOPS token so kubectl works without KUBECONFIG.
# The MCP server (claude-sandbox-kubectl-mcp.sh) decrypts its own temp kubeconfig
# directly via kube_from_sops.sh — it does not read from here.
# Failures are non-fatal (e.g. SOPS_AGE_KEY not yet available at web_setup.sh time).
_kube_stderr="$(mktemp)"
if ! "$REPO_ROOT/devinfra/claude/kube_from_sops.sh" "$HOME/.kube/config" 2>"$_kube_stderr"; then
  echo "WARNING: kubeconfig: failed to write ~/.kube/config: $(cat "$_kube_stderr")" >&2
else
  echo "kubeconfig: wrote ~/.kube/config — ServiceAccount claude-code-web, claude-sandbox namespace (full CRUD: pods/log/exec, services, configmaps, secrets, PVCs, events, deployments, jobs, cronjobs; quota: 8 CPU, 16Gi, 20 pods); cluster-wide read on nodes, Flux, HelmReleases, cert-manager, CNPG, etc." >&2
fi
rm -f "$_kube_stderr"

# Add Nix default profile bin to PATH so hook subprocesses find Nix-installed tools
# (sops, bb, gh, etc.) without relying on /usr/local/bin symlinks.
if [ -d "/nix/var/nix/profiles/default/bin" ]; then
  echo "PATH: prepended /nix/var/nix/profiles/default/bin (Nix default profile)" >&2
  echo 'export PATH=/nix/var/nix/profiles/default/bin:$PATH'
fi
