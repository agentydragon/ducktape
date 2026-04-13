# Harbor CI infrastructure
#
# CLEANUP(2026-04-06): All ducktape project images fully migrated to GHCR.
# The CI robot account is unused. The pull robot is needed only by props
# (agent images in Harbor's `props` project). The harbor webhook token and
# receiver have been removed (all ImageRepositories track GHCR now).
# Once props also migrates off Harbor, suspend this Terraform resource and
# orphan with `removed` blocks.
#
# Creates:
#   - ducktape project (private, single project for all CI-pushed images)
#   - ci robot account with push+pull on the ducktape project (CI push) — UNUSED
#   - pull robot account with read-only access — only props still needs this
#   - harbor webhook token — UNUSED (receiver removed, kept for state compat)
#   - github webhook token for the Flux github Receiver
#   - github webhook registration for Flux Receiver
#
# Stores all credentials as K8s Secrets in flux-system (direct, no Vault).

data "kubernetes_secret" "harbor_admin_password" {
  metadata {
    name      = "harbor-admin-initial"
    namespace = "harbor"
  }
}

data "kubernetes_secret" "github_secrets_sync_pat" {
  metadata {
    name      = "github-secrets-sync-pat"
    namespace = "flux-system"
  }
}

provider "harbor" {
  url      = var.harbor_url
  username = "admin"
  password = data.kubernetes_secret.harbor_admin_password.data["HARBOR_ADMIN_PASSWORD"]
}

provider "github" {
  owner = "agentydragon"
  token = data.kubernetes_secret.github_secrets_sync_pat.data["token"]
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

# Desired CI robot credentials (SOPS-encrypted, Flux-deployed k8s Secret).
# Source of truth: secrets/ci/harbor-ci-robot.sops.yaml (for ci_env.sh).
# Mirrored here as a k8s Secret so tofu-controller can read it.
data "kubernetes_secret" "harbor_ci_robot_desired" {
  metadata {
    name      = "harbor-ci-robot-desired"
    namespace = "flux-system"
  }
}

# System-level robot account for CI push (GitHub Actions + BuildBuddy)
resource "harbor_robot_account" "ci" {
  name        = "ci"
  description = "CI/CD robot account — pushes images from GitHub Actions and BuildBuddy"
  level       = "system"
  secret      = data.kubernetes_secret.harbor_ci_robot_desired.data["password"]

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

# --- K8s Secrets (direct, replacing Vault + ESO) ---

resource "kubernetes_secret" "harbor_ci_robot" {
  metadata {
    name      = "harbor-ci-robot"
    namespace = "flux-system"
  }

  data = {
    username = harbor_robot_account.ci.full_name
    password = harbor_robot_account.ci.secret
  }
}

resource "kubernetes_secret" "harbor_pull_robot" {
  metadata {
    name      = "harbor-pull-robot"
    namespace = "flux-system"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "props"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "props"
    }
  }

  type = "kubernetes.io/dockerconfigjson"

  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        "registry.allegedly.works" = {
          username = harbor_robot_account.pull.full_name
          password = harbor_robot_account.pull.secret
          auth     = base64encode("${harbor_robot_account.pull.full_name}:${harbor_robot_account.pull.secret}")
        }
      }
    })
  }
}

resource "kubernetes_secret" "harbor_webhook_token" {
  metadata {
    name      = "harbor-webhook-token"
    namespace = "flux-system"
  }

  data = {
    token = random_password.harbor_webhook_token.result
  }

  lifecycle {
    # Don't rotate the token after initial creation — rotating it would require
    # reconfiguring the Harbor webhook notification and the Flux Receiver path.
    ignore_changes = [data]
  }
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
    # Don't rotate after initial creation — rotating requires reconfiguring the
    # GitHub webhook URL (path changes with the sha256 of the token).
    ignore_changes = [data]
  }
}

# --- GitHub webhook for Flux Receiver ---

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
