# Headlamp — OIDC login for the Kubernetes dashboard

resource "authentik_application" "headlamp" {
  name              = "Headlamp"
  slug              = "headlamp"
  protocol_provider = authentik_provider_oauth2.providers["headlamp"].id
  meta_description  = "Kubernetes cluster UI"
  meta_launch_url   = "https://headlamp.allegedly.works"
  meta_icon         = "https://raw.githubusercontent.com/kubernetes-sigs/headlamp/main/frontend/public/android-chrome-512x512.png"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "headlamp_admins" {
  target = authentik_application.headlamp.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

resource "kubernetes_secret" "headlamp_oidc" {
  metadata {
    name      = "headlamp-oidc-secret"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "headlamp"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "headlamp"
    }
  }

  data = {
    OIDC_CLIENT_ID     = authentik_provider_oauth2.providers["headlamp"].client_id
    OIDC_CLIENT_SECRET = authentik_provider_oauth2.providers["headlamp"].client_secret
    OIDC_ISSUER_URL    = "https://auth.allegedly.works/application/o/headlamp/"
    OIDC_SCOPES        = "profile,email,offline_access"
  }
}
