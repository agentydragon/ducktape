# Forgejo repo + service user for Rai's personal financial-leash agent (an OpenAI/Codex
# CLI workload, run outside the cluster from his own machine — see haku-state
# memory/operator-model/operator.md and surface/plans/provider-trust-threat-model.md for the
# trust reasoning behind giving OpenAI finance data).
#
# Provisions a private `finance-agent/finance-agent` repo owned by a dedicated `finance-agent`
# service user (full read/write on its own repo), plus a write collaborator grant for `haku`
# (operator, 2026-09-30) so Haku can help maintain it. No `claude` grant — not asked for.
#
# No data lives in this repo — the agent queries Plaid live rather than committing transaction
# data to git history, which can't be un-committed later. The repo holds code/config only. Mirrors
# tf/gitops/budget-ledger and tf/gitops/augur-evidence, minus their CI/sidecar consumers (this
# repo has no in-cluster consumer at all).
#
# The Secret lands in the `forgejo` namespace, same as every other module here. Rai reads it
# directly with his own kubectl access (`kubectl -n forgejo get secret finance-agent-git-creds -o
# jsonpath=...`) to set up his local clone / Codex CLI — no ESO copy anywhere, since nothing
# in-cluster consumes it.

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

resource "random_password" "finance_agent" {
  length  = 48
  special = false
}

resource "forgejo_user" "finance_agent" {
  login                = "finance-agent"
  email                = "finance-agent@allegedly.works"
  password             = random_password.finance_agent.result
  must_change_password = false
  visibility           = "private"
}

resource "forgejo_repository" "finance_agent" {
  owner          = forgejo_user.finance_agent.login
  name           = "finance-agent"
  description    = "Rai's personal financial-leash agent workspace (code/config only, no transaction data committed)."
  private        = true
  default_branch = "main"
  # Initial commit so `main` exists to clone/push against. No content seed.
  auto_init = true
}

# Write access for the haku agent account (user provisioned by tf/gitops/haku-state), so Haku
# can help build/maintain this repo directly (operator, 2026-09-30).
resource "forgejo_collaborator" "haku" {
  repository_id = forgejo_repository.finance_agent.id
  user          = "haku"
  permission    = "write"
}

# Git credentials for Rai's own local clone. Not copied anywhere in-cluster — retrieved
# directly via kubectl, same access he already has to every other secret here.
#
# repo_url is the PUBLIC host, unlike every other module's `forgejo-http.forgejo:3000` — those
# credentials are consumed by in-cluster pods; this one is consumed by Rai's own laptop, which
# can't resolve the internal cluster-DNS name.
resource "kubernetes_secret" "finance_agent_git_creds_source" {
  metadata {
    name      = "finance-agent-git-creds"
    namespace = "forgejo"
  }

  data = {
    username = forgejo_user.finance_agent.login
    password = random_password.finance_agent.result
    repo_url = "https://git.allegedly.works/${forgejo_user.finance_agent.login}/${forgejo_repository.finance_agent.name}.git"
  }
}
