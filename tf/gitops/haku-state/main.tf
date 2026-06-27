# Forgejo repo + service user for Haku, the personal background agent
# (see haku/PLAN.md).
#
# Provisions a private `haku/haku-state` repo owned by a dedicated `haku`
# service user (full read/write on its own repo; scan runs commit+push items,
# intake, steering, and log as this user), plus a read-only grant for the
# `claude` agent account. A Kubernetes Secret in the `haku-sandbox` namespace
# carries the git credentials, consumed by in-cluster scan runs. Mirrors
# tf/gitops/augur-evidence. The repo starts empty (auto_init only) — no seed.

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

resource "random_password" "haku" {
  length  = 48
  special = false
}

resource "forgejo_user" "haku" {
  login                = "haku"
  email                = "haku@allegedly.works"
  password             = random_password.haku.result
  must_change_password = false
  visibility           = "private"
}

resource "forgejo_repository" "state" {
  owner          = forgejo_user.haku.login
  name           = "haku-state"
  description    = "Haku's state: item queue, intake, steering, and log. See haku/PLAN.md."
  private        = true
  default_branch = "main"
  # Initial commit so `main` exists for branch protection. No content seed.
  auto_init = true
}

# Block force-pushes (and branch deletion) on main. Forgejo rejects force-push
# on any protected branch by default — there is no separate "force push" toggle
# — so protecting `main` is what disallows it. enable_push keeps Haku's ordinary
# commits working; without it, protecting the branch would require PRs, which v0
# doesn't want.
resource "forgejo_branch_protection" "state_main" {
  repository_id = forgejo_repository.state.id
  branch_name   = "main"
  enable_push   = true
}

# Read-only access for the claude agent account (user provisioned by
# tf/gitops/forgejo-claude; until it exists this resource fails and the
# Terraform CR retries on its interval).
resource "forgejo_collaborator" "claude" {
  repository_id = forgejo_repository.state.id
  user          = "claude"
  permission    = "read"
}

# Write credentials for the haku-state git consumers, delivered to:
#   - haku-sandbox: in-cluster scan runs / the self-hosted worker.
#   - haku-console: the console, which lives in its own trusted namespace (NOT
#     haku-sandbox) so it can hold secrets Haku may not read. The git creds are
#     not such a secret (Haku writes haku-state anyway), but the console needs
#     them there. See haku/PLAN.md → "The agent-authored console".
#   - flux-system: basic auth for the haku-state GitRepository, which the
#     haku-state-workloads Kustomization reconciles into haku-sandbox under a
#     constrained SA (cluster/k8s/haku/workloads). Read-only pull — Flux never
#     pushes; the haku user is just the only principal on the repo.
# The haku-sandbox/haku-console namespaces are each created by their own Flux
# kustomization (the wrapping forgejo/haku-state Kustomization dependsOn them);
# flux-system always exists. This resource retries until each namespace exists.
resource "kubernetes_secret" "haku_state_git_write" {
  for_each = toset(["haku-sandbox", "haku-console", "flux-system"])

  metadata {
    name      = "haku-state-git-write"
    namespace = each.value
  }

  data = {
    username = forgejo_user.haku.login
    password = random_password.haku.result
    repo_url = "http://forgejo-http.forgejo:3000/${forgejo_user.haku.login}/${forgejo_repository.state.name}.git"
  }
}

# imagePullSecret so Haku's UI Deployment (haku-sandbox) can pull the image its
# Forgejo CI builds (git.allegedly.works/haku/ui:<sha>). The package is private
# (haku's repo is private), so the kubelet needs auth. Pulls go over HTTPS via the
# public host (kubelet image pulls are node-level, not subject to the pod's
# mitmproxy egress). The CI workflow itself pushes with Forgejo Actions' built-in
# job token — no push cred needed here. See cluster/k8s/haku-ci + haku/PLAN.md.
resource "kubernetes_secret" "haku_forgejo_registry_pull" {
  metadata {
    name      = "haku-forgejo-registry-pull"
    namespace = "haku-sandbox"
  }

  type = "kubernetes.io/dockerconfigjson"

  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        "git.allegedly.works" = {
          username = forgejo_user.haku.login
          password = random_password.haku.result
          auth     = base64encode("${forgejo_user.haku.login}:${random_password.haku.result}")
        }
      }
    })
  }
}

# Registration token for the contained Forgejo Actions runner (cluster/k8s/haku-ci),
# which builds Haku's UI image from haku-state. The svalabs/forgejo provider has no
# runner-token resource, so fetch it from the repo's registration-token API as the
# repo-owning haku user (owner ⇒ repo admin) and deliver it to the haku-ci namespace
# as the Secret the runner registers with. GET returns the repo's *current* token
# (it doesn't rotate on read), so repeated applies don't churn the Secret.
# depends_on forces the read to apply-time, after the repo exists.
data "http" "haku_ci_registration_token" {
  url    = "${var.forgejo_url}/api/v1/repos/${forgejo_user.haku.login}/${forgejo_repository.state.name}/actions/runners/registration-token"
  method = "GET"
  request_headers = {
    Authorization = "Basic ${base64encode("${forgejo_user.haku.login}:${random_password.haku.result}")}"
    Accept        = "application/json"
  }
  depends_on = [forgejo_repository.state]
}

# The haku-ci namespace is created by its own Flux kustomization (cluster/k8s/haku-ci);
# this resource retries until it exists. Replaces the manual SOPS bootstrap token.
resource "kubernetes_secret" "haku_ci_runner_token" {
  metadata {
    name      = "haku-ci-runner-token"
    namespace = "haku-ci"
  }

  data = {
    token = jsondecode(data.http.haku_ci_registration_token.response_body).token
  }
}
