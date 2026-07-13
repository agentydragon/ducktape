# BuildBuddy remote cache/execution credentials.
# Decrypts the API key from SOPS using ~/.ssh/id_ed25519 and renders
# ~/.config/bazel/buildbuddy.bazelrc via sops-nix templates.
{ config, ... }:
{
  sops.secrets.buildbuddy_api_key = {
    sopsFile = ../../../secrets/buildbuddy.yaml;
  };

  sops.templates."buildbuddy.bazelrc" = {
    path = "${config.xdg.configHome}/bazel/buildbuddy.bazelrc";
    content = ''
      # Credential only: repositories decide whether to enable the rbe config.
      common:rbe --remote_header=x-buildbuddy-api-key=${config.sops.placeholder.buildbuddy_api_key}
    '';
    mode = "0600";
  };

  # TODO: consider moving to repo-local .envrc with sops caching
  ducktape.sopsEnv.BUILDBUDDY_API_KEY = {
    sopsFile = ../../../secrets/buildbuddy.yaml;
    key = "buildbuddy_api_key";
  };
}
