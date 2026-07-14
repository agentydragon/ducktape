# The `z-claude` wrapper: Claude Code routed to z.ai's GLM via the cluster LiteLLM
# proxy (Anthropic /v1/messages shape, model glm-5.2-anthropic), disabling WebFetch/
# WebSearch (the GLM tool-call shape differs from Anthropic's; see
# cluster/k8s/litellm/app/generate_litellm.py). Auth token is $LITELLM_ZAI_KEY — a
# z.ai-scoped LiteLLM virtual key whose value is SSOT in
# secrets/litellm-zai-clients-key.yaml (provisioned by tf/gitops/litellm-keys via
# sops_file, decryptable by this host's age key).
#
# Shared as-is by the laptop `z-claude` alias (nix/home/home.nix) and the agent-box
# `zai` user (nix/home/hosts/agent-box/zai.nix) — same executable name everywhere.
{ pkgs }:
pkgs.writeShellScriptBin "z-claude" ''
  exec env \
    IS_DEMO=1 \
    ANTHROPIC_BASE_URL=https://litellm.allegedly.works \
    ANTHROPIC_AUTH_TOKEN="$LITELLM_ZAI_KEY" \
    ANTHROPIC_MODEL=glm-5.2-anthropic \
    CLAUDE_STATUSLINE_ROUTE=litellm:zai \
    claude --disallowed-tools "WebFetch WebSearch" \
    "$@"
''
