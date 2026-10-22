terraform {
  required_version = ">= 1.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
    litellm = {
      source  = "ncecere/litellm"
      version = "~> 2.0"
    }
  }
}

# This root keeps the existing `litellm_keys` state schema long enough to destroy
# the Terraform-owned keys, teams, and Kubernetes Secrets as one acknowledged apply.
# The Tofu Kustomization remains Ready after this marker has replaced the old
# BuildBuddy side effect; Crossplane's Kustomization depends on that Ready state.

data "kubernetes_secret" "litellm_master_key" {
  metadata {
    name      = "litellm-master-key"
    namespace = "litellm"
  }
}

provider "litellm" {
  api_base = "http://litellm.litellm.svc.cluster.local:4000"
  api_key  = data.kubernetes_secret.litellm_master_key.data["api-key"]
}

resource "terraform_data" "buildbuddy_live_tests_secret" {
  triggers_replace = ["crossplane-handoff"]

  lifecycle {
    precondition {
      condition     = var.buildbuddy_api_key != ""
      error_message = "The BuildBuddy API key must be set for the handoff cleanup."
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "/usr/local/bin/buildbuddy-secret-sync delete"
    # BUILDBUDDY_API_KEY is injected into the runner pod and inherited by
    # local-exec. Do not map it here: destroy provisioners cannot reference vars.
  }
}
