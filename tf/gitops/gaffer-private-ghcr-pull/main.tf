# GHCR pull-secret automation for gaffer-private private images.
#
# Reads a SOPS-deployed read:packages PAT and synthesizes a single
# `kubernetes.io/dockerconfigjson` Secret named `gaffer-ghcr-pull` in
# `flux-system`. Reflector mirrors it into each consumer namespace
# (auto-reflection annotations below). Adding a new gaffer-private app
# only needs the namespace name added to the auto-namespaces list.

data "kubernetes_secret" "ghcr_pat" {
  metadata {
    name      = "github-pat-ghcr-read"
    namespace = "flux-system"
  }
}

locals {
  dockerconfigjson = jsonencode({
    auths = {
      "ghcr.io" = {
        auth = base64encode("agentydragon:${data.kubernetes_secret.ghcr_pat.data["token"]}")
      }
    }
  })

  # Namespaces that need to pull gaffer-private images. Reflector
  # auto-mirrors the source secret into each of these as
  # `gaffer-ghcr-pull`. Add a new namespace here when wiring up a new
  # gaffer-private app.
  consumer_namespaces = [
    "augur",
    "listing-monitor",
    "thrive-scraper",
  ]
}

resource "kubernetes_secret" "ghcr_pull" {
  metadata {
    name      = "gaffer-ghcr-pull"
    namespace = "flux-system"
    annotations = {
      description = "Read-only GHCR pull credential for gaffer-private's private images. Used directly by Flux ImageRepository scanning; reflected by Reflector into each consumer namespace as `gaffer-ghcr-pull` for kubelet image pulls."

      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = join(",", local.consumer_namespaces)
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = join(",", local.consumer_namespaces)
    }
  }
  type = "kubernetes.io/dockerconfigjson"
  data = {
    ".dockerconfigjson" = local.dockerconfigjson
  }
}
