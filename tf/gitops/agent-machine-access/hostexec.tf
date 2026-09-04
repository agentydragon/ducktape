# ============================================================================
# hostexec — per-host token-exchange providers for remote command execution
# ============================================================================
# haku-console exchanges the operator's own `haku-console` token (jwt-bearer;
# `jwt_federation_providers` below) for a short-lived token scoped to one host:
# aud = client_id = `hostexec-<host>`, carrying the operator's `hostexec-*`
# groups. `hostexecd` on the node verifies it against Authentik's JWKS (RS256)
# and checks the `hostexec-<run_as>-<host>` group. No RFC 8693: Authentik mints
# a fresh token bound to the per-host provider, preserving the operator identity.
# `hostexecd` holds no client credential — only the operator's own token has
# value. See haku/hostexec/README.md.

# "May run as <run_as> on <host>." The operator holds all four; each name must
# equal hostexecd's expected `hostexec-<run_as>-<host>` string exactly
# (haku/hostexec/hostexecd/authentik.rs).
resource "authentik_group" "hostexec" {
  for_each = toset([
    "hostexec-agentydragon-wyrm2",
    "hostexec-root-wyrm2",
    "hostexec-agentydragon-rugged",
    "hostexec-root-rugged",
    "hostexec-agentydragon-atlas",
    "hostexec-root-atlas",
  ])
  name  = each.key
  users = [data.authentik_user.agentydragon.pk]
}

# Emits the operator's `hostexec-*` group memberships into the exchanged token
# (requested via the `groups` scope, i.e. the console's `exchange_scope` must
# include `groups`). hostexecd checks for `hostexec-<run_as>-<host>` among them.
resource "authentik_property_mapping_provider_scope" "hostexec_groups" {
  name       = "hostexec-groups"
  scope_name = "groups"
  expression = <<-EXPR
    return {"groups": [group.name for group in request.user.ak_groups.all() if group.name.startswith("hostexec-")]}
  EXPR
}

# Per-host token-exchange target. aud = client_id = `hostexec-<host>` (Authentik
# stamps the requesting client_id as the audience). Short TTL — the token is
# minted just before the call and is single-use at hostexecd. `confidential`
# matches the other exchange targets; the secret is unused (the federated
# client_credentials grant bypasses it) and never distributed — the console
# presents only the client_id + the operator's token as the assertion.
resource "authentik_provider_oauth2" "hostexec" {
  for_each = toset(["wyrm2", "rugged", "atlas"])

  name                  = "hostexec-${each.key}"
  client_id             = "hostexec-${each.key}"
  client_type           = "confidential"
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  signing_key           = data.authentik_certificate_key_pair.self_signed.id
  access_token_validity = "minutes=1"
  issuer_mode           = "per_provider"

  # The token-exchange trust: a token issued by the operator-login provider
  # (`haku-console`) may authenticate on behalf of this provider as a jwt-bearer
  # client assertion. Only the operator can obtain a `haku-console` token (the
  # `haku-console-access` group gates that login), so only the operator can
  # exchange; run_as is then gated by the `hostexec-*` group claims above.
  jwt_federation_providers = [authentik_provider_oauth2.haku_console_operator.id]

  # `groups` carries the operator's run_as authorizations; `openid` carries `sub`
  # (audit). No email/profile — hostexecd needs neither.
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    authentik_property_mapping_provider_scope.hostexec_groups.id,
  ]

  # Token-exchange only — no interactive redirect (allowed_redirect_uris omitted).
}

# Each provider needs an application object (Authentik hygiene; matches the other
# machine providers, e.g. stalwart-haku). No policy binding: access is the
# federation trust above plus the operator's group membership, not an interactive
# application grant.
resource "authentik_application" "hostexec" {
  for_each = authentik_provider_oauth2.hostexec

  name              = "hostexec-${each.key}"
  slug              = "hostexec-${each.key}"
  protocol_provider = each.value.id
  meta_description  = "Remote command execution on ${each.key} (operator's own Authentik identity; hostexecd verifies)"
}
