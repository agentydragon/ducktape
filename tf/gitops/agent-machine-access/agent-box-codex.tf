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
