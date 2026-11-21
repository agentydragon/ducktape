# Forgejo repo access for agentydragon-owned source mirrors.
#
# Adopts the existing `agentydragon/{ducktape,gaffer-private}` repos under Terraform
# and grants Haku read access. Provider wiring mirrors tf/gitops/forgejo-claude.
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
