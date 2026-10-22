terraform {
  required_version = ">= 1.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
  }
}

data "kubernetes_secret" "litellm_buildbuddy_live_tests" {
  metadata {
    name      = "litellm-key-buildbuddy-live-tests"
    namespace = "litellm"
  }
}

# BuildBuddy rotates the public encryption key independently of LiteLLM. Include
# its ID in the trigger so the secret gets resealed whenever that key changes.
data "external" "buildbuddy_public_key" {
  program = ["/usr/local/bin/buildbuddy-secret-sync", "public-key-id"]
}

resource "terraform_data" "buildbuddy_live_tests_secret" {
  triggers_replace = [
    sha256(data.kubernetes_secret.litellm_buildbuddy_live_tests.data["api-key"]),
    data.external.buildbuddy_public_key.result.key_id,
  ]

  provisioner "local-exec" {
    command = "/usr/local/bin/buildbuddy-secret-sync set"
    environment = {
      TF_VAR_buildbuddy_api_key = var.buildbuddy_api_key
      LITELLM_API_KEY           = data.kubernetes_secret.litellm_buildbuddy_live_tests.data["api-key"]
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "/usr/local/bin/buildbuddy-secret-sync delete"
    # TF_VAR_buildbuddy_api_key is injected into the runner pod and inherited by
    # local-exec. Do not map it here: destroy provisioners cannot reference vars.
  }
}
