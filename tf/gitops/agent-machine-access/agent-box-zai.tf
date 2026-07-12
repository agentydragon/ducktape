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
