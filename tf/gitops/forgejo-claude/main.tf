# Forgejo service account for Claude agent sessions.
#
# Provisions a `claude` service user that owns no repos of its own; private
# data repos grant it read-only collaboration where agents should be able to
# pull (e.g. gaffer-private tf/thrive-scrape adds it as reader on
# thrive-scrape). The HTTP Basic credentials land in the `forgejo` namespace;
# ESO copies them into `claude-sandbox`, where agent sessions fetch them on demand
# (announced by devinfra/claude/claude_hook/creds_banner.sh), and into
# `agents-infra` for forgejo-token-rotation. Provider wiring mirrors
# tf/gitops/augur-evidence.

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

resource "random_password" "claude" {
  length  = 48
  special = false
}

resource "forgejo_user" "claude" {
  login                = "claude"
  email                = "claude@allegedly.works"
  password             = random_password.claude.result
  must_change_password = false
  visibility           = "private"
}

# Read credentials for agent sessions, copied into claude-sandbox
# (cluster/cdk8s/claude_sandbox_secrets.py).
resource "kubernetes_secret" "claude_forgejo_credentials_source" {
  metadata {
    name      = "claude-forgejo-credentials"
    namespace = "forgejo"
  }

  data = {
    username     = forgejo_user.claude.login
    password     = random_password.claude.result
    url          = "https://git.allegedly.works"
    internal_url = "http://forgejo-http.forgejo:3000"
  }
}

# Source credential for forgejo-token-rotation, copied into agents-infra
# (cluster/cdk8s/forgejo/token_rotation.py). The rotator mints the API token
# that `tea` consumes, while this Terraform root remains the owner of the
# account password.
resource "kubernetes_secret" "claude_forgejo_token_mint_source" {
  metadata {
    name      = "forgejo-token-mint-claude"
    namespace = "forgejo"
  }

  data = {
    username     = forgejo_user.claude.login
    password     = random_password.claude.result
    url          = "https://git.allegedly.works"
    internal_url = var.forgejo_url
  }
}

# The claude-sandbox and agents-infra ExternalSecrets adopt the Secrets these
# addresses created there.
# CLEANUP(added 2026-09-28): Remove once the forgejo_claude state has forgotten
# both addresses. Never destroy the ESO-owned Secrets.
removed {
  from = kubernetes_secret.claude_forgejo_credentials
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret.claude_forgejo_token_mint
  lifecycle {
    destroy = false
  }
}
