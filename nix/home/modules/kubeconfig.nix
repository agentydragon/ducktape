# Kubernetes admin kubeconfig, decrypted from SOPS at home-manager activation time.
# Placed at ~/.kube/config using the shared secrets/shared/kubeconfig.yaml secret.
# The cluster envrc overrides KUBECONFIG to cluster/terraform/main/kubeconfig when
# inside the cluster directory; this provides the fallback for all other contexts.
{ config, ... }:
{
  sops.secrets.kubeconfig = {
    sopsFile = ../../../secrets/shared/kubeconfig.yaml;
    key = "kubeconfig";
    path = "${config.home.homeDirectory}/.kube/config";
    mode = "0600";
  };
}
