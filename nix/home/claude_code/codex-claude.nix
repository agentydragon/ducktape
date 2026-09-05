# `codex-claude`: Claude Code on ChatGPT/Codex (GPT-6 Astra etc.) via the cluster LiteLLM
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
  model = "chatgpt/ant-messages/gpt-6-astra";
  haikuModel = "chatgpt/ant-messages/gpt-5.6-luna";
  gatewayDiscovery = true;
  # Codex 0.153.4 permits Astra's context window up to 872k (SSOT:
  # model_rosters.py). Claude Code does not discover it, so set it explicitly.
  maxContextTokens = 872000;
  maxOutputTokens = 128000;
}
