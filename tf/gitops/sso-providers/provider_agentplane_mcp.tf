# External MCP authentication is separate from app login and operator federation.
resource "authentik_provider_oauth2" "agentplane_mcp" {
  name                       = "agentplane-staging-mcp"
  client_id                  = "agentplane-staging-mcp"
  client_type                = "confidential"
  authorization_flow         = data.authentik_flow.implicit_consent.id
  invalidation_flow          = data.authentik_flow.invalidation.id
  signing_key                = data.authentik_certificate_key_pair.self_signed.id
  issuer_mode                = "per_provider"
  sub_mode                   = "user_uuid"
  include_claims_in_id_token = true
  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]
  allowed_redirect_uris = [{
    matching_mode = "strict"
    url           = "${local.agentplane_mcp_origin}/auth/callback"
  }]
}

resource "authentik_application" "agentplane_mcp" {
  name              = "Agentplane staging MCP"
  slug              = "agentplane-staging-mcp"
  protocol_provider = authentik_provider_oauth2.agentplane_mcp.id
  meta_description  = "External MCP enrollment with operator consent in the Agentplane integration app"
}

resource "authentik_policy_binding" "agentplane_mcp_access" {
  target = authentik_application.agentplane_mcp.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

resource "random_password" "agentplane_mcp_signing_key" {
  length  = 64
  special = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "random_id" "agentplane_mcp_encryption_key" {
  byte_length = 32
  lifecycle {
    prevent_destroy = true
  }
}

locals {
  agentplane_mcp_origin = "https://agentplane-actions-staging.allegedly.works"
  agentplane_mcp_issuer = "https://auth.allegedly.works/application/o/${authentik_application.agentplane_mcp.slug}/"
  agentplane_mcp_oauth = {
    config_url                  = "${local.agentplane_mcp_issuer}.well-known/openid-configuration"
    upstream_client_id          = authentik_provider_oauth2.agentplane_mcp.client_id
    upstream_client_secret_file = "/etc/agentplane-mcp/client-secret"
    base_url                    = local.agentplane_mcp_origin
    integration_app_url         = "https://agentplane-staging.allegedly.works"
    jwt_signing_key_file        = "/etc/agentplane-mcp/jwt-signing-key"
    encryption_key_file         = "/etc/agentplane-mcp/encryption-key"
    upstream_issuer             = local.agentplane_mcp_issuer
    upstream_subject            = data.authentik_user.agentplane_operator.uuid
    approving_operator = {
      issuer  = local.agentplane_actions_issuer
      subject = data.authentik_user.agentplane_operator.uuid
      role    = "operator"
    }
  }
}

resource "kubernetes_secret" "agentplane_mcp_oauth" {
  metadata {
    name      = "agentplane-mcp-oauth"
    namespace = "authentik"
    annotations = {
      description                                                     = "Agentplane external MCP client, stable token signing key and encrypted OAuth storage key"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "agentplane-staging"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "agentplane-staging"
    }
  }
  data = {
    oauth           = jsonencode(local.agentplane_mcp_oauth)
    client-secret   = authentik_provider_oauth2.agentplane_mcp.client_secret
    jwt-signing-key = random_password.agentplane_mcp_signing_key.result
    # Fernet requires URL-safe base64 of exactly 32 bytes, retaining padding.
    encryption-key = replace(replace(random_id.agentplane_mcp_encryption_key.b64_std, "+", "-"), "/", "_")
  }
  lifecycle {
    precondition {
      condition     = data.authentik_user.agentplane_operator.uuid != ""
      error_message = "The MCP operator must have an authoritative Authentik UUID subject."
    }
  }
}
