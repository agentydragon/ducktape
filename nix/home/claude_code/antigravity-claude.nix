# `antigravity-claude`: Claude Code on Google's Antigravity OAuth session (a personal
# "Google AI Plus" subscription, distinct from the AI-Studio GEMINI_API_KEY that
# gemini-claude uses) via the cluster LiteLLM proxy, reading the litellm_antigravity_key
# sops secret — an antigravity-scoped virtual key (SSOT in tf/gitops/litellm-keys).
# LiteLLM reaches CLIProxyAPI's antigravity/ant-messages/* route with Claude Code's native
# Anthropic /v1/messages wire; CLIProxyAPI holds the OAuth session against Google's
# internal Cloud Code API (cloudcode-pa.googleapis.com), so this key never carries it.
# WebFetch/WebSearch are disabled: they are Anthropic-hosted server tools a non-Anthropic
# backend cannot execute. The antigravity-clients team falls back to the flash-lite tier,
# so a throttled model degrades instead of hard-failing — but only at routing time: a
# model outside the key's allowlist is refused during auth, before the router sees it, so
# both names below must be ones the proxy serves and the key admits (ANTIGRAVITY_MODELS
# in cluster/cdk8s/model_rosters.py, antigravity_client_models in
# tf/gitops/litellm-keys/main.tf). See ./gateway.nix for the shared wrapper pattern.
#
# gemini-pro-agent is Antigravity's Gemini 3.1 Pro (High) slug -- a tier the direct
# GEMINI_API_KEY cannot reach at all (gemini-3.1-pro-preview measured RESOURCE_EXHAUSTED,
# quota 0, per model_rosters.py). That's the main reason to reach for this wrapper over
# gemini-claude.
{ pkgs, config }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "antigravity-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenFile = config.sops.secrets.litellm_antigravity_key.path;
  model = "antigravity/ant-messages/gemini-pro-agent";
  haikuModel = "antigravity/ant-messages/gemini-3.5-flash-lite";
  disallowedTools = [
    "WebFetch"
    "WebSearch"
  ];
  # Gemini's published window/output (SSOT: cluster/cdk8s/model_rosters.py
  # GEMINI_CONTEXT_WINDOW / GEMINI_MAX_OUTPUT_TOKENS) -- borrowed from the same Gemini
  # generation the direct API exposes; not independently measured for this
  # Antigravity-specific slug. Without maxContextTokens Claude Code assumes 200k for
  # this unrecognized slug and compacts away most of the real window.
  maxContextTokens = 1048576;
  maxOutputTokens = 65536;
}
