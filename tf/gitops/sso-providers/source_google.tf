# ============================================================================
# Google OAuth Source — "Sign in with Google" for Authentik
# ============================================================================
#
# Security model: We trust Google to authenticate users. If Google says
# user@example.com logged in, we accept that and link to the corresponding
# authentik user (if it exists with a matching email).
#
# - user_matching_mode = "email_link": On first Google login, auto-link
#   to existing authentik user with matching email. Creates the connection.
# - No new users are created — only pre-existing users can log in via Google.
# ============================================================================

# Read client secret from K8s secret (client ID is inlined)
data "kubernetes_secret" "google_oauth" {
  metadata {
    name      = "authentik-google-oauth"
    namespace = "authentik"
  }
}

locals {
  google_client_id     = "230253529789-0rci9kjfqagfg9eg53kuckrbcoc74ahr.apps.googleusercontent.com"
  google_client_secret = try(data.kubernetes_secret.google_oauth.data["GOOGLE_OAUTH_CLIENT_SECRET"], "")
}

data "authentik_flow" "default_source_authentication" {
  count = local.google_client_id != "" ? 1 : 0
  slug  = "default-source-authentication"
}

resource "authentik_source_oauth" "google" {
  count = local.google_client_id != "" ? 1 : 0

  name                = "Google"
  slug                = "google"
  provider_type       = "google"
  authorization_url   = "https://accounts.google.com/o/oauth2/v2/auth"
  access_token_url    = "https://oauth2.googleapis.com/token"
  profile_url         = "https://openidconnect.googleapis.com/v1/userinfo"
  oidc_jwks_url       = "https://www.googleapis.com/oauth2/v3/certs"
  consumer_key        = local.google_client_id
  consumer_secret     = local.google_client_secret
  authentication_flow = data.authentik_flow.default_source_authentication[0].id
  # On first Google login, link to existing user with matching email
  # If user doesn't exist in authentik, login fails (no auto-creation)
  user_matching_mode = "email_link"
}

# The Google source is wired into the login UI via a blueprint patch on the
# built-in `default-authentication-identification` stage — see
# cluster/k8s/authentik/app/blueprints/users.yaml. No dedicated stage or
# binding is needed here.
