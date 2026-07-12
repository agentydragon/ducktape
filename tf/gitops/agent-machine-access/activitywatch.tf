# ============================================================================
# Shared: kubectl-sandbox-users group
# ============================================================================

data "authentik_user" "agentydragon" {
  username = "agentydragon"
}

data "authentik_user" "auragon" {
  username = "auragon"
}

# ============================================================================
# ActivityWatch query access
# ============================================================================

locals {
  activitywatch_agent_clients = {
    haku = {
      username         = "activitywatch-haku"
      name             = "ActivityWatch haku query service account"
      email            = "activitywatch-haku@allegedly.works"
      target_namespace = "haku-sandbox"
      secret_name      = "activitywatch-haku-client-credentials"
      order            = 10
    }
    claude_sandbox = {
      username         = "activitywatch-claude-sandbox"
      name             = "ActivityWatch claude-sandbox query service account"
      email            = "activitywatch-claude-sandbox@allegedly.works"
      target_namespace = "claude-sandbox"
      secret_name      = "activitywatch-claude-sandbox-client-credentials"
      order            = 20
    }
  }
}

resource "authentik_group" "activitywatch_users" {
  name  = "activitywatch-users"
  users = [data.authentik_user.agentydragon.pk]
}

resource "authentik_provider_oauth2" "activitywatch_agent_credentials" {
  name        = "activitywatch-agent-client-credentials"
  client_id   = "activitywatch-agent-client-credentials"
  client_type = "confidential"

  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true
  access_token_validity      = "hours=1"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]
}

resource "authentik_application" "activitywatch_agent_credentials" {
  name              = "ActivityWatch agent client credentials"
  slug              = "activitywatch-agent-client-credentials"
  protocol_provider = authentik_provider_oauth2.activitywatch_agent_credentials.id
  meta_description  = "Machine client_credentials provider for ActivityWatch query access"
}

resource "authentik_provider_proxy" "activitywatch" {
  name                  = "activitywatch"
  external_host         = "https://activitywatch.allegedly.works"
  internal_host         = "http://activitywatch-readonly.activitywatch.svc.cluster.local:5600"
  mode                  = "proxy"
  authentication_flow   = data.authentik_flow.authentication.id
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  access_token_validity = "hours=1"

  jwt_federation_providers = [authentik_provider_oauth2.activitywatch_agent_credentials.id]
}

resource "authentik_application" "activitywatch" {
  name              = "ActivityWatch"
  slug              = "activitywatch"
  protocol_provider = authentik_provider_proxy.activitywatch.id
  meta_description  = "Personal ActivityWatch query surface"
  meta_launch_url   = "https://activitywatch.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "activitywatch_users" {
  target = authentik_application.activitywatch.uuid
  group  = authentik_group.activitywatch_users.id
  order  = 0
}

resource "authentik_user" "activitywatch_agent" {
  for_each = local.activitywatch_agent_clients

  username = each.value.username
  name     = each.value.name
  email    = each.value.email
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "activitywatch_agent" {
  for_each = local.activitywatch_agent_clients

  identifier   = "${each.value.username}-client-credentials"
  user         = authentik_user.activitywatch_agent[each.key].id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "client_credentials app-password for ${each.value.name}"
}

resource "authentik_policy_binding" "activitywatch_agent_credentials" {
  for_each = local.activitywatch_agent_clients

  target = authentik_application.activitywatch_agent_credentials.uuid
  user   = authentik_user.activitywatch_agent[each.key].id
  order  = each.value.order
}

resource "authentik_policy_binding" "activitywatch_agent_proxy" {
  for_each = local.activitywatch_agent_clients

  target = authentik_application.activitywatch.uuid
  user   = authentik_user.activitywatch_agent[each.key].id
  order  = each.value.order
}

resource "kubernetes_secret" "activitywatch_agent_client_credentials" {
  for_each = local.activitywatch_agent_clients

  metadata {
    name      = each.value.secret_name
    namespace = "authentik"
    annotations = {
      "description"                                                   = "ActivityWatch query OAuth credentials for ${each.value.name}. Reflected into ${each.value.target_namespace}; caller mints a source JWT, exchanges it for an Authentik proxy token, then queries https://activitywatch.allegedly.works."
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = each.value.target_namespace
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = each.value.target_namespace
    }
  }

  data = {
    activitywatch_url = "https://activitywatch.allegedly.works"
    token_url         = "https://auth.allegedly.works/application/o/token/"
    client_id         = authentik_provider_oauth2.activitywatch_agent_credentials.client_id
    username          = authentik_user.activitywatch_agent[each.key].username
    password          = authentik_token.activitywatch_agent[each.key].key
    source_scopes     = "openid profile email"
    proxy_client_id   = authentik_provider_proxy.activitywatch.client_id
    proxy_scopes      = "openid profile email ak_proxy"
  }
}
