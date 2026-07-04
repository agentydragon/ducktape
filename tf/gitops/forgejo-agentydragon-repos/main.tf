# Forgejo repo access for agentydragon-owned source mirrors.
#
# Provisions `agent-box-codex` and `agent-box-zai` service users, attaches their
# SSH push keys (agent-box-{codex,zai}-forgejo), adopts the existing
# `agentydragon/{ducktape,gaffer-private}` repos under Terraform, grants each
# agent-box user write collaboration, and grants Haku read access to those same
# source mirrors. Provider wiring mirrors tf/gitops/forgejo-claude.

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

data "forgejo_user" "haku" {
  login = "haku"
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

# --- agent-box-zai service user + write collaboration ---
# Same shape as agent-box-codex above: the zai agent authenticates to Forgejo
# over SSH (key below), so the password is never delivered anywhere.
resource "random_password" "agent_box_zai" {
  length  = 48
  special = false
}

resource "forgejo_user" "agent_box_zai" {
  login                = "agent-box-zai"
  email                = "agent-box-zai@allegedly.works"
  password             = random_password.agent_box_zai.result
  must_change_password = false
  visibility           = "private"
}

# agent-box-zai's git push key (private half lives on agent-box as the zai user's
# Forgejo identity, ssh_keys/agent-box-zai-forgejo). Inlined because the
# tofu-controller runs only this module path, so file() cannot read repo-root.
resource "forgejo_ssh_key" "agent_box_zai" {
  user  = forgejo_user.agent_box_zai.login
  key   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBKUgk4jTaYnkK5hq1c5htDiDsIlmHzkUoVq2RqDfKd4 agent-box-zai-forgejo"
  title = "agent-box-zai-forgejo"
}

# Write (not read) so agent-box-zai can push topic branches for PRs.
resource "forgejo_collaborator" "agent_box_zai_ducktape" {
  repository_id = forgejo_repository.ducktape.id
  user          = forgejo_user.agent_box_zai.login
  permission    = "write"
}

resource "forgejo_collaborator" "agent_box_zai_gaffer" {
  repository_id = forgejo_repository.gaffer_private.id
  user          = forgejo_user.agent_box_zai.login
  permission    = "write"
}

# --- haku read collaboration ---
# Read-only access lets Haku clone these in-cluster source mirrors with its
# existing haku-state-git-write basic-auth credential.
resource "forgejo_collaborator" "haku_ducktape_read" {
  repository_id = forgejo_repository.ducktape.id
  user          = data.forgejo_user.haku.login
  permission    = "read"
}

resource "forgejo_collaborator" "haku_gaffer_read" {
  repository_id = forgejo_repository.gaffer_private.id
  user          = data.forgejo_user.haku.login
  permission    = "read"
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
