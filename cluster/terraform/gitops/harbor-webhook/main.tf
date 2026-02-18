# Harbor webhook notification → Flux Receiver
#
# Configures the Harbor `props` project to send push_artifact events to the
# Flux webhook receiver, eliminating the 5-minute ImageRepository poll lag.
#
# Depends on harbor-ci having run first (props project must exist).

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

provider "vault" {
  address = var.vault_address
  token   = var.vault_token
}

data "vault_kv_secret_v2" "harbor_webhook_token" {
  mount = "kv"
  name  = "harbor/webhook-token"
}

data "harbor_project" "props" {
  name = "props"
}

locals {
  token            = data.vault_kv_secret_v2.harbor_webhook_token.data["token"]
  flux_webhook_url = "${var.flux_webhook_base_url}/hook/${sha256(local.token)}"
}

resource "harbor_project_webhook" "flux_receiver" {
  name             = "flux-image-receiver"
  project_id       = data.harbor_project.props.id
  description      = "Notifies Flux webhook receiver on image push, triggering immediate ImageRepository rescan"
  enabled          = true
  notify_type      = "http"
  address          = local.flux_webhook_url
  auth_header      = local.token
  skip_cert_verify = false
  events_types     = ["PUSH_ARTIFACT"]
}
