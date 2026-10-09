# Forgejo registry tenant for ducktape CI-built in-cluster images.
#
# Creates a dedicated `ducktape-ci` Forgejo user that owns CI-pushed images
# (git.allegedly.works/ducktape-ci/<image>, e.g. codex-pod). Its password is the
# shared value in the SOPS-provisioned `forgejo-images-creds` Secret, so CI push
# (secrets/ci/forgejo-images-registry.sops.yaml) and the kubelet/Flux pull
# credential (the same Secret, distributed by ESO into consuming namespaces)
# all authenticate as this user. Deliberately no proxy (unlike props): clients
# talk to Forgejo directly.

data "kubernetes_secret" "forgejo_admin" {
  metadata {
    name      = "forgejo-admin-password"
    namespace = "forgejo"
  }
}

data "kubernetes_secret" "images_creds" {
  metadata {
    name      = "forgejo-images-creds"
    namespace = "forgejo-images"
  }
}

locals {
  images_user = "ducktape-ci"
}

provider "forgejo" {
  host     = var.forgejo_url
  username = data.kubernetes_secret.forgejo_admin.data["username"]
  password = data.kubernetes_secret.forgejo_admin.data["password"]
}

resource "forgejo_user" "images" {
  login                = local.images_user
  email                = "ducktape-ci@allegedly.works"
  password             = data.kubernetes_secret.images_creds.data["password"]
  must_change_password = false
  visibility           = "private"
}

# A webhook on the ducktape-ci user that fires when CI pushes an image, so Flux scans the new
# tag at once instead of on the 5m ImageRepository poll. A user-level hook fires for packages
# linked to no repository, which every ducktape-ci image is; a repository-scoped hook would
# need each package linked to a repository first.
#
# The Receiver it calls (cluster/cdk8s/forgejo/images.py) serves /hook/<sha256 of token, receiver
# name and namespace> and checks no signature: the unguessable path is the secret. An External
# Secrets `Password` generator mints the token once and never refreshes it, since a new token
# changes that path; it is read here from the namespace it is minted into.
data "kubernetes_secret" "webhook_token" {
  metadata {
    name      = var.webhook_token_secret
    namespace = var.receiver_namespace
  }
}

# The svalabs/forgejo provider has no webhook resource for a user. `/user/hooks` acts for the
# authenticated user, so this provider authenticates as ducktape-ci rather than as the admin.
provider "restapi" {
  uri                  = var.forgejo_url
  username             = local.images_user
  password             = data.kubernetes_secret.images_creds.data["password"]
  id_attribute         = "id"
  write_returns_object = true
}

# Forgejo refuses to deliver to private addresses, so the hook targets the Receivers' public host.
resource "restapi_object" "package_webhook" {
  path          = "/api/v1/user/hooks"
  update_method = "PATCH"
  data = jsonencode({
    type   = "forgejo"
    active = true
    events = ["package"]
    config = {
      content_type = "json"
      url          = "https://${var.webhook_host}/hook/${sha256(join("", [data.kubernetes_secret.webhook_token.data["token"], var.receiver_name, var.receiver_namespace]))}"
    }
  })
  ignore_all_server_changes = true

  depends_on = [forgejo_user.images]
}

# ESO already owns this Secret through the agent-workspaces ExternalSecret.
# CLEANUP(added 2026-09-11): Remove once all forgejo_images states have forgotten
# kubernetes_secret.agent_workspaces_pull. Never destroy the ESO-owned Secret.
removed {
  from = kubernetes_secret.agent_workspaces_pull
  lifecycle {
    destroy = false
  }
}
