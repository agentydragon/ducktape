# `codex-claude`: Claude Code on ChatGPT/Codex (GPT-6 Astra etc.) via the cluster LiteLLM
# proxy (→ CLIProxyAPI), reading the litellm_codex_key sops secret — a codex-scoped virtual key (SSOT in
# cluster/k8s/litellm/crossplane-keys). LiteLLM fronts CLIProxyAPI with the `anthropic/` provider (no
# shape translation), so CLIProxyAPI still does the Codex tool-call translation
# (function_call -> tool_use) that LiteLLM's own Responses bridge couldn't. Reasoning effort
# is driven by Claude Code's `effortLevel`. Also baked into the codex-pod image. See
# ./gateway.nix for the shared wrapper pattern.
{ pkgs, config }:
let
  inherit (pkgs) lib;
  models = (lib.importJSON ../../../model_catalog/claude-wrappers.json).codex-claude;
in
import ./gateway.nix { inherit pkgs lib; } "codex-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenFile = config.sops.secrets.litellm_codex_key.path;
  inherit (models) model;
  inherit (models) haikuModel;
  gatewayDiscovery = true;
  # Preserve this wrapper's explicit Claude budgets from model_catalog/nix.py;
  # Codex's configurable window is not proof of subscription serving capacity.
  inherit (models) maxContextTokens maxOutputTokens;
}
