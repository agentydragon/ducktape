# Flux webhook token management
#
# Manages the GitHub webhook token and receiver registration for the Flux
# github Receiver, independently of Harbor. Decoupled from harbor-ci so that
# the Flux webhook receiver can reconcile without waiting for Harbor to be
# healthy.
#
# Creates:
#   - github-webhook-token: K8s Secret in flux-system with the HMAC token
#     used by the Flux Receiver to validate incoming GitHub webhook payloads
#   - GitHub repository webhook on ducktape pointing to the Flux Receiver URL

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

resource "random_password" "github_webhook_token" {
  length  = 40
  special = false
}

resource "kubernetes_secret" "github_webhook_token" {
  metadata {
    name      = "github-webhook-token"
    namespace = "flux-system"
  }

  data = {
    token = random_password.github_webhook_token.result
  }

  lifecycle {
    # Don't rotate after initial creation — rotating requires reconfiguring
    # the GitHub webhook URL (path changes with the sha256 of the token).
    ignore_changes = [data]
  }
}

resource "github_repository_webhook" "flux_receiver" {
  repository = "ducktape"

  configuration {
    url          = "https://flux-webhook.allegedly.works/hook/${sha256(random_password.github_webhook_token.result)}"
    content_type = "json"
    secret       = random_password.github_webhook_token.result
    insecure_ssl = false
  }

  active = true
  events = ["push", "registry_package"]

  lifecycle {
    # Token has ignore_changes on its random_password, so the URL is stable.
    # Only recreate if the webhook is manually deleted from GitHub.
    ignore_changes = [configuration]
  }
}
