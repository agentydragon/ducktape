# GitHub Actions Secrets Sync
#
# Reads secrets from K8s, pushes them to GitHub Actions repository secrets
# for agentydragon/ducktape. Managed by tofu-controller (15m interval).
#
# Sources:
#   - Harbor CI robot (K8s secret from harbor-ci TF module) -> PROPS_REGISTRY_USERNAME, PROPS_REGISTRY_PASSWORD
#   - BuildBuddy API key (K8s SOPS secret) -> BUILDBUDDY_API_KEY
#   - Attic push token (K8s SOPS secret) -> ATTIC_TOKEN
#
# Auth: fine-grained GitHub PAT stored as K8s Secret (SOPS-deployed by Flux).

provider "github" {
  owner = "agentydragon"
  token = data.kubernetes_secret.github_secrets_sync_pat.data["token"]
}

# --- Data Sources ---

data "kubernetes_secret" "github_secrets_sync_pat" {
  metadata {
    name      = "github-secrets-sync-pat"
    namespace = "flux-system"
  }
}

data "kubernetes_secret" "harbor_ci_robot" {
  metadata {
    name      = "harbor-ci-robot"
    namespace = "flux-system"
  }
}

data "kubernetes_secret" "buildbuddy_api_key" {
  metadata {
    name      = "buildbuddy-api-key"
    namespace = "claude-sandbox"
  }
}

data "kubernetes_secret" "attic_push_token" {
  metadata {
    name      = "attic-push-token"
    namespace = "claude-sandbox"
  }
}

# --- GitHub Actions Secrets ---

resource "github_actions_secret" "buildbuddy_api_key" {
  repository      = "ducktape"
  secret_name     = "BUILDBUDDY_API_KEY"
  plaintext_value = data.kubernetes_secret.buildbuddy_api_key.data["api-key"]
}

resource "github_actions_secret" "props_registry_username" {
  repository      = "ducktape"
  secret_name     = "PROPS_REGISTRY_USERNAME"
  plaintext_value = data.kubernetes_secret.harbor_ci_robot.data["username"]
}

resource "github_actions_secret" "props_registry_password" {
  repository      = "ducktape"
  secret_name     = "PROPS_REGISTRY_PASSWORD"
  plaintext_value = data.kubernetes_secret.harbor_ci_robot.data["password"]
}

resource "github_actions_secret" "attic_token" {
  repository      = "ducktape"
  secret_name     = "ATTIC_TOKEN"
  plaintext_value = data.kubernetes_secret.attic_push_token.data["token"]
}
