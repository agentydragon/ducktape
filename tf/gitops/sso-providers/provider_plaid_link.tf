# Plaid Link's app handles its own Authentik login. The browser receives only a
# signed, short-lived session cookie; OAuth tokens and the client secret stay server-side.

resource "random_password" "plaid_link_session_secret" {
  length  = 64
  special = false
}

resource "authentik_provider_oauth2" "plaid_link" {
  name               = "plaid-link-oidc"
  client_id          = "plaid-link"
  client_type        = "confidential"
  grant_types        = ["authorization_code"]
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  access_token_validity      = "hours=24"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.email.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode     = "strict"
      url               = "https://plaid-mcp.allegedly.works/auth/callback"
      redirect_uri_type = "authorization"
    }
  ]
}

# The Authentik blueprint created this Application when Plaid used proxy auth.
# Import it so the existing group binding and launch tile keep the same app identity.
import {
  to = authentik_application.plaid_link
  id = "plaid-link"
}

resource "authentik_application" "plaid_link" {
  name              = "Plaid Link"
  slug              = "plaid-link"
  protocol_provider = authentik_provider_oauth2.plaid_link.id
  meta_description  = "Human Plaid Link management UI for the synced Plaid database"
  meta_launch_url   = "https://plaid-mcp.allegedly.works/link"
  open_in_new_tab   = true
}

# ESO reads this exact Secret through a get-only Role in the authentik namespace
# and writes the app-scoped copy into plaid-mcp.
resource "kubernetes_secret" "plaid_link_oidc" {
  metadata {
    name      = "plaid-link-oidc-config"
    namespace = "authentik"
    annotations = {
      description = "OIDC client and session signing keys for Plaid Link"
    }
  }

  data = {
    client_id      = authentik_provider_oauth2.plaid_link.client_id
    client_secret  = authentik_provider_oauth2.plaid_link.client_secret
    session_secret = random_password.plaid_link_session_secret.result
  }
}
