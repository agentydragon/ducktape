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

# ============================================================================
# kubectl-sandbox-mcp — Scoped kubectl MCP (token exchange → sandbox only)
# ============================================================================
# Authenticates callers via Authentik consent, then exchanges the caller's
# token for one scoped to kubectl-sandbox-users group. Even if the caller is
# a cluster admin, the exchanged token only carries sandbox-level permissions.

## User-facing public OAuth2 client (Claude Code / other MCP clients). No
## client secret — relies on PKCE. Users only need client_id to connect.
resource "authentik_provider_oauth2" "kubectl_sandbox_scoped" {
  name               = "kubectl-sandbox-mcp"
  client_id          = "kubectl-sandbox-mcp"
  client_type        = "public"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # Custom scope mapping overrides the `groups` claim to a fixed
  # `["kubectl-sandbox-users"]` regardless of the authenticating user's actual
  # groups. This achieves privilege scoping at token-issue time — no RFC 8693
  # exchange needed, no confidential secondary client. Even a cluster admin
  # who logs in here only gets sandbox-level RBAC (via the OIDC group claim
  # mapping in kube-apiserver's AuthenticationConfiguration).
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
    authentik_property_mapping_provider_scope.kubectl_sandbox_fixed_groups.id,
  ]

  # - Claude Code: http://localhost:<port>/callback
  # - Claude.ai web (custom connectors): https://claude.ai/api/mcp/auth_callback
  # - kubernetes-mcp-server's built-in callback (browser testing): /oauth/callback
  allowed_redirect_uris = [
    {
      matching_mode = "regex"
      url           = "^http://localhost:[0-9]+/callback$"
    },
    {
      matching_mode = "strict"
      url           = "https://claude.ai/api/mcp/auth_callback"
    },
    {
      matching_mode = "strict"
      url           = "https://kubectl-sandbox-mcp.allegedly.works/oauth/callback"
    },
  ]
}

## Scope mapping: hardcodes `groups = ["kubectl-sandbox-users"]` on the issued
## token. This is what makes kubectl-sandbox-mcp scope-safe: the user's actual
## group memberships are NOT forwarded into the token; kube-apiserver only
## sees the sandbox group.
resource "authentik_property_mapping_provider_scope" "kubectl_sandbox_fixed_groups" {
  name       = "kubectl-sandbox-mcp-fixed-groups"
  scope_name = "groups"
  expression = <<-EXPR
    # Overrides the user's real groups with a fixed sandbox-only group.
    # The kubectl-sandbox-mcp application authenticates users via consent,
    # but the issued token only carries this group — no privilege escalation.
    return {"groups": ["kubectl-sandbox-users"]}
  EXPR
}

## Machine-client scope mapping for kubectl-sandbox-client-credentials.
## This provider is trusted by kube-apiserver, so keep the effective groups as
## an explicit allowlist of known machine principals. Unknown users get no
## Kubernetes RBAC group instead of falling back to sandbox.
resource "authentik_property_mapping_provider_scope" "kubectl_machine_groups" {
  name       = "kubectl-client-credentials-machine-groups"
  scope_name = "groups"
  expression = <<-EXPR
    username = request.user.username
    if username == "ak-kubectl-sandbox-client-credentials-client_credentials":
        return {"groups": ["kubectl-sandbox-users"]}
    if username == "haku-k8s":
        return {"groups": ["haku"]}
    # agent-box VM users map 1:1 to a same-named k8s group (group == username).
    # Add a user by extending this set.
    if username in {"agent-box-codex", "agent-box-zai"}:
        return {"groups": [username]}
    return {"groups": []}
  EXPR
}

resource "authentik_application" "kubectl_sandbox_scoped" {
  name              = "kubectl-sandbox-mcp"
  slug              = "kubectl-sandbox-mcp"
  protocol_provider = authentik_provider_oauth2.kubectl_sandbox_scoped.id
  meta_description  = "Sandbox kubectl MCP — token issued with fixed sandbox group"
  meta_launch_url   = "https://kubectl-sandbox-mcp.allegedly.works"
}

resource "authentik_policy_binding" "kubectl_sandbox_scoped_users" {
  target = authentik_application.kubectl_sandbox_scoped.uuid
  group  = authentik_group.kubectl_sandbox_users.id
  order  = 0
}

## No Kubernetes Secret needed — the OAuth2 provider is public (PKCE) and
## the pod runs in plain passthrough mode (no token exchange). The previous
## kubernetes_secret.kubectl_sandbox_scoped resource will be destroyed on
## the next TF apply.

# ============================================================================
# kubectl-sandbox-client-credentials — machine-to-machine OIDC for kubeconfig
# ============================================================================
# Non-interactive sibling of kubectl-sandbox-mcp: confidential OAuth2 client
# using `client_credentials` grant, so a CronJob can mint JWTs without a
# browser. Reuses the same kube-apiserver trusted issuer/audience for every
# machine identity; the provider's machine-only scope mapping is an explicit
# principal-to-groups allowlist.
#
# Consumer: cluster/k8s/agents/authentik-jwt-rotation/ CronJob. It runs
# biweekly, exchanges client_id + client_secret for a JWT, commits the JWT
# SOPS-encrypted to secrets/claude-web-k8s-jwt.yaml. `write_kubeconfig.py`
# on Claude Code sessions decrypts and embeds it.

resource "authentik_provider_oauth2" "kubectl_sandbox_client_credentials" {
  name        = "kubectl-sandbox-client-credentials"
  client_id   = "kubectl-sandbox-client-credentials"
  client_type = "confidential" # client_secret auto-generated by Authentik

  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # 45d access-token validity — comfortable margin over the biweekly rotation
  # CronJob. See cluster/k8s/agents/authentik-jwt-rotation/ for cadence math.
  access_token_validity = "hours=1080"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    authentik_property_mapping_provider_scope.kubectl_machine_groups.id,
  ]

  # client_credentials doesn't redirect, so allowed_redirect_uris is omitted.
}

resource "authentik_application" "kubectl_sandbox_client_credentials" {
  name              = "kubectl-sandbox-client-credentials"
  slug              = "kubectl-sandbox-client-credentials"
  protocol_provider = authentik_provider_oauth2.kubectl_sandbox_client_credentials.id
  meta_description  = "Machine-to-machine kubectl access with explicit Authentik principal-to-group mapping"
}

# Authentik auto-creates an internal service account for client_credentials
# grants (username: ak-<slug>-client_credentials). The client_credentials flow
# authenticates as this user. Without a policy binding for it, the grant is
# rejected with invalid_grant.
data "authentik_user" "kubectl_sandbox_cc_auto" {
  username = "ak-kubectl-sandbox-client-credentials-client_credentials"
}

resource "authentik_policy_binding" "kubectl_sandbox_cc_auto_user" {
  target = authentik_application.kubectl_sandbox_client_credentials.uuid
  user   = data.authentik_user.kubectl_sandbox_cc_auto.pk
  order  = 0
}

# K8s Secret holding the client_id + client_secret. Lives in agents-infra
# (where the authentik-jwt-rotation CronJob runs) and is the only place this
# credential exists outside Authentik — the CC web sandbox only ever holds
# the already-minted JWT (in secrets/claude-web-k8s-jwt.yaml SOPS).
resource "kubernetes_secret" "kubectl_sandbox_client_credentials" {
  metadata {
    name      = "kubectl-sandbox-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id + client_secret for kubectl-sandbox-client-credentials OIDC provider (mounted by authentik-jwt-rotation CronJob)"
    }
  }

  data = {
    client_id     = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_id
    client_secret = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_secret
  }
}

# ============================================================================
# haku-k8s — Haku's k8s identity, reusing the shared client_credentials provider
# ============================================================================
# Haku mints its k8s JWT through the EXISTING
# kubectl-sandbox-client-credentials OAuth2 provider — no dedicated provider,
# so no kube-apiserver issuer entry and no cluster bootstrap. Instead of a
# provider-level client_secret, the authentik-jwt-rotation CronJob authenticates
# as the haku-k8s service account using that SA's app-password token paired with
# the shared provider's client_id. Authentik issues a token FOR the haku-k8s SA,
# and the provider's machine-only scope mapping emits groups: ["haku"] only for
# that exact username. Unknown machine principals receive no Kubernetes group.
# The apiserver maps that claim to oidc-ksbx-groups:haku — read-only access to
# the haku namespace, isolated from the sandbox group.
#
# Consumer: cluster/k8s/agents/authentik-jwt-rotation/ CronJob (the haku-k8s
# rotations.yaml entry). It exchanges client_id + username + app-password for a
# JWT and commits it SOPS-encrypted to secrets/haku-k8s-jwt.yaml.

# Service account whose identity the issued JWT carries. The machine scope
# mapping branches on this username (haku-k8s) to emit groups: ["haku"].
resource "authentik_user" "haku_k8s" {
  username = "haku-k8s"
  name     = "Haku k8s service account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

# App-password token for the haku-k8s SA, used as the password in the
# client_credentials username/password exchange against the shared provider's
# client_id.
resource "authentik_token" "haku_k8s" {
  identifier   = "haku-k8s-client-credentials"
  user         = authentik_user.haku_k8s.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for Haku's k8s JWT rotation"
}

# Authorize the haku-k8s SA on the shared client_credentials application so the
# grant is accepted. Mirrors the existing per-user bindings on this app; uses a
# distinct order to avoid collisions.
resource "authentik_policy_binding" "haku_k8s_client_credentials" {
  target = authentik_application.kubectl_sandbox_client_credentials.uuid
  user   = authentik_user.haku_k8s.id
  order  = 2
}

# K8s Secret holding the shared provider's client_id + the haku-k8s username
# and app-password. Lives in agents-infra (where the authentik-jwt-rotation
# CronJob runs) and is the only place this credential exists outside Authentik
# — the minted JWT lives SOPS-encrypted in secrets/haku-k8s-jwt.yaml.
resource "kubernetes_secret" "haku_client_credentials" {
  metadata {
    name      = "haku-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id (shared kubectl-sandbox-client-credentials provider) + haku-k8s username/app-password, authenticating the haku-k8s service account so its JWT carries groups: [\"haku\"] (mounted by authentik-jwt-rotation CronJob)"
    }
  }

  data = {
    client_id = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_id
    username  = authentik_user.haku_k8s.username
    password  = authentik_token.haku_k8s.key
  }
}

# ============================================================================
# agent-box-codex — self-hosted Codex VM k8s identity
# ============================================================================
# The agent-box codex user mints its k8s JWT through the EXISTING
# kubectl-sandbox-client-credentials OAuth2 provider, matching the haku-k8s
# pattern above. Authentik issues a token for this service account, and the
# provider's explicit machine-principal mapping emits groups:
# ["agent-box-codex"] only for this exact username.
#
# Consumer: cluster/k8s/agents/authentik-jwt-rotation/ CronJob
# (agent-box-codex entry). It writes secrets/agent-box-codex-k8s-jwt.yaml, which
# home-manager decrypts on the agent-box VM to render /home/codex/.kube/config.

resource "authentik_user" "agent_box_codex" {
  username = "agent-box-codex"
  name     = "Agent-box Codex k8s service account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "agent_box_codex" {
  identifier   = "agent-box-codex-client-credentials"
  user         = authentik_user.agent_box_codex.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for agent-box Codex k8s JWT rotation"
}

resource "authentik_policy_binding" "agent_box_codex_client_credentials" {
  target = authentik_application.kubectl_sandbox_client_credentials.uuid
  user   = authentik_user.agent_box_codex.id
  order  = 3
}

resource "kubernetes_secret" "agent_box_codex_client_credentials" {
  metadata {
    name      = "agent-box-codex-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id (shared kubectl-sandbox-client-credentials provider) + agent-box-codex username/app-password, authenticating the agent-box Codex service account so its JWT carries groups: [\"agent-box-codex\"] (mounted by authentik-jwt-rotation CronJob)"
    }
  }

  data = {
    client_id = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_id
    username  = authentik_user.agent_box_codex.username
    password  = authentik_token.agent_box_codex.key
  }
}

# ============================================================================
# agent-box-zai — self-hosted zai (Claude Code via LiteLLM/z.ai) VM k8s identity
# ============================================================================
# The agent-box zai user mints its k8s JWT through the EXISTING
# kubectl-sandbox-client-credentials OAuth2 provider, matching the agent-box-codex
# pattern above. Authentik issues a token for this service account, and the
# provider's explicit machine-principal mapping emits groups: ["agent-box-zai"]
# only for this exact username.
#
# Consumer: cluster/k8s/agents/authentik-jwt-rotation/ CronJob (agent-box-zai
# entry). It writes secrets/agent-box-zai-k8s-jwt.yaml, which home-manager
# decrypts on the agent-box VM to render /home/zai/.kube/config.

resource "authentik_user" "agent_box_zai" {
  username = "agent-box-zai"
  name     = "Agent-box zai k8s service account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "agent_box_zai" {
  identifier   = "agent-box-zai-client-credentials"
  user         = authentik_user.agent_box_zai.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for agent-box zai k8s JWT rotation"
}

resource "authentik_policy_binding" "agent_box_zai_client_credentials" {
  target = authentik_application.kubectl_sandbox_client_credentials.uuid
  user   = authentik_user.agent_box_zai.id
  order  = 4
}

resource "kubernetes_secret" "agent_box_zai_client_credentials" {
  metadata {
    name      = "agent-box-zai-client-credentials"
    namespace = "agents-infra"
    annotations = {
      description = "client_id (shared kubectl-sandbox-client-credentials provider) + agent-box-zai username/app-password, authenticating the agent-box zai service account so its JWT carries groups: [\"agent-box-zai\"] (mounted by authentik-jwt-rotation CronJob)"
    }
  }

  data = {
    client_id = authentik_provider_oauth2.kubectl_sandbox_client_credentials.client_id
    username  = authentik_user.agent_box_zai.username
    password  = authentik_token.agent_box_zai.key
  }
}
