# Gaffer-private Flux deploy-key retirement.
#
# The `gaffer-private` GitRepository uses the ducktape-automation GitHub App
# secret directly. This module is kept only while the Terraform CR runs its
# destroy plan to revoke the old deploy key and delete its generated Secret.
# Keep `github-pat-gaffer-private-flux` present until that destroy has completed.

provider "github" {
  owner = "agentydragon"
  token = data.kubernetes_secret.github_pat.data["token"]
}

data "kubernetes_secret" "github_pat" {
  metadata {
    name      = "github-pat-gaffer-private-flux"
    namespace = "flux-system"
  }
}

resource "tls_private_key" "gaffer_private_flux" {
  algorithm = "ED25519"
}

resource "github_repository_deploy_key" "gaffer_private_flux" {
  title      = "flux-image-automation"
  repository = "gaffer-private"
  key        = tls_private_key.gaffer_private_flux.public_key_openssh
  read_only  = false
}

resource "kubernetes_secret" "gaffer_private_deploy_key" {
  metadata {
    name      = "gaffer-private-deploy-key"
    namespace = "flux-system"
    annotations = {
      description = "SSH deploy key consumed by the gaffer-private GitRepository. Managed end-to-end by tofu-controller — keypair, GitHub registration, k8s secret all in one apply."
    }
  }

  data = {
    identity       = tls_private_key.gaffer_private_flux.private_key_openssh
    "identity.pub" = tls_private_key.gaffer_private_flux.public_key_openssh
    # github.com ed25519 host key, published at
    # https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
    known_hosts = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
  }

  type = "Opaque"

  depends_on = [github_repository_deploy_key.gaffer_private_flux]
}
