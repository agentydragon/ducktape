# GitLab's OIDC subject uses Authentik's stable user UID; the GitLab module
# reads the same UID from the reflected identity Secret before linking it.
resource "authentik_provider_oauth2" "gitlab_spike" {
  name               = "gitlab-spike-oauth2"
  client_id          = "gitlab-spike"
  client_type        = "confidential"
  grant_types        = ["authorization_code"]
  authorization_flow = data.authentik_flow.implicit_consent.id
  invalidation_flow  = data.authentik_flow.invalidation.id
  signing_key        = data.authentik_certificate_key_pair.self_signed.id

  issuer_mode                = "per_provider"
  sub_mode                   = "hashed_user_id"
  include_claims_in_id_token = true

  property_mappings = [
    data.authentik_property_mapping_provider_scope.openid.id,
    data.authentik_property_mapping_provider_scope.email.id,
    data.authentik_property_mapping_provider_scope.profile.id,
  ]

  allowed_redirect_uris = [
    {
      matching_mode     = "strict"
      url               = "https://gitlab-spike.allegedly.works/users/auth/openid_connect/callback"
      redirect_uri_type = "authorization"
    },
  ]
}

resource "authentik_application" "gitlab_spike" {
  name              = "GitLab Spike"
  slug              = "gitlab-spike"
  protocol_provider = authentik_provider_oauth2.gitlab_spike.id
  meta_description  = "GitLab replacement spike"
  meta_publisher    = "GitLab"
}

resource "authentik_policy_binding" "gitlab_spike_agentydragon" {
  target = authentik_application.gitlab_spike.uuid
  user   = tonumber(authentik_user.agentydragon.id)
  order  = 0
}

# The chart consumes `provider`; the Terraform GitLab module consumes the
# actual stable subject and account attributes from the same reflected Secret.
resource "kubernetes_secret_v1" "gitlab_spike_oidc" {
  metadata {
    name      = "gitlab-spike-oidc"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "gitlab-spike"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "gitlab-spike"
    }
  }

  data = {
    provider = yamlencode({
      name  = "openid_connect"
      label = "Authentik"
      args = {
        name               = "openid_connect"
        scope              = ["openid", "profile", "email"]
        response_type      = "code"
        issuer             = "https://auth.allegedly.works/application/o/gitlab-spike/"
        discovery          = true
        client_auth_method = "query"
        uid_field          = "sub"
        client_options = {
          identifier   = authentik_provider_oauth2.gitlab_spike.client_id
          secret       = authentik_provider_oauth2.gitlab_spike.client_secret
          redirect_uri = "https://gitlab-spike.allegedly.works/users/auth/openid_connect/callback"
        }
      }
    })
    subject  = data.authentik_user.agentydragon.uid
    username = data.authentik_user.agentydragon.username
    email    = data.authentik_user.agentydragon.email
    name     = data.authentik_user.agentydragon.name
  }
}
