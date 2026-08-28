
resource "authentik_provider_oauth2" "tana_mcp_facade" {
  name               = "tana-mcp-facade"
  client_id          = "tana-mcp-facade"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://tana-mcp-facade.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "tana_mcp_facade" {
  name              = "Tana MCP Facade"
  slug              = "tana-mcp-facade"
  protocol_provider = authentik_provider_oauth2.tana_mcp_facade.id
  meta_description  = "Public OAuth facade for the Tana MCP server"
  meta_launch_url   = "https://tana-mcp-facade.allegedly.works"
}

resource "authentik_group" "tana_agentydragon_gmail_com_account_access" {
  name  = "tana-agentydragon-gmail-com-account-access"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "tana_mcp_facade_account_access" {
  target = authentik_application.tana_mcp_facade.uuid
  group  = authentik_group.tana_agentydragon_gmail_com_account_access.id
  order  = 0
}

# Canonical OIDC client credentials. Reflector mirrors this Secret into the
# facade namespace after that namespace has been created.
resource "kubernetes_secret" "tana_mcp_facade_oidc_source" {
  metadata {
    name      = "tana-mcp-facade-oidc"
    namespace = "authentik"
    annotations = {
      description                                                     = "Tana MCP facade OIDC client credentials"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "tana-mcp"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "tana-mcp"
    }
  }

  data = {
    client_id     = authentik_provider_oauth2.tana_mcp_facade.client_id
    client_secret = authentik_provider_oauth2.tana_mcp_facade.client_secret
  }
}

removed {
  from = kubernetes_secret.tana_mcp_facade_oidc

  lifecycle {
    destroy = false
  }
}

# --- Manifold MCP facade (public OAuth facade, gates access to Manifold Markets) ---
