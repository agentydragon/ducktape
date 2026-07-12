# ============================================================================
# haku-grocy — Haku's read-only identity for the grocy-sf MCP server
# ============================================================================
# Haku reads the SF Grocy instance through the standard grocy-sf MCP
# (grocy-mcp-sf.allegedly.works), no separate facade. The authentik-jwt-rotation
# CronJob mints a client_credentials JWT FOR this `haku` service account against
# the dedicated grocy-mcp-haku-sf provider (above) — separate from the user-facing
# grocy-mcp-sf so its 30-day validity doesn't lengthen claude.ai user tokens, but
# sharing the same signing key so the MCP's JWKS still validates it. The MCP runs
# its usual jwt-bearer exchange into the grocy-sf proxy provider and the outpost
# injects X-authentik-username=haku — which maps to the read-only `haku` Grocy user
# (empty permission set, provisioned by cluster/k8s/grocy/sf/haku-user). Read-only
# is enforced server-side by Grocy (its API gates every write on a permission this
# user lacks); this identity adds no write capability.
#
# Mirrors the haku-k8s pattern above (user_password client_credentials), but the SA
# username is `haku` (not haku-k8s) because that username is what the outpost
# forwards to Grocy.
#
# Consumer: cluster/k8s/agents/authentik-jwt-rotation/ CronJob (haku-grocy entry).
# It mints the JWT and commits it SOPS-encrypted to secrets/haku-grocy-jwt.yaml.

resource "authentik_user" "haku_grocy" {
  username = "haku"
  name     = "Haku grocy read-only service account"
  # Required for the same SA's stalwart-haku provider (below): Stalwart's OIDC
  # directory requires the "email" scope/claim and rejects an empty one, so
  # /jmap/session 403s without this even though the token authenticates fine.
  email = "haku@allegedly.works"
  type  = "service_account"
  path  = "goauthentik.io/service-accounts"
}

# App-password token, used as the password in the client_credentials
# username/password exchange against the grocy-mcp-haku-sf provider's client_id.
resource "authentik_token" "haku_grocy" {
  identifier   = "haku-grocy-client-credentials"
  user         = authentik_user.haku_grocy.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for Haku's grocy-sf JWT rotation"
}

# Authorize the haku SA to mint tokens on the dedicated grocy-mcp-haku-sf application.
resource "authentik_policy_binding" "haku_grocy_mcp_haku" {
  target = authentik_application.grocy_mcp_haku_sf.uuid
  user   = authentik_user.haku_grocy.id
  order  = 1
}

# Authorize the haku SA on the grocy-sf proxy application too: the MCP's
# downstream call to Grocy traverses that outpost, so the jwt-bearer exchange must
# be authorized end to end (the household group binding covers humans, not this SA).
resource "authentik_policy_binding" "haku_grocy_sf_proxy" {
  target = authentik_application.grocy_sf.uuid
  user   = authentik_user.haku_grocy.id
  order  = 1
}

# K8s Secret holding the grocy-mcp-haku-sf provider's client_id + the haku username
# and app-password. Lives in agents-infra (where the authentik-jwt-rotation CronJob
# runs); the minted JWT lives SOPS-encrypted in secrets/haku-grocy-jwt.yaml.
resource "kubernetes_secret" "haku_grocy_client_credentials" {
  metadata {
    name      = "haku-grocy-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id (grocy-mcp-haku-sf OAuth2 provider) + haku username/app-password, authenticating the haku service account so the authentik-jwt-rotation CronJob can mint its grocy-sf MCP JWT"
    }
  }

  data = {
    client_id = authentik_provider_oauth2.grocy_mcp_haku_sf.client_id
    username  = authentik_user.haku_grocy.username
    password  = authentik_token.haku_grocy.key
  }
}

# ============================================================================
# haku-mail — Haku's mailbox identity for the Stalwart mailserver
# ============================================================================
# Stalwart (cluster/k8s/haku/mailbox) authenticates its `haku` mail account
# exclusively via OIDC bearer tokens: its directory points at this provider's
# issuer and pins requireAudience to this client_id, so kubectl-sandbox JWTs
# (or any other Authentik app's tokens) can never authenticate to the mailbox.
# The authentik-jwt-rotation CronJob mints the JWT for the existing `haku`
# service account (haku_grocy above; preferred_username=haku is what Stalwart's
# claimUsername resolves to the pre-created `haku` principal) — see the
# haku-mail entry in cluster/k8s/agents/authentik-jwt-rotation/rotations.yaml.

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

# Dedicated machine principal for declarative Stalwart configuration. Unlike
# the `haku` mailbox user, this identity is granted an administrator role by
# the Stalwart plan and its JWT is exposed only inside haku-mailbox.
resource "authentik_user" "stalwart_reconciler" {
  username = "stalwart-reconciler"
  name     = "Stalwart declarative reconciler"
  email    = "stalwart-reconciler@allegedly.works"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "stalwart_reconciler" {
  identifier   = "stalwart-reconciler-client-credentials"
  user         = authentik_user.stalwart_reconciler.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for Stalwart plan reconciliation"
}

resource "authentik_policy_binding" "stalwart_reconciler" {
  target = authentik_application.stalwart_haku.uuid
  user   = authentik_user.stalwart_reconciler.id
  order  = 2
}

resource "kubernetes_secret" "stalwart_reconciler_client_credentials" {
  metadata {
    name      = "stalwart-reconciler-client-credentials"
    namespace = "haku-mailbox"
    annotations = {
      description = "Authentik credentials used by the Stalwart reconcile Job to mint an ephemeral JWT"
    }
  }

  data = {
    client_id = authentik_provider_oauth2.stalwart_haku.client_id
    username  = authentik_user.stalwart_reconciler.username
    password  = authentik_token.stalwart_reconciler.key
  }
}
