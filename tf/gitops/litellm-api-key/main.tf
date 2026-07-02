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
    secret_suffix = "litellm-api-key"
    namespace     = "flux-system"
  }
}

# LiteLLM master API key — used to authenticate to the LiteLLM proxy at
# litellm.allegedly.works. Also used by Gatus health checks and agent sandboxes.

resource "random_password" "api_key" {
  length  = 48
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

# Primary secret in litellm namespace, reflected to agent sandboxes and props.
resource "kubernetes_secret" "litellm_master_key" {
  metadata {
    name      = "litellm-master-key"
    namespace = "litellm"
    annotations = {
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "claude-sandbox,openclaw-sandbox,props"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "claude-sandbox,openclaw-sandbox,props"
    }
  }

  data = {
    api-key = random_password.api_key.result
  }
}

# LITELLM_SALT_KEY — encrypts credential material LiteLLM stores in its virtual-key
# DB. Set once, NEVER rotate: rotating orphans everything already encrypted with it
# (the lifecycle block guards against accidental regeneration).
resource "random_password" "salt_key" {
  length  = 48
  special = false

  lifecycle {
    ignore_changes  = [length, special]
    prevent_destroy = true
  }
}

resource "kubernetes_secret" "litellm_salt_key" {
  metadata {
    name      = "litellm-salt-key"
    namespace = "litellm"
  }

  data = {
    key = random_password.salt_key.result
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Gatus needs the key as LITELLM_API_KEY env var (via envFrom).
resource "kubernetes_secret" "litellm_api_key_gatus" {
  metadata {
    name      = "litellm-api-key"
    namespace = "gatus"
  }

  data = {
    LITELLM_API_KEY = random_password.api_key.result
  }
}
