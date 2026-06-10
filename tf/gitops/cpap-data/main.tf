# Forgejo repo + service users for the CPAP EDF archive.
#
# Provisions a private `cpap-data/cpap-data` repo owned by a dedicated
# `cpap-data` service user (full read/write on its own repo, used by the
# nightly cpap-sync CronJob to commit+push files pulled from the ez Share SD
# card), plus a read-only `cpap-data-reader` collaborator (used by Claude Code
# to clone the archive for analysis). Two Kubernetes Secrets carry the
# respective git credentials. Auth is HTTPS Basic over the in-cluster Forgejo
# service (no SSH endpoint needed). Mirrors tf/gitops/augur-evidence.
#
# The Secrets land directly in the ducktape-owned `cpap-sync` namespace (no
# cross-repo reflection dance like budget -> augur); the read Secret is
# additionally reflected (emberstack) into `claude-sandbox` so Claude Code web
# sessions can read it via the kubectl-local MCP.

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

# Write credentials for the sync CronJob, consumed in place in cpap-sync.
resource "kubernetes_secret" "cpap_data_git_write" {
  metadata {
    name      = "cpap-data-git-write"
    namespace = "cpap-sync"
  }

  data = {
    username = forgejo_user.writer.login
    password = random_password.writer.result
    repo_url = "http://forgejo-http.forgejo:3000/${forgejo_user.writer.login}/${forgejo_repository.data.name}.git"
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
    repo_url = "http://forgejo-http.forgejo:3000/${forgejo_user.writer.login}/${forgejo_repository.data.name}.git"
  }
}
