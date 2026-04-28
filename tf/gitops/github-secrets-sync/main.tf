# GitHub Actions Secrets Sync
#
# Syncs a single SOPS age key to GitHub Actions so CI can decrypt secrets
# from git. Managed by tofu-controller (15m interval).
#
# The CI age key is a narrow-scope key that can only decrypt CI-relevant
# secrets (BuildBuddy API key, Docker CI mTLS, Attic token, Harbor creds,
# GitHub PAT). It cannot decrypt cluster tokens, Nebula keys, or other
# infrastructure secrets.
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

data "kubernetes_secret" "ci_age_key" {
  metadata {
    name      = "ci-age-key"
    namespace = "flux-system"
  }
}

# --- GitHub Actions Secrets ---

resource "github_actions_secret" "sops_age_key" {
  repository      = "ducktape"
  secret_name     = "SOPS_AGE_KEY"
  plaintext_value = data.kubernetes_secret.ci_age_key.data["age-key"]
}

resource "github_actions_secret" "sops_age_key_gaffer_private" {
  repository      = "gaffer-private"
  secret_name     = "SOPS_AGE_KEY"
  plaintext_value = data.kubernetes_secret.ci_age_key.data["age-key"]
}

# Data sources for harbor_ci_robot, buildbuddy_api_key, attic_push_token
# were removed (no `removed` block needed for data sources — just delete).
