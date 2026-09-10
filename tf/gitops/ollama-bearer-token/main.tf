terraform {
  required_version = ">= 1.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9.0"
    }
  }

  backend "pg" {}
}

# Bearer token for direct Ollama API access (bypassing LiteLLM).
# The Ollama deployment's nginx auth-proxy sidecar validates this token.

resource "random_password" "bearer_token" {
  length  = 48
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "kubernetes_secret_v1_data" "ollama_bearer_token" {
  metadata {
    name      = "ollama-bearer-token"
    namespace = "ollama"
  }

  # Adopt the existing Secret's token in place when starting with fresh state.
  # Flux creates the object and manages its metadata, including on cold bootstrap.
  field_manager = "ollama-bearer-token"
  force         = true

  data = {
    token = random_password.bearer_token.result
  }
}
