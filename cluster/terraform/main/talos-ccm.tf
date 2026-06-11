# Talos Cloud Controller Manager — inline manifest for bootstrap.
#
# Rendered from the same Helm chart + values as the Flux HelmRelease at
# k8s/talos-cloud-controller-manager/helmrelease.yaml. Tofu parses that
# file directly so they can never drift.
#
# Why inline manifest: The CCM removes the
# node.cloudprovider.kubernetes.io/uninitialized taint from nodes. Without
# it running before Flux, no workloads (including Flux itself) can schedule
# on Talos nodes.

locals {
  talos_ccm_helmrelease = yamldecode(
    file("${path.module}/../../k8s/talos-cloud-controller-manager/helmrelease.yaml")
  )

  talos_ccm_chart   = local.talos_ccm_helmrelease.spec.chart.spec.chart
  talos_ccm_repo    = "oci://ghcr.io/siderolabs/charts"
  talos_ccm_values  = local.talos_ccm_helmrelease.spec.values
  talos_ccm_version = try(local.talos_ccm_helmrelease.spec.chart.spec.version, null)
}

data "helm_template" "talos_ccm" {
  name       = local.talos_ccm_chart
  namespace  = "kube-system"
  repository = local.talos_ccm_repo
  chart      = local.talos_ccm_chart
  version    = local.talos_ccm_version
  values     = [yamlencode(local.talos_ccm_values)]
}
