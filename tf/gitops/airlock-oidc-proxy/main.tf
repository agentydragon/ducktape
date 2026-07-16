terraform {
  required_version = ">= 1.0"

  backend "kubernetes" {
    secret_suffix = "airlock-oidc-proxy"
    namespace     = "flux-system"
  }
}

data "kubernetes_secret" "authentik_bootstrap" {
  metadata {
    name      = "authentik-bootstrap"
    namespace = "authentik"
  }
}

provider "authentik" {
  url   = "http://authentik-server.authentik.svc.cluster.local"
  token = data.kubernetes_secret.authentik_bootstrap.data["AUTHENTIK_BOOTSTRAP_TOKEN"]
}

# This intentionally empty module keeps the existing Terraform state alive for
# one final apply, which destroys the Airlock OIDCProxy provider, application,
# policy binding, and Kubernetes client-credentials Secret.
#
# CLEANUP(added 2026-07-15): Delete tf/gitops/airlock-oidc-proxy,
#   cluster/k8s/agents/airlock-oidc-proxy-tf, and the root Flux Kustomization
#   entry after Terraform/airlock-oidc-proxy is Ready on this revision with an
#   empty plan and the airlock-oidc-proxy Authentik application/provider plus
#   airlock/airlock-oidc-proxy-credentials Secret are all absent.
