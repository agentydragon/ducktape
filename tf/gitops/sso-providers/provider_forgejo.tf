# Forgejo — OIDC login for the git server

resource "authentik_application" "forgejo" {
  name              = "Forgejo"
  slug              = "forgejo"
  protocol_provider = authentik_provider_oauth2.confidential["forgejo"].id
  meta_description  = "Forgejo Git Repository Management"
  meta_publisher    = "Forgejo"
  meta_icon         = "https://cdn.simpleicons.org/forgejo"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "forgejo_admins" {
  target = authentik_application.forgejo.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# Forgejo helm chart expects keys named "key" (client_id) and "secret" (client_secret).
resource "kubernetes_secret" "forgejo_oauth" {
  metadata {
    name      = "forgejo-oauth-client-secret"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "forgejo,flux-system"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "forgejo,flux-system"
    }
  }

  data = {
    key    = authentik_provider_oauth2.confidential["forgejo"].client_id
    secret = authentik_provider_oauth2.confidential["forgejo"].client_secret
  }
}
