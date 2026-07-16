# `z-claude`: Claude Code on z.ai GLM via the cluster LiteLLM proxy, reading $LITELLM_ZAI_KEY
# (a z.ai-scoped virtual key; SSOT in tf/gitops/litellm-keys). The proxy's zai-clients team
# routes Claude Code's claude-* slugs to GLM, so both tiers pin to glm-5.2-anthropic.
# WebFetch/WebSearch are disabled: GLM's tool-call shape differs from Anthropic's
# (see cluster/k8s/litellm/app/generate_litellm.py). Shared with the agent-box `zai` user
# (nix/home/hosts/agent-box/zai.nix). See ./gateway.nix for the shared wrapper pattern.
{ pkgs }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "z-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenEnvVar = "LITELLM_ZAI_KEY";
  model = "glm-5.2-anthropic";
  haikuModel = "glm-5.2-anthropic";
  disallowedTools = [
    "WebFetch"
    "WebSearch"
  ];
}
