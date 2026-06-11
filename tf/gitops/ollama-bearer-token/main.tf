terraform {
  required_version = ">= 1.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
  }

  backend "kubernetes" {
    secret_suffix = "ollama-bearer-token"
    namespace     = "flux-system"
  }
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

resource "kubernetes_secret" "ollama_bearer_token" {
  metadata {
    name      = "ollama-bearer-token"
    namespace = "ollama"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "claude-sandbox,openclaw-sandbox"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "claude-sandbox,openclaw-sandbox"
    }
  }

  data = {
    token = random_password.bearer_token.result
  }
}
