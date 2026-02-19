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
    secret_suffix = "inventree-admin"
    namespace     = "flux-system"
  }
}

provider "vault" {
  address = var.vault_address
  token   = var.vault_token
}

resource "random_password" "admin_password" {
  length  = 24
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "db_password" {
  length  = 32
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "vault_kv_secret_v2" "inventree_admin" {
  mount = "kv"
  name  = "inventree/admin"
  cas   = 0

  data_json = jsonencode({
    admin_password = random_password.admin_password.result
  })

  lifecycle {
    ignore_changes = [data_json]
  }
}

resource "vault_kv_secret_v2" "inventree_db" {
  mount = "kv"
  name  = "inventree/db"
  cas   = 0

  data_json = jsonencode({
    db_password = random_password.db_password.result
  })

  lifecycle {
    ignore_changes = [data_json]
  }
}
