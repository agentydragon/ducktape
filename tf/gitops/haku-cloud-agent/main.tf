# Haku's Anthropic-hosted (cloud) Managed Agent, managed declaratively via the
# claude-managed-agents provider. Anthropic runs the agent loop and the sandbox;
# Haku reaches the operator's cluster through the kubectl-machine-mcp passthrough
# MCP (cluster/k8s/agents/kubectl-machine-mcp), authenticated by a static_bearer
# vault credential carrying the haku-k8s Authentik JWT. The MCP forwards the JWT
# to kube-apiserver, which maps groups:["haku"] -> oidc-ksbx-groups:haku, so Haku
# gets full CRUD in haku-sandbox plus cluster-wide diagnostics read.
#
# Supersedes the imperative anthropic_hosted/provision.sh bring-up (Path B,
# bash+curl+env-var KUBE_TOKEN), which the provider couldn't model.
#
# CREDENTIAL OWNERSHIP: Terraform owns the environment, agent, vault shell, and
# deployment. The vault's static_bearer credential (the rotating haku JWT) is
# seeded and refreshed OUT OF BAND — the haku JWT rotates ~every 44 days and the
# provider's write-only token can't track that. Per anthropic_hosted/PLAN.md
# ("static_bearer + rotation CronJob"), the authentik-jwt-rotation CronJob will
# own it (follow-up). Until then, seed it once against the TF-created vault:
#
#   ant beta:vaults:credentials create --vault-id "$(tofu output -raw vault_id)" <<'YAML'
#   display_name: haku k8s bearer (kubectl-machine-mcp)
#   auth:
#     type: static_bearer
#     mcp_server_url: https://kubectl-machine-mcp.allegedly.works/mcp
#     token: <sops -d --extract '["jwt"]' secrets/haku-k8s-jwt.yaml>
#   YAML

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

    IMPORTANT (v0 bring-up): your operating manual and run procedure are not wired
    yet. Do exactly what each user message asks, then stop.
  EOT

  mcp_servers = [
    {
      type = "url"
      name = "kubectl-machine"
      url  = "https://kubectl-machine-mcp.allegedly.works/mcp"
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
  ]
}

# Vault shell. The static_bearer credential inside it is seeded/rotated out of
# band (see the header comment) — Terraform does not manage it.
resource "claude-managed-agents_vault" "haku_cloud" {
  display_name = "haku-cloud"
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
