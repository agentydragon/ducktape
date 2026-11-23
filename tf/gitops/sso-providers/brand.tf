# Domain-matched brand for allegedly.works. It sets no `flow_authentication`, so Authentik falls
# back to `default-authentication-flow`; cluster/k8s/authentik/app/blueprints/users.yaml patches
# that flow's stages.

resource "authentik_brand" "allegedly_works" {
  domain         = "allegedly.works"
  default        = false
  branding_title = "allegedly.works"

  # Authentik stores its bundled asset paths here when unset, and the provider
  # diffs them against the omitted attributes, so every plan would PUT the brand.
  lifecycle {
    ignore_changes = [branding_logo, branding_favicon]
  }
}
