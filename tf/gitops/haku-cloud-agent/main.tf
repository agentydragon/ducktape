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

resource "claude-managed-agents_agent" "haku_cloud" {
  name  = "haku-cloud"
  model = "claude-sonnet-4-6" # TEMP(bring-up): revisit (opus) once the cloud runtime is proven

  system = <<-EOT
    You are Haku, the operator's tireless background executive assistant, running
    in an Anthropic-hosted sandbox.

    You reach the operator's Kubernetes cluster through the `kubectl-machine`
    MCP server (tools prefixed `pods_`, `resources_`, `events_`, …). Your access
    is scoped by the bearer token the platform injects: full CRUD in the
    `haku-sandbox` namespace (create/exec/delete pods for ephemeral compute) and
    cluster-wide read for diagnostics. Spin ephemeral pods in `haku-sandbox` to
    do in-cluster work (Plaid, in-cluster MCPs, git), then clean them up.

    You also have READ-ONLY access to the operator's grocy stock/pantry via the
    `grocy-sf` MCP server. Use it to answer questions about what's in stock,
    expiring, or shopping-list-worthy. Writes are rejected server-side (403) —
    never try to mutate stock.

    IMPORTANT (v0 bring-up): your operating manual and run procedure are not wired
    yet. Do exactly what each user message asks, then stop.
  EOT

  mcp_servers = [
    {
      type = "url"
      name = "kubectl-machine"
      url  = "https://kubectl-machine-mcp.allegedly.works/mcp"
    },
    # Read-only grocy-sf (the operator's pantry/stock). Always declared; it only
    # works when the grocy-sf vault credential exists (see haku_grocy below). If the
    # rotator hasn't published the token Secret, the credential is absent and these
    # tools just fail to authenticate — the accepted fallback, not a hard error.
    {
      type = "url"
      name = "grocy-sf"
      url  = "https://grocy-mcp-sf.allegedly.works/mcp"
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
    # grocy-sf toolset — read-only by construction: the `haku` Grocy user has empty
    # permissions, so the Grocy API serves reads (200) and rejects every write
    # (403). always_allow is therefore safe; the server-side ACL is the fence.
    {
      type            = "mcp_toolset"
      mcp_server_name = "grocy-sf"
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

# Tolerant read of the grocy-sf token Secret (published by the rotator's haku-grocy
# entry). Unlike kube_token — a hard kubernetes_secret_v1 whose absence aborts the
# whole apply — grocy is OPTIONAL: kubernetes_resources returns an empty list rather
# than erroring, so a missing Secret simply leaves grocy_enabled = false and skips
# the credential below. Never let an absent grocy token break the haku_kube
# credential. Secret .data is base64 (raw API object), so decode (kubernetes_secret_v1
# would have auto-decoded).
data "kubernetes_resources" "grocy_token" {
  api_version    = "v1"
  kind           = "Secret"
  namespace      = "flux-system"
  field_selector = "metadata.name==haku-cloud-grocy-sf-token"
}

locals {
  grocy_secret  = try(data.kubernetes_resources.grocy_token.objects[0], null)
  grocy_enabled = local.grocy_secret != null
}

# Read-only grocy-sf bearer, bound to the grocy-sf MCP URL. count gates on the
# Secret's presence (the accepted fallback: no Secret -> no grocy credential ->
# grocy tools just don't authenticate). token write-only; token_wo_version = exp
# epoch so each rotation re-sends (same pattern as haku_kube).
resource "claude-managed-agents_vault_credential" "haku_grocy" {
  count        = local.grocy_enabled ? 1 : 0
  vault_id     = claude-managed-agents_vault.haku_cloud.id
  display_name = "haku grocy-sf bearer (read-only, grocy-sf MCP)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://grocy-mcp-sf.allegedly.works/mcp"
    token            = base64decode(local.grocy_secret.data["jwt"])
    token_wo_version = tonumber(base64decode(local.grocy_secret.data["token-exp"]))
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
