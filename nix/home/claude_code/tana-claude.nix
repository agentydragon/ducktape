# `tana-claude`: Claude Code on Tana-UI models via the cluster LiteLLM proxy, reading the
# litellm_tana_key sops secret — a tana-scoped virtual key (SSOT in
# tf/gitops/litellm-keys). The main proxy's in-process Tana provider owns the Tana
# credential; this client key never carries it. Tana encodes reasoning effort in the model name, so each entry is one
# family at its default effort (see cluster/cdk8s/litellm/test_config.py). See
# ./gateway.nix for the shared wrapper pattern.
{ pkgs, config }:
let
  inherit (pkgs) lib;
in
import ./gateway.nix { inherit pkgs lib; } "tana-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenFile = config.sops.secrets.litellm_tana_key.path;
  model = "tana/ant-messages/claude-sonnet-4-6";
  haikuModel = "tana/ant-messages/claude-haiku-4-5";
  gatewayDiscovery = true;
}
