terraform {
  required_version = ">= 1.0"

  backend "pg" {
    schema_name = "main"
  }

  required_providers {
    # From persistent-auth
    external = { source = "hashicorp/external", version = "~> 2.3.0" }
    # From infrastructure + persistent-auth + nixos-dev-env
    proxmox = { source = "bpg/proxmox", version = "~> 0.91.0" }
    # From infrastructure
    hcloud     = { source = "hetznercloud/hcloud", version = "~> 1.45" }
    talos      = { source = "siderolabs/talos", version = "~> 0.10.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.38.0" }
    # From flux
    flux = { source = "fluxcd/flux", version = "~> 1.7.0" }
    helm = { source = "hashicorp/helm", version = "~> 3.1.0" }
    # Utility (from multiple)
    local  = { source = "hashicorp/local", version = "~> 2.5.0" }
    null   = { source = "hashicorp/null", version = "~> 3.2.0" }
    random = { source = "hashicorp/random", version = "~> 3.7.0" }
    tls    = { source = "hashicorp/tls", version = "~> 4.1.0" }
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
      name    = var.proxmox_node_name
      address = var.proxmox_node_name
    }
  }
}

# Kubernetes/Helm/Flux — file-based kubeconfig written by infrastructure resources.
# During bootstrap first pass (-target on infra), the file doesn't exist yet —
# fine because no k8s/helm/flux resources are targeted.
provider "kubernetes" {
  config_path = "${path.module}/kubeconfig"
}

provider "helm" {
  kubernetes {
    config_path = "${path.module}/kubeconfig"
  }
}

provider "flux" {
  kubernetes = {
    config_path = "${path.module}/kubeconfig"
  }
  git = {
    url    = "ssh://git@github.com/agentydragon/ducktape.git"
    branch = "devel"
    ssh = {
      username    = "git"
      private_key = tls_private_key.flux_deploy.private_key_openssh
    }
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

provider "talos" {}
