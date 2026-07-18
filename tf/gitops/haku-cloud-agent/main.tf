# Haku's Anthropic-hosted (cloud) Managed Agent, managed declaratively via the
# claude-managed-agents provider. Anthropic runs the agent loop and the sandbox;
# Haku reaches the operator's cluster through the kubectl-machine-mcp passthrough
# MCP (cluster/k8s/agents/kubectl-machine-mcp), authenticated by a static_bearer
# vault credential carrying the haku-k8s Authentik JWT. The MCP forwards the JWT
# to kube-apiserver, which maps groups:["haku"] -> oidc-ksbx-groups:haku, so Haku
# gets full CRUD in haku-sandbox plus cluster-wide diagnostics read.
#
# Supersedes the retired imperative bring-up (Path B, bash+curl+env-var
# KUBE_TOKEN); see haku/runtime/managed_agent/anthropic_hosted/README.md.
#
# CREDENTIAL ROTATION: the static_bearer credential carries the haku-k8s Authentik
# JWT, which rotates ~every 44 days. The chain is fully automatic and the rotator
# stays dumb (it only mints + commits): authentik-jwt-rotation writes the token as
# the haku-cloud-kube-token k8s Secret -> Flux applies it -> tofu reads it
# in-cluster (kubernetes_secret_v1 data source, below) -> tofu re-sends it into
# the Anthropic vault. The token is a TF 1.11 write-only attribute, so it only
# re-sends when token_wo_version changes; we set that to the JWT's exp epoch
# (monotonic, bumps on every mint).

# api_key is read from the ANTHROPIC_API_KEY env var, injected into the
# tofu-controller runner from the haku-cloud-anthropic-api-key Secret (a
# dedicated, spend-capped Anthropic workspace; regular key, not admin).
provider "claude-managed-agents" {}

resource "claude-managed-agents_environment" "haku_cloud" {
  name = "haku-cloud"

  config = {
    type = "cloud"
    # v0: open egress — Haku reaches many domains (Gmail, Plaid, the MCP, …). The
    # cluster credential stays scoped regardless: it is only presented to the
    # kubectl-machine-mcp URL via the vault. TODO: tighten to `limited` with an
    # explicit allowed_hosts list once the data-source set is settled.
    networking = {
      type = "unrestricted"
    }
  }
}

# SYNC: `model` + full toolset are identical to the self-hosted agent
# (<../../../haku/runtime/managed_agent/self_hosted/haku.agent.yaml>). SSOT
# haku/base/agent_shared.yaml, parity enforced by //haku/base:test_agent_config_ssot
# — a red run shows any drift. The vault + both credentials below are shared:
# self-hosted references this vault via the haku-cloud-agent-ids output Secret.
resource "claude-managed-agents_agent" "haku_cloud" {
  name  = "haku-cloud"
  model = "claude-sonnet-4-6" # TEMP(bring-up): revisit (opus) once the cloud runtime is proven.

  system = <<-EOT
    You are Haku, the operator's tireless background executive assistant, running
    in an Anthropic-hosted sandbox.

    You reach the operator's Kubernetes cluster through the `kubectl-machine`
    MCP server (tools prefixed `pods_`, `resources_`, `events_`, …). Your access
    is scoped by the bearer token the platform injects: full CRUD in the
    `haku-sandbox` namespace (create/exec/delete pods for ephemeral compute) and
    cluster-wide read for diagnostics. Spin ephemeral pods in `haku-sandbox` to
    do in-cluster work (Plaid, in-cluster MCPs, git), then clean them up.

    You also have access to haku-console's MCP catalog via the `haku-console`
    MCP server — most relevantly READ-ONLY Tana and Grocy tools
    (`tana_rw_search_nodes`, `tana_rw_read_node`, `grocy_sf_stock_get`,
    `grocy_sf_products_list`, …), which auto-approve under the console's reviewed
    policy. Write tools on those servers (grocy_sf stock mutations, tana_rw node
    edits, and any other non-auto-approved console tool) route through the
    console's operator-approval queue instead of executing directly — never
    expect them to complete without a human clicking approve.

    IMPORTANT (v0 bring-up): your operating manual and run procedure are not wired
    yet. Do exactly what each user message asks, then stop.
  EOT

  mcp_servers = [
    {
      type = "url"
      name = "kubectl-machine"
      url  = "https://kubectl-machine-mcp.allegedly.works/mcp"
    },
    # haku-console's aggregated MCP catalog (Tana + Grocy read tools to start; also
    # grants reach to whatever else console exposes — Gmail/Calendar reads, osm, and every
    # approval-gated tool — gated by the console's own auto-approval/approval-queue
    # policy, not by anything here). Supersedes the standalone `tana-mcp-ro` facade
    # (Tana reads are now `tana-rw` tools allowlisted in the console's auto-approval
    # policy) and Haku's dedicated read-only grocy-sf credential (`grocy-mcp-haku-sf`):
    # console's existing grocy-sf catalog entry already exposed the same read tools,
    # plus approval-gated writes the direct credential could never reach.
    {
      type = "url"
      name = "haku-console"
      url  = "https://haku.allegedly.works/mcp"
    },
  ]

  tools = [
    # Cloud-sandbox built-ins (bash/read/write/edit/glob/grep) for orchestration
    # glue; the cluster fence is RBAC on the haku token, so auto-allow.
    {
      type = "agent_toolset_20260401"
      default_config = {
        enabled           = true
        permission_policy = { type = "always_allow" }
      }
    },
    # The kubectl MCP toolset — Haku's hands on the cluster.
    {
      type            = "mcp_toolset"
      mcp_server_name = "kubectl-machine"
      default_config = {
        permission_policy = { type = "always_allow" }
      }
    },
    # haku-console toolset — always_allow is safe here for the same reason it is
    # for kubectl-machine: the execution sandbox is not the trust boundary for this
    # server, the console's own approval queue is (auto-approved reads execute
    # immediately, everything else becomes a pending approval).
    {
      type            = "mcp_toolset"
      mcp_server_name = "haku-console"
      default_config = {
        permission_policy = { type = "always_allow" }
      }
    },
  ]
}

resource "claude-managed-agents_vault" "haku_cloud" {
  display_name = "haku-cloud"
}

# Read the rotator-published token in-cluster (tf-runner SA; kubernetes provider
# auto-configures in-cluster, as in tf/gitops/cpap-data). The tofu-controller's
# spec.vars don't resolve secretKeyRef, so we read the Secret here instead. The
# JWT lands in tfstate (sensitive) — same as cpap-data's data.kubernetes_secret;
# the credential's `token` is still write-only so it isn't re-emitted on read.
data "kubernetes_secret_v1" "kube_token" {
  metadata {
    name      = "haku-cloud-kube-token"
    namespace = "flux-system"
  }
}

# The haku-k8s Authentik JWT, bound to the kubectl-machine-mcp URL. Anthropic
# presents it when the agent connects to that MCP; the MCP forwards it to
# kube-apiserver (groups:["haku"] -> oidc-ksbx-groups:haku). token is write-only;
# token_wo_version = the JWT exp epoch so each rotation re-sends (see header).
resource "claude-managed-agents_vault_credential" "haku_kube" {
  vault_id     = claude-managed-agents_vault.haku_cloud.id
  display_name = "haku k8s bearer (kubectl-machine-mcp)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://kubectl-machine-mcp.allegedly.works/mcp"
    token            = data.kubernetes_secret_v1.kube_token.data["jwt"]
    token_wo_version = tonumber(data.kubernetes_secret_v1.kube_token.data["token-exp"])
  }
}

# haku-console's Agent bearer for the static "Haku" Agent slot, read straight from
# its haku-sandbox copy (tf/gitops/haku-state's kubernetes_secret.haku_console_agent_api
# writes the same value into both haku-sandbox and haku-console) — the tf-runner has
# cluster-wide secret read, so no separate reflected copy is needed here.
data "kubernetes_secret_v1" "haku_console_agent_api" {
  metadata {
    name      = "haku-console-agent-api"
    namespace = "haku-sandbox"
  }
}

# Bearer for haku-console's aggregated MCP catalog. The token is static (no exp),
# so token_wo_version is derived from its content hash: a re-mint changes the
# digest and re-sends, a no-op apply keeps it stable. (12 hex digits stays within
# float64's safe-integer range.)
resource "claude-managed-agents_vault_credential" "haku_console" {
  vault_id     = claude-managed-agents_vault.haku_cloud.id
  display_name = "haku console bearer (haku-console MCP catalog)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://haku.allegedly.works/mcp"
    token            = data.kubernetes_secret_v1.haku_console_agent_api.data["token"]
    token_wo_version = parseint(substr(sha256(data.kubernetes_secret_v1.haku_console_agent_api.data["token"]), 0, 12), 16)
  }
}

resource "claude-managed-agents_deployment" "haku_cloud" {
  name           = "haku-cloud"
  description    = "Haku's Anthropic-hosted agent: reaches the cluster via kubectl-machine-mcp."
  agent          = claude-managed-agents_agent.haku_cloud.id
  environment_id = claude-managed-agents_environment.haku_cloud.id
  vault_ids      = [claude-managed-agents_vault.haku_cloud.id]
  desired_status = "active"

  # v0/P0: the initial message is a connectivity test (list haku-sandbox pods).
  # Replace with the real wake ("do one scan pass per the run procedure, then
  # commit, push, stop") and add a `schedule` block once the run procedure is
  # wired. For now it is on-demand: `ant beta:deployments run`.
  initial_events = [
    {
      type    = "user.message"
      content = jsonencode([{ type = "text", text = "List the pods in the haku-sandbox namespace and report their names and status." }])
    },
  ]
}
