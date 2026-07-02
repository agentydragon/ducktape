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
# Deliberately NOT minted yet: an L0 (Haku orchestrator) key — no Anthropic models
# are registered in LiteLLM, and L0 must never be allowlisted for GLM (that would
# hand a confused L0 a personal-data egress path to z.ai); and a gate-validator
# classifier key, for the same no-Anthropic-models reason. Both arrive with the
# models they need.

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
  # Only the three models the Codex/ChatGPT-account backend actually serves
  # (see _CHATGPT_MODELS in generate_litellm.py).
  oai_lane_models = [
    for m in ["gpt-5.4", "gpt-5.5", "gpt-5.3-codex-spark"] :
    "${m}-chatgpt"
  ]
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

# Each key reflects into exactly its lane namespace (haku-sandbox-{zai,oai}) for
# the lane llm-proxy to mount. The reflector annotations are inert until the lane
# namespace exists, so declaring them now means the key appears there the moment
# the lane lands — no follow-up change needed here.

resource "kubernetes_secret" "haku_lane_zai" {
  metadata {
    name      = "litellm-key-haku-lane-zai"
    namespace = "litellm"
    annotations = {
      description                                                      = "LiteLLM virtual key for the haku zai worker lane (GLM models only); reflected into haku-sandbox-zai for its lane llm-proxy"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"             = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"  = "haku-sandbox-zai"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"        = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"     = "haku-sandbox-zai"
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
      description                                                      = "LiteLLM virtual key for the haku oai worker lane (chatgpt models only); reflected into haku-sandbox-oai for its lane llm-proxy"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"             = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"  = "haku-sandbox-oai"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"        = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"     = "haku-sandbox-oai"
    }
  }

  data = {
    api-key = litellm_key.haku_lane_oai.key
  }
}
