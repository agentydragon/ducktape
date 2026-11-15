# Plaid Link's app handles its own Authentik login. The browser receives only a
# signed, short-lived session cookie; OAuth tokens and the client secret stay server-side.

resource "random_password" "plaid_link_session_secret" {
  length  = 64
  special = false
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
  protocol_provider = authentik_provider_oauth2.providers["plaid_link"].id
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
    client_id      = authentik_provider_oauth2.providers["plaid_link"].client_id
    client_secret  = authentik_provider_oauth2.providers["plaid_link"].client_secret
    session_secret = random_password.plaid_link_session_secret.result
  }
}
