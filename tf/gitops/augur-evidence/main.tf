# Forgejo repo + service users for augur's public exogenous evidence.
#
# Provisions a private `augur-evidence/augur-evidence` repo owned by a dedicated
# `augur-evidence` service user (full read/write on its own repo, used by the
# daily git scraper to commit+push refreshed evidence), plus a read-only
# `augur-evidence-reader` collaborator (used by the git-sync sidecar on the augur
# Deployment to pull the current evidence set). Two Kubernetes Secrets carry the
# respective git credentials. Auth is HTTPS Basic over the in-cluster Forgejo
# service (no SSH endpoint needed). Mirrors tf/gitops/budget-ledger.
#
# The Secrets land in the `forgejo` namespace. The parked unit copies them into
# the ducktape-owned `budget` namespace through ESO
# (cluster/cdk8s/parked/augur_evidence.py), and Reflector mirrors those copies
# into `augur` -- the augur namespace is reconciled from gaffer-private, so
# copying there directly would need a cross-repo object. budget-ledger delivers
# its git creds the same way.

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
  login                = "augur-evidence"
  email                = "augur-evidence@allegedly.works"
  password             = random_password.writer.result
  must_change_password = false
  visibility           = "private"
}

resource "forgejo_user" "reader" {
  login                = "augur-evidence-reader"
  email                = "augur-evidence-reader@allegedly.works"
  password             = random_password.reader.result
  must_change_password = false
  visibility           = "private"
}

resource "forgejo_repository" "evidence" {
  owner          = forgejo_user.writer.login
  name           = "augur-evidence"
  description    = "Raw public exogenous evidence (FRED/Yahoo/Zillow), scraped daily by the augur evidence CronJob. Do not edit by hand."
  private        = true
  default_branch = "main"
  # Create an initial commit so `main` exists for the scraper to clone/push and
  # the git-sync sidecar to pull.
  auto_init = true
}

# Read-only access for the git-sync sidecar.
resource "forgejo_collaborator" "reader" {
  repository_id = forgejo_repository.evidence.id
  user          = forgejo_user.reader.login
  permission    = "read"
}

# Read-only access for the claude agent account (user provisioned by
# tf/gitops/forgejo-claude; until it exists this resource fails and the
# Terraform CR retries on its interval).
resource "forgejo_collaborator" "claude" {
  repository_id = forgejo_repository.evidence.id
  user          = "claude"
  permission    = "read"
}

# Write credentials for the scraper CronJob, which runs alongside the augur app.
resource "kubernetes_secret" "augur_evidence_git_write_source" {
  metadata {
    name      = "augur-evidence-git-write"
    namespace = "forgejo"
  }

  data = {
    username = forgejo_user.writer.login
    password = random_password.writer.result
    repo_url = "http://forgejo-http.forgejo:3000/${forgejo_user.writer.login}/${forgejo_repository.evidence.name}.git"
  }
}

# Read credentials for the git-sync sidecar.
resource "kubernetes_secret" "augur_evidence_git_read_source" {
  metadata {
    name      = "augur-evidence-git-read"
    namespace = "forgejo"
  }

  data = {
    username = forgejo_user.reader.login
    password = random_password.reader.result
    repo_url = "http://forgejo-http.forgejo:3000/${forgejo_user.writer.login}/${forgejo_repository.evidence.name}.git"
  }
}

# The budget ExternalSecrets adopt the Secrets these addresses created there.
# CLEANUP(added 2026-09-28): Remove once the augur_evidence state has forgotten
# both addresses, which happens on the first apply after the unit is revived.
# Never destroy the ESO-owned Secrets.
removed {
  from = kubernetes_secret.augur_evidence_git_write
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret.augur_evidence_git_read
  lifecycle {
    destroy = false
  }
}
