# Low-privilege agent kubeconfig rendered from a rotated SOPS JWT.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.agentKubeconfig;
  kubeconfigLines = [
    "apiVersion: v1"
    "kind: Config"
    "clusters:"
    "- name: cluster"
    "  cluster:"
    "    server: ${cfg.server}"
    "contexts:"
    "- name: ${cfg.user}"
    "  context:"
    "    cluster: cluster"
    "    user: ${cfg.user}"
  ]
  ++ lib.optional (cfg.namespace != null) "    namespace: ${cfg.namespace}"
  ++ [
    "current-context: ${cfg.user}"
    "users:"
    "- name: ${cfg.user}"
    "  user:"
    "    token: \"${config.sops.placeholder.agent_k8s_jwt}\""
  ];
in
{
  # TODO: dedupe this template with devinfra/k8s/kubeconfig.py or another shared
  # kubeconfig materialization path. This starts simple for agent-box, but the
  # token->kubeconfig shape should not drift across agent environments.
  options.ducktape.agentKubeconfig = {
    enable = lib.mkEnableOption "agent bearer-token kubeconfig";
    sopsFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to the SOPS-encrypted YAML file containing a jwt key.";
    };
    server = lib.mkOption {
      type = lib.types.str;
      default = "https://kubeapi.allegedly.works";
      description = "Kubernetes API server URL.";
    };
    user = lib.mkOption {
      type = lib.types.str;
      description = "Kubeconfig user and context name.";
    };
    namespace = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional default namespace for the rendered context.";
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets.agent_k8s_jwt = {
      inherit (cfg) sopsFile;
      key = "jwt";
    };

    sops.templates."agent-kubeconfig" = {
      path = "${config.home.homeDirectory}/.kube/config";
      content = lib.concatStringsSep "\n" kubeconfigLines + "\n";
      mode = "0600";
    };
  };
}
