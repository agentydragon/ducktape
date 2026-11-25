# GitLab's OIDC subject uses Authentik's stable user UID; the GitLab module
# reads the same UID from the reflected identity Secret before linking it.

resource "authentik_application" "gitlab_spike" {
  name              = "GitLab Spike"
  slug              = "gitlab-spike"
  protocol_provider = authentik_provider_oauth2.providers["gitlab_spike"].id
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
        issuer             = "https://auth.allegedly.works/application/o/${authentik_application.gitlab_spike.slug}/"
        discovery          = true
        client_auth_method = "query"
        uid_field          = "sub"
        client_options = {
          identifier   = authentik_provider_oauth2.providers["gitlab_spike"].client_id
          secret       = authentik_provider_oauth2.providers["gitlab_spike"].client_secret
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
