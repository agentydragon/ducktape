data "kubernetes_secret_v1" "bootstrap_token" {
  metadata {
    name      = "gitlab-bootstrap-token"
    namespace = "gitlab-spike"
  }
}

data "kubernetes_secret_v1" "oidc" {
  metadata {
    name      = "gitlab-spike-oidc"
    namespace = "gitlab-spike"
  }
}

data "kubernetes_secret_v1" "forgejo_admin" {
  metadata {
    name      = "forgejo-admin-password"
    namespace = "forgejo"
  }
}

provider "forgejo" {
  host     = var.forgejo_url
  username = data.kubernetes_secret_v1.forgejo_admin.data["username"]
  password = data.kubernetes_secret_v1.forgejo_admin.data["password"]
}

provider "gitlab" {
  base_url = "http://gitlab-spike-webservice-default.gitlab-spike.svc:8181/api/v4/"
  token    = data.kubernetes_secret_v1.bootstrap_token.data["token"]
}

resource "random_password" "agentydragon" {
  length  = 48
  special = false
}

resource "gitlab_group" "spike" {
  name             = "Spike"
  path             = "spike"
  description      = "Disposable GitLab replacement spike"
  visibility_level = "private"
}

resource "gitlab_user" "agentydragon" {
  name              = data.kubernetes_secret_v1.oidc.data["name"]
  username          = data.kubernetes_secret_v1.oidc.data["username"]
  email             = data.kubernetes_secret_v1.oidc.data["email"]
  password          = random_password.agentydragon.result
  is_admin          = false
  can_create_group  = false
  skip_confirmation = true
  projects_limit    = 0
}

resource "gitlab_user_identity" "agentydragon" {
  user_id           = tonumber(gitlab_user.agentydragon.id)
  external_provider = "openid_connect"
  external_uid      = data.kubernetes_secret_v1.oidc.data["subject"]
}

resource "gitlab_group_membership" "agentydragon" {
  group_id     = tonumber(gitlab_group.spike.id)
  user_id      = tonumber(gitlab_user.agentydragon.id)
  access_level = "owner"
}

resource "gitlab_user_sshkey" "agentydragon" {
  for_each = var.human_ssh_keys

  user_id    = tonumber(gitlab_user.agentydragon.id)
  title      = each.key
  key        = each.value.key
  expires_at = each.value.expires_at
}

resource "gitlab_project" "acceptance" {
  # Approved for the disposable Free-tier spike; these policies require paid GitLab tiers.
  # checkov:skip=CKV_GLB_1:Required MR approvals are Premium; evaluate Free without enforcing two reviewers.
  # checkov:skip=CKV_GLB_3:Secret-file push rules are Premium; this repo contains only disposable acceptance fixtures.
  # checkov:skip=CKV_GLB_4:Signed-commit push rules are Premium; the benchmark includes unsigned synthetic commits.
  name                            = "Acceptance"
  path                            = "acceptance"
  description                     = "Private GitLab replacement spike acceptance project"
  namespace_id                    = tonumber(gitlab_group.spike.id)
  visibility_level                = "private"
  default_branch                  = "main"
  initialize_with_readme          = true
  repository_access_level         = "private"
  container_registry_access_level = "private"
}

resource "gitlab_branch_protection" "acceptance_main" {
  project            = gitlab_project.acceptance.id
  branch             = "main"
  push_access_level  = "maintainer"
  merge_access_level = "maintainer"
  allow_force_push   = false
}

resource "gitlab_deploy_key" "acceptance" {
  for_each = var.project_deploy_keys

  project    = gitlab_project.acceptance.id
  title      = each.key
  key        = each.value.key
  can_push   = each.value.can_push
  expires_at = each.value.expires_at
}

resource "gitlab_project_access_token" "flux" {
  project      = gitlab_project.acceptance.id
  name         = "gitlab-spike-flux"
  description  = "Read the Git source and push Flux image updates for the acceptance project"
  access_level = "maintainer"
  scopes       = ["read_repository", "write_repository"]

  rotation_configuration = {
    expiration_days    = 90
    rotate_before_days = 14
  }
}

resource "gitlab_project_access_token" "proof" {
  project      = gitlab_project.acceptance.id
  name         = "gitlab-spike-proof"
  description  = "Push unprotected proof branches and read acceptance project metadata"
  access_level = "developer"
  scopes       = ["read_api", "write_repository"]

  rotation_configuration = {
    expiration_days    = 90
    rotate_before_days = 14
  }
}

data "gitlab_user" "flux" {
  user_id = gitlab_project_access_token.flux.user_id
}

resource "random_password" "benchmark" {
  length  = 48
  special = false
}

resource "gitlab_user" "benchmark" {
  name     = "GitLab Spike Benchmark"
  username = "spike-benchmark"
  email    = "gitlab-spike-benchmark@invalid.example"
  password = random_password.benchmark.result
  is_admin = false
  # External users cannot create the benchmark's disposable personal projects.
  is_external       = false
  can_create_group  = false
  projects_limit    = 20
  skip_confirmation = true
}

# The benchmark creates projects without namespace_id, so they live only in
# this disposable service user's personal namespace and are removed by its job.
resource "gitlab_personal_access_token" "benchmark" {
  user_id     = tonumber(gitlab_user.benchmark.id)
  name        = "gitlab-spike-benchmark"
  description = "Disposable acceptance benchmark token"
  scopes      = ["api", "write_repository"]
  expires_at  = "2027-01-22"
}

resource "gitlab_project_deploy_token" "registry" {
  project  = gitlab_project.acceptance.id
  name     = "gitlab-spike-registry-pull"
  username = "gitlab-spike-registry-reader"
  scopes   = ["read_registry"]
}

resource "gitlab_user_runner" "acceptance" {
  runner_type = "project_type"
  project_id  = tonumber(gitlab_project.acceptance.id)
  description = "gitlab-spike project runner"
  untagged    = true
  tag_list    = []
}

resource "random_password" "flux_webhook" {
  length  = 40
  special = false
}

locals {
  flux_receiver_url = "https://flux-webhook.allegedly.works/hook/${sha256(join("", [random_password.flux_webhook.result, "gitlab-spike", "flux-system"]))}"
}

resource "gitlab_project_hook" "flux_receiver" {
  project                 = gitlab_project.acceptance.id
  url                     = local.flux_receiver_url
  name                    = "Flux GitOps receiver"
  description             = "Notify Flux when Git source or pipeline status changes"
  token                   = random_password.flux_webhook.result
  push_events             = true
  pipeline_events         = true
  tag_push_events         = false
  enable_ssl_verification = true
}

resource "kubernetes_secret_v1" "flux_git" {
  metadata {
    name      = "gitlab-spike-git"
    namespace = "flux-system"
  }

  data = {
    username = data.gitlab_user.flux.username
    password = gitlab_project_access_token.flux.token
  }
}

resource "kubernetes_secret_v1" "proof_token" {
  metadata {
    name      = "gitlab-spike-proof"
    namespace = "gitlab-spike"
  }

  data = {
    username = "oauth2"
    token    = gitlab_project_access_token.proof.token
  }
}

resource "kubernetes_secret_v1" "registry_pull" {
  for_each = toset(["flux-system", "gitlab-spike-demo"])

  metadata {
    name      = "gitlab-spike-registry"
    namespace = each.value
  }

  type = "kubernetes.io/dockerconfigjson"

  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        "registry-gitlab-spike.allegedly.works" = {
          username = gitlab_project_deploy_token.registry.username
          password = gitlab_project_deploy_token.registry.token
          auth     = base64encode("${gitlab_project_deploy_token.registry.username}:${gitlab_project_deploy_token.registry.token}")
        }
      }
    })
  }
}

resource "kubernetes_secret_v1" "flux_webhook" {
  metadata {
    name      = "gitlab-spike-webhook"
    namespace = "flux-system"
  }

  data = {
    token = random_password.flux_webhook.result
  }
}

resource "kubernetes_secret_v1" "runner" {
  metadata {
    name      = "gitlab-spike-runner"
    namespace = "gitlab-spike-ci"
  }

  data = {
    "runner-token"              = gitlab_user_runner.acceptance.token
    "runner-registration-token" = ""
  }
}

resource "kubernetes_secret_v1" "benchmark_token" {
  metadata {
    name      = "gitlab-spike-benchmark-token"
    namespace = "gitlab-spike"
  }

  data = {
    token = gitlab_personal_access_token.benchmark.token
  }
}

resource "random_password" "forgejo_benchmark" {
  length  = 48
  special = false
}

resource "forgejo_user" "benchmark" {
  login                = "gitlab-spike-control"
  email                = "gitlab-spike-control@invalid.example"
  password             = random_password.forgejo_benchmark.result
  must_change_password = false
  visibility           = "private"
}

resource "kubernetes_secret_v1" "forgejo_benchmark" {
  metadata {
    name      = "forgejo-spike-benchmark"
    namespace = "gitlab-spike"
  }

  data = {
    username = forgejo_user.benchmark.login
    password = random_password.forgejo_benchmark.result
  }
}
