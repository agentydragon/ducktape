#
# Same shape as the Manifold facade: confidential OAuth2 client wrapped by
# OIDCProxy, restricted to agentydragon via a one-user group + policy binding.
# The downstream PostScan Mail REST API is reached via a static x-api-key
# held by the postscanmail-mcp-server sidecar.

# Bump `keepers.version` to force the provider to be replaced, which
# regenerates the client_secret in Authentik and re-renders the
# `postscanmail-mcp-oidc` Kubernetes Secret. Reloader rolls the facade pod
# automatically on Secret change.
resource "random_id" "postscanmail_mcp_secret_rotation" {
  byte_length = 4
  keepers = {
    version = "2"
  }
}

resource "authentik_provider_oauth2" "postscanmail_mcp" {
  name               = "postscanmail-mcp"
  client_id          = "postscanmail-mcp"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # Same reason as ha-mcp.tf: haku-console links an operator OAuth association here, and the
  # Terraform provider's `minutes=10` default made it renew ~150x/day, any one of which can
  # permanently wedge the association.
  access_token_validity = "hours=24"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://postscanmail-mcp.allegedly.works/auth/callback"
    },
  ]

  lifecycle {
    replace_triggered_by = [random_id.postscanmail_mcp_secret_rotation]
  }
}

resource "authentik_application" "postscanmail_mcp" {
  name              = "PostScan Mail MCP Facade"
  slug              = "postscanmail-mcp"
  protocol_provider = authentik_provider_oauth2.postscanmail_mcp.id
  meta_description  = "OAuth facade for the PostScan Mail Developer API. Read+write surface (list_items, request_open/discard/rescan/shred — paid actions); restricted to agentydragon."
  meta_launch_url   = "https://postscanmail-mcp.allegedly.works"
}

resource "authentik_group" "postscanmail_mcp_users" {
  name  = "postscanmail-mcp-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "postscanmail_mcp_users" {
  target = authentik_application.postscanmail_mcp.uuid
  group  = authentik_group.postscanmail_mcp_users.id
  order  = 0
}

# Canonical OIDC client credentials. Reflector mirrors this Secret into the
# facade namespace after that namespace has been created.
resource "kubernetes_secret" "postscanmail_mcp_oidc_source" {
  metadata {
    name      = "postscanmail-mcp-oidc"
    namespace = "authentik"
    annotations = {
      description                                                     = "PostScan Mail MCP OIDC client credentials"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "postscanmail-mcp"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "postscanmail-mcp"
    }
  }

  data = {
    client_id     = authentik_provider_oauth2.postscanmail_mcp.client_id
    client_secret = authentik_provider_oauth2.postscanmail_mcp.client_secret
  }
}

removed {
  from = kubernetes_secret.postscanmail_mcp_oidc

  lifecycle {
    destroy = false
  }
}

# --- Plaid DB MCP facade (public OAuth facade over read-only Postgres MCP) ---
