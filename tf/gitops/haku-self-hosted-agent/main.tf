# Haku's SELF-HOSTED Managed Agent, managed declaratively via the
# claude-managed-agents provider. Supersedes the imperative `ant`-CLI bring-up
# (haku/runtime/managed_agent/self_hosted/provision.sh + haku.{environment,agent,
# deployment}.yaml, now retired): the agent loop runs at Anthropic, but tool
# execution runs in the haku-worker pod in haku-sandbox (the pod is the trust
# boundary — "Ember posture"). The worker (worker.py) polls this environment's
# work queue and services tool calls locally with agent_toolset_20260401.
#
# Sibling to tf/gitops/haku-cloud-agent (the Anthropic-SANDBOXED variant). Both
# are the same operator's Haku in one spend-capped Anthropic workspace; they keep
# separate environments/agents/vaults because their tool postures differ (this one
# runs bash in-pod; the cloud one reaches the cluster via kubectl-machine-mcp).
#
# CAVEAT (verify on first `tofu plan`): `tofu validate` passes (the env config +
# the deployment `schedule` block type-check against the provider schema), but the
# API may still reject the schedule's semantics at plan/apply. The schedule mirrors
# the working imperative haku.deployment.yaml cron; adjust if plan rejects it.
#
# CREATE is non-destructive: it provisions a NEW environment/agent/vault/deployment
# in parallel with the live imperative one. The running worker is unaffected until
# the deliberate cutover (new env id + regenerated env key) — see
# cluster/k8s/haku/self-hosted-agent-tf/README.md.

# api_key from ANTHROPIC_API_KEY (injected into the tofu-controller runner from
# the shared haku-cloud-anthropic-api-key Secret; see the Terraform CR).
provider "claude-managed-agents" {}

# Self-hosted: tool execution (and thus the real egress fence) is the haku-worker
# pod's haku-mitmproxy + CCNP, not an Anthropic sandbox. `networking` is moot here
# but the provider schema requires it, so mirror haku-cloud's `unrestricted`.
resource "claude-managed-agents_environment" "haku_selfhosted" {
  name = "haku-selfhosted"

  config = {
    type = "self_hosted"
    networking = {
      type = "unrestricted"
    }
  }
}

resource "claude-managed-agents_agent" "haku_selfhosted" {
  name  = "haku-selfhosted"
  model = "claude-sonnet-4-6" # TEMP(bring-up): restore claude-opus-4-8 once sessions run end-to-end

  # Thin pointer: behavior is single-sourced in the cloned base manual + run
  # procedure (the worker clones ducktape into the workdir at startup), so the
  # gmail-labeling grant, hard rules, etc. all live there — not duplicated here.
  system = <<-EOT
    You are Haku, the operator's tireless background executive assistant. A worker
    in your haku-sandbox namespace has cloned your home into the agent workdir
    (`/workspace`). Your file tools (read/glob/grep/edit/write) take paths
    RELATIVE to that workdir — absolute paths are rejected; only `bash` takes
    absolute paths. Your operating manual is at `ducktape/haku/base/instructions.md`
    and your run procedure at `ducktape/haku/run.md`; your haku-state checkout —
    your memory and primary write surface — is at `haku-state`, with git auth and
    in-cluster kubectl already in place.

    Read the manual and the run procedure, then execute the run procedure end to
    end. Each user message is a wake: do one scan pass, commit and push haku-state,
    then stop and wait for the next wake.
  EOT

  mcp_servers = [
    # Read-only Tana facade (tana-mcp-ro): read tools only; the PAT is injected
    # server-side and write tools are hidden. Bearer-gated by haku_tana_ro.
    {
      type = "url"
      name = "tana-ro"
      url  = "https://tana-mcp-ro.allegedly.works/mcp"
    },
    # gmail-labeling: Haku's ONE sanctioned world-write. The server confines every
    # operation to labels under `haku/` by construction (closure invariant, enforced
    # server-side before any Gmail call), so always_allow is safe; the server, not
    # in-agent gating, is the fence. Bearer-gated by haku_gmail_labeling.
    {
      type = "url"
      name = "gmail-labeling"
      url  = "https://gmail-labeling.allegedly.works/mcp"
    },
  ]

  tools = [
    # The fixed in-pod toolset (bash/read/write/edit/glob/grep) the worker supplies.
    # The pod is the trust boundary (Ember posture), so everything is auto-allowed.
    {
      type = "agent_toolset_20260401"
      default_config = {
        enabled           = true
        permission_policy = { type = "always_allow" }
      }
    },
    # tana-ro toolset — read-only by construction (facade exposes only the read
    # allowlist), so always_allow is safe.
    {
      type            = "mcp_toolset"
      mcp_server_name = "tana-ro"
      default_config = {
        permission_policy = { type = "always_allow" }
      }
    },
    # gmail-labeling toolset — write, but bounded by construction to `haku/` labels,
    # so always_allow is safe. Haku's one sanctioned world-write.
    {
      type            = "mcp_toolset"
      mcp_server_name = "gmail-labeling"
      default_config = {
        permission_policy = { type = "always_allow" }
      }
    },
  ]
}

resource "claude-managed-agents_vault" "haku_selfhosted" {
  display_name = "haku-selfhosted"
}

# The tana-mcp-ro facade's static client bearer, read from its owning namespace
# (tana-mcp/haku-tana-ro-token, key "token") — the tf-runner has cluster-wide
# secret read, so no reflected copy is needed. Static token (no exp), so
# token_wo_version is a content hash: a re-mint re-sends, a no-op apply stays stable.
data "kubernetes_secret_v1" "tana_ro_token" {
  metadata {
    name      = "haku-tana-ro-token"
    namespace = "tana-mcp"
  }
}

resource "claude-managed-agents_vault_credential" "haku_tana_ro" {
  vault_id     = claude-managed-agents_vault.haku_selfhosted.id
  display_name = "haku tana bearer (read-only, tana-mcp-ro facade)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://tana-mcp-ro.allegedly.works/mcp"
    token            = data.kubernetes_secret_v1.tana_ro_token.data["token"]
    token_wo_version = parseint(substr(sha256(data.kubernetes_secret_v1.tana_ro_token.data["token"]), 0, 12), 16)
  }
}

# The gmail-labeling MCP's static client bearer, read from its owning namespace
# (gmail-labeling/haku-gmail-labeling-token, key "token"). Same Secret the server
# validates and that reflector mirrors to haku-sandbox for the Claude-web runtime.
data "kubernetes_secret_v1" "gmail_labeling_token" {
  metadata {
    name      = "haku-gmail-labeling-token"
    namespace = "gmail-labeling"
  }
}

resource "claude-managed-agents_vault_credential" "haku_gmail_labeling" {
  vault_id     = claude-managed-agents_vault.haku_selfhosted.id
  display_name = "haku gmail-labeling bearer (managed labels under haku/)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://gmail-labeling.allegedly.works/mcp"
    token            = data.kubernetes_secret_v1.gmail_labeling_token.data["token"]
    token_wo_version = parseint(substr(sha256(data.kubernetes_secret_v1.gmail_labeling_token.data["token"]), 0, 12), 16)
  }
}

# The wake trigger: a scheduled deployment fires one fresh session per tick (the
# warm-session supervisor is deferred — haku-state is the durable memory, so a cold
# session just re-orients). Mirrors the retired haku.deployment.yaml.
resource "claude-managed-agents_deployment" "haku_selfhosted" {
  name           = "haku-selfhosted-scan"
  description    = "Haku's self-hosted agent: tool calls run in the haku-worker pod (haku-sandbox)."
  agent          = claude-managed-agents_agent.haku_selfhosted.id
  environment_id = claude-managed-agents_environment.haku_selfhosted.id
  vault_ids      = [claude-managed-agents_vault.haku_selfhosted.id]
  desired_status = "active"

  initial_events = [
    {
      type    = "user.message"
      content = jsonencode([{ type = "text", text = "Wake: do one scan pass per the run procedure, then commit, push, and stop." }])
    },
  ]

  schedule = {
    type       = "cron"
    expression = "0 */6 * * *"
    timezone   = "America/Los_Angeles"
  }
}
