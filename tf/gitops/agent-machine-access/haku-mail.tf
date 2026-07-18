# ============================================================================
# haku-mail — Haku's mailbox identity for the Stalwart mailserver
# ============================================================================
# Stalwart (cluster/k8s/haku/mailbox) authenticates its `haku` mail account
# exclusively via OIDC bearer tokens: its directory points at this provider's
# issuer and pins requireAudience to this client_id, so kubectl-sandbox JWTs
# (or any other Authentik app's tokens) can never authenticate to the mailbox.
# The authentik-jwt-rotation CronJob mints the JWT for the existing `haku`
# service account (authentik_user.haku_grocy, haku-service-account.tf;
# preferred_username=haku is what Stalwart's claimUsername resolves to the
# pre-created `haku` principal) — see the haku-mail entry in
# cluster/k8s/agents/authentik-jwt-rotation/rotations.yaml.

resource "authentik_provider_oauth2" "stalwart_haku" {
  name        = "stalwart-haku"
  client_id   = "stalwart-haku"
  client_type = "confidential"

  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # 30d access-token validity — comfortable margin over the rotation CronJob's
  # re-mint threshold, and Stalwart validates the JWT offline against this
  # provider's JWKS on every request anyway.
  access_token_validity = "days=30"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  # client_credentials doesn't redirect, so allowed_redirect_uris is omitted.
}

resource "authentik_application" "stalwart_haku" {
  name              = "Stalwart Haku mailbox"
  slug              = "stalwart-haku"
  protocol_provider = authentik_provider_oauth2.stalwart_haku.id
  meta_description  = "Machine client_credentials provider for haku's mailbox (Stalwart OIDC directory)"
}

resource "authentik_policy_binding" "haku_stalwart" {
  target = authentik_application.stalwart_haku.uuid
  user   = authentik_user.haku_grocy.id
  order  = 1
}

# client_id + haku username/app-password for the rotation CronJob (same SA and
# app-password as haku-grocy; the credential authenticates the SA, the provider
# determines the audience).
resource "kubernetes_secret" "haku_mail_client_credentials" {
  metadata {
    name      = "haku-mail-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id (stalwart-haku OAuth2 provider) + haku username/app-password, authenticating the haku service account so the authentik-jwt-rotation CronJob can mint its mailbox JWT"
    }
  }

  data = {
    client_id = authentik_provider_oauth2.stalwart_haku.client_id
    username  = authentik_user.haku_grocy.username
    password  = authentik_token.haku_grocy.key
  }
}
