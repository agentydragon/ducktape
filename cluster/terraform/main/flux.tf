# Flux GitOps bootstrap — requires infrastructure to be applied first.
#
# The Flux bootstrap manifests in cluster/k8s/flux/flux-system are the source of truth. Keep
# OpenTofu out of the business of regenerating them: Flux bootstrap
# customization is reviewed in git, and this resource only seeds those committed
# manifests into an empty cluster.

locals {
  flux_bootstrap_components_path = "${path.module}/../../k8s/flux/flux-system/gotk-components.yaml"
  flux_bootstrap_sync_path       = "${path.module}/../../k8s/flux/flux-system/gotk-sync.yaml"
}

removed {
  from = flux_bootstrap_git.cluster

  lifecycle {
    destroy = false
  }
}

resource "null_resource" "flux_bootstrap" {
  triggers = {
    gotk_components_sha256 = filesha256(local.flux_bootstrap_components_path)
    gotk_sync_sha256       = filesha256(local.flux_bootstrap_sync_path)
  }

  depends_on = [
    kubernetes_secret.sops_age_cluster_secrets,
    local_file.kubeconfig,
    null_resource.wait_for_nodes_ready,
  ]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]

    environment = {
      KUBECONFIG = local_file.kubeconfig.filename
    }

    command = <<-EOT
      set -euo pipefail

      kubectl apply -f ${local.flux_bootstrap_components_path}
      kubectl wait --for=condition=Established \
        crd/gitrepositories.source.toolkit.fluxcd.io \
        crd/kustomizations.kustomize.toolkit.fluxcd.io \
        --timeout=60s
      kubectl -n flux-system rollout status deployment/source-controller --timeout=300s
      kubectl -n flux-system rollout status deployment/kustomize-controller --timeout=300s

      kubectl apply -f ${local.flux_bootstrap_sync_path}
      kubectl -n flux-system wait gitrepository.source.toolkit.fluxcd.io/flux-system \
        --for=condition=Ready \
        --timeout=300s
      kubectl -n flux-system wait kustomization.kustomize.toolkit.fluxcd.io/flux-system \
        --for=condition=Ready \
        --timeout=600s
  EOT
  }
}
