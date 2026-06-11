# Cilium CNI + Gateway API CRDs — deployed via helm CLI during bootstrap.
#
# Cilium's rendered manifest is large, so keep it out of Talos inline manifests
# and install it via helm CLI after the k8s API is reachable.

locals {
  cilium_version      = "1.19.2"
  gateway_api_version = "v1.5.1"
}

resource "null_resource" "gateway_api_crds" {
  depends_on = [
    null_resource.wait_for_k8s_api,
    local_file.kubeconfig,
  ]

  provisioner "local-exec" {
    environment = {
      KUBECONFIG = local_file.kubeconfig.filename
    }
    # Experimental channel — Cilium 1.19 requires TLSRoute v1alpha2, which the
    # standard channel dropped in v1.5.0 (graduated to v1 only).
    # Server-side apply required: HTTPRoute CRD exceeds 256KB annotation limit.
    command = <<-EOT
      set -e
      kubectl apply --server-side --force-conflicts \
        -f https://github.com/kubernetes-sigs/gateway-api/releases/download/${local.gateway_api_version}/experimental-install.yaml
      kubectl wait --for=condition=Established \
        crd/gatewayclasses.gateway.networking.k8s.io \
        crd/gateways.gateway.networking.k8s.io \
        crd/httproutes.gateway.networking.k8s.io \
        crd/referencegrants.gateway.networking.k8s.io \
        crd/tlsroutes.gateway.networking.k8s.io \
        --timeout=60s
    EOT
  }
}

resource "null_resource" "cilium_bootstrap" {
  depends_on = [
    null_resource.gateway_api_crds,
    local_file.kubeconfig,
  ]

  provisioner "local-exec" {
    environment = {
      KUBECONFIG = local_file.kubeconfig.filename
    }
    command = <<-EOT
      set -e
      helm repo add cilium https://helm.cilium.io/ && helm repo update cilium
      helm upgrade --install cilium cilium/cilium \
        --version ${local.cilium_version} \
        --namespace kube-system \
        --create-namespace \
        -f ${path.module}/cilium-values.yaml \
        --wait \
        --wait-for-jobs \
        --atomic \
        --timeout 600s
    EOT
  }
}

# Wait for Kubernetes API to be accessible before installing Cilium
resource "null_resource" "wait_for_k8s_api" {
  depends_on = [
    talos_machine_bootstrap.cluster,
    local_file.kubeconfig
  ]

  provisioner "local-exec" {
    environment = {
      KUBECONFIG = local_file.kubeconfig.filename
    }
    command = "timeout 600 bash -c 'until kubectl get nodes --request-timeout=30s 2>/dev/null; do sleep 10; done'"
  }
}

# Wait for all nodes to be Ready
resource "null_resource" "wait_for_nodes_ready" {
  depends_on = [null_resource.cilium_bootstrap]

  provisioner "local-exec" {
    environment = {
      KUBECONFIG = local_file.kubeconfig.filename
    }
    command = "kubectl wait --for=condition=Ready node --all --timeout=600s"
  }
}
