# Plaid Spend uses a public native OAuth client for GNOME. The server computes
# one shared view from the SOPS-managed cluster card configuration.

resource "authentik_application" "plaid_spend_desktop" {
  name              = "Plaid Spend Desktop"
  slug              = "plaid-spend-desktop"
  protocol_provider = authentik_provider_oauth2.providers["plaid_spend_desktop"].id
  meta_description  = "Native GNOME indicator for the shared Plaid card spend view"
  meta_launch_url   = "https://plaid-spend.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "plaid_spend_desktop_access" {
  target = authentik_application.plaid_spend_desktop.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

# The web UI is a confidential server-side client. The browser keeps only the
# short-lived signed session cookie; the OIDC tokens and client secret stay here.
resource "random_password" "plaid_spend_web_session_secret" {
  length  = 64
  special = false
}

resource "authentik_application" "plaid_spend_web" {
  name              = "Plaid Spend"
  slug              = "plaid-spend-web"
  protocol_provider = authentik_provider_oauth2.providers["plaid_spend_web"].id
  meta_description  = "Browser view of the shared Plaid card spend data"
  meta_launch_url   = "https://plaid-spend.allegedly.works/"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "plaid_spend_web_access" {
  target = authentik_application.plaid_spend_web.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

# ESO grants plaid-mcp access to this one Secret only, then mirrors it into the
# service namespace for the web process.
resource "kubernetes_secret" "plaid_spend_web_oidc" {
  metadata {
    name      = "plaid-spend-web-oidc-config"
    namespace = "authentik"
    annotations = {
      description = "OIDC client and session signing keys for Plaid Spend web login"
    }
  }

  data = {
    client_id      = authentik_provider_oauth2.providers["plaid_spend_web"].client_id
    client_secret  = authentik_provider_oauth2.providers["plaid_spend_web"].client_secret
    session_secret = random_password.plaid_spend_web_session_secret.result
  }
}
