# `gemini-claude`: Claude Code on Google Gemini via the cluster LiteLLM proxy, reading
# $GEMINI_LITELLM_KEY — a gemini-scoped virtual key (SSOT in tf/gitops/litellm-keys).
# LiteLLM translates Claude Code's Anthropic /v1/messages calls to the `gemini/` provider;
# the upstream reaches Google with the in-cluster GEMINI_API_KEY, so this key never carries
# it. WebFetch/WebSearch are disabled: they are Anthropic-hosted server tools a non-Anthropic
# backend cannot execute. The
# gemini-clients team falls back to the flash-lite tier, so a quota-throttled preview
# model (gemini-3.1-pro-preview has tight Google quota) degrades instead of hard-failing. See
# ./gateway.nix for the shared wrapper pattern.
#
# Prompt caching (settled empirically 2026-07-18, do not relitigate): Claude Code's
# Anthropic `cache_control` breakpoints do NOT translate through LiteLLM's Anthropic→Gemini
# adapter, but Gemini's automatic implicit caching replaces them — Google caches a stable
# leading prefix for 2.5+/3.x (~75% off cached input, best-effort so not every call hits),
# and LiteLLM 1.90.2 forwards a byte-stable prefix and reports it (`cached_tokens` /
# `cache_read_input_tokens`). So no 100x cost blowup; you trade Anthropic's deterministic
# ~90% cache for Gemini's best-effort ~75%.
{ pkgs }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "gemini-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenEnvVar = "GEMINI_LITELLM_KEY";
  model = "google/oai-chat/gemini-3.1-pro-preview";
  haikuModel = "google/oai-chat/gemini-3.5-flash-lite";
  disallowedTools = [
    "WebFetch"
    "WebSearch"
  ];
}
