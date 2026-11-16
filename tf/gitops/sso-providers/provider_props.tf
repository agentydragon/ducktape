# Props — OIDC login for the eval dashboard (props.allegedly.works)
#
# The props backend is the OIDC relying party (authlib + session cookie); see
# props/backend/oidc.py. This provider mints the confidential client; the
# client_id/secret and a generated session-signing secret are written to a K8s
# secret in the authentik namespace and reflected into the props namespace,
# where the Deployment consumes them as PROPS_OIDC_* env vars.

resource "authentik_application" "props" {
  name              = "Props"
  slug              = "props"
  protocol_provider = authentik_provider_oauth2.confidential["props"].id
  meta_description  = "LLM critic evaluation dashboard"
  meta_launch_url   = "https://props.allegedly.works"
}

# Gate login to the admins group (which contains agentydragon). The backend
# additionally checks the email against PROPS_OIDC_ADMIN_EMAILS (defense in depth).
resource "authentik_policy_binding" "props_admins" {
  target = authentik_application.props.uuid
  group  = data.authentik_group.admins.id
  order  = 0
}

# Signs the props session cookie (Starlette SessionMiddleware secret_key).
# Generated here so it lives with the client credentials and rotates together.
resource "random_password" "props_session_secret" {
  length  = 64
  special = false
}
