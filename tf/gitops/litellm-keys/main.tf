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
    sops = {
      source  = "carlpett/sops"
      version = "~> 1.0"
    }
  }
}

# LiteLLM virtual keys for the Haku worker lanes (haku/archive/2026_08_multi_agent.md).
# Declarative per-key model allowlists + budgets; deleting a resource here is the
# kill switch for that lane. Auth = the SOPS-managed master key applied by
# cluster/k8s/litellm/secrets/ before the LiteLLM Deployment starts.
#
# STATE COUPLING (see cluster/AGENTS.md "Wiping a backing DB orphans tofu state"):
# litellm_key resources live in the litellm-db CNPG database and their IDs live in
# this module's tofu state. Wiping litellm-db without clearing the litellm_keys
# state schema breaks the next plan; recovery per
# cluster/docs/troubleshooting.md § "Resource ID Desync After Wiping a Backing
# Datastore".
#
# Deliberately NOT minted yet: a Haku (orchestrator) key — Haku receives its
# own Anthropic API model allowlist when its LLM path moves behind LiteLLM.

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

# One static key per worker lane, held by that lane's llm-proxy (never by workers —
# they authenticate to the proxy with per-job tokens). Budgets are the coarse
# lane-level cap; per-job budgets are enforced by the lane proxy.

# The former Haku dispatch-lane keys and workers-LiteLLM credentials were
# retired with the dispatch plane. Agent workspaces and the public coder retain
# separately scoped Codex keys below.

# ============================================================================
# cheap-experiments — shared low-cost key for temporary agent experiments
# ============================================================================
# Agents receive this Secret only through an expiring Haku Console Kubernetes grant.
# The canonical Secret lives beside LiteLLM; the Agentplane testing copy is owned by
# the cdk8s-generated ESO distribution, and LiteLLM enforces the model allowlist below.

resource "litellm_key" "cheap_experiments" {
  key_alias       = "cheap-experiments"
  models          = var.model_allowlists.cheap_experiments_models
  max_budget      = 50
  budget_duration = "30d"
  metadata = {
    consumer = "haku-console-temporary-agent-experiments"
  }
}

resource "kubernetes_secret" "cheap_experiments" {
  metadata {
    name      = "litellm-key-cheap-experiments"
    namespace = "litellm"
    annotations = {
      description = "LiteLLM virtual key for temporary agent experiments; Mistral, Google, Ollama, Anthropic Haiku, and OpenAI Luna only"
    }
  }

  data = {
    api-key = litellm_key.cheap_experiments.key
  }
}

resource "litellm_key" "agentplane_staging" {
  key_alias       = "agentplane-staging"
  models          = concat(var.model_allowlists.oai_lane_models, var.model_allowlists.claude_client_models)
  max_budget      = 50
  budget_duration = "30d"
  metadata = {
    consumer = "agentplane-staging"
  }
}

moved {
  from = kubernetes_secret.cheap_experiments_agentplane_staging
  to   = kubernetes_secret.agentplane_staging
}

# Only the workload-authenticated LLM ingress holds the model key; runners use a placeholder.
resource "kubernetes_secret" "agentplane_staging" {
  metadata {
    name      = "litellm-key-agentplane-staging"
    namespace = "agentplane-staging"
    annotations = {
      description = "Server-held OpenAI and Claude subscription key for Agentplane staging; never mounted into runner Pods"
    }
  }

  data = {
    api-key = litellm_key.agentplane_staging.key
  }
}

# ============================================================================
# codex-pod — OpenAI/ChatGPT-backend key for the interactive codex agent pod
# ============================================================================
# Routes the codex-pod agent's Codex CLI at LiteLLM's `chatgpt/oai-responses/*`
# (Codex-account) models instead of an interactive ChatGPT sign-in. Scoped to the
# oai lane models; deleting this is the kill switch. Reflected into codex-pod,
# consumed as LITELLM_API_KEY.

resource "litellm_key" "codex_pod" {
  key_alias       = "codex-pod"
  models          = var.model_allowlists.oai_lane_models
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
      description                                                     = "LiteLLM virtual key for the codex-pod agent (chatgpt/oai-responses/* models only); reflected into codex-pod as LITELLM_API_KEY"
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
# public-coder-agent — OpenClaw and Console shells, Codex + Gemini lane
# ============================================================================
# OpenClaw and its Console-launched Codex sandbox are two shells for the same durable Agent.
# The key is reflected into public-coder-agent (OpenClaw consumes it directly) and into haku-console:
# once the Console-launched Codex runner moves onto the colocated egress fence (#4670), the Console
# holds this key and substitutes it into the fence's decide responses for the `codex-litellm` handle,
# so the sandbox only ever presents the inert placeholder. Deleting this key is the Agent's provider
# kill switch.

resource "litellm_key" "public_coder_agent" {
  key_alias = "public-coder-agent"
  # Codex subscription models on both wire surfaces, the Gemini chat lineup,
  # plus embeddings. Both subscription wire surfaces remain allowlisted because
  # this shared key serves Responses-lane OpenClaw/Console consumers and clients
  # that still use the Anthropic Messages lane.
  # Embeddings ride along because OpenClaw's memory index needs a backend and
  # this agent has no route to api.openai.com -- its egress allowlist is git
  # hosting plus package indexes, and it should not gain one merely to embed.
  # Gemini reaches Google through LiteLLM's own in-cluster GEMINI_API_KEY, so
  # this key never carries that credential either.
  models = concat(var.model_allowlists.codex_client_models, var.model_allowlists.oai_lane_models, var.model_allowlists.gemini_client_models, var.model_allowlists.embedding_client_models)
  metadata = {
    consumer = "public-coder-agent"
  }
}

resource "kubernetes_secret" "public_coder_agent" {
  metadata {
    name      = "litellm-key-public-coder-agent"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for public-coder-agent OpenClaw and least-credential runner-proxy-mediated Haku Console Codex shells (chatgpt/oai-responses/* models); subscription models through CLIProxyAPI, Gemini chat models, plus embeddings"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "public-coder-agent,haku-console"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "public-coder-agent,haku-console"
    }
  }

  data = {
    api-key = litellm_key.public_coder_agent.key
  }
}

# ============================================================================
# tana-clients — scoped key for laptop tana-claude (Tana-UI models via LiteLLM)
# ============================================================================
# Pattern-B pinned key: value in a git SOPS file in this module dir, decrypted with the
# shared narrow client-key age key (the existing tf-runner
# SOPS_AGE_KEY). The laptop tana-claude wrapper reads it from its sops-nix secret file.
# The main LiteLLM proxy calls Tana in-process, so this scoped client key never
# carries the Tana Firebase credential.
#
# Gotcha for every pinned key here: the value must start with `sk-`. LiteLLM rejects any
# other bearer token before it looks the key up at all, as a guard against replayed token
# hashes ("LiteLLM Virtual Key expected. Received=..., expected to start with 'sk-'",
# proxy/auth/user_api_key_auth.py). A key minted without the prefix fails every request
# with a 401 that names authentication rather than the key's shape.

data "sops_file" "tana_clients_key" {
  source_file = "${path.module}/litellm-tana-clients-key.yaml"
}

resource "litellm_team" "tana_clients" {
  team_alias = "tana-clients"
  router_settings = {
    # Claude Code background Anthropic API slugs fall back to the cheap tana haiku tier.
    fallbacks = [
      {
        model           = "*"
        fallback_models = ["tana/ant-messages/claude-haiku-4-5"]
      }
    ]
  }
}

resource "litellm_key" "tana_clients" {
  key_alias = "tana-clients"
  key       = data.sops_file.tana_clients_key.data["litellm_tana_key"]
  models    = var.model_allowlists.tana_client_models
  team_id   = litellm_team.tana_clients.id
  metadata = {
    consumer = "laptop-tana-claude"
  }
}

# ============================================================================
# claude-subscription-clients — scoped key for the laptop litellm-claude wrapper
# ============================================================================
# Same Pattern-B pinned key: value in a git SOPS file in this module dir, decrypted with the
# shared narrow client-key age key. The laptop litellm-claude wrapper reads it via
# its sops-nix secret file. CLIProxyAPI holds the Claude OAuth session, so this
# scoped key never carries it.
#
# One deliberate difference from its sibling client keys: no team, so no `model = "*"`
# fallback. Those exist on the tana/codex/gemini lanes because Claude Code names Claude
# models a non-Claude lane cannot serve, which cannot happen here.

data "sops_file" "claude_subscription_clients_key" {
  source_file = "${path.module}/litellm-claude-subscription-clients-key.yaml"
}

resource "litellm_key" "claude_subscription_clients" {
  key_alias = "claude-subscription-clients"
  key       = data.sops_file.claude_subscription_clients_key.data["litellm_claude_subscription_key"]
  models    = var.model_allowlists.claude_client_models
  metadata = {
    consumer = "laptop-litellm-claude"
  }
}

# ============================================================================
# codex-clients — scoped key for laptop + agent-box + codex-pod codex-claude
# ============================================================================
# Same Pattern-B pinned key. The chatgpt/ant-messages/* upstream reaches CLIProxyAPI with the in-cluster
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
        fallback_models = ["chatgpt/ant-messages/gpt-5.6-luna"]
      }
    ]
  }
}

resource "litellm_key" "codex_clients" {
  key_alias = "codex-clients"
  key       = data.sops_file.codex_clients_key.data["litellm_codex_key"]
  models    = var.model_allowlists.codex_client_models
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
      description                                                     = "LiteLLM virtual key for codex-claude consumers (chatgpt/ant-messages/* models only); reflected into codex-pod as CODEX_LITELLM_KEY"
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
# laptop gemini-claude wrapper reads it from its sops-nix secret file. LiteLLM
# reaches Google with the in-cluster GEMINI_API_KEY, so this scoped key never carries it.

data "sops_file" "gemini_clients_key" {
  source_file = "${path.module}/litellm-gemini-clients-key.yaml"
}

resource "litellm_team" "gemini_clients" {
  team_alias = "gemini-clients"
  router_settings = {
    # Keep a fallback to the cheap, high-quota flash-lite tier instead of
    # hard-failing Claude Code.
    fallbacks = [
      {
        model           = "*"
        fallback_models = ["google/goog-generate/gemini-3.5-flash-lite"]
      }
    ]
  }
}

resource "litellm_key" "gemini_clients" {
  key_alias = "gemini-clients"
  key       = data.sops_file.gemini_clients_key.data["litellm_gemini_key"]
  models    = var.model_allowlists.gemini_client_models
  team_id   = litellm_team.gemini_clients.id
  metadata = {
    consumer = "laptop-gemini-claude"
  }
}

# Disposable agent workspaces (cluster/k8s/agents/agent-sandbox/): operator-
# codex workspace lane: the codex CLI's baked LiteLLM provider
# (cluster/k8s/agents/agent-sandbox/workspace-image/codex-config.toml) uses
# the `chatgpt/oai-responses/*` Codex-account models, same allowlist as codex-pod.
resource "litellm_key" "agent_workspaces_codex" {
  key_alias = "agent-workspaces-codex"
  models    = var.model_allowlists.oai_lane_models
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
      description                                                     = "LiteLLM virtual key for the codex workspace lane (chatgpt/oai-responses/* models only); reflected into agent-workspaces for the codex SandboxTemplate"
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
