# The `z-claude` wrapper: Claude Code routed to z.ai's GLM via the cluster LiteLLM
# proxy (Anthropic /v1/messages shape, model glm-5.2-anthropic), disabling WebFetch/
# WebSearch (the GLM tool-call shape differs from Anthropic's; see
# cluster/k8s/litellm/app/generate_litellm.py). Auth token is $LITELLM_ZAI_KEY — a
# z.ai-scoped LiteLLM virtual key whose value is SSOT in
# secrets/litellm-zai-clients-key.yaml (provisioned by tf/gitops/litellm-keys via
# sops_file, decryptable by this host's age key).
#
# Both the main model (ANTHROPIC_MODEL) and the Haiku/background tier
# (ANTHROPIC_DEFAULT_HAIKU_MODEL — the current var; ANTHROPIC_SMALL_FAST_MODEL is
# deprecated, see docs.claude.com/en/docs/claude-code/model-config) are pinned to
# glm-5.2-anthropic so Claude Code's background tasks (title generation, etc.)
# don't 403 against the GLM-only virtual key. Any other claude-* slug Claude Code
# emits is rerouted to GLM at the proxy by the zai-clients team's `*` fallback
# (litellm_team.zai_clients in tf/gitops/litellm-keys/main.tf).
# TODO(2026-07-13): consider routing the Haiku/background tier to a cheaper GLM
#   (glm-5-turbo-anthropic or glm-4.5-air-anthropic) once background-task quality
#   on those is confirmed.
#
# Shared as-is by the laptop `z-claude` alias (nix/home/home.nix) and the agent-box
# `zai` user (nix/home/hosts/agent-box/zai.nix) — same executable name everywhere.
#
# Auth is Bearer-only (ANTHROPIC_AUTH_TOKEN); `-u ANTHROPIC_API_KEY` strips any inherited
# key (e.g. wyrm2's real Anthropic key) so Claude Code doesn't see both set at once.
{ pkgs }:
pkgs.writeShellScriptBin "z-claude" ''
  exec env \
    -u ANTHROPIC_API_KEY \
    IS_DEMO=1 \
    ANTHROPIC_BASE_URL=https://litellm.allegedly.works \
    ANTHROPIC_AUTH_TOKEN="$LITELLM_ZAI_KEY" \
    ANTHROPIC_MODEL=glm-5.2-anthropic \
    ANTHROPIC_DEFAULT_HAIKU_MODEL=glm-5.2-anthropic \
    claude --disallowed-tools "WebFetch WebSearch" \
    "$@"
''
