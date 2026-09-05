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

locals {
  # Model names must match generated model_name entries in
  # cluster/k8s/litellm/app/proxy-config.yaml ({provider}/{shape}/{model} scheme,
  # derivations in cluster/k8s/litellm/app/model_rosters.py). Spelled out rather
  # than built with a for-expression so the names are greppable and so the test
  # can compare structurally -- HCL2 returns a for-expression as unevaluated
  # source text, not a list.
  #
  # The Codex-subscription models on LiteLLM's Responses surface, for Codex CLI clients
  # (haku oai zone, codex-pod, agent-workspaces-codex) -- served by CLIProxyAPI; see
  # _cliproxy_responses_entries in cluster/k8s/litellm/app/test_litellm_config.py,
  # which pins this list against it.
  oai_lane_models = [
    "chatgpt/oai-responses/gpt-5.4",
    "chatgpt/oai-responses/gpt-5.5",
    "chatgpt/oai-responses/gpt-5.6-sol",
    "chatgpt/oai-responses/gpt-5.6-terra",
    "chatgpt/oai-responses/gpt-5.6-luna",
    "chatgpt/oai-responses/gpt-5.3-codex-spark",
  ]
  # Tana-UI models fronted through tana-litellm (TANA_MODELS in model_rosters.py).
  tana_client_models = [
    "tana/ant-messages/claude-sonnet-4-6",
    "tana/ant-messages/claude-opus-4-6",
    "tana/ant-messages/claude-haiku-4-5",
  ]
  # Codex-subscription models on the Anthropic Messages surface, fronted through
  # CLIProxyAPI (_cliproxy_messages_entries) -- Claude Code clients.
  codex_client_models = [
    "chatgpt/ant-messages/gpt-5.4",
    "chatgpt/ant-messages/gpt-5.5",
    "chatgpt/ant-messages/gpt-5.6-sol",
    "chatgpt/ant-messages/gpt-5.6-terra",
    "chatgpt/ant-messages/gpt-5.6-luna",
    "chatgpt/ant-messages/gpt-5.3-codex-spark",
  ]
  # Claude-subscription models on the Anthropic Messages surface, fronted through CLIProxyAPI's
  # Claude OAuth session (_cliproxy_claude_entries, ANTHROPIC_MODELS in model_rosters.py) -- the
  # Console-launched Claude runner. A different upstream session on the same pod as the codex lane
  # above; distinct from the direct-API anthropic-api/ant-messages/* entries.
  claude_client_models = [
    "anthropic-max20/ant-messages/claude-opus-5",
    "anthropic-max20/ant-messages/claude-sonnet-5",
    "anthropic-max20/ant-messages/claude-fable-5",
    "anthropic-max20/ant-messages/claude-haiku-4-5-20251001",
  ]
  # Gemini embeddings (GEMINI_EMBEDDING_MODELS in test_litellm_config.py). Granted to
  # agents whose egress cannot reach api.openai.com: the main openclaw gateway holds
  # a direct OpenAI Platform key for memorySearch, but a domain-confined agent has no
  # route to it and should not gain one just to embed. Routing embeddings through
  # LiteLLM keeps them on the in-cluster path the agent already uses for turns.
  embedding_client_models = [
    # Compatibility alias for public-coder-agent's existing durable index.
    "gemini-embedding-2",
    "google/oai-embeddings/gemini-embedding-2",
    "google/oai-embeddings/gemini-embedding-001",
  ]
  # Google Gemini models (GEMINI_MODELS in model_rosters.py) fronted through the
  # `gemini/` provider. Current generation only -- see that module for why the
  # 2.5 generation, gemini-3-pro-preview (shut down), and every non-latest 3.x
  # minor version are excluded. Consumed by the laptop gemini-claude alias and
  # public-coder-agent.
  gemini_client_models = [
    "google/oai-chat/gemini-3.7-flash",
    "google/oai-chat/gemini-3.5-flash-lite",
  ]
  # Shared by agents only through an expiring Haku Console Kubernetes grant. This
  # is intentionally an exact, cheap-model-only set rather than a provider-wide
  # prefix or wildcard. The Ollama names cover every model/context/protocol variant
  # emitted by the main proxy config; Mistral is the API-key-verified chat roster.
  cheap_experiments_models = [
    "google/oai-chat/gemini-3.7-flash",
    "google/oai-chat/gemini-3.5-flash-lite",
    "google/oai-embeddings/gemini-embedding-2",
    "google/oai-embeddings/gemini-embedding-001",
    "mistral/oai-chat/codestral-2508",
    "mistral/oai-chat/codestral-latest",
    "mistral/oai-chat/magistral-medium-latest",
    "mistral/oai-chat/magistral-small-latest",
    "mistral/oai-chat/ministral-14b-latest",
    "mistral/oai-chat/ministral-14b-2512",
    "mistral/oai-chat/ministral-8b-latest",
    "mistral/oai-chat/ministral-8b-2512",
    "mistral/oai-chat/ministral-3b-latest",
    "mistral/oai-chat/ministral-3b-2512",
    "mistral/oai-chat/mistral-code-fim-latest",
    "mistral/oai-chat/mistral-code-latest",
    "mistral/oai-chat/mistral-medium",
    "mistral/oai-chat/mistral-medium-2604",
    "mistral/oai-chat/mistral-medium-3",
    "mistral/oai-chat/mistral-medium-3-5",
    "mistral/oai-chat/mistral-medium-3.5",
    "mistral/oai-chat/mistral-medium-latest",
    "mistral/oai-chat/mistral-small-2603",
    "mistral/oai-chat/mistral-small-latest",
    "mistral/oai-chat/mistral-vibe-cli-fast",
    "mistral/oai-chat/mistral-vibe-cli-latest",
    "mistral/oai-chat/mistral-vibe-cli-with-tools",
    "mistral/oai-chat/voxtral-small-2507",
    "mistral/oai-chat/voxtral-small-latest",
    "gpt-oss-20b-128k-openai-chat",
    "gpt-oss-20b-128k-ollama-native",
    "gpt-oss-20b-256k-openai-chat",
    "gpt-oss-20b-256k-ollama-native",
    "gpt-oss-20b-512k-openai-chat",
    "gpt-oss-20b-512k-ollama-native",
    "gpt-oss-20b-1m-openai-chat",
    "gpt-oss-20b-1m-ollama-native",
    "gpt-oss-120b-128k-openai-chat",
    "gpt-oss-120b-128k-ollama-native",
    "gemma4-31b-it-q8_0-128k-openai-chat",
    "gemma4-31b-it-q8_0-128k-ollama-native",
    "anthropic-api/ant-messages/claude-haiku-4-5-20251001",
    "chatgpt/ant-messages/gpt-5.6-luna",
    "chatgpt/oai-responses/gpt-5.6-luna",
  ]
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
# There is deliberately no reflector copy or standing RoleBinding: the grant names
# both the namespace and Secret, and LiteLLM enforces the model allowlist below.
# The one standing copy is kubernetes_secret.cheap_experiments_agentplane_staging
# below, read by the Agentplane staging runner Pods as their API key.

resource "litellm_key" "cheap_experiments" {
  key_alias       = "cheap-experiments"
  models          = local.cheap_experiments_models
  max_budget      = 50
  budget_duration = "30d"
  metadata = {
    consumer = "haku-console-temporary-agent-experiments"
  }
}

resource "kubernetes_secret" "cheap_experiments" {
  metadata {
    name      = "litellm-key-cheap-experiments"
    namespace = "litellm-cheap-experiments"
    annotations = {
      description = "LiteLLM virtual key for temporary agent experiments; Mistral, Google, Ollama, Anthropic Haiku, and OpenAI Luna only"
    }
  }

  data = {
    api-key = litellm_key.cheap_experiments.key
  }
}

# Standing copy for the Agentplane staging runners (cluster/k8s/agentplane-staging):
# the agentplane-runner SandboxTemplate reads it as ANTHROPIC_AUTH_TOKEN and
# OPENAI_API_KEY. Not reflected — written straight into the namespace, so the
# agentplane-staging Namespace must exist before this applies. The key's budget
# and model allowlist above are the kill switch.
resource "kubernetes_secret" "cheap_experiments_agentplane_staging" {
  metadata {
    name      = "litellm-key-cheap-experiments"
    namespace = "agentplane-staging"
    annotations = {
      description = "Standing copy of the cheap-experiments LiteLLM virtual key for the Agentplane staging runner Pods; the key's budget and model allowlist (tf/gitops/litellm-keys) are the kill switch"
    }
  }

  data = {
    api-key = litellm_key.cheap_experiments.key
  }
}

# Copy for the Agentplane egress proxy (cluster/k8s/agentplane-staging/egress): the
# litellm-cheap-experiments EgressCredential names this Secret, and the proxy substitutes it into
# the requests that carry its placeholder. Written into the credentials namespace, which is where a
# credential's secretRef resolves and which nothing but the proxy may read -- so once the runners
# hold the placeholder instead, this is the only copy an agent's traffic can spend. Flux owns that
# Namespace, so it must exist before this applies.
resource "kubernetes_secret" "cheap_experiments_agentplane_egress_credentials" {
  metadata {
    name      = "litellm-key-cheap-experiments"
    namespace = "agentplane-egress-credentials"
    annotations = {
      description = "The cheap-experiments LiteLLM virtual key as the Agentplane egress proxy substitutes it; sandboxes send agentplane-credential-litellm-cheap-experiments and never hold this"
    }
  }

  data = {
    api-key = litellm_key.cheap_experiments.key
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
  models = concat(local.codex_client_models, local.oai_lane_models, local.gemini_client_models, local.embedding_client_models)
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
# haku-console-claude — Console-launched Claude runner, via the colocated egress fence
# ============================================================================
# Claude Code's inference runs on the flat-rate Claude subscription models on the Anthropic Messages
# surface (anthropic-max20/ant-messages/*, #5086), fronted by CLIProxyAPI's Claude OAuth session -- the same
# /v1/messages passthrough as the codex lane but a different upstream session. The Console colocated
# egress fence (#4670) substitutes this key for the runner's inert placeholder on the internal
# LiteLLM origin; the runner never holds it. Scoped to only the anthropic-max20/ant-messages lane.
# The direct-API lane is separately exposed as anthropic-api/ant-messages/* (model_rosters.py). The
# key does not admit the Codex, Gemini, or embedding models. Deleting this key is the runner's
# provider kill switch. Reflected into haku-console, where the Console pod resolves it for substitution.

resource "litellm_key" "haku_console_claude" {
  key_alias = "haku-console-claude"
  models    = local.claude_client_models
  metadata = {
    consumer = "haku-console-claude"
  }
}

resource "kubernetes_secret" "haku_console_claude" {
  metadata {
    name      = "litellm-key-haku-console-claude"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the Console-launched Claude runner (anthropic-max20/ant-messages/* Claude-subscription models via CLIProxyAPI); reflected into haku-console, substituted by the colocated egress fence for the runner's inert placeholder"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "haku-console"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "haku-console"
    }
  }

  data = {
    api-key = litellm_key.haku_console_claude.key
  }
}

# ============================================================================
# tana-clients — scoped key for laptop tana-claude (Tana-UI models via tana-litellm)
# ============================================================================
# Pattern-B pinned key: value in a git SOPS file in this module dir, decrypted with the
# shared narrow client-key age key (the existing tf-runner
# SOPS_AGE_KEY). The laptop tana-claude wrapper reads it via ducktape.sopsEnv
# (TANA_LITELLM_KEY). The tana/ant-messages/* upstream reaches tana-litellm with the in-cluster master
# key, so this scoped key never carries it.

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
  models    = concat(["anthropic-api/ant-messages/*"], local.tana_client_models)
  team_id   = litellm_team.tana_clients.id
  metadata = {
    consumer = "laptop-tana-claude"
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
  models    = concat(["anthropic-api/ant-messages/*"], local.codex_client_models)
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
# laptop gemini-claude wrapper reads it via ducktape.sopsEnv (GEMINI_LITELLM_KEY). LiteLLM
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
        fallback_models = ["google/oai-chat/gemini-3.5-flash-lite"]
      }
    ]
  }
}

resource "litellm_key" "gemini_clients" {
  key_alias = "gemini-clients"
  key       = data.sops_file.gemini_clients_key.data["litellm_gemini_key"]
  models    = concat(["anthropic-api/ant-messages/*"], local.gemini_client_models)
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
