terraform {
  required_version = ">= 1.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    litellm = {
      source  = "ncecere/litellm"
      version = "~> 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
    sops = {
      source  = "carlpett/sops"
      version = "~> 1.0"
    }
  }
}

# LiteLLM virtual keys for the Haku worker lanes (haku/plans/multi_agent.md).
# Declarative per-key model allowlists + budgets; deleting a resource here is the
# kill switch for that lane. Auth = the master key managed by ../litellm-api-key
# (the litellm-keys Terraform CR dependsOn it).
#
# STATE COUPLING (see cluster/AGENTS.md "Wiping a backing DB orphans tofu state"):
# litellm_key resources live in the litellm-db CNPG database and their IDs live in
# this module's tofu state. Wiping litellm-db without clearing the litellm_keys
# state schema breaks the next plan; recovery per
# cluster/docs/troubleshooting.md § "Resource ID Desync After Wiping a Backing
# Datastore".
#
# Deliberately NOT minted yet: a Haku (orchestrator) key — Haku must never be
# allowlisted for GLM (that would hand a confused Haku a personal-data egress
# path to z.ai); it arrives with its own claude-* allowlist when Haku's LLM
# path moves behind LiteLLM.

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

locals {
  # Model names must match generated model_name entries in
  # cluster/k8s/litellm/app/proxy-config.yaml.
  zai_lane_models = [
    for m in ["glm-4.5", "glm-4.5-air", "glm-4.6", "glm-4.7", "glm-5", "glm-5-turbo", "glm-5.1", "glm-5.2"] :
    "${m}-anthropic"
  ]
  # Only the models the Codex/ChatGPT-account backend actually serves
  # (see _CHATGPT_MODELS in generate_litellm.py).
  oai_lane_models = [
    for m in ["gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.3-codex-spark"] :
    "${m}-chatgpt"
  ]
  # Real Anthropic models (ANTHROPIC_MODELS in generate_litellm.py) — the
  # dispatcher's classifier gate.
  classifier_models = ["claude-sonnet-5", "claude-haiku-4-5"]
}

# One static key per worker lane, held by that lane's llm-proxy (never by workers —
# they authenticate to the proxy with per-job tokens). Budgets are the coarse
# lane-level cap; per-job budgets are enforced by the lane proxy.

resource "litellm_key" "haku_lane_zai" {
  key_alias       = "haku-lane-zai"
  models          = local.zai_lane_models
  max_budget      = 25
  budget_duration = "30d"
  metadata = {
    lane = "zai"
  }
}

resource "litellm_key" "haku_lane_oai" {
  key_alias       = "haku-lane-oai"
  models          = local.oai_lane_models
  max_budget      = 25
  budget_duration = "30d"
  metadata = {
    lane = "oai"
  }
}

# Both zone keys reflect into haku-dispatch, where the shared workers-LiteLLM
# mounts them as upstream credentials (haku/plans/multi_agent.md → key
# containment). Worker pods never see them — workers hold only per-job virtual
# keys minted on the workers-LiteLLM.

resource "kubernetes_secret" "haku_lane_zai" {
  metadata {
    name      = "litellm-key-haku-lane-zai"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the haku zai worker zone (GLM models only); reflected into haku-dispatch as the workers-LiteLLM upstream credential"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "haku-dispatch"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "haku-dispatch"
    }
  }

  data = {
    api-key = litellm_key.haku_lane_zai.key
  }
}

resource "kubernetes_secret" "haku_lane_oai" {
  metadata {
    name      = "litellm-key-haku-lane-oai"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the haku oai worker zone (chatgpt models only); reflected into haku-dispatch as the workers-LiteLLM upstream credential"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "haku-dispatch"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "haku-dispatch"
    }
  }

  data = {
    api-key = litellm_key.haku_lane_oai.key
  }
}

# Control credentials for the second-layer workers-LiteLLM (haku-dispatch): its
# master key (held by it and by the gate-validator, which mints per-job virtual
# keys with it — never by workers or L0) and its salt key (encrypts key material
# in haku-dispatch-db; set once, NEVER rotate).

resource "random_password" "workers_litellm_master_key" {
  length  = 48
  special = false

  lifecycle {
    ignore_changes = [length, special]
  }
}

resource "kubernetes_secret" "workers_litellm_master_key" {
  metadata {
    name      = "workers-litellm-master-key"
    namespace = "haku-dispatch"
  }

  data = {
    api-key = random_password.workers_litellm_master_key.result
  }
}

resource "random_password" "workers_litellm_salt_key" {
  length  = 48
  special = false

  lifecycle {
    ignore_changes  = [length, special]
    prevent_destroy = true
  }
}

resource "kubernetes_secret" "workers_litellm_salt_key" {
  metadata {
    name      = "workers-litellm-salt-key"
    namespace = "haku-dispatch"
  }

  data = {
    key = random_password.workers_litellm_salt_key.result
  }

  lifecycle {
    prevent_destroy = true
  }
}

# The dispatcher's classifier key: claude-* only, so the classifier gate runs
# through LiteLLM (Langfuse logging, budget, kill switch) instead of the
# dispatcher holding a raw Anthropic key. Reflected into haku-dispatch.

resource "litellm_key" "dispatcher_classifier" {
  key_alias       = "haku-dispatcher-classifier"
  models          = local.classifier_models
  max_budget      = 10
  budget_duration = "30d"
  metadata = {
    consumer = "haku-dispatcher"
  }
}

resource "kubernetes_secret" "dispatcher_classifier" {
  metadata {
    name      = "litellm-key-dispatcher-classifier"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the haku dispatcher's classifier gate (claude-* only); reflected into haku-dispatch"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "haku-dispatch"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "haku-dispatch"
    }
  }

  data = {
    api-key = litellm_key.dispatcher_classifier.key
  }
}

# ============================================================================
# codex-pod — OpenAI/ChatGPT-backend key for the interactive codex agent pod
# ============================================================================
# Routes the codex-pod agent's Codex CLI at LiteLLM's chatgpt/ (Codex-account)
# models instead of an interactive ChatGPT sign-in. Scoped to the same oai lane
# models as the haku oai lane (gpt-5.4/5.5/codex-spark-chatgpt); deleting this is
# the kill switch. Reflected into codex-pod, consumed as LITELLM_API_KEY.

resource "litellm_key" "codex_pod" {
  key_alias       = "codex-pod"
  models          = local.oai_lane_models
  max_budget      = 50
  budget_duration = "30d"
  metadata = {
    consumer = "codex-pod"
  }
}

resource "kubernetes_secret" "codex_pod" {
  metadata {
    name      = "litellm-key-codex-pod"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the codex-pod agent (chatgpt/ oai models only); reflected into codex-pod as LITELLM_API_KEY"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "codex-pod"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "codex-pod"
    }
  }

  data = {
    api-key = litellm_key.codex_pod.key
  }
}

# ============================================================================
# zai-clients — z.ai-scoped key for interactive Claude-Code-on-GLM clients
# ============================================================================
# A LiteLLM virtual key for the laptop `z-claude` alias (nix/home/home.nix) and the
# agent-box `zai` user (nix/home/hosts/agent-box/zai.nix), both driving Claude Code
# against z.ai's GLM through this proxy. Scoped to GLM models only (the raw z.ai key
# stays cluster-side as litellm-zai-key, used upstream by the glm-*-anthropic routes).
# No budget — interactive, user-driven use; the model scope is the guardrail.
#
# KEY SSOT: the value lives in a git SOPS file (litellm-zai-clients-key.yaml, in this
# module dir — co-located because the tf-runner's tofu only sees the module path, not
# the repo root), NOT a cluster Secret. This module reads it via the sops_file data
# source (source_file), decrypting with a dedicated narrow age key (litellm-zai-clients)
# mounted as SOPS_AGE_KEY into this module's tf-runner (see the litellm-keys Terraform
# CR) — NOT the broad cluster SOPS key, so the runner can decrypt only this one file.
# Laptops/agent-box read the same SOPS file via ducktape.sopsEnv (LITELLM_ZAI_KEY).

data "sops_file" "zai_clients_key" {
  source_file = "${path.module}/litellm-zai-clients-key.yaml"
}

# zai-clients team: proxy-side catch-all routing Claude Code's claude-* slugs to
# z.ai GLM. Attached to the zai_clients virtual key below (laptop z-claude alias +
# agent-box zai user). Two mechanisms:
#  - model_aliases: rewrite the two real Claude deployments (which ARE in
#    model_list and would otherwise reach real Anthropic) to GLM.
#  - router_settings.fallbacks [{"*": [...]}]: any claude-* slug NOT in
#    model_list (future Anthropic releases, claude-sonnet-4-5, etc.) hits
#    NotFoundError and falls back to GLM — zero maintenance on new versions.
# Non-claude/non-GLM slugs are still blocked by the key's models allowlist
# (claude-* + glm-*-anthropic), so z.ai-only containment holds.
resource "litellm_team" "zai_clients" {
  team_alias = "zai-clients"
  model_aliases = {
    "claude-sonnet-5"  = "glm-5.2-anthropic"
    "claude-haiku-4-5" = "glm-5.2-anthropic"
  }
  router_settings = {
    fallbacks = [
      {
        model           = "*"
        fallback_models = ["glm-5.2-anthropic"]
      }
    ]
  }
}

resource "litellm_key" "zai_clients" {
  key_alias = "zai-clients"
  key       = data.sops_file.zai_clients_key.data["litellm_zai_key"]
  models    = concat(["claude-*"], local.zai_lane_models)
  team_id   = litellm_team.zai_clients.id
  metadata = {
    consumer = "laptop-z-claude, agent-box-zai"
  }
}
