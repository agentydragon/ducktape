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
    secret_suffix = "sso-secrets"
    namespace     = "flux-system"
  }
}

provider "vault" {
  address         = var.vault_address
  token           = var.vault_token
  skip_tls_verify = true # Self-signed internal CA
}

# --- Client Secret Generation ---
# Each OAuth2 app gets an immutable random secret. Bump rotation_version to rotate.

resource "random_password" "gitea_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "grafana_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "harbor_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "matrix_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "vault_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "inventree_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "headscale_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "headlamp_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "gatus_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "random_password" "openclaw_agent_client_secret" {
  length  = 32
  special = false
  keepers = { rotation_version = var.rotation_version }

  lifecycle {
    ignore_changes = [length, special]
  }
}

# --- Vault Storage: Basic Credentials ---

resource "vault_kv_secret_v2" "gitea_oidc" {
  mount = "kv"
  name  = "sso/gitea"

  data_json = jsonencode({
    client_id     = "gitea"
    client_secret = random_password.gitea_client_secret.result
  })
}

resource "vault_kv_secret_v2" "grafana_oidc" {
  mount = "kv"
  name  = "sso/grafana"

  data_json = jsonencode({
    client_id     = "grafana"
    client_secret = random_password.grafana_client_secret.result
  })
}

resource "vault_kv_secret_v2" "harbor_oidc" {
  mount = "kv"
  name  = "sso/harbor"

  data_json = jsonencode({
    client_id     = "harbor"
    client_secret = random_password.harbor_client_secret.result
  })
}

resource "vault_kv_secret_v2" "matrix_oidc" {
  mount = "kv"
  name  = "sso/matrix"

  data_json = jsonencode({
    client_id     = "matrix"
    client_secret = random_password.matrix_client_secret.result
  })
}

resource "vault_kv_secret_v2" "vault_oidc" {
  mount = "kv"
  name  = "sso/vault"

  data_json = jsonencode({
    client_id     = "vault"
    client_secret = random_password.vault_client_secret.result
  })
}

resource "vault_kv_secret_v2" "inventree_oidc" {
  mount = "kv"
  name  = "sso/inventree"

  data_json = jsonencode({
    client_id     = "inventree"
    client_secret = random_password.inventree_client_secret.result
  })
}

resource "vault_kv_secret_v2" "headscale_oidc" {
  mount = "kv"
  name  = "sso/headscale"

  data_json = jsonencode({
    client_id     = "headscale"
    client_secret = random_password.headscale_client_secret.result
  })
}

resource "vault_kv_secret_v2" "headlamp_oidc" {
  mount = "kv"
  name  = "sso/headlamp"

  data_json = jsonencode({
    client_id     = "headlamp"
    client_secret = random_password.headlamp_client_secret.result
  })
}

resource "vault_kv_secret_v2" "gatus_oidc" {
  mount = "kv"
  name  = "sso/gatus"

  data_json = jsonencode({
    client_id     = "gatus"
    client_secret = random_password.gatus_client_secret.result
  })
}

resource "vault_kv_secret_v2" "openclaw_agent_oidc" {
  mount = "kv"
  name  = "sso/openclaw-agent"

  data_json = jsonencode({
    client_id     = "openclaw-agent"
    client_secret = random_password.openclaw_agent_client_secret.result
  })
}

# --- Vault Storage: Full OIDC Provider Configs ---
# These are consumed by application-side ESO to configure OIDC in the app itself.

resource "vault_kv_secret_v2" "grafana_oidc_config" {
  mount = "kv"
  name  = "sso/oidc-providers/grafana"

  data_json = jsonencode({
    enabled             = true
    name                = "Authentik"
    client_id           = "grafana"
    client_secret       = random_password.grafana_client_secret.result
    scopes              = "openid email profile"
    auth_url            = "https://auth.allegedly.works/application/o/authorize/"
    token_url           = "http://authentik-server.authentik/application/o/token/"
    api_url             = "http://authentik-server.authentik/application/o/userinfo/"
    role_attribute_path = "contains(groups[*], 'Grafana Admins') && 'Admin' || 'Viewer'"
    allow_sign_up       = true
  })
}

resource "vault_kv_secret_v2" "matrix_oidc_config" {
  mount = "kv"
  name  = "sso/oidc-providers/matrix"

  data_json = jsonencode({
    oidc_providers = [
      {
        idp_id        = "authentik"
        idp_name      = "Authentik SSO"
        discover      = true
        issuer        = "https://auth.allegedly.works/application/o/matrix/"
        client_id     = "matrix"
        client_secret = random_password.matrix_client_secret.result
        scopes = [
          "openid",
          "profile",
          "email"
        ]
        user_mapping_provider = {
          config = {
            localpart_template    = "{{ user.preferred_username }}"
            display_name_template = "{{ user.name }}"
            email_template        = "{{ user.email }}"
          }
        }
        allow_existing_users = true
        enable_registration  = true
      }
    ]
  })
}
