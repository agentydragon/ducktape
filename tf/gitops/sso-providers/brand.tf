# Domain-matched brand for allegedly.works.
#
# Historically paired with a `custom-authentication-flow` (no-MFA) so that
# direct logins at auth.allegedly.works bypassed Authentik's built-in
# `default-authentication-mfa-validation` stage. That custom flow was
# deleted in 2026-04 in favor of the built-in `default-authentication-flow`
# with the MFA validation stage patched to `not_configured_action: skip`
# (see cluster/k8s/authentik/app/blueprints/users.yaml). Net effect is
# identical for users without MFA enrolled; per-user MFA still enforces
# when configured. With the custom flow gone, the brand no longer needs
# `flow_authentication` — Authentik falls back to default-authentication-flow.

resource "authentik_brand" "allegedly_works" {
  domain         = "allegedly.works"
  default        = false
  branding_title = "allegedly.works"
}
