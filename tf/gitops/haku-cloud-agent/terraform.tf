terraform {
  required_version = ">= 1.0"

  required_providers {
    # CAUTION: low-user-count community provider holding an Anthropic API key.
    # On every version bump, manually review the source diff since the last
    # pinned tag (focus: internal/client/ egress+secret handling,
    # .github/workflows/release.yml) BEFORE updating the lock hashes. Do not
    # auto-merge Renovate/update_deps bumps. See memory
    # project_acma_provider_repin_review and the matching MODULE.bazel comment.
    claude-managed-agents = {
      source  = "modus-agendi/anthropic-claude-managed-agents"
      version = "~> 1.1"
    }
  }

  # Placeholder; the actual backend (pg, tofu-state) is injected by the Terraform
  # CR's backendConfig.customConfiguration (see cluster/k8s/haku/cloud-agent-tf).
  backend "kubernetes" {
    secret_suffix = "haku-cloud-agent"
    namespace     = "flux-system"
  }
}
