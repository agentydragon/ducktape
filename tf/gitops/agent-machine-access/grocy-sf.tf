# --- Grocy SF household (proxy provider + MCP OAuth2) ---

resource "authentik_group" "grocy_sf_household" {
  name = "SF household"
  users = [
    data.authentik_user.agentydragon.pk,
    data.authentik_user.auragon.pk,
  ]
}

resource "authentik_provider_proxy" "grocy_sf" {
  name                  = "grocy-sf"
  external_host         = "https://grocy-sf.allegedly.works"
  internal_host         = "http://grocy.grocy-sf.svc.cluster.local:80"
  mode                  = "proxy"
  authentication_flow   = data.authentik_flow.authentication.id
  authorization_flow    = data.authentik_flow.implicit_consent.id
  invalidation_flow     = data.authentik_flow.invalidation.id
  access_token_validity = "hours=24"

  jwt_federation_providers = [
    authentik_provider_oauth2.grocy_mcp_sf.id,
  ]
}

resource "authentik_application" "grocy_sf" {
  name              = "Grocy SF"
  slug              = "grocy-sf"
  protocol_provider = authentik_provider_proxy.grocy_sf.id
  meta_description  = "Groceries & household management (SF)"
  meta_icon         = "https://cdn.simpleicons.org/grocy"
  meta_launch_url   = "https://grocy-sf.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "grocy_sf_household" {
  target = authentik_application.grocy_sf.uuid
  group  = authentik_group.grocy_sf_household.id
  order  = 0
}

moved {
  from = authentik_policy_binding.grocy_sf_admins
  to   = authentik_policy_binding.grocy_sf_household
}

resource "authentik_provider_oauth2" "grocy_mcp_sf" {
  name               = "grocy-mcp-sf"
  client_id          = "grocy-mcp-sf"
  client_type        = "confidential"
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  include_claims_in_id_token = true

  # Same reason as ha-mcp.tf: haku-console holds an operator OAuth association here, and the
  # Terraform provider's `minutes=10` default made it renew ~150x/day, any one of which can
  # permanently wedge the association. Matches the `grocy-sf` proxy provider above.
  access_token_validity = "hours=24"

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
    data.authentik_property_mapping_provider_scope.offline_access.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode = "strict"
      url           = "https://grocy-mcp-sf.allegedly.works/auth/callback"
    },
  ]
}

resource "authentik_application" "grocy_mcp_sf" {
  name              = "Grocy MCP SF"
  slug              = "grocy-mcp-sf"
  protocol_provider = authentik_provider_oauth2.grocy_mcp_sf.id
  meta_description  = "Auth-aware MCP server for Grocy SF household"
  meta_launch_url   = "https://grocy-mcp-sf.allegedly.works"
}

resource "authentik_policy_binding" "grocy_mcp_sf_household" {
  target = authentik_application.grocy_mcp_sf.uuid
  group  = authentik_group.grocy_sf_household.id
  order  = 0
}

moved {
  from = authentik_policy_binding.grocy_mcp_sf_admins
  to   = authentik_policy_binding.grocy_mcp_sf_household
}

# Canonical OIDC client credentials. Reflector mirrors this Secret into the
# household namespace after that namespace has been created.
resource "kubernetes_secret" "grocy_mcp_oidc_sf_source" {
  metadata {
    name      = "grocy-mcp-oidc-sf"
    namespace = "authentik"
    annotations = {
      description                                                     = "Grocy SF MCP OIDC client credentials"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "grocy-sf"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "grocy-sf"
    }
  }

  data = {
    client_id             = authentik_provider_oauth2.grocy_mcp_sf.client_id
    client_secret         = authentik_provider_oauth2.grocy_mcp_sf.client_secret
    grocy_proxy_client_id = authentik_provider_proxy.grocy_sf.client_id
  }
}

# --- agentplane read-only identity ---

# The account agentplane-staging's egress proxy presents to grocy-sf.allegedly.works for
# claude-ai sandboxes (`grocy-sf-readonly` in cluster/cdk8s/agentplane/egress_staging_credentials.py),
# as HTTP Basic with this app password. The outpost turns Basic credentials into a
# client_credentials grant against the grocy-sf proxy provider itself, so no token is minted
# or stored outside Authentik. Grocy creates the matching user on its first request with no
# permissions (DEFAULT_PERMISSIONS=none, cluster/k8s/grocy/app-base), and the egress proxy
# presents the password only on GETs to Grocy's read routes.
resource "authentik_user" "agentplane_grocy_sf_readonly" {
  username = "agentplane-grocy-sf-readonly"
  name     = "agentplane Grocy SF read-only service account"
  type     = "service_account"
  path     = "goauthentik.io/service-accounts"
}

resource "authentik_token" "agentplane_grocy_sf_readonly" {
  identifier   = "agentplane-grocy-sf-readonly"
  user         = authentik_user.agentplane_grocy_sf_readonly.id
  intent       = "app_password"
  expiring     = false
  retrieve_key = true
  description  = "HTTP Basic password agentplane-staging's egress proxy presents to the grocy-sf outpost"
}

resource "authentik_policy_binding" "grocy_sf_agentplane_readonly" {
  target = authentik_application.grocy_sf.uuid
  user   = authentik_user.agentplane_grocy_sf_readonly.id
  order  = 1
}

resource "kubernetes_secret" "agentplane_grocy_sf_readonly" {
  metadata {
    name      = "agentplane-grocy-sf-readonly"
    namespace = "agents-infra"
    annotations = {
      description = "App password of the agentplane-grocy-sf-readonly Authentik service account, copied by ESO into agentplane-staging's egress-credentials namespace"
    }
  }

  data = {
    password = authentik_token.agentplane_grocy_sf_readonly.key
  }
}
