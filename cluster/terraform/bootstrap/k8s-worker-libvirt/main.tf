# K8s Worker (Libvirt) — NixOS VM joining the Talos cluster via KubeSpan
#
# Local QEMU/KVM variant. No Proxmox dependency.
# Image is built locally via `nix build` (no SCP upload).
#
# After boot, kubespand and kubelet auto-start.
# Approve the CSR: kubectl certificate approve <csr-name>

# =============================================================================
# REMOTE STATE
# =============================================================================

data "terraform_remote_state" "infrastructure" {
  backend = "local"
  config = {
    path = "../infrastructure/terraform.tfstate"
  }
}

# =============================================================================
# LOCALS
# =============================================================================

locals {
  # SSH key handling - try common key types in order of preference
  ssh_key_candidates = [
    pathexpand("~/.ssh/id_ed25519.pub"),
    pathexpand("~/.ssh/id_ecdsa.pub"),
    pathexpand("~/.ssh/id_rsa.pub")
  ]
  ssh_key_path = var.ssh_public_key != "" ? "" : (
    fileexists(local.ssh_key_candidates[0]) ? local.ssh_key_candidates[0] :
    fileexists(local.ssh_key_candidates[1]) ? local.ssh_key_candidates[1] :
    fileexists(local.ssh_key_candidates[2]) ? local.ssh_key_candidates[2] :
    ""
  )
  ssh_public_key = var.ssh_public_key != "" ? var.ssh_public_key : (
    local.ssh_key_path != "" ? trimspace(file(local.ssh_key_path)) : ""
  )

  # NixOS image build inputs
  nix_dir_hash = sha1(join("", [for f in sort(fileset("${path.module}/../../../../nix", "**/*.nix")) : filesha1("${path.module}/../../../../nix/${f}")]))
  repo_root    = "${path.module}/../../../.."

  # K8s credentials from infrastructure state
  infra = data.terraform_remote_state.infrastructure.outputs

  # The CA cert from talos_machine_secrets is already base64-encoded.
  # Decode it for the PEM file written by cloud-init.
  k8s_ca_cert_pem = base64decode(local.infra.k8s_ca_cert)

  # Construct bootstrap kubeconfig from infrastructure state.
  # Server is localhost:7445 — HAProxy on the VM proxies to api.allegedly.works:6443.
  bootstrap_kubeconfig = yamlencode({
    apiVersion = "v1"
    kind       = "Config"
    clusters = [{
      name = "kubernetes"
      cluster = {
        certificate-authority-data = local.infra.k8s_ca_cert
        server                     = "https://localhost:7445"
      }
    }]
    contexts = [{
      name = "bootstrap@kubernetes"
      context = {
        cluster = "kubernetes"
        user    = "bootstrap"
      }
    }]
    current-context = "bootstrap@kubernetes"
    users = [{
      name = "bootstrap"
      user = {
        token = local.infra.k8s_bootstrap_token
      }
    }]
  })

  # Render cloud-init from the shared template in proxmox-vm module
  cloud_init_user_data = templatefile("${path.module}/../../../../terraform/modules/proxmox-vm/cloud-init.yaml.tpl", {
    k8s_cluster_join = {
      bootstrap_kubeconfig = local.bootstrap_kubeconfig
      ca_cert              = local.k8s_ca_cert_pem
      cluster_id           = local.infra.kubespan_cluster_id
      cluster_secret       = local.infra.kubespan_cluster_secret
      node_name            = "k8s-worker-test"
    }
  })
}

# =============================================================================
# PROVIDERS
# =============================================================================

provider "libvirt" {
  uri = var.libvirt_uri
}

# =============================================================================
# VALIDATION
# =============================================================================

check "ssh_key_required" {
  assert {
    condition     = local.ssh_public_key != ""
    error_message = "No SSH public key found. Provide via ssh_public_key variable or create ~/.ssh/id_ed25519."
  }
}

# =============================================================================
# NIXOS IMAGE (local build, no upload)
# =============================================================================

resource "null_resource" "nixos_image_build" {
  triggers = {
    nix_dir_hash = local.nix_dir_hash
    flake_target = "k8s-worker-test"
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      echo "Building k8s-worker-test NixOS qcow2 image..."
      cd "${local.repo_root}"
      nix build ./nix#k8s-worker-test-image -o k8s-worker-test-image
      echo "Image ready at $(readlink -f k8s-worker-test-image)/*.qcow2"
    EOT
  }
}

# =============================================================================
# VM INSTANCE
# =============================================================================

module "k8s_worker_test" {
  source = "../../../../terraform/modules/libvirt-vm"

  vm_name          = "k8s-worker-test"
  vcpus            = 4
  memory_mb        = 8192
  disk_size_gb     = 50
  auto_start       = true
  qcow2_image_path = "${local.repo_root}/k8s-worker-test-image/nixos.qcow2"

  cloud_init_user_data = local.cloud_init_user_data

  storage_pool       = var.libvirt_storage_pool
  network_name       = var.libvirt_network_name
  uefi_firmware_path = var.uefi_firmware_path

  depends_on = [null_resource.nixos_image_build]
}
