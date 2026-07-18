# `gemini-claude`: Claude Code on Google Gemini via the cluster LiteLLM proxy, reading
# $GEMINI_LITELLM_KEY — a gemini-scoped virtual key (SSOT in tf/gitops/litellm-keys).
# LiteLLM translates Claude Code's Anthropic /v1/messages calls to the `gemini/` provider;
# the upstream reaches Google with the in-cluster GEMINI_API_KEY, so this key never carries
# it. WebFetch/WebSearch are disabled: they are Anthropic-hosted server tools a non-Anthropic
# backend cannot execute (same reasoning as z-claude's GLM tool-shape exclusion). The
# gemini-clients team falls back to gemini-3.5-flash, so a quota-throttled preview model
# (gemini-3-pro-preview has tight Google quota) degrades instead of hard-failing. See
# ./gateway.nix for the shared wrapper pattern.
{ pkgs }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "gemini-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenEnvVar = "GEMINI_LITELLM_KEY";
  model = "gemini-3-pro-preview";
  haikuModel = "gemini-3.5-flash";
  disallowedTools = [
    "WebFetch"
    "WebSearch"
  ];
}
