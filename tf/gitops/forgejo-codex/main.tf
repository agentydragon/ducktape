# Forgejo identity + repo access for the self-hosted codex agent (agent-box VM).
#
# Provisions a `codex` service user, attaches its SSH push key
# (agent-box-codex-forgejo), adopts the existing `agentydragon/{ducktape,
# gaffer-private}` repos under Terraform, grants codex write collaboration, and
# protects the default branches so codex must open PRs while agentydragon keeps
# direct push (push whitelist). Provider wiring mirrors tf/gitops/forgejo-claude.

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

resource "random_password" "codex" {
  length  = 48
  special = false
}

# The codex agent authenticates to Forgejo over SSH (key below), so the password
# is never delivered anywhere — it just satisfies the required field.
resource "forgejo_user" "codex" {
  login                = "codex"
  email                = "codex@allegedly.works"
  password             = random_password.codex.result
  must_change_password = false
  visibility           = "private"
}

# codex's git push key (private half lives on agent-box as the codex user's
# Forgejo identity, ssh_keys/agent-box-codex-forgejo). Inlined because the
# tofu-controller runs only this module path, so file() cannot read repo-root.
resource "forgejo_ssh_key" "codex" {
  user  = forgejo_user.codex.login
  key   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILbs0SByOeeZOjlAdR/Oi/ijQJ2j+STf7E1F5oCQxEF3"
  title = "agent-box-codex-forgejo"
}

# --- Repo adoption (existing repos; import, do not create) ---
# The human agentydragon account is created by OIDC login, not Terraform.
data "forgejo_user" "agentydragon" {
  login = "agentydragon"
}

# Field set matches the live repo settings (verified via the Forgejo API) so the
# import is diff-free: both repos are private with all-default feature toggles;
# only default_branch differs.
resource "forgejo_repository" "ducktape" {
  owner          = data.forgejo_user.agentydragon.login
  name           = "ducktape"
  private        = true
  default_branch = "devel"
}

import {
  to = forgejo_repository.ducktape
  id = "agentydragon/ducktape"
}

resource "forgejo_repository" "gaffer_private" {
  owner          = data.forgejo_user.agentydragon.login
  name           = "gaffer-private"
  private        = true
  default_branch = "main"
}

import {
  to = forgejo_repository.gaffer_private
  id = "agentydragon/gaffer-private"
}

# --- codex write collaboration ---
# Write (not read) so codex can push topic branches for PRs; branch protection
# below blocks direct pushes to the default branch.
resource "forgejo_collaborator" "codex_ducktape" {
  repository_id = forgejo_repository.ducktape.id
  user          = forgejo_user.codex.login
  permission    = "write"
}

resource "forgejo_collaborator" "codex_gaffer" {
  repository_id = forgejo_repository.gaffer_private.id
  user          = forgejo_user.codex.login
  permission    = "write"
}

# --- Branch protection: PR-but-not-push for everyone except agentydragon ---
# enable_push + push whitelist keeps agentydragon's direct pushes working while
# forcing codex (and any other collaborator) through PRs. Forgejo always rejects
# force-push on a protected branch, so no separate toggle is needed.
resource "forgejo_branch_protection" "ducktape_devel" {
  repository_id            = forgejo_repository.ducktape.id
  branch_name              = "devel"
  enable_push              = true
  enable_push_whitelist    = true
  push_whitelist_usernames = [data.forgejo_user.agentydragon.login]
}

# gaffer-private is currently empty (no `main` branch yet); the rule attaches by
# branch name and takes effect once the first push creates main.
resource "forgejo_branch_protection" "gaffer_main" {
  repository_id            = forgejo_repository.gaffer_private.id
  branch_name              = "main"
  enable_push              = true
  enable_push_whitelist    = true
  push_whitelist_usernames = [data.forgejo_user.agentydragon.login]
}
