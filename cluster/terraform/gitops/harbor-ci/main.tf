# Harbor CI infrastructure
#
# CLEANUP(2026-03-30): Ducktape project images migrated to GHCR. The CI robot
# account is no longer used for pushing. The pull robot is still needed by props
# (agent images pulled from Harbor). Once props also migrates off Harbor, suspend
# this Terraform resource and orphan with `removed` blocks.
#
# Creates:
#   - ducktape project (private, single project for all CI-pushed images)
#   - ci robot account with push+pull on the ducktape project (CI push) — NO LONGER USED
#   - pull robot account with read-only access (imagePullSecrets in app namespaces)
#   - webhook token for the Flux harbor Receiver
#   - github webhook token for the Flux github Receiver
#
# Stores all credentials in Vault at kv/harbor/{ci-robot,pull-robot,webhook-token}
# and kv/flux/github-webhook-token.

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
  auth_login_jwt {
    mount = "kubernetes"
    role  = "tf-runner"
    jwt   = fileexists("/var/run/secrets/kubernetes.io/serviceaccount/token") ? file("/var/run/secrets/kubernetes.io/serviceaccount/token") : "not-in-cluster"
  }
}

# Orphan old per-service projects — they still exist in Harbor (with images) but
# are no longer managed by Terraform. Remove these blocks once all images have
# been pushed to ducktape/ and the old projects are manually deleted.
removed {
  from = harbor_project.props
  lifecycle { destroy = false }
}
removed {
  from = harbor_project.inventree
  lifecycle { destroy = false }
}
removed {
  from = harbor_project.openclaw
  lifecycle { destroy = false }
}
removed {
  from = harbor_project.activitywatch
  lifecycle { destroy = false }
}
removed {
  from = harbor_project.oauth_broker
  lifecycle { destroy = false }
}

# Single project for all CI-built images
resource "harbor_project" "ducktape" {
  name   = "ducktape"
  public = false
}

# System-level robot account for CI push (GitHub Actions)
resource "harbor_robot_account" "ci" {
  name        = "ci"
  description = "CI/CD robot account — pushes images from GitHub Actions"
  level       = "system"

  permissions {
    kind      = "project"
    namespace = harbor_project.ducktape.name

    access {
      action   = "push"
      resource = "repository"
    }
    access {
      action   = "pull"
      resource = "repository"
    }
    access {
      action   = "read"
      resource = "artifact"
    }
    access {
      action   = "create"
      resource = "tag"
    }
  }
}

# Read-only robot account for imagePullSecrets in app namespaces.
# Distributed via Reflector from flux-system to consumer namespaces.
resource "harbor_robot_account" "pull" {
  name        = "pull"
  description = "Read-only robot for imagePullSecrets in app namespaces"
  level       = "system"

  permissions {
    kind      = "project"
    namespace = harbor_project.ducktape.name

    access {
      action   = "pull"
      resource = "repository"
    }
    access {
      action   = "read"
      resource = "artifact"
    }
  }
}

# Webhook token for the Flux harbor Receiver (Harbor → Flux ImageRepository)
resource "random_password" "harbor_webhook_token" {
  length  = 40
  special = false
}

# GitHub webhook token for the Flux github Receiver (GitHub push → Flux GitRepository)
resource "random_password" "github_webhook_token" {
  length  = 40
  special = false
}

resource "vault_kv_secret_v2" "harbor_ci_robot" {
  mount = "kv"
  name  = "harbor/ci-robot"

  data_json = jsonencode({
    username = harbor_robot_account.ci.full_name
    password = harbor_robot_account.ci.secret
  })
}

resource "vault_kv_secret_v2" "harbor_pull_robot" {
  mount = "kv"
  name  = "harbor/pull-robot"

  data_json = jsonencode({
    username = harbor_robot_account.pull.full_name
    password = harbor_robot_account.pull.secret
  })
}

resource "vault_kv_secret_v2" "harbor_webhook_token" {
  mount = "kv"
  name  = "harbor/webhook-token"

  data_json = jsonencode({
    token = random_password.harbor_webhook_token.result
  })

  lifecycle {
    # Don't rotate the token after initial creation — rotating it would require
    # reconfiguring the Harbor webhook notification and the Flux Receiver path.
    ignore_changes = [data_json]
  }
}

resource "vault_kv_secret_v2" "github_webhook_token" {
  mount = "kv"
  name  = "flux/github-webhook-token"

  data_json = jsonencode({
    token = random_password.github_webhook_token.result
  })

  lifecycle {
    # Don't rotate after initial creation — rotating requires reconfiguring the
    # GitHub webhook URL (path changes with the sha256 of the token).
    ignore_changes = [data_json]
  }
}
