# `antigravity-claude`: Claude Code on Google's Antigravity OAuth session (a personal
# "Google AI Plus" subscription, distinct from the AI-Studio GEMINI_API_KEY that
# gemini-claude uses) via the cluster LiteLLM proxy, reading the litellm_antigravity_key
# sops secret — an antigravity-scoped virtual key (SSOT in tf/gitops/litellm-keys).
# LiteLLM reaches CLIProxyAPI's antigravity/ant-messages/* route with Claude Code's native
# Anthropic /v1/messages wire; CLIProxyAPI holds the OAuth session against Google's
# internal Cloud Code API (cloudcode-pa.googleapis.com), so this key never carries it.
# WebFetch/WebSearch are disabled: they are Anthropic-hosted server tools a non-Anthropic
# backend cannot execute. CLIProxyAPI's pinned model registry currently lacks a suitable
# small Antigravity model, so this paused wrapper does not set a Haiku submodel. Before
# enabling it, make sure all configured names are served by the proxy and admitted by the
# key (ANTIGRAVITY_ROUTES in model_catalog/catalog.py, antigravity_client_models in
# tf/gitops/litellm-keys/main.tf). See ./gateway.nix for the shared wrapper pattern.
#
# gemini-pro-agent is Antigravity's Gemini 3.1 Pro (High) slug -- a tier the direct
# GEMINI_API_KEY cannot reach at all (gemini-3.1-pro-preview measured RESOURCE_EXHAUSTED,
# quota 0, per model_catalog/catalog.py). That's the main reason to reach for this wrapper over
# gemini-claude.
{ pkgs, config }:
let
  inherit (pkgs) lib;
  models = (lib.importJSON ../../../model_catalog/claude-wrappers.json).antigravity-claude;
in
import ./gateway.nix { inherit pkgs lib; } "antigravity-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenFile = config.sops.secrets.litellm_antigravity_key.path;
  inherit (models) model;
  disallowedTools = [
    "WebFetch"
    "WebSearch"
  ];
  # Preserve Claude-specific budgets from model_catalog/nix.py, including the
  # 65,536 output setting; the Antigravity route declares 65,535 separately.
  inherit (models) maxContextTokens maxOutputTokens;
}
