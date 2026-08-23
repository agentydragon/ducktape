# BuildBuddy remote cache/execution credentials.
# Exports the API key and renders ~/.config/bazel/buildbuddy.bazelrc from it.
{ config, ... }:
{
  # The shared cluster Secret is the only copy of this key; .sops.yaml grants
  # this file the workstation and agent keys so it can be read here directly.
  # Declaring it through sopsEnv also gives the template below its placeholder.
  ducktape.sopsEnv.BUILDBUDDY_API_KEY = {
    sopsFile = ../../../cluster/k8s/agents/shared-secrets/buildbuddy-api-key.sops.yaml;
    key = "stringData/api-key";
    name = "buildbuddy_api_key";
  };

  sops.templates."buildbuddy.bazelrc" = {
    path = "${config.xdg.configHome}/bazel/buildbuddy.bazelrc";
    content = ''
      # Credential only: repositories decide whether to enable the rbe config.
      common:rbe --remote_header=x-buildbuddy-api-key=${config.sops.placeholder.buildbuddy_api_key}
    '';
    mode = "0600";
  };
}
