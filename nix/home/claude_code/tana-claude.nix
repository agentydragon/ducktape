# `tana-claude`: Claude Code on Tana-UI models via the cluster LiteLLM proxy, reading the
# litellm_tana_key sops secret — a tana-scoped virtual key retained in
# secrets/litellm-tana-clients-key.yaml. The client key/team are removed while parked; recreate
# them with a reviewed allowlist before enabling this retained renderer.
# The main proxy's in-process Tana provider owns the Tana
# credential; this client key never carries it. Tana encodes reasoning effort in the model name, so each entry is one
# family at its default effort (see cluster/cdk8s/litellm/test_config.py). See
# ./gateway.nix for the shared wrapper pattern.
{ pkgs, config }:
let
  inherit (pkgs) lib;
  # Keep the renderer, but fail clearly if imported while the route selection is parked.
  models =
    (lib.importJSON ../../../model_catalog/claude-wrappers.json).tana-claude
      or (throw "tana-claude is parked (#9574); review gateway limits and restore its routes and recreate its client key/team before enabling");
in
import ./gateway.nix { inherit pkgs lib; } "tana-claude" {
  baseUrl = "https://litellm.allegedly.works";
  authTokenFile = config.sops.secrets.litellm_tana_key.path;
  inherit (models) model;
  inherit (models) haikuModel;
  gatewayDiscovery = true;
}
