# `tana-claude`: Claude Code on Tana-UI models via the cluster LiteLLM proxy (→
# tana-litellm), reading $TANA_LITELLM_KEY — a tana-scoped virtual key (SSOT in
# tf/gitops/litellm-keys). LiteLLM fronts the DB-less tana-litellm with the `anthropic/`
# provider; the upstream reaches tana-litellm with the in-cluster master key, so this key
# never carries it. Tana encodes reasoning effort in the model name, so each entry is one
# family at its default effort (see cluster/k8s/litellm/app/generate_litellm.py). See
# ./gateway.nix for the shared wrapper pattern.
{ pkgs }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "tana-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenEnvVar = "TANA_LITELLM_KEY";
  model = "tana-claude-sonnet-4-6";
  haikuModel = "tana-claude-haiku-4-5";
  gatewayDiscovery = true;
}
