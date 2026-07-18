# ============================================================================
# haku-service-account — Haku's shared Authentik service-account identity
# ============================================================================
# The `haku` service account and its app-password token. Formerly also carried
# Haku's dedicated read-only grocy-sf identity (grocy-mcp-haku-sf provider +
# bindings + credentials secret) — retired once Haku's grocy-sf access moved
# to haku-console's own remote_server_oauth entry (console reads/writes now
# gate on the console's approval policy instead of a server-side permission
# scope). The user + token stay: haku-mail.tf's stalwart-haku provider binds
# this same SA for Haku's mailbox identity.

resource "authentik_user" "haku_grocy" {
  username = "haku"
  name     = "Haku service account"
  # Required for the same SA's stalwart-haku provider (haku-mail.tf): Stalwart's
  # OIDC directory requires the "email" scope/claim and rejects an empty one, so
  # /jmap/session 403s without this even though the token authenticates fine.
  email = "haku@allegedly.works"
  type  = "service_account"
  path  = "goauthentik.io/service-accounts"
}

# App-password token, used as the password in the client_credentials
# username/password exchange against haku-mail.tf's stalwart-haku provider.
resource "authentik_token" "haku_grocy" {
  identifier   = "haku-grocy-client-credentials"
  user         = authentik_user.haku_grocy.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for Haku's service-account identity"
}
