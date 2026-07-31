resource "authentik_group" "kubectl_sandbox_users" {
  name  = "kubectl-sandbox-users"
  users = [data.authentik_user.agentydragon.pk]
}

# ============================================================================
# kubectl-passthrough-mcp — Passthrough kubectl MCP (caller's own permissions)
# ============================================================================
# Forwards the caller's Authentik JWT directly to kube-apiserver. The caller
# gets their own OIDC permissions — not sandbox-scoped.

resource "authentik_provider_oauth2" "kubectl_passthrough_mcp" {
  name               = "kubectl-passthrough-mcp"
  client_id          = "kubectl-passthrough-mcp"
  client_type        = "public" # PKCE-based; no client secret distributed to users.
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # Same reason as ha-mcp.tf: haku-console holds an operator OAuth association here, and the
  # Terraform provider's `minutes=10` default made it renew ~150x/day, any one of which can
  # permanently wedge the association. This server was wedged that way on 2026-07-30.
  access_token_validity = "hours=24"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  # - Claude Code: http://localhost:<port>/callback
  # - kubernetes-mcp-server's built-in callback (browser testing): /oauth/callback
  # - haku-console's operator_oauth flow (mcp_approval.py): /api/mcp/operator-auth/callback.
  #   kubernetes-mcp-server has no DCR endpoint of its own (it just mirrors Authentik's
  #   OAuth metadata, which has none either), so haku-console is configured with this
  #   provider's static client_id instead of registering dynamically — safe to share
  #   across callers since this is a public/PKCE client and redirect_uri is validated
  #   per request.
  #
  # TODO: Consider adding https://claude.ai/api/mcp/auth_callback for Claude.ai
  # Custom Connectors. Intentionally omitted for now — using the passthrough
  # server from Claude.ai would give the caller full (admin) cluster access
  # through the web UI, which may be more exposure than we want. The
  # kubectl-sandbox-mcp provider includes it because its scope mapping keeps
  # the caller sandbox-only regardless.
  allowed_redirect_uris = [
    {
      matching_mode = "regex"
      url           = "^http://localhost:[0-9]+/callback$"
    },
    {
      matching_mode = "strict"
      url           = "https://kubectl-passthrough-mcp.allegedly.works/oauth/callback"
    },
    {
      matching_mode = "strict"
      url           = "https://haku.allegedly.works/api/mcp/operator-auth/callback"
    },
  ]
}

moved {
  from = authentik_provider_oauth2.kubectl_sandbox_mcp
  to   = authentik_provider_oauth2.kubectl_passthrough_mcp
}

resource "authentik_application" "kubectl_passthrough_mcp" {
  name              = "kubectl-passthrough-mcp"
  slug              = "kubectl-passthrough-mcp"
  protocol_provider = authentik_provider_oauth2.kubectl_passthrough_mcp.id
  meta_description  = "Passthrough kubectl MCP — forwards caller's JWT to kube-apiserver (caller's own permissions)"
  meta_launch_url   = "https://kubectl-passthrough-mcp.allegedly.works"
}

moved {
  from = authentik_application.kubectl_sandbox_mcp
  to   = authentik_application.kubectl_passthrough_mcp
}

resource "authentik_policy_binding" "kubectl_passthrough_mcp_users" {
  target = authentik_application.kubectl_passthrough_mcp.uuid
  group  = authentik_group.kubectl_sandbox_users.id
  order  = 0
}

moved {
  from = authentik_policy_binding.kubectl_sandbox_mcp_users
  to   = authentik_policy_binding.kubectl_passthrough_mcp_users
}

## Passthrough server needs no Secret — the OAuth2 provider is public (PKCE)
## so there's no client_secret, and passthrough mode doesn't do token exchange.
## The previous kubernetes_secret.kubectl_passthrough_mcp was deleted above;
## TF will destroy the state-only resource on the next apply.
