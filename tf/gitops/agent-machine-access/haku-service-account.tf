# ============================================================================
# haku-service-account — Haku's shared Authentik service-account identity
# ============================================================================
# The `haku` service account and its app-password token, which haku-mail.tf's
# stalwart-haku provider binds for Haku's mailbox identity. The `haku_grocy`
# resource names date from the SA's first use as Haku's read-only grocy-sf identity.

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
