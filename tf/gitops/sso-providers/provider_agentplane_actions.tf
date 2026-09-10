# Action-only JWT-bearer target. See x/agentplane/docs/operator_federation.md for
# the pinned Authentik/provider source proving the shared subject mode and grant.
# Native provider federation preserves the AccessToken's database user; the
# target provider policy is the authorization boundary.
resource "authentik_provider_oauth2" "agentplane_actions" {
  name                  = "agentplane-actions"
  client_id             = "agentplane-actions"
  client_type           = "confidential"
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  signing_key           = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity = "minutes=1"
  issuer_mode           = "per_provider"
  # Match the login provider so the same Authentik user has the same `sub` after exchange.
  sub_mode = "hashed_user_id"

  jwt_federation_providers = [authentik_provider_oauth2.agentplane_staging.id]
  jwt_federation_sources   = []
  property_mappings        = [data.authentik_property_mapping_provider_scope.openid.id]
  # No interactive redirects. The generated client secret is never distributed.
}

resource "authentik_application" "agentplane_actions" {
  name              = "Agentplane Actions"
  slug              = "agentplane-actions"
  protocol_provider = authentik_provider_oauth2.agentplane_actions.id
  meta_description  = "Action decisions with the operator's own federated Authentik identity"
}

# Authentik 2026.2.1's native client-credentials grant checks target policy with
# the source token's user. The Action Service independently verifies the target
# issuer, audience, signature, and expiry; it does not duplicate that policy.
resource "authentik_policy_binding" "agentplane_actions_access" {
  target = authentik_application.agentplane_actions.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

# Resolve the existing managed user by primary key, NEVER by display name or a
# newly provisioned identity. The provider's CoreUsersRetrieve response exposes
# uid and uuid; no token exchange, guessed UUID, or local hash derivation needed.
data "authentik_user" "agentplane_operator" {
  pk = tonumber(authentik_user.agentydragon.id)
}

locals {
  agentplane_actions_issuer = "https://auth.allegedly.works/application/o/${authentik_application.agentplane_actions.slug}/"
}
