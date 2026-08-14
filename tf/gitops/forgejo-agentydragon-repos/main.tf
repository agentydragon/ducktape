# Forgejo repo access for agentydragon-owned source mirrors.
#
# Provisions the AI-agent service users (`agent-box-codex`,
# `codex-pod`, …), attaches their SSH push keys, adopts the existing
# `agentydragon/{ducktape,gaffer-private}` repos under Terraform, and grants Haku
# read access. Provider wiring mirrors tf/gitops/forgejo-claude.
#
# CONVENTION — agents are READ-only and open PRs via AGit. No agent gets write on
# the upstream repos, so none can advance `devel`/`main` (branch protection can't
# help here: Forgejo has no force-push allowlist, and the GitHub→Forgejo mirror
# force-pushes those branches, so they must stay unprotected — see the note at the
# bottom). Instead each agent proposes changes with Forgejo's AGit flow —
# `git push origin HEAD:refs/for/<branch> -o topic=<t>` over its SSH key — which
# opens/updates a PR with only read access, no fork and no API token (the SSH key
# they already have is the only credential). Every future on-box AI provider
# follows this pattern.

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

locals {
  forgejo_token_mint_users = {
    agent-box-codex = {
      username = forgejo_user.agent_box_codex.login
      password = random_password.agent_box_codex.result
    }
    codex-pod = {
      username = forgejo_user.codex_pod.login
      password = random_password.codex_pod.result
    }
  }
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

# --- agent-box-codex read collaboration (fork model) ---
resource "forgejo_collaborator" "agent_box_codex_ducktape" {
  repository_id = forgejo_repository.ducktape.id
  user          = forgejo_user.agent_box_codex.login
  permission    = "read"
}

resource "forgejo_collaborator" "agent_box_codex_gaffer" {
  repository_id = forgejo_repository.gaffer_private.id
  user          = forgejo_user.agent_box_codex.login
  permission    = "read"
}

# --- codex-pod (in-cluster Nix-image codex agent) ---
# Same shape as agent-box-codex: read-only + fork model, authenticating over SSH.
resource "random_password" "codex_pod" {
  length  = 48
  special = false
}

# Authenticates over SSH (key below); the password just satisfies the required field.
resource "forgejo_user" "codex_pod" {
  login                = "codex-pod"
  email                = "codex-pod@allegedly.works"
  password             = random_password.codex_pod.result
  must_change_password = false
  visibility           = "private"
}

# codex-pod's git push key — the same id_ed25519 planted into the pod from the
# codex-bootstrap-identity Secret (cluster/k8s/agents/codex-pod). Inlined because
# the tofu-controller runs only this module path, so file() cannot read repo-root.
resource "forgejo_ssh_key" "codex_pod" {
  user  = forgejo_user.codex_pod.login
  key   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHc/XFJXzEigh9Y7K70CcQHCpuQLDvAK5UsAFU/aPaFE codex-pod"
  title = "codex-pod"
}

resource "forgejo_collaborator" "codex_pod_ducktape" {
  repository_id = forgejo_repository.ducktape.id
  user          = forgejo_user.codex_pod.login
  permission    = "read"
}

# Source credentials for forgejo-token-rotation. The passwords still originate
# here; the rotator converts them into API tokens and tea configs for consumers.
resource "kubernetes_secret" "forgejo_token_mint" {
  for_each = local.forgejo_token_mint_users

  metadata {
    name      = "forgejo-token-mint-${each.key}"
    namespace = "agents-infra"
  }

  data = {
    username     = each.value.username
    password     = each.value.password
    url          = "https://git.allegedly.works"
    internal_url = var.forgejo_url
  }
}

# --- haku read collaboration ---
# Read-only access lets Haku clone these in-cluster source mirrors with its
# existing haku-forgejo-git basic-auth credential.
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
