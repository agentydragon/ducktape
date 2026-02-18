terraform {
  required_version = ">= 1.0"

  required_providers {
    authentik = {
      source  = "goauthentik/authentik"
      version = "~> 2025.12.0"
    }
  }

  backend "kubernetes" {
    secret_suffix = "authentik-blueprint-openclaw"
    namespace     = "flux-system"
  }
}

provider "authentik" {
  url   = var.authentik_url
  token = var.authentik_token
}

data "authentik_flow" "default_invalidation" {
  slug = "default-provider-invalidation-flow"
}

data "authentik_flow" "default_authorization_flow" {
  slug = "default-provider-authorization-implicit-consent"
}

data "authentik_flow" "default_authentication" {
  slug = "default-authentication-flow"
}

data "authentik_group" "admins" {
  name = "authentik Admins"
}

# OpenClaw Proxy Provider — outpost proxies traffic and handles auth
resource "authentik_provider_proxy" "openclaw" {
  name                  = "openclaw"
  external_host         = var.openclaw_url
  internal_host         = "http://openclaw.openclaw.svc.cluster.local:18789"
  mode                  = "proxy"
  authentication_flow   = data.authentik_flow.default_authentication.id
  authorization_flow    = data.authentik_flow.default_authorization_flow.id
  invalidation_flow     = data.authentik_flow.default_invalidation.id
  access_token_validity = "hours=1"
}

resource "authentik_application" "openclaw" {
  name              = "OpenClaw"
  slug              = "openclaw"
  protocol_provider = authentik_provider_proxy.openclaw.id
  meta_description  = "OpenClaw AI Agent"
  meta_launch_url   = var.openclaw_url
  open_in_new_tab   = true
}

# Restrict access to admins
resource "authentik_policy_binding" "openclaw_access" {
  target = authentik_application.openclaw.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

data "authentik_service_connection_kubernetes" "local" {
  name = "Local Kubernetes Cluster"
}

# Dedicated outpost — deploys proxy pods in authentik namespace
resource "authentik_outpost" "openclaw" {
  name               = "openclaw-outpost"
  type               = "proxy"
  service_connection = data.authentik_service_connection_kubernetes.local.id

  protocol_providers = [
    authentik_provider_proxy.openclaw.id
  ]

  config = jsonencode({
    authentik_host         = var.authentik_url
    authentik_host_browser = "https://auth.allegedly.works"
    # Pin outpost pods to VPS nodes (co-locate with Authentik server)
    kubernetes_json_patches = {
      deployment = [
        {
          op    = "add"
          path  = "/spec/template/spec/nodeSelector"
          value = { "topology.kubernetes.io/region" = "hetzner" }
        }
      ]
    }
  })
}
