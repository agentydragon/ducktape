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
#   - haku-sandbox: in-cluster scan runs / the self-hosted worker + the haku-ui
#     backend (operator clicks/feedback → Forgejo writes).
#   - flux-system: basic auth for the haku-state GitRepository, which the
#     haku-state-workloads Kustomization reconciles into haku-sandbox under a
#     constrained SA (cluster/k8s/haku/workloads). Read-only pull — Flux never
#     pushes; the haku user is just the only principal on the repo.
# (The console no longer consumes this: it became a bare trusted shell with no
# haku-state write path — feedback/trace moved into haku-ui.)
# The haku-sandbox namespace is created by its own Flux kustomization (the wrapping
# forgejo/haku-state Kustomization dependsOn it); flux-system always exists. This
# resource retries until each namespace exists.
resource "kubernetes_secret" "haku_state_git_write" {
  for_each = toset(["haku-sandbox", "flux-system"])

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

# dockerconfigjson for the private git.allegedly.works/haku/ui package, in haku-sandbox:
#   - the imagePullSecret Haku's UI Deployment uses to pull the image its Forgejo CI builds
#     (kubelet pulls over HTTPS via the public host — node-level, not subject to the pod's
#     mitmproxy egress), and
#   - the auth Haku's own ImageRepository uses to scan the registry for new tags (the image
#     automation is reconciled into haku-sandbox; see haku/state_template/k8s/haku-ui-image-automation).
# The CI push credential is a repo Action secret (below), NOT this pull secret.
# See cluster/k8s/haku-ci + haku/PLAN.md.
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

# Registry push credential for the build-ui Forgejo Actions workflow, delivered as
# a repo Action secret (`${{ secrets.REGISTRY_PUSH_TOKEN }}`).
#
# Why this is needed: Forgejo's auto-generated Actions token (github.token) CANNOT
# push container packages — the workflow `permissions: { packages: write }` block is
# not honored yet (the granular-permissions feature is forgejo/forgejo#3571, still
# open, targeted for the Forgejo 16 dev cycle). On Forgejo 15 a real credential is
# the only way. We use the haku owner's own credential (haku owns the haku/ui
# package; verified: haku basic auth -> HTTP 202 on a blob-upload handshake = push
# allowed). The workflow logs in as `${{ github.repository_owner }}` (= haku).
#
# Least-privilege note: this is haku's full credential rather than a scoped
# write:package token — the svalabs/forgejo provider has no token-minting resource,
# and the CI already runs as haku (it clones haku-state and builds haku's image), so
# this isn't a new trust boundary. If a tighter scope is wanted later, mint a
# write:package token via a small Job (cf. authentik-jwt-rotation) and swap it in.
resource "forgejo_repository_action_secret" "registry_push" {
  repository_id = forgejo_repository.state.id
  name          = "REGISTRY_PUSH_TOKEN"
  data          = random_password.haku.result
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

# Webhook token shared between the Forgejo package webhook and the Flux generic
# Receiver `haku-ui-forgejo` (cluster/k8s/haku/ui-image-automation). The Receiver's
# webhook path is sha256(token + receiver-name + namespace), so the token must match
# on both ends. Don't rotate after creation — the path would change and orphan the
# Forgejo webhook URL.
resource "random_password" "forgejo_webhook_token" {
  length  = 40
  special = false
}

resource "kubernetes_secret" "forgejo_webhook_token" {
  metadata {
    name      = "forgejo-webhook-token"
    namespace = "flux-system"
  }

  data = {
    token = random_password.forgejo_webhook_token.result
  }

  lifecycle {
    ignore_changes = [data]
  }
}

# Fire a webhook on container-package publish (the CI image push) so Flux reconciles the
# haku-ui ImageRepository immediately rather than on its 5m poll. The generic receiver doesn't
# validate a signature, so the unguessable sha256(token) path is the secret — no `secret` in
# the webhook config.
#
# Targets the *public* Flux webhook host (`flux-webhook.allegedly.works`, the same HTTPRoute
# GitHub delivers to), NOT the in-cluster ClusterIP. Why: Forgejo blocks webhook delivery to
# private/loopback addresses by default (empty `[webhook] ALLOWED_HOST_LIST`, SSRF protection),
# so the original internal `http://webhook-receiver.flux-system/...` URL fired but was silently
# dropped — pickup stayed on the 5m poll (verified 2026-06-29: hook `updated_at` bumped on push,
# receiver logged zero hits). A public host is `external` → allowed by default, no Forgejo config
# change. The in-cluster→public hairpin is already exercised by Forgejo's OIDC to
# auth.allegedly.works; verified the public path triggers an off-cycle scan (HTTP 200 + receiver
# hit + immediate ImageRepository scan).
#
# ALSO REQUIRED: the webhook only fires while the `haku/ui` package is LINKED to this repo. It was
# created owner-scoped + unlinked (`repository: null`), so the repo-scoped `package` event never
# fired. Linked out-of-band via `POST /api/v1/packages/haku/container/ui/-/link/haku-state` (→201,
# durable across pushes). The OCI `org.opencontainers.image.source` label (haku-state ui/Dockerfile)
# is the would-be auto-linker but Forgejo 15 didn't honor it. Not codified here: the svalabs/forgejo
# provider has no package-link resource, and the package doesn't exist until the first CI push.
# TODO(link-as-code): a `data "http"` POST to the link endpoint (auth pattern as
#   `haku_ci_registration_token`) would self-heal it (404 pre-first-push → 201 after); idempotent.
resource "forgejo_repository_webhook" "haku_ui_image" {
  repository_id = forgejo_repository.state.id
  type          = "forgejo"
  active        = true
  events        = ["package"]
  config = {
    content_type = "json"
    url          = "https://flux-webhook.allegedly.works/hook/${sha256(join("", [random_password.forgejo_webhook_token.result, "haku-ui-forgejo", "flux-system"]))}"
  }
}
