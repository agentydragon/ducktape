terraform {
  required_version = ">= 1.0"

  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
  }

  backend "kubernetes" {
    secret_suffix = "inventree-tokens"
    namespace     = "flux-system"
  }
}

provider "vault" {
  address = var.vault_address
  token   = var.vault_token
}

# Shared password for the sandbox-agent InvenTree user.
# Both openclaw-sandbox and claude-sandbox use the same account.
resource "random_password" "sandbox_agent" {
  length  = 32
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "vault_kv_secret_v2" "inventree_sandbox_agent" {
  mount = "kv"
  name  = "inventree/sandbox-agent"
  cas   = 0

  data_json = jsonencode({
    password = random_password.sandbox_agent.result
  })

  lifecycle {
    ignore_changes = [data_json]
  }
}
