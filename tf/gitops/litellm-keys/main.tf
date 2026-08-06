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
  # The Codex-subscription models on LiteLLM's Responses surface, for Codex CLI clients
  # (haku oai zone, codex-pod, agent-workspaces-codex). Same names as before 2026-08-06,
  # now served by CLIProxyAPI rather than the retired litellm-chatgpt sub-instance --
  # see _cliproxy_responses_entries in cluster/k8s/litellm/app/test_litellm_config.py,
  # which is what these must stay in sync with.
  oai_lane_models = [
    for m in ["gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.3-codex-spark"] :
    "${m}-chatgpt"
  ]
  # Real Anthropic models (ANTHROPIC_MODELS in model_rosters.py) — the
  # dispatcher's classifier gate.
  classifier_models = ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"]
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
# openclaw — Codex subscription through LiteLLM and CLIProxyAPI
# ============================================================================
# OpenClaw owns the agent loop and talks Anthropic Messages to the main LiteLLM
# proxy's `codex-*` models. LiteLLM forwards those requests to CLIProxyAPI,
# which owns the ChatGPT/Codex OAuth session and translates tool calls. This
# avoids OpenClaw selecting its Codex app-server runtime. (It also avoided the
# separate litellm-chatgpt Responses provider's independently managed OAuth state,
# retired 2026-08-06.)

resource "litellm_key" "openclaw" {
  key_alias = "openclaw"
  models    = concat(local.codex_client_models, local.embedding_client_models)
  metadata = {
    consumer = "openclaw"
  }
}

# CLEANUP(added 2026-07-29): drop agent-lab from both reflection namespace lists
#   after 2026-07-30, together with cluster/k8s/agents/agent-lab. The lab drives
#   experiment agents through this same Codex-subscription lane rather than
#   minting a second virtual key for a namespace that is about to be deleted.
resource "kubernetes_secret" "openclaw" {
  metadata {
    name      = "litellm-key-openclaw"
    namespace = "litellm"
    annotations = {
      description                                                     = "Legacy OpenClaw LiteLLM virtual key retained for agent-lab experiments"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "agent-lab"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "agent-lab"
    }
  }

  data = {
    api-key = litellm_key.openclaw.key
  }
}

# ============================================================================
# public-coder-agent — second OpenClaw agent, same Codex-subscription lane
# ============================================================================
# The coder agent at public-coder-agent.allegedly.works runs the same harness
# against the same `codex-*` models, but gets its own virtual key rather than
# sharing openclaw's: usage is then attributable per agent, and either can be
# revoked without taking the other down. Deleting this key is its kill switch.

resource "litellm_key" "public_coder_agent" {
  key_alias = "public-coder-agent"
  # Both lanes: the Codex subscription models and the z.ai GLM models, so the
  # agent can be switched between them without reissuing credentials.
  # Embeddings ride along because OpenClaw's memory index needs a backend and
  # this agent has no route to api.openai.com -- its egress allowlist is git
  # hosting plus package indexes, and it should not gain one merely to embed.
  models = concat(local.codex_client_models, local.zai_lane_models, local.embedding_client_models)
  metadata = {
    consumer = "public-coder-agent"
  }
}

resource "kubernetes_secret" "public_coder_agent" {
  metadata {
    name      = "litellm-key-public-coder-agent"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for the public-coder-agent OpenClaw instance (Codex subscription models through CLIProxyAPI only)"
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
#  - model_aliases: rewrite the real Claude deployments (which ARE in
#    model_list and would otherwise reach real Anthropic) to GLM.
#  - router_settings.fallbacks [{"*": [...]}]: any claude-* slug NOT in
#    model_list (future or retired Anthropic slugs, etc.) hits
#    NotFoundError and falls back to GLM — zero maintenance on new versions.
# Non-claude/non-GLM slugs are still blocked by the key's models allowlist
# (claude-* + glm-*-anthropic), so z.ai-only containment holds.
resource "litellm_team" "zai_clients" {
  team_alias = "zai-clients"
  model_aliases = {
    "claude-opus-5"             = "glm-5.2-anthropic"
    "claude-sonnet-5"           = "glm-5.2-anthropic"
    "claude-fable-5"            = "glm-5.2-anthropic"
    "claude-haiku-4-5-20251001" = "glm-5.2-anthropic"
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

# ============================================================================
# tana-clients — scoped key for laptop tana-claude (Tana-UI models via tana-litellm)
# ============================================================================
# Pattern-B pinned key (like zai-clients): value in a git SOPS file in this module dir,
# decrypted with the reused litellm-zai-clients narrow age key (the existing tf-runner
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
# Same Pattern-B pinned key (like zai-clients / tana-clients): value in a git SOPS file
# in this module dir, decrypted with the reused litellm-zai-clients narrow age key. The
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
# trusted personal dev sandboxes, one key per LLM lane (zai below, codex
# further down) — deliberately no budget caps (operator-only consumers);
# deleting a lane's key resource is that lane's LLM kill switch.
resource "litellm_key" "agent_workspaces" {
  key_alias = "agent-workspaces"
  models    = local.zai_lane_models
  metadata = {
    consumer = "agent-workspaces sandboxes"
  }
}

# Reflected into agent-workspaces, where the workspace SandboxTemplate reads it
# as ANTHROPIC_AUTH_TOKEN (base URL points at this LiteLLM).
resource "kubernetes_secret" "agent_workspaces_key" {
  metadata {
    name      = "litellm-key-agent-workspaces"
    namespace = "litellm"
    annotations = {
      description                                                     = "LiteLLM virtual key for disposable agent workspaces (GLM models only); reflected into agent-workspaces for the workspace SandboxTemplate"
      "reflector.v1.k8s.emberstack.com/reflection-allowed"            = "true"
      "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces" = "agent-workspaces"
      "reflector.v1.k8s.emberstack.com/reflection-auto-enabled"       = "true"
      "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"    = "agent-workspaces"
    }
  }

  data = {
    api-key = litellm_key.agent_workspaces.key
  }
}

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
