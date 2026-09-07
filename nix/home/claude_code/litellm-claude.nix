# `litellm-claude`: Claude Code on its ordinary Claude models, but routed through the cluster
# LiteLLM proxy (→ CLIProxyAPI's Claude OAuth session) instead of straight to Anthropic —
# which is the whole point, since plain `claude` already reaches the subscription. The detour
# buys Langfuse traces, spend visibility, and one revocable key. Its siblings are named for
# their backends because driving non-Claude models is what makes them unusual; here the
# models are unremarkable and the proxy is the deviation.
#
# Reads $CLAUDE_SUBSCRIPTION_LITELLM_KEY, a virtual key scoped to the subscription roster
# (SSOT in tf/gitops/litellm-keys). CLIProxyAPI holds the OAuth session, so this key never
# carries it. Mirrors the in-cluster haku-console `claude_code` harness
# (cluster/k8s/haku/console/config.yaml), which runs this same lane. See ./gateway.nix for
# the shared wrapper pattern.
#
# No maxContextTokens, deliberately: the route embeds a Claude family token, Claude Code
# normalizes it by substring to the canonical model, and for a recognized model it ignores
# CLAUDE_CODE_MAX_CONTEXT_TOKENS and uses its own table. Setting one here would be inert.
# The sibling wrappers need it only because their routes name non-Claude models.
#
# Costs read as fiction: Claude Code prices from that same static table at Anthropic list
# rates, while a subscription call bills nothing. Traces are honest; `costUSD` is not.
{ pkgs, config }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "litellm-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenFile = config.sops.secrets.litellm_claude_subscription_key.path;
  # Sonnet as the default, matching the haku-console harness: Opus is a one-word change,
  # but it draws down the subscription's quota far faster. The `[1m]` suffix is Claude
  # Code's own convention for requesting the 1M-context beta (see the `fO` alias table);
  # it's checked by a bare regex against the model-ID string with no provider gate, so it
  # survives being routed through this proxy. Matched by a new model_name entry in
  # cluster/k8s/litellm/app/proxy-config.yaml.
  model = "anthropic-max20/ant-messages/claude-sonnet-5[1m]";
  haikuModel = "anthropic-max20/ant-messages/claude-haiku-4-5-20251001";
  # LiteLLM filters /v1/models by the key's allowlist (`get_complete_model_list` prefers a
  # non-empty key list), so discovery offers exactly the subscription roster.
  gatewayDiscovery = true;
}
