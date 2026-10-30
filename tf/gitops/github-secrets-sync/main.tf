# GitHub Actions Secrets Sync
#
# Syncs GitHub Actions credentials from Kubernetes. Managed by the Crossplane
# OpenTofu provider.
#
# SOPS_AGE_KEY lets privileged CI decrypt CI-only credentials from git.
# BUILDBUDDY_API_KEY is synchronized separately so build/test jobs can receive
# only the BuildBuddy capability instead of the broader CI decryption identity.
# The Forgejo registry credentials are synchronized to both repositories from the
# same Kubernetes Secret used by the Forgejo tenant and its pull consumers.
#
# Auth: fine-grained GitHub PAT stored as K8s Secret (SOPS-deployed by Flux).
# Required PAT permissions are documented in README.md.

provider "github" {
  owner = "agentydragon"
  token = data.kubernetes_secret.github_secrets_sync_pat.data["token"]
}

# --- Data Sources ---

data "kubernetes_secret" "github_secrets_sync_pat" {
  metadata {
    name      = "github-secrets-sync-pat"
    namespace = "flux-system"
  }
}

# Reuse Flux's SOPS-managed App identity; do not maintain another key copy.
data "kubernetes_secret" "automation_app" {
  metadata {
    name      = "ducktape-automation-github-app"
    namespace = "flux-system"
  }
}

data "kubernetes_secret" "ci_age_key" {
  metadata {
    name      = "ci-age-key"
    namespace = "flux-system"
  }
}

data "kubernetes_secret" "buildbuddy_api_key" {
  metadata {
    name      = "buildbuddy-api-key"
    namespace = "flux-system"
  }
}

data "kubernetes_secret" "forgejo_images_creds" {
  metadata {
    name      = "forgejo-images-creds"
    namespace = "forgejo-images"
  }
}

data "kubernetes_secret" "attic_ci_writer" {
  metadata {
    name      = "attic-ducktape-ci-writer"
    namespace = "nix-cache"
  }
}

data "kubernetes_secret" "attic_ci_reader" {
  metadata {
    name      = "attic-ducktape-ci-reader"
    namespace = "nix-cache"
  }
}

data "kubernetes_secret" "pr_visuals_s3_credentials" {
  metadata {
    name      = "pr-visuals-s3-credentials"
    namespace = "flux-system"
  }
}

data "github_user" "agentydragon" {
  username = "agentydragon"
}

# --- GitHub Actions Secrets ---

resource "github_actions_secret" "automation_app_private_key" {
  repository      = "ducktape"
  secret_name     = "DUCKTAPE_AUTOMATION_PRIVATE_KEY"
  plaintext_value = data.kubernetes_secret.automation_app.data["githubAppPrivateKey"]
}

resource "github_actions_secret" "sops_age_key" {
  repository      = "ducktape"
  secret_name     = "SOPS_AGE_KEY"
  plaintext_value = data.kubernetes_secret.ci_age_key.data["age-key"]
}

resource "github_actions_secret" "sops_age_key_gaffer_private" {
  repository      = "gaffer-private"
  secret_name     = "SOPS_AGE_KEY"
  plaintext_value = data.kubernetes_secret.ci_age_key.data["age-key"]
}

resource "github_actions_secret" "forgejo_images_username" {
  repository      = "ducktape"
  secret_name     = "FORGEJO_IMAGES_USERNAME"
  plaintext_value = data.kubernetes_secret.forgejo_images_creds.data["username"]
}

resource "github_actions_secret" "forgejo_images_password" {
  repository      = "ducktape"
  secret_name     = "FORGEJO_IMAGES_PASSWORD"
  plaintext_value = data.kubernetes_secret.forgejo_images_creds.data["password"]
}

resource "github_actions_secret" "forgejo_images_username_gaffer_private" {
  repository      = "gaffer-private"
  secret_name     = "FORGEJO_IMAGES_USERNAME"
  plaintext_value = data.kubernetes_secret.forgejo_images_creds.data["username"]
}

resource "github_actions_secret" "forgejo_images_password_gaffer_private" {
  repository      = "gaffer-private"
  secret_name     = "FORGEJO_IMAGES_PASSWORD"
  plaintext_value = data.kubernetes_secret.forgejo_images_creds.data["password"]
}

resource "github_actions_secret" "attic_token" {
  repository      = "ducktape"
  secret_name     = "ATTIC_TOKEN"
  plaintext_value = data.kubernetes_secret.attic_ci_writer.data["attic_token"]
}

resource "github_actions_secret" "attic_read_token" {
  repository      = "ducktape"
  secret_name     = "ATTIC_READ_TOKEN"
  plaintext_value = data.kubernetes_secret.attic_ci_reader.data["attic_token"]
}

resource "github_actions_secret" "buildbuddy_api_key" {
  repository      = "ducktape"
  secret_name     = "BUILDBUDDY_API_KEY"
  plaintext_value = data.kubernetes_secret.buildbuddy_api_key.data["api-key"]
}

# Fork pull requests cannot receive repository Actions secrets, even after a
# maintainer clicks GitHub's "Approve and run workflows" button. This
# environment is used by the base-branch-owned pull_request_target workflow:
# non-agent fork PR heads wait for an explicit review before any PR code runs.
# The Terraform resource label remains stable to avoid an unnecessary state
# address migration; the user-facing GitHub environment is `fork-ci-review`.
resource "github_repository_environment" "trusted_pr_ci" {
  repository          = "ducktape"
  environment         = "fork-ci-review"
  can_admins_bypass   = false
  prevent_self_review = true

  reviewers {
    users = [data.github_user.agentydragon.id]
  }
}

removed {
  from = github_actions_environment_secret.trusted_pr_ci_sops_age_key
  lifecycle {
    destroy = true
  }
}

resource "github_actions_secret" "pr_visuals_access_key" {
  repository      = "ducktape"
  secret_name     = "PR_VISUALS_ACCESS_KEY"
  plaintext_value = data.kubernetes_secret.pr_visuals_s3_credentials.data["accessKey"]
}

resource "github_actions_secret" "pr_visuals_secret_key" {
  repository      = "ducktape"
  secret_name     = "PR_VISUALS_SECRET_KEY"
  plaintext_value = data.kubernetes_secret.pr_visuals_s3_credentials.data["secretKey"]
}

# --- GitHub Actions Variables ---

# The variable predates Terraform ownership. Import it so the OpenTofu provider adopts
# the existing GitHub Actions variable instead of trying to create a duplicate.
import {
  to = github_actions_variable.props_registry_url
  id = "ducktape:PROPS_REGISTRY_URL"
}

# Where props CI pushes agent images: the standalone props registry proxy, which
# records agent definitions and forwards to Forgejo's registry. CI authenticates
# as the evaluator Postgres role. Props is disabled; provision its GitHub Actions
# credentials from the cluster before re-enabling the publish job.
resource "github_actions_variable" "props_registry_url" {
  repository    = "ducktape"
  variable_name = "PROPS_REGISTRY_URL"
  value         = "props-registry.allegedly.works"
}

# Data sources for harbor_ci_robot, attic_push_token, and
# vm_images_s3_credentials were removed (no `removed` block needed for data
# sources — just delete). The GitHub Actions secrets VM_IMAGES_S3_* are no
# longer needed after the qcow2 publisher moved in-cluster. The in-cluster
# vm-images publisher has since been retired; active VM images publish as OCI.
# Removing the resource blocks here destroys the GitHub secrets on next apply.
