# Gaffer-private Flux deploy-key.
#
# CLEANUP(added 2026-05-03): superseded by ducktape-automation GitHub App auth
# (cluster/k8s/flux-system/ducktape-automation-github-app.sops.yaml).
# `gaffer-private` GitRepository now references that Secret directly. To
# retire this module: drop prevent_destroy on all three resources below,
# `tofu destroy` (revokes the deploy key, deletes the in-cluster Secret),
# then delete this directory + BUILD.bazel target + the
# cluster/k8s/gaffer-private-source/{deploy-key-tf,github-pat-gaffer-private-flux.sops}.yaml
# manifests.
#
# Self-contained: generates an ED25519 keypair, registers the public half
# as a write-scoped GitHub deploy key on agentydragon/gaffer-private, and
# writes the flux SSH GitRepository secret consumed by the
# `gaffer-private` GitRepository
# (cluster/k8s/gaffer-private-source/source.yaml).
#
# Auth: fine-grained PAT with Administration:R/W on
# agentydragon/gaffer-private, deployed as the
# `github-pat-gaffer-private-flux` Secret via SOPS-encrypted manifest in
# cluster/k8s/gaffer-private-source/.
#
# Rotation: bump tls_private_key.gaffer_private_flux's keepers and TF
# generates a new keypair, re-registers it on GitHub, and rotates the
# k8s secret atomically. prevent_destroy guards against accidental
# destruction (matching the existing ducktape flux-system deploy-key
# lifecycle).

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

  lifecycle { prevent_destroy = true }
}

resource "github_repository_deploy_key" "gaffer_private_flux" {
  title      = "flux-image-automation"
  repository = "gaffer-private"
  key        = tls_private_key.gaffer_private_flux.public_key_openssh
  read_only  = false

  lifecycle { prevent_destroy = true }
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

  lifecycle { prevent_destroy = true }

  depends_on = [github_repository_deploy_key.gaffer_private_flux]
}
