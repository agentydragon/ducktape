# Harbor webhook notification → Flux Receiver
#
# CLEANUP(2026-03-30): Ducktape project images migrated to GHCR. This webhook
# is no longer needed for ducktape images (Flux now watches GHCR directly).
# Keep alive until props also migrates, then suspend/remove.
#
# Configures the Harbor `ducktape` project to send push_artifact events to the
# Flux webhook receiver, eliminating the 5-minute ImageRepository poll lag.
#
# Depends on harbor-ci having run first (ducktape project must exist).

data "kubernetes_secret" "harbor_admin_password" {
  metadata {
    name      = "harbor-admin-initial"
    namespace = "harbor"
  }
}

provider "harbor" {
  url      = var.harbor_url
  username = "admin"
  password = data.kubernetes_secret.harbor_admin_password.data["HARBOR_ADMIN_PASSWORD"]
}

data "kubernetes_secret" "harbor_webhook_token" {
  metadata {
    name      = "harbor-webhook-token"
    namespace = "flux-system"
  }
}

data "harbor_project" "ducktape" {
  name = "ducktape"
}

locals {
  token            = data.kubernetes_secret.harbor_webhook_token.data["token"]
  flux_webhook_url = "${var.flux_webhook_base_url}/hook/${sha256(local.token)}"
}

resource "harbor_project_webhook" "flux_receiver" {
  name             = "flux-image-receiver"
  project_id       = data.harbor_project.ducktape.id
  description      = "Notifies Flux webhook receiver on image push, triggering immediate ImageRepository rescan"
  enabled          = true
  notify_type      = "http"
  address          = local.flux_webhook_url
  auth_header      = local.token
  skip_cert_verify = false
  events_types     = ["PUSH_ARTIFACT"]
}
