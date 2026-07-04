# Forgejo service account for Claude agent sessions.
#
# Provisions a `claude` service user that owns no repos of its own; private
# data repos grant it read-only collaboration where agents should be able to
# pull (e.g. gaffer-private tf/thrive-scrape adds it as reader on
# thrive-scrape). The HTTP Basic credentials land in the `claude-sandbox`
# namespace, where agent sessions fetch them on demand — announced by
# devinfra/claude/claude_hook/creds_banner.sh. Provider wiring mirrors
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

# Read credentials for agent sessions, directly in claude-sandbox (the
# namespace is ducktape-owned, so no reflection hop is needed).
resource "kubernetes_secret" "claude_forgejo_credentials" {
  metadata {
    name      = "claude-forgejo-credentials"
    namespace = "claude-sandbox"
  }

  data = {
    username     = forgejo_user.claude.login
    password     = random_password.claude.result
    url          = "https://git.allegedly.works"
    internal_url = "http://forgejo-http.forgejo:3000"
  }
}

# Source credential for forgejo-token-rotation. The rotator mints the API token
# that `tea` consumes, while this Terraform root remains the owner of the
# account password.
resource "kubernetes_secret" "claude_forgejo_token_mint" {
  metadata {
    name      = "forgejo-token-mint-claude"
    namespace = "agents-infra"
  }

  data = {
    username     = forgejo_user.claude.login
    password     = random_password.claude.result
    url          = "https://git.allegedly.works"
    internal_url = var.forgejo_url
  }
}
