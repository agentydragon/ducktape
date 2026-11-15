# Airlock — server-side OIDC login for the OAuth credential broker.
# The backend exchanges the authorization code and signs an HttpOnly session
# cookie; the browser never receives Authentik access or ID tokens.

resource "authentik_application" "airlock" {
  name              = "Airlock"
  slug              = "airlock-server"
  protocol_provider = authentik_provider_oauth2.providers["airlock"].id
  meta_description  = "OAuth credential broker"
  meta_launch_url   = "https://airlock.allegedly.works"
  open_in_new_tab   = true
}

resource "authentik_policy_binding" "airlock_admins" {
  target = authentik_application.airlock.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

resource "kubernetes_secret" "airlock_oidc" {
  metadata {
    name      = "airlock-oidc-config"
    namespace = "authentik"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "airlock"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "airlock"
    }
  }

  data = {
    client-id     = authentik_provider_oauth2.providers["airlock"].client_id
    client-secret = authentik_provider_oauth2.providers["airlock"].client_secret
  }
}
