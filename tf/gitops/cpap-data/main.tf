# Forgejo repo + service users for the CPAP EDF archive.
#
# Provisions a private `cpap-data/cpap-data` repo owned by a dedicated
# `cpap-data` service user (full read/write on its own repo, used by the
# nightly cpap-sync CronJob to commit+push files pulled from the ez Share SD
# card), plus read-only collaborators for `cpap-data-reader`, `claude`, and
# `haku`. Two Kubernetes Secrets carry the
# respective git credentials. Auth is HTTPS Basic against the public ingress
# (git.allegedly.works) — unlike augur-evidence's in-cluster URL, because the
# sync CronJob runs hostNetwork on wyrm2 where host DNS is simpler than
# cluster DNS, and the same URL then works for every consumer (CronJob,
# laptops, Claude Code web). Mirrors tf/gitops/augur-evidence otherwise.
#
# The Secrets land directly in the ducktape-owned `cpap-sync` namespace (no
# cross-repo reflection dance like budget -> augur); the read Secret is
# additionally reflected (emberstack) into `claude-sandbox` so Claude Code web
# sessions can read it through the connected Kubernetes MCP path.

data "kubernetes_secret" "forgejo_admin" {
  metadata {
    name      = "forgejo-admin-password"
    namespace = "forgejo"
  }
}

provider "forgejo" {
  host     = var.forgejo_url
  username = data.kubernetes_secret.forgejo_admin.data["username"]
  password = data.kubernetes_secret.forgejo_admin.data["password"]
}

resource "random_password" "writer" {
  length  = 48
  special = false
}

resource "random_password" "reader" {
  length  = 48
  special = false
}

resource "forgejo_user" "writer" {
  login                = "cpap-data"
  email                = "cpap-data@allegedly.works"
  password             = random_password.writer.result
  must_change_password = false
  visibility           = "private"
}

resource "forgejo_user" "reader" {
  login                = "cpap-data-reader"
  email                = "cpap-data-reader@allegedly.works"
  password             = random_password.reader.result
  must_change_password = false
  visibility           = "private"
}

resource "forgejo_repository" "data" {
  owner          = forgejo_user.writer.login
  name           = "cpap-data"
  description    = "ResMed CPAP EDF archive, synced nightly from the ez Share SD card by the cpap-sync CronJob. Do not edit by hand."
  private        = true
  default_branch = "main"
  # Create an initial commit so `main` exists for the sync job to clone/push
  # and readers to clone.
  auto_init = true
}

# Read-only access for Claude Code analysis sessions.
resource "forgejo_collaborator" "reader" {
  repository_id = forgejo_repository.data.id
  user          = forgejo_user.reader.login
  permission    = "read"
}

# Read-only access for the claude agent account (user provisioned by
# tf/gitops/forgejo-claude; until it exists this resource fails and the
# Terraform CR retries on its interval).
resource "forgejo_collaborator" "claude" {
  repository_id = forgejo_repository.data.id
  user          = "claude"
  permission    = "read"
}

# Read-only access for the haku agent account (user provisioned by
# tf/gitops/haku-state).
resource "forgejo_collaborator" "haku" {
  repository_id = forgejo_repository.data.id
  user          = "haku"
  permission    = "read"
}

# Write credentials for the sync CronJob, consumed in place in cpap-sync.
resource "kubernetes_secret" "cpap_data_git_write" {
  metadata {
    name      = "cpap-data-git-write"
    namespace = "cpap-sync"
  }

  data = {
    username = forgejo_user.writer.login
    password = random_password.writer.result
    repo_url = "https://git.allegedly.works/${forgejo_user.writer.login}/${forgejo_repository.data.name}.git"
  }
}

# Read credentials for Claude Code, reflected into claude-sandbox.
resource "kubernetes_secret" "cpap_data_git_read" {
  metadata {
    name      = "cpap-data-git-read"
    namespace = "cpap-sync"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "claude-sandbox"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "claude-sandbox"
    }
  }

  data = {
    username = forgejo_user.reader.login
    password = random_password.reader.result
    repo_url = "https://git.allegedly.works/${forgejo_user.writer.login}/${forgejo_repository.data.name}.git"
  }
}
