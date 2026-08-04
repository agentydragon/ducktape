resource "authentik_user" "agent_sa" {
  username = "agent-service-account"
  name     = "Agent Service Account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "agent_sa_token" {
  identifier   = "agent-api-key"
  user         = authentik_user.agent_sa.id
  intent       = "api"
  expiring     = false
  retrieve_key = true
  description  = "Bearer token for Claude/OpenClaw sandbox agents"
}

resource "kubernetes_secret" "agent_bearer_token" {
  metadata {
    name      = "agent-bearer-token"
    namespace = "claude-sandbox"
  }

  data = {
    token = authentik_token.agent_sa_token.key
  }
}
