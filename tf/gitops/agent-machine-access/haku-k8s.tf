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
