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
    sops = {
      source  = "carlpett/sops"
      version = "~> 1.0"
    }
  }
}

# LiteLLM virtual keys for the Haku worker lanes (haku/archive/2026_08_multi_agent.md).
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
# Deliberately NOT minted yet: a Haku (orchestrator) key — Haku receives its
# own claude-* allowlist when its LLM path moves behind LiteLLM.

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
  # The Codex-subscription models on LiteLLM's Responses surface, for Codex CLI clients
  # (haku oai zone, codex-pod, agent-workspaces-codex). Same names as before 2026-08-06,
  # now served by CLIProxyAPI rather than the retired litellm-chatgpt sub-instance --
  # see _cliproxy_responses_entries in cluster/k8s/litellm/app/test_litellm_config.py,
  # which pins this list against it. Spelled out rather than built with a for-expression
  # so the names are greppable and so the test can compare structurally -- HCL2 returns a
  # for-expression as unevaluated source text, not a list.
  oai_lane_models = [
    "gpt-5.4-chatgpt",
    "gpt-5.5-chatgpt",
    "gpt-5.6-sol-chatgpt",
    "gpt-5.6-terra-chatgpt",
    "gpt-5.6-luna-chatgpt",
    "gpt-5.3-codex-spark-chatgpt",
  ]
  # Tana-UI models fronted through tana-litellm (_TANA_MODELS in test_litellm_config.py).
  tana_client_models = ["tana-claude-sonnet-4-6", "tana-claude-opus-4-6", "tana-claude-haiku-4-5"]
  # Codex-subscription models fronted through CLIProxyAPI (_cliproxy_entries).
  codex_client_models = [
    "codex-gpt-5.4",
    "codex-gpt-5.5",
    "codex-gpt-5.6-sol",
    "codex-gpt-5.6-terra",
    "codex-gpt-5.6-luna",
    "codex-gpt-5.3-codex-spark",
  ]
  # Gemini embeddings (GEMINI_EMBEDDING_MODELS in test_litellm_config.py). Granted to
  # agents whose egress cannot reach api.openai.com: the main openclaw gateway holds
  # a direct OpenAI Platform key for memorySearch, but a domain-confined agent has no
  # route to it and should not gain one just to embed. Routing embeddings through
  # LiteLLM keeps them on the in-cluster path the agent already uses for turns.
  embedding_client_models = ["gemini-embedding-2", "gemini-embedding-001"]
  # Google Gemini models (GEMINI_MODELS in test_litellm_config.py) fronted through the
  # `gemini/` provider. Consumed by the laptop gemini-claude alias.
  gemini_client_models = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-pro-latest",
    "gemini-flash-latest",
  ]
}

# One static key per worker lane, held by that lane's llm-proxy (never by workers —
# they authenticate to the proxy with per-job tokens). Budgets are the coarse
# lane-level cap; per-job budgets are enforced by the lane proxy.

# The former Haku dispatch-lane keys and workers-LiteLLM credentials were
# retired with the dispatch plane. Agent workspaces and the public coder retain
# separately scoped Codex keys below.

# ============================================================================
# codex-pod — OpenAI/ChatGPT-backend key for the interactive codex agent pod
# ============================================================================
# Routes the codex-pod agent's Codex CLI at LiteLLM's `*-chatgpt` (Codex-account)
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
      description                                                     = "LiteLLM virtual key for the codex-pod agent (*-chatgpt oai models only); reflected into codex-pod as LITELLM_API_KEY"
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
# public-coder-agent — second OpenClaw agent, Codex-subscription + Gemini
# ============================================================================
# The coder agent at public-coder-agent.allegedly.works runs the same harness
# against the same `codex-*` models plus the Gemini chat lineup, but gets its
# own virtual key rather than sharing openclaw's: usage is then attributable
# per agent, and either can be revoked without taking the other down. Deleting
# this key is its kill switch.

resource "litellm_key" "public_coder_agent" {
  key_alias = "public-coder-agent"
  # Codex subscription models, the Gemini chat lineup, plus embeddings.
  # Embeddings ride along because OpenClaw's memory index needs a backend and
  # this agent has no route to api.openai.com -- its egress allowlist is git
  # hosting plus package indexes, and it should not gain one merely to embed.
  # Gemini reaches Google through LiteLLM's own in-cluster GEMINI_API_KEY, so
  # this key never carries that credential either.
  models = concat(local.codex_client_models, local.gemini_client_models, local.embedding_client_models)
  metadata = {
    consumer = "public-coder-agent"
  }
}

resource "kubernetes_secret" "public_coder_agent" {
  metadata {
    name      = "litellm-key-public-coder-agent"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the public-coder-agent OpenClaw instance (Codex subscription models through CLIProxyAPI, Gemini chat models, plus embeddings)"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "public-coder-agent"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "public-coder-agent"
    }
  }

  data = {
    api-key = litellm_key.public_coder_agent.key
  }
}

# ============================================================================
# tana-clients — scoped key for laptop tana-claude (Tana-UI models via tana-litellm)
# ============================================================================
# Pattern-B pinned key: value in a git SOPS file in this module dir, decrypted with the
# shared narrow client-key age key (the existing tf-runner
# SOPS_AGE_KEY). The laptop tana-claude wrapper reads it via ducktape.sopsEnv
# (TANA_LITELLM_KEY). The tana-* upstream reaches tana-litellm with the in-cluster master
# key, so this scoped key never carries it.

data "sops_file" "tana_clients_key" {
  source_file = "${path.module}/litellm-tana-clients-key.yaml"
}

resource "litellm_team" "tana_clients" {
  team_alias = "tana-clients"
  router_settings = {
    # Claude Code background claude-* slugs fall back to the cheap tana haiku tier.
    fallbacks = [
      {
        model           = "*"
        fallback_models = ["tana-claude-haiku-4-5"]
      }
    ]
  }
}

resource "litellm_key" "tana_clients" {
  key_alias = "tana-clients"
  key       = data.sops_file.tana_clients_key.data["litellm_tana_key"]
  models    = concat(["claude-*"], local.tana_client_models)
  team_id   = litellm_team.tana_clients.id
  metadata = {
    consumer = "laptop-tana-claude"
  }
}

# ============================================================================
# codex-clients — scoped key for laptop + agent-box + codex-pod codex-claude
# ============================================================================
# Same Pattern-B pinned key. The codex-* upstream reaches CLIProxyAPI with the in-cluster
# cli-proxy client key (ESO-mirrored into litellm), so this key never carries it. codex-pod
# receives the value via the reflected kubernetes_secret below (CODEX_LITELLM_KEY), NOT
# sops — the image has no sops-nix.

data "sops_file" "codex_clients_key" {
  source_file = "${path.module}/litellm-codex-clients-key.yaml"
}

resource "litellm_team" "codex_clients" {
  team_alias = "codex-clients"
  router_settings = {
    fallbacks = [
      {
        model           = "*"
        fallback_models = ["codex-gpt-5.6-luna"]
      }
    ]
  }
}

resource "litellm_key" "codex_clients" {
  key_alias = "codex-clients"
  key       = data.sops_file.codex_clients_key.data["litellm_codex_key"]
  models    = concat(["claude-*"], local.codex_client_models)
  team_id   = litellm_team.codex_clients.id
  metadata = {
    consumer = "laptop-codex-claude, agent-box-codex, codex-pod"
  }
}

# Reflected into codex-pod so the baked codex-claude wrapper reads CODEX_LITELLM_KEY.
resource "kubernetes_secret" "codex_clients_key" {
  metadata {
    name      = "litellm-codex-clients-key"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for codex-claude consumers (codex-* models only); reflected into codex-pod as CODEX_LITELLM_KEY"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "codex-pod"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "codex-pod"
    }
  }

  data = {
    CODEX_LITELLM_KEY = litellm_key.codex_clients.key
  }
}

# ============================================================================
# gemini-clients — scoped key for laptop gemini-claude (Google Gemini via `gemini/`)
# ============================================================================
# Same Pattern-B pinned key: value in a git SOPS file in this module dir, decrypted with
# the shared narrow client-key age key. The
# laptop gemini-claude wrapper reads it via ducktape.sopsEnv (GEMINI_LITELLM_KEY). LiteLLM
# reaches Google with the in-cluster GEMINI_API_KEY, so this scoped key never carries it.

data "sops_file" "gemini_clients_key" {
  source_file = "${path.module}/litellm-gemini-clients-key.yaml"
}

resource "litellm_team" "gemini_clients" {
  team_alias = "gemini-clients"
  router_settings = {
    # A quota-throttled preview model (gemini-3-pro-preview) degrades to the cheap,
    # high-quota flash tier instead of hard-failing Claude Code.
    fallbacks = [
      {
        model           = "*"
        fallback_models = ["gemini-3.5-flash"]
      }
    ]
  }
}

resource "litellm_key" "gemini_clients" {
  key_alias = "gemini-clients"
  key       = data.sops_file.gemini_clients_key.data["litellm_gemini_key"]
  models    = concat(["claude-*"], local.gemini_client_models)
  team_id   = litellm_team.gemini_clients.id
  metadata = {
    consumer = "laptop-gemini-claude"
  }
}

# Disposable agent workspaces (cluster/k8s/agents/agent-sandbox/): operator-
# codex workspace lane: the codex CLI's baked LiteLLM provider
# (cluster/k8s/agents/agent-sandbox/workspace-image/codex-config.toml) uses
# the `*-chatgpt` Codex-account models, same allowlist as codex-pod.
resource "litellm_key" "agent_workspaces_codex" {
  key_alias = "agent-workspaces-codex"
  models    = local.oai_lane_models
  metadata = {
    consumer = "agent-workspaces codex-lane sandboxes"
  }
}

# Reflected into agent-workspaces, where the codex-lane SandboxTemplate reads
# it as LITELLM_API_KEY (the env_key named by the baked codex config).
resource "kubernetes_secret" "agent_workspaces_codex_key" {
  metadata {
    name      = "litellm-key-agent-workspaces-codex"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the codex workspace lane (*-chatgpt oai models only); reflected into agent-workspaces for the codex SandboxTemplate"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "agent-workspaces"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "agent-workspaces"
    }
  }

  data = {
    api-key = litellm_key.agent_workspaces_codex.key
  }
}
