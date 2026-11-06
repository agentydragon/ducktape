# GitHub Actions run and pull request webhook for the CDviz receiver.

data "kubernetes_secret" "github_secrets_sync_pat" {
  metadata {
    name      = "github-secrets-sync-pat"
    namespace = "flux-system"
  }
}

provider "github" {
  owner = "agentydragon"
  token = data.kubernetes_secret.github_secrets_sync_pat.data["token"]
}

resource "random_password" "github_webhook" {
  length  = 40
  special = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_secret" "github_webhook" {
  metadata {
    name      = var.webhook_secret_name
    namespace = var.namespace
  }

  data = {
    token = random_password.github_webhook.result
  }

  lifecycle {
    # This signing key is persistent authentication material for the receiver.
    prevent_destroy = true
  }
}

resource "github_repository_webhook" "cdviz" {
  repository = "ducktape"

  configuration {
    url          = var.webhook_url
    content_type = "json"
    secret       = random_password.github_webhook.result
    insecure_ssl = false
  }

  active = true
  events = ["workflow_run", "workflow_job", "pull_request"]
}
