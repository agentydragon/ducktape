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
    secret_suffix = "langfuse-secrets"
    namespace     = "flux-system"
  }
}

provider "vault" {
  address = var.vault_address
  token   = var.vault_token
}

resource "random_password" "nextauth_secret" {
  length  = 64
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "salt" {
  length  = 64
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "encryption_key" {
  # Must be exactly 64 hex chars (256-bit key)
  length  = 64
  special = false
  upper   = false

  lifecycle {
    ignore_changes = [length, special, upper]
  }
}

resource "random_password" "postgres_password" {
  length  = 32
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "vault_kv_secret_v2" "langfuse_secrets" {
  mount = "kv"
  name  = "langfuse/secrets"
  cas   = 0

  data_json = jsonencode({
    nextauth_secret   = random_password.nextauth_secret.result
    salt              = random_password.salt.result
    encryption_key    = random_password.encryption_key.result
    postgres_password = random_password.postgres_password.result
  })

  lifecycle {
    ignore_changes = [data_json]
  }
}
