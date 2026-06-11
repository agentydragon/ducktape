# Talos cluster client configuration, decrypted from SOPS at home-manager activation time.
# Placed at ~/.talos/config using the shared secrets/shared/talosconfig.yaml secret.
# The cluster envrc overrides TALOSCONFIG to cluster/terraform/main/talosconfig.yml when
# inside the cluster directory; this provides the fallback for all other contexts.
{ config, ... }:
{
  sops.secrets.talosconfig = {
    sopsFile = ../../../secrets/shared/talosconfig.yaml;
    key = "talosconfig";
    path = "${config.home.homeDirectory}/.talos/config";
    mode = "0600";
  };
}
