terraform {
  required_version = ">= 1.0"

  backend "pg" {
    schema_name = "main"
  }

  required_providers {
    sops = { source = "carlpett/sops", version = "~> 1.4.0" }
    # bpg/proxmox 0.91.0 (not rechecked on 0.113): adding `pool_id` to a VM already in the pool fails; use `proxmox_virtual_environment_pool_membership`.
    proxmox = { source = "bpg/proxmox", version = "~> 0.113.0" }

    talos      = { source = "siderolabs/talos", version = "~> 0.11.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 3.2.0" }
    helm       = { source = "hashicorp/helm", version = "~> 3.3.0" }

    local = { source = "hashicorp/local", version = "~> 2.9.0" }
    null  = { source = "hashicorp/null", version = "~> 3.3.0" }
    ovh   = { source = "ovh/ovh", version = "~> 2.0" }
  }
}

# Proxmox — uses PROXMOX_VE_API_TOKEN env var (root@pam!tofu from keyring).
# Handles both persistent-auth resources (user/role management) and
# infrastructure resources (VM creation, file uploads).
provider "proxmox" {
  endpoint = "https://${var.proxmox_api_host}:8006/"
  insecure = true
  ssh {
    agent    = true
    username = "root"
    node {
      name = var.proxmox_node_name
      # Use VLAN IP directly — hostname "atlas" resolves via Nebula DNS which
      # this terraform manages, creating a chicken-and-egg problem during
      # bootstrap (Nebula isn't running yet when tofu needs to SSH to Proxmox).
      address = var.proxmox_api_host
    }
  }
}

# Kubernetes/Helm — file-based kubeconfig written by infrastructure resources.
# During bootstrap first pass (-target on infra), the file doesn't exist yet —
# fine because no k8s/helm resources are targeted.
provider "kubernetes" {
  config_path = "${path.module}/kubeconfig"
}

provider "helm" {
  kubernetes = {
    config_path = "${path.module}/kubeconfig"
  }
}

provider "talos" {}

data "sops_file" "ovh_credentials" {
  source_file = "${path.module}/../../../secrets/ovh-credentials.sops.yaml"
}

provider "ovh" {
  endpoint           = "ovh-us"
  application_key    = data.sops_file.ovh_credentials.data["application_key"]
  application_secret = data.sops_file.ovh_credentials.data["application_secret"]
  consumer_key       = data.sops_file.ovh_credentials.data["consumer_key"]
}
