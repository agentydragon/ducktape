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
