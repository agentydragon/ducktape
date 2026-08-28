
resource "authentik_provider_oauth2" "plaid_db_mcp" {
  name               = "plaid-db-mcp"
  client_id          = "plaid-db-mcp"
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
      url           = "https://plaid-db.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "plaid_db_mcp" {
  name              = "Plaid DB MCP Facade"
  slug              = "plaid-db-mcp"
  protocol_provider = authentik_provider_oauth2.plaid_db_mcp.id
  meta_description  = "OAuth facade for read-only SQL access to the synced Plaid Postgres database; restricted to agentydragon."
  meta_launch_url   = "https://plaid-db.allegedly.works"
}

resource "authentik_group" "plaid_db_mcp_users" {
  name  = "plaid-db-mcp-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_policy_binding" "plaid_db_mcp_users" {
  target = authentik_application.plaid_db_mcp.uuid
  group  = authentik_group.plaid_db_mcp_users.id
  order  = 0
}

# Canonical OIDC client credentials. Reflector mirrors this Secret into the
# facade namespace after that namespace has been created.
resource "kubernetes_secret" "plaid_db_mcp_oidc_source" {
  metadata {
    name      = "plaid-db-mcp-oidc"
    namespace = "authentik"
    annotations = {
      description                                                     = "Plaid DB MCP OIDC client credentials"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "plaid-mcp"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "plaid-mcp"
    }
  }

  data = {
    client_id     = authentik_provider_oauth2.plaid_db_mcp.client_id
    client_secret = authentik_provider_oauth2.plaid_db_mcp.client_secret
  }
}

removed {
  from = kubernetes_secret.plaid_db_mcp_oidc

  lifecycle {
    destroy = false
  }
}
