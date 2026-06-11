# ============================================================================
# OpenClaw Agent — M2M auth via client_credentials grant
# ============================================================================

resource "authentik_provider_oauth2" "openclaw_agent" {
  name               = "openclaw-agent"
  client_id          = "openclaw-agent"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  access_token_validity      = "hours=1"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.propose.id,
    data.authentik_property_mapping_provider_scope.read.id,
  ]

  allowed_redirect_uris = []
}

resource "authentik_application" "openclaw_agent" {
  name              = "OpenClaw Agent"
  slug              = "openclaw-agent"
  protocol_provider = authentik_provider_oauth2.openclaw_agent.id
  meta_description  = "Machine-to-machine auth for openclaw agents"
}

resource "authentik_user" "openclaw_agent_sa" {
  username  = "openclaw-agent-sa"
  name      = "OpenClaw Agent (Service Account)"
  type      = "service_account"
  is_active = true
  path      = "service-accounts"
}

resource "authentik_policy_binding" "openclaw_agent_sa" {
  target = authentik_application.openclaw_agent.uuid
  user   = authentik_user.openclaw_agent_sa.id
  order  = 0
}

resource "kubernetes_secret" "airlock_secrets" {
  metadata {
    name      = "airlock-secrets"
    namespace = "authentik"
    annotations = {
      description                                                     = "OAuth2 client creds for openclaw-agent — reflected to airlock and openclaw-gateway"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "airlock,openclaw-gateway"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "airlock,openclaw-gateway"
    }
  }

  data = {
    "client-id"     = authentik_provider_oauth2.openclaw_agent.client_id
    "client-secret" = authentik_provider_oauth2.openclaw_agent.client_secret
  }
}
