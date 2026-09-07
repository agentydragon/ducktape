# `max-claude`: Claude Code on the Anthropic Max subscription via the cluster LiteLLM proxy
# (→ CLIProxyAPI's Claude OAuth session), reading $MAX_LITELLM_KEY — a subscription-scoped
# virtual key (SSOT in tf/gitops/litellm-keys). The upstream OAuth session lives in
# CLIProxyAPI, so this key never carries it. Mirrors the in-cluster haku-console
# `claude_code` harness (cluster/k8s/haku/console/config.yaml), which runs this same lane.
# See ./gateway.nix for the shared wrapper pattern.
#
# No maxContextTokens, deliberately: the route embeds a Claude family token, Claude Code
# normalizes it by substring to the canonical model, and for a recognized model it ignores
# CLAUDE_CODE_MAX_CONTEXT_TOKENS and uses its own table. Setting one here would be inert.
# The sibling wrappers need it only because their routes name non-Claude models.
#
# Costs read as fiction: Claude Code prices from that same static table at Anthropic list
# rates, while a subscription call bills nothing. Traces are honest; `costUSD` is not.
{ pkgs }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "max-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenEnvVar = "MAX_LITELLM_KEY";
  # Sonnet as the default, matching the haku-console harness: Opus is a one-word change,
  # but it draws down the subscription's quota far faster.
  model = "anthropic-max20/ant-messages/claude-sonnet-5";
  haikuModel = "anthropic-max20/ant-messages/claude-haiku-4-5-20251001";
  # LiteLLM filters /v1/models by the key's allowlist (`get_complete_model_list` prefers a
  # non-empty key list), so discovery offers exactly the subscription roster.
  gatewayDiscovery = true;
}
