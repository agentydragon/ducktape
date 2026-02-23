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
    secret_suffix = "authentik-passwords"
    namespace     = "flux-system"
  }
}

provider "vault" {
  address = var.vault_address
  token   = var.vault_token
}

resource "random_password" "admin_password" {
  length  = 32
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "secret_key" {
  length  = 64
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "vault_kv_secret_v2" "authentik_passwords" {
  mount = "kv"
  name  = "authentik/passwords"
  cas   = 0

  data_json = jsonencode({
    admin_password = random_password.admin_password.result
    secret_key     = random_password.secret_key.result
  })

  lifecycle {
    ignore_changes = [data_json]
  }
}
