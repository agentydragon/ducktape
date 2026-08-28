# `codex-claude`: Claude Code on ChatGPT/Codex (gpt-5.6-sol etc.) via the cluster LiteLLM
# proxy (→ CLIProxyAPI), reading $CODEX_LITELLM_KEY — a codex-scoped virtual key (SSOT in
# tf/gitops/litellm-keys). LiteLLM fronts CLIProxyAPI with the `anthropic/` provider (no
# shape translation), so CLIProxyAPI still does the Codex tool-call translation
# (function_call -> tool_use) that LiteLLM's own Responses bridge couldn't. Reasoning effort
# is driven by Claude Code's `effortLevel`. Also baked into the codex-pod image. See
# ./gateway.nix for the shared wrapper pattern.
{ pkgs }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "codex-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenEnvVar = "CODEX_LITELLM_KEY";
  model = "chatgpt/ant-messages/gpt-5.6-sol";
  haikuModel = "chatgpt/ant-messages/gpt-5.6-luna";
  gatewayDiscovery = true;
  # Measured serving-path window/output for the gpt-5.6 Codex models (SSOT:
  # cluster/k8s/litellm/app/model_rosters.py CODEX_CONTEXT_WINDOW / CODEX_MAX_TOKENS). Without
  # these Claude Code assumes 200k and compacts at ~166k, clipping the real 372k window.
  maxContextTokens = 372000;
  maxOutputTokens = 128000;
}
