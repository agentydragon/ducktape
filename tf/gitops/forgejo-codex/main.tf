# Forgejo identity + repo access for the self-hosted Codex agent (agent-box VM).
#
# Provisions an `agent-box-codex` service user, attaches its SSH push key
# (agent-box-codex-forgejo), adopts the existing `agentydragon/{ducktape,
# gaffer-private}` repos under Terraform, and grants agent-box-codex write
# collaboration. Provider wiring mirrors tf/gitops/forgejo-claude.

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

resource "random_password" "agent_box_codex" {
  length  = 48
  special = false
}

# The agent-box Codex agent authenticates to Forgejo over SSH (key below), so
# the password is never delivered anywhere — it just satisfies the required field.
resource "forgejo_user" "agent_box_codex" {
  login                = "agent-box-codex"
  email                = "agent-box-codex@allegedly.works"
  password             = random_password.agent_box_codex.result
  must_change_password = false
  visibility           = "private"
}

# agent-box-codex's git push key (private half lives on agent-box as the codex
# user's Forgejo identity, ssh_keys/agent-box-codex-forgejo). Inlined because
# the tofu-controller runs only this module path, so file() cannot read repo-root.
resource "forgejo_ssh_key" "agent_box_codex" {
  user  = forgejo_user.agent_box_codex.login
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

# --- agent-box-codex write collaboration ---
# Write (not read) so agent-box-codex can push topic branches for PRs.
resource "forgejo_collaborator" "agent_box_codex_ducktape" {
  repository_id = forgejo_repository.ducktape.id
  user          = forgejo_user.agent_box_codex.login
  permission    = "write"
}

resource "forgejo_collaborator" "agent_box_codex_gaffer" {
  repository_id = forgejo_repository.gaffer_private.id
  user          = forgejo_user.agent_box_codex.login
  permission    = "write"
}

# --- Default branches intentionally unprotected ---
# Forgejo's protected-branch API can whitelist normal pushes, but it cannot
# whitelist force-push. Protected branches reject `git push --force` even when
# `agentydragon` is in `push_whitelist_usernames`, so the Forgejo mirrors leave
# `ducktape/devel` and `gaffer-private/main` unprotected.

moved {
  from = random_password.codex
  to   = random_password.agent_box_codex
}

moved {
  from = forgejo_user.codex
  to   = forgejo_user.agent_box_codex
}

moved {
  from = forgejo_ssh_key.codex
  to   = forgejo_ssh_key.agent_box_codex
}

moved {
  from = forgejo_collaborator.codex_ducktape
  to   = forgejo_collaborator.agent_box_codex_ducktape
}

moved {
  from = forgejo_collaborator.codex_gaffer
  to   = forgejo_collaborator.agent_box_codex_gaffer
}
