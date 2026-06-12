# Bootstrap Process Analysis: Dependency Guarantees and Gaps

## Overview

This document analyzes the actual bootstrap process, identifying what dependencies are guaranteed by our current
configuration and where gaps exist that could cause race conditions or failures.

## Bootstrap Stages and Dependency Guarantees

### Stage 1: Terraform Provider Initialization

**What happens:**

- Terraform reads provider configurations
- Proxmox provider initializes with credentials
- Talos provider initializes

**Guarantees:**

- ✅ Proxmox provider has hardcoded endpoint/credentials - always available
- ✅ Talos provider doesn't need cluster to exist yet

**Gaps:** None

### Stage 2: VM Creation (Proxmox)

**What happens:**

- `module.nodes` creates VMs via Proxmox provider
- VMs boot with Talos installer image

**Guarantees:**

- ✅ Terraform DAG ensures VMs created in parallel
- ✅ Each VM's `proxmox_virtual_environment_vm` resource completes only when VM is started

**Gaps:** None - Proxmox provider handles this correctly

### Stage 3: Talos Bootstrap

**What happens:**

1. `talos_machine_secrets.talos` generates cluster secrets
2. `data.talos_machine_configuration.config` generates configs for each node
3. `talos_machine_configuration_apply.apply` applies config to each node
4. `talos_machine_bootstrap.talos` bootstraps the cluster on first control plane

**Guarantees:**

- ✅ `talos_machine_configuration_apply` has implicit dependency on VM resources (via IP references)
- ✅ `talos_machine_bootstrap` explicitly depends on all machine configs via `depends_on`
- ✅ Bootstrap waits for etcd quorum

**Gaps:**

- ⚠️ No explicit wait for all nodes to be healthy before proceeding
- ⚠️ Worker nodes with kubelet volume issues aren't detected here

### Stage 4: Kubeconfig Generation

**What happens:**

- `talos_cluster_kubeconfig.talos` generates kubeconfig
- `local_file.kubeconfig` writes it to disk

**Guarantees:**

- ✅ `talos_cluster_kubeconfig` depends on `talos_machine_bootstrap`
- ✅ Provider configuration for kubernetes/helm uses `local_file.kubeconfig.filename`

**Gaps:** None - clean dependency chain

### Stage 5: Kubernetes/Helm Provider Initialization

**What happens:**

- Kubernetes provider initializes using kubeconfig
- Helm provider initializes using kubeconfig

**Guarantees:**

- ✅ Providers can't initialize until kubeconfig file exists (implicit dependency)
- ✅ Provider initialization validates API server connectivity

**Gaps:**

- ⚠️ No guarantee all nodes are Ready (just API server responding)

### Stage 6: CNI Installation (Cilium)

**What happens:**

- `helm_release.cilium_bootstrap` installs Cilium

**Current code:**

```hcl
resource "helm_release" "cilium_bootstrap" {
  depends_on = [
    talos_machine_bootstrap.talos,
    talos_cluster_kubeconfig.talos
  ]
  wait = true  # Critical!
}
```

**Guarantees:**

- ✅ `wait = true` means Helm waits for Cilium pods to be Ready
- ✅ Explicit `depends_on` ensures cluster exists

**Gaps:**

- ⚠️ No explicit wait for ALL Cilium pods across ALL nodes
- ⚠️ Could return success even if some nodes' Cilium pods are pending

### Stage 7: Sealed Secrets Keypair Deployment

**What happens:**

- `tls_private_key.sealed_secrets` generates keypair
- `kubernetes_secret.sealed_secrets_key` deploys it

**Current code:**

```hcl
resource "kubernetes_secret" "sealed_secrets_key" {
  depends_on = [
    helm_release.cilium_bootstrap
  ]
}
```

**Guarantees:**

- ✅ Explicit dependency on Cilium ensures network is ready
- ✅ Kubernetes provider validates secret creation

**Gaps:**

- ⚠️ No dependency on nodes being Ready
- ⚠️ Secret could be created even if some nodes can't schedule pods

### Stage 8: Flux Bootstrap

**What happens:**

- `null_resource.flux_bootstrap` runs flux bootstrap command

**Current code:**

```hcl
resource "null_resource" "flux_bootstrap" {
  depends_on = [
    kubernetes_secret.sealed_secrets_key,
    null_resource.wait_for_k8s_api
  ]
}
```

**Guarantees:**

- ✅ Depends on sealed secrets keypair
- ✅ `wait_for_k8s_api` ensures API is responsive
- ✅ Flux bootstrap command fails if cluster unhealthy

**Gaps:**

- ⚠️ No wait for nodes to be Ready
- ⚠️ No wait for Cilium to be fully operational on all nodes

### Stage 9: GitOps Reconciliation

**What happens:**

- Flux reconciles kustomizations from git
- Installs sealed-secrets controller, CSI driver, etc.

**Guarantees in Flux:**

```yaml
# storage/kustomization.yaml
spec:
  dependsOn:
    - name: sealed-secrets
```

**Guarantees:**

- ✅ Flux respects `dependsOn` fields
- ✅ Health checks in kustomizations

**Gaps:**

- ⚠️ If nodes aren't Ready, pods may not schedule
- ⚠️ No detection of worker node kubelet volume issues

## Critical Gaps and Solutions

### Gap 1: No Wait for All Nodes Ready

**Problem:** Terraform proceeds even if worker nodes have kubelet volume mount issues

**Solution:**

```hcl
resource "null_resource" "wait_for_nodes_ready" {
  depends_on = [helm_release.cilium_bootstrap]

  provisioner "local-exec" {
    command = <<-EOF
      echo "Waiting for all nodes to be Ready..."
      kubectl wait --for=condition=Ready nodes --all --timeout=600s

      # Verify kubelet on each node
      for node in $(kubectl get nodes -o name); do
        kubectl get $node -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -q True || exit 1
      done
    EOF
  }
}

# Make sealed secrets depend on this
resource "kubernetes_secret" "sealed_secrets_key" {
  depends_on = [
    helm_release.cilium_bootstrap,
    null_resource.wait_for_nodes_ready  # ADD THIS
  ]
}
```

### Gap 2: No Cilium Full Health Check

**Problem:** Helm might return before Cilium is fully operational on all nodes

**Solution:**

```hcl
resource "null_resource" "wait_for_cilium_ready" {
  depends_on = [helm_release.cilium_bootstrap]

  provisioner "local-exec" {
    command = <<-EOF
      # Wait for all Cilium pods
      kubectl wait --for=condition=ready pod -l k8s-app=cilium -n kube-system --timeout=300s

      # Verify Cilium status if CLI available
      if command -v cilium >/dev/null 2>&1; then
        cilium status --wait
      fi
    EOF
  }
}
```

### Gap 3: No CSI Driver Health Verification

**Problem:** Storage kustomization might appear ready but CSI pods are crashing

**Solution:**

```hcl
resource "null_resource" "wait_for_csi" {
  depends_on = [null_resource.flux_bootstrap]

  provisioner "local-exec" {
    command = <<-EOF
      # Wait for CSI controller and all node plugins
      kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/name=proxmox-csi-plugin \
        -n csi-proxmox --timeout=300s
    EOF
  }
}
```

### Gap 4: No Detection of Talos Node Issues

**Problem:** Worker nodes with kubelet volume mount failures go undetected

**Solution:**

```hcl
resource "null_resource" "verify_talos_services" {
  depends_on = [talos_machine_bootstrap.talos]
  for_each = var.nodes

  provisioner "local-exec" {
    command = <<-EOF
      talosctl service kubelet -n ${each.value.ip} | grep -q Running || \
        (echo "Kubelet not running on ${each.key}" && exit 1)
    EOF
  }
}
```

## Recommended Changes Priority

1. **HIGH**: Add `wait_for_nodes_ready` check after Cilium
2. **HIGH**: Make Flux bootstrap depend on all nodes being Ready
3. **MEDIUM**: Add explicit Cilium health verification
4. **MEDIUM**: Add CSI driver health checks in terraform
5. **LOW**: Add Talos service verification

## Current State Assessment

**What works:**

- Basic dependency chain is correct
- Flux dependencies work well
- Sealed secrets keypair is deployed before Flux

**What's fragile:**

- Worker node failures are not detected early
- Race conditions possible if nodes are slow to become Ready
- No verification that critical services are actually healthy before proceeding

**Reliability:** ~80% - Works when everything boots normally, fails when nodes have issues

## Conclusion

Our current dependencies are **mostly sufficient** for the happy path, but lack robustness for detecting and
handling common failure modes like:

- Worker nodes failing to mount kubelet volumes
- Partial CNI deployment
- Slow node initialization

The main issue is that we proceed based on **resource creation** rather than **actual health verification**.
Adding explicit health checks at each stage would make the bootstrap process much more reliable.
