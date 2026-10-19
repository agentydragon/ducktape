# `antigravity-claude`: Claude Code on Google's Antigravity OAuth session (a personal
# "Google AI Plus" subscription, distinct from the AI-Studio GEMINI_API_KEY that
# gemini-claude uses) via the cluster LiteLLM proxy, reading the litellm_antigravity_key
# sops secret — an antigravity-scoped virtual key (SSOT in tf/gitops/litellm-keys).
# LiteLLM reaches CLIProxyAPI's antigravity/ant-messages/* route with Claude Code's native
# Anthropic /v1/messages wire; CLIProxyAPI holds the OAuth session against Google's
# internal Cloud Code API (cloudcode-pa.googleapis.com), so this key never carries it.
# WebFetch/WebSearch are disabled: they are Anthropic-hosted server tools a non-Anthropic
# backend cannot execute. The roster has Gemini 3.5 Flash-Lite, but this paused wrapper
# does not set a Haiku submodel until its tool behavior is validated. Before enabling it,
# make sure all configured names are served by the proxy and admitted by the key
# (ANTIGRAVITY_ROUTES in model_catalog/catalog.py, antigravity_client_models in
# tf/gitops/litellm-keys/main.tf). See ./gateway.nix for the shared wrapper pattern.
#
# The Antigravity wrapper uses Gemini 3 Flash because the direct GEMINI_API_KEY route
# for 3.1 Pro preview returned RESOURCE_EXHAUSTED (quota 0, per model_catalog/catalog.py).
# The unsupported Antigravity Pro IDs remain parked until CLIProxyAPI registers them again.
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
  # Preserve this wrapper's configured 1,048,576/65,536 client budgets from
  # model_catalog/nix.py.
  inherit (models) maxContextTokens maxOutputTokens;
}
