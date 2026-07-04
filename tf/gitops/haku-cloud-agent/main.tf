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

# SYNC: the shared bits below — `model`, and the `tana-ro` + `gmail-labeling` MCP
# servers/toolsets (URLs + always_allow) — are duplicated in the self-hosted surface,
# <../../../haku/runtime/managed_agent/self_hosted/haku.agent.yaml> (YAML applied via `ant`).
# No SSOT (this tofu module can't read repo-wide files from the runner), so keep the two in
# step by hand. Cloud-only here: kubectl-machine + grocy-sf MCPs and networking.
resource "claude-managed-agents_agent" "haku_cloud" {
  name  = "haku-cloud"
  model = "claude-sonnet-4-6" # TEMP(bring-up): revisit (opus) once the cloud runtime is proven. SYNC model with self_hosted/haku.agent.yaml.

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

    You have READ-ONLY access to the operator's Tana knowledge base via the
    `tana-ro` MCP server (search_nodes, read_node, get_children, list_tags, …).
    Use it to look things up in Tana. Write tools are not exposed; never attempt
    to edit, move, or create Tana nodes.

    You may organize the operator's Gmail with labels under `haku/` via the
    `gmail-labeling` MCP server — your ONE sanctioned write to the world. The server
    confines every operation to that namespace (nothing outside `haku/`, never message
    content), so use it freely within that bound; everything else in Gmail you only read.

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
    # Read-only Tana facade (mcp-oauth-facade, tana-mcp-ro): exposes only read
    # tools (search_nodes/read_node/get_children/…); the Tana PAT is injected
    # server-side and every write tool is hidden. Bearer-gated by haku_tana_ro.
    {
      type = "url"
      name = "tana-ro"
      url  = "https://tana-mcp-ro.allegedly.works/mcp"
    },
    # gmail-labeling: Haku's ONE sanctioned world-write. The server confines every
    # operation to labels under `haku/` by construction (closure invariant, enforced
    # server-side before any Gmail call) — nothing outside the prefix, never message
    # content — so always_allow is safe; the server, not in-agent gating, is the fence.
    # Bearer-gated by haku_gmail_labeling (below).
    {
      type = "url"
      name = "gmail-labeling"
      url  = "https://gmail-labeling.allegedly.works/mcp"
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
    # tana-ro toolset — read-only by construction: the facade exposes only the
    # read allowlist and rejects every write tool, so always_allow is safe.
    {
      type            = "mcp_toolset"
      mcp_server_name = "tana-ro"
      default_config = {
        permission_policy = { type = "always_allow" }
      }
    },
    # gmail-labeling toolset — write, but bounded by construction: the server confines
    # every op to `haku/` labels (closure invariant), so always_allow is safe. Haku's
    # one sanctioned world-write; the server is the fence, not in-agent gating.
    {
      type            = "mcp_toolset"
      mcp_server_name = "gmail-labeling"
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

# The grocy-sf token Secret, published by the rotator (haku-grocy entry) and applied
# by THIS module's kustomization (haku-grocy-sf-token.sops.yaml) — so it is always
# present when this applies, exactly like kube_token above. Same hard
# kubernetes_secret_v1 read: a tolerant kubernetes_resources lookup would need
# cluster-wide CRD read for GVK discovery (tf-runner-role grants only secrets +
# leases), not worth a chart-level RBAC grant for an always-committed secret.
data "kubernetes_secret_v1" "grocy_token" {
  metadata {
    name      = "haku-cloud-grocy-sf-token"
    namespace = "flux-system"
  }
}

# Read-only grocy-sf bearer, bound to the grocy-sf MCP URL. token write-only;
# token_wo_version = the JWT exp epoch so each rotation re-sends (as haku_kube).
resource "claude-managed-agents_vault_credential" "haku_grocy" {
  vault_id     = claude-managed-agents_vault.haku_cloud.id
  display_name = "haku grocy-sf bearer (read-only, grocy-sf MCP)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://grocy-mcp-sf.allegedly.works/mcp"
    token            = data.kubernetes_secret_v1.grocy_token.data["jwt"]
    token_wo_version = tonumber(data.kubernetes_secret_v1.grocy_token.data["token-exp"])
  }
}

# The tana-mcp-ro facade's static client bearer, read straight from its owning
# namespace (tana-mcp/haku-tana-ro-token, key "token") — the tf-runner has
# cluster-wide secret read, so no reflected copy is needed. This is the same
# Secret the facade itself validates (MCP_FACADE_CLIENT_AUTH__STATIC_BEARER) and
# that reflector mirrors to haku-sandbox for the self-hosted worker.
data "kubernetes_secret_v1" "tana_ro_token" {
  metadata {
    name      = "haku-tana-ro-token"
    namespace = "tana-mcp"
  }
}

# Read-only Tana bearer, bound to the tana-mcp-ro URL. The token is static (no
# exp), so token_wo_version is derived from its content hash: a re-mint changes
# the digest and re-sends, a no-op apply keeps it stable. (12 hex digits stays
# within float64's safe-integer range.)
resource "claude-managed-agents_vault_credential" "haku_tana_ro" {
  vault_id     = claude-managed-agents_vault.haku_cloud.id
  display_name = "haku tana bearer (read-only, tana-mcp-ro facade)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://tana-mcp-ro.allegedly.works/mcp"
    token            = data.kubernetes_secret_v1.tana_ro_token.data["token"]
    token_wo_version = parseint(substr(sha256(data.kubernetes_secret_v1.tana_ro_token.data["token"]), 0, 12), 16)
  }
}

# The gmail-labeling MCP's static client bearer, read straight from its owning
# namespace (gmail-labeling/haku-gmail-labeling-token, key "token") — the tf-runner
# has cluster-wide secret read, so no reflected copy is needed. This is the same
# Secret the server validates (STATIC_BEARER) and that reflector mirrors to
# haku-sandbox for the Claude-web + self-hosted runtimes.
data "kubernetes_secret_v1" "gmail_labeling_token" {
  metadata {
    name      = "haku-gmail-labeling-token"
    namespace = "gmail-labeling"
  }
}

# Write-capable but bounded: the gmail-labeling server confines every op to `haku/`
# labels by construction. Bound to the gmail-labeling MCP URL. The token is static
# (no exp), so token_wo_version is its content hash — a re-mint re-sends, a no-op
# apply stays stable (as haku_tana_ro).
resource "claude-managed-agents_vault_credential" "haku_gmail_labeling" {
  vault_id     = claude-managed-agents_vault.haku_cloud.id
  display_name = "haku gmail-labeling bearer (managed labels under haku/)"

  auth = {
    type             = "static_bearer"
    mcp_server_url   = "https://gmail-labeling.allegedly.works/mcp"
    token            = data.kubernetes_secret_v1.gmail_labeling_token.data["token"]
    token_wo_version = parseint(substr(sha256(data.kubernetes_secret_v1.gmail_labeling_token.data["token"]), 0, 12), 16)
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
