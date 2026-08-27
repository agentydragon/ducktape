# Cluster Decision Record

Standing decisions, invariants, and retirement records for the cluster — each
entry states the decision or constraint as it binds today. Open work lives in
<plan.md>.

## CNI: Cilium with VXLAN

VXLAN tunnel mode (UDP 8472). OVH and Proxmox nodes are not on the same L2; native
routing fails. The full network layering and MTU model (pod 1370 → Cilium VXLAN →
`nebula1` 1420 → `eno1` 1500, including the Cilium-`MTU`-is-underlay gotcha) lives in
<network.md>.

## Storage Strategy

Public-critical services run on distributed services (SeaweedFS, CNPG
Postgres) backed by OVH storage; storage-heavy services that tolerate home
downtime use Proxmox storage.

| Location | Services                                                               | Rationale                         |
| -------- | ---------------------------------------------------------------------- | --------------------------------- |
| OVH      | Authentik, Grafana, Gateway, DNS, cert-mgr                             | Always-on, critical path          |
| Home     | Ollama                                                                 | Storage-heavy, tolerates downtime |
| OVH      | SeaweedFS, attic-db, Forgejo, Nix cache chunks + Loki/Mimir/Tempo (S3) | Replicated across 2 kimsufi nodes |

CNPG: individual clusters per app. Two profiles: OVH-HA (2 instances, OVH
kimsufi) and Proxmox-single (1 instance). See <cnpg_conventions.md>.

## OVH-Only Resilience Invariants

**Rule**: These services MUST work with OVH only (Proxmox completely down). No
Proxmox-pinned storage (`lvm-proxmox-*`, `local-path-proxmox`) or Proxmox-pinned
workloads.

| Service   | Status | Storage            | Notes                                                         |
| --------- | ------ | ------------------ | ------------------------------------------------------------- |
| DNS       | OK     | `local-path`       | CNPG on OVH/HIL nodes                                         |
| Website   | OK     | None (stateless)   |                                                               |
| Ingress   | OK     | None (hostNetwork) | Cilium Gateway on OVH                                         |
| Authentik | OK     | `local-path`       | All components pinned                                         |
| Grafana   | OK     | CNPG OVH-HA        | grafana-operator managed, JWT auth, no admin creds dependency |

**Compliance checklist** for critical-path changes:

1. No Proxmox-pinned PVCs (`lvm-proxmox-*`, `local-path-proxmox`) in dependency chain
2. No `topology.kubernetes.io/region: proxmox` affinity
3. Can schedule on OVH nodes
4. All upstream dependencies also pass 1-3

**Proxmox-dependent services** (tolerate downtime by design): Nix cache,
BuildBuddy, Ollama, InvenTree, ActivityWatch.

### Proxmox CSI removed (2026-07-16)

Proxmox CSI (`proxmox-csi-retain`) is gone. Primary reason: it hotplugs each PV as a
virtual SCSI disk onto the VM, capping PVs-per-node (`max-volume-attachments = 29`) —
too low, and OpenEBS LVM has no such limit. It had also started crash-looping after
the network topology change broke its path to the Proxmox API, and with a single
physical Proxmox host LVM-local storage has the same failure domain anyway. Rationale
and full context: <lessons_learned/2026_07_16_disable_proxmox_csi.md>. All consumers
now use `lvm-proxmox-hdd`.

The `lifecycle { ignore_changes = [disk] }` rule on the wyrm2 VM (only there to avoid
fighting CSI-hotplugged disks) has been removed, so tofu manages all wyrm2 disks
declaratively again. The `disk` blocks in `terraform/main/proxmox-vms.tf` are now the
authoritative shape — the next `bazel run //cluster:bootstrap` re-plans them, so eyeball
that plan for unexpected resizes/deletions (e.g. any CSI-retained orphan disks) before
applying.

## Cilium Gateway API `Programmed=False`

Upstream bug [cilium/cilium#42786](https://github.com/cilium/cilium/issues/42786):
hostNetwork mode leaves the Gateway status as `Programmed=False` /
`AddressNotAssigned`, even when Cilium has generated the `CiliumEnvoyConfig`,
Envoy listeners are present on the selected nodes, and routes work — traffic
flows through the hostNetwork Envoy listeners.

**Decision**: keep the hostNetwork Gateway plus Route 53 wildcard/apex records
pointing directly at the public OVH Kubernetes node IPs. Do not alert solely on
`Programmed=False` for `gateway-system/cluster-gateway`; use blackbox probes
against the public node IPs and Cilium/Envoy programming signals instead.
Setting `Gateway.spec.addresses` to static OVH node IPs is not a real
replacement for a routed VIP/LB; it may help status only if Cilium supports
that shape, but it would not add failover or change internet routing.

Revisit only if Cilium fixes hostNetwork Gateway status or if we introduce a
normal external exposure layer. More normal Cilium exposure models, if we
decide to leave hostNetwork mode:

1. Provider-managed load balancer: public OVH LB IP -> Kubernetes
   `LoadBalancer`/`NodePort` Service -> Cilium service handling -> Envoy ->
   backends. This is the ordinary "intermediate entity" model: the provider owns
   the routed public IP and health/failover behavior.
2. Provider-routed or floating VIPs: OVH Additional IP / routed address block ->
   Cilium `CiliumLoadBalancerIPPool` assigns Service IPs -> Cilium BGP or L2
   announcement advertises them. LB-IPAM only allocates IPs; it does not make
   arbitrary public addresses reachable unless the provider network routes them
   to us.
3. External LB to NodePort: a provider or self-hosted load balancer targets node
   ports, while Cilium still handles the in-cluster service path.

If we move to one of these, disable `gatewayAPI.hostNetwork.enabled` and let the
generated Gateway Service be the externally exposed object.

## Secrets: SOPS SSOT

All secrets are SOPS (age-encrypted in git, decrypted by Flux). ESO is still installed
but only with the Kubernetes provider, mirroring a small number of secrets cross-namespace
(authentik, agent sandboxes, openhands). Stakater Reloader restarts pods on
changes. Vault was decommissioned 2026-04-19 — see
<../archive/2026_04_19_vault_migration.md>.

## Google OAuth Client redirect URIs (blocked upstream)

The Google Cloud OAuth client backing Authentik's "Sign in with Google"
source (client_id `230253529789-…`, referenced from
`tf/gitops/sso-providers/source_google.tf`) is hand-managed in the
GCP Console. Every new forward-auth app needs its callback URI
(`https://<app>.allegedly.works/source/oauth/callback/google/`)
appended to the client's Authorized redirect URIs by hand — a
`redirect_uri_mismatch` is the symptom on first sign-in. The
behaviour itself is intentional in Authentik
(<https://github.com/goauthentik/authentik/issues/19883> closed as
not-planned); standalone proxy outposts run flows on the proxied
domain. Domain-Level forward-auth would centralise the callback but
sacrifices per-app group restrictions
(<https://docs.goauthentik.io/add-secure-apps/providers/proxy/forward_auth/>),
which is the entire reason for using forward-auth here.

**Why this isn't behind GitOps yet:** Web Application OAuth 2.0
Client IDs in GCP have no public CRUD API
(<https://issuetracker.google.com/issues/116182848>, filed 2018),
hence no Terraform resource
(<https://github.com/hashicorp/terraform-provider-google/issues/6074>).
`google_iap_client` is IAP-only; `google_iam_oauth_client` is
Workforce Identity Federation-only — neither covers the type of
client in use. Migrating to one of those products to unblock GitOps
is a much larger lift than the manual click.

Hit list when this changes:

- [ ] Watch <https://issuetracker.google.com/issues/116182848> for a
      public OAuth-client management API. When it lands and the
      Terraform provider gains a resource, move the client and its
      redirect URIs to TF.
- [ ] Until then: record per-app callback URI additions in the app's
      commit message so future audits can rebuild the list from git.

## Kubeconfig Endpoints (Current State)

| Consumer                      | Endpoint                    | Mechanism                            |
| ----------------------------- | --------------------------- | ------------------------------------ |
| Talos nodes (kubelet)         | `localhost:7445`            | KubePrism (built-in Talos API proxy) |
| NixOS workers (wyrm2, rugged) | `localhost:7445`            | haproxy -> all CP Nebula IPs         |
| TF state / `cluster/.envrc`   | `terraform/main/kubeconfig` | File written by OpenTofu             |
| `~/.kube/config` (wyrm2)      | `localhost:7445`            | Via local haproxy                    |

`api.allegedly.works` exists on port 443 behind the cluster Cilium Gateway
(TLSRoute in `k8s/kube-api-proxy/` with TLS passthrough to the `kubernetes`
Service). This is what Claude Code web sandboxes use to reach the k8s API —
Anthropic's egress proxy only allows port 443 outbound, so a non-standard port
would not work. TLS passthrough preserves client certificates for x509 auth.

## OpenTofu State Backend

All 6 former TF roots consolidated into a single root at `cluster/terraform/main/` with
PG backend (CNPG `tofu-state-db`, schema `main`, OVH local-path). Backup CronJob
removed 2026-06-01; will be restored via CNPG replication once wyrm2 is back.

All in-cluster tofu-controller `Terraform` CRs also use the PG backend (one schema per CR
in the same `tofu-state-db` cluster; reflector mirrors PG creds into `flux-system`).
PG advisory locks auto-release on runner-pod death — no more stale-Lease problem.

Zero `terraform_remote_state` dependencies — everything is in the same root. Persistent-auth
resources have `lifecycle { prevent_destroy = true }`. Bootstrap uses targeted applies
(`-target`) instead of separate directories. Single `proxmox` provider using
`PROXMOX_VE_API_TOKEN` env var (`root@pam`).

**Access**: From k8s workers (wyrm2, rugged), `.envrc` auto-detects ClusterIP and connects
directly — no port-forward. From non-workers, fall back to `kubectl port-forward`.

## GitHub Webhook Reconciliation

Flux `Receiver` at `flux-webhook.allegedly.works`. GitHub webhook registered by
`flux-webhook-token` Terraform (`github_repository_webhook.flux_receiver`).

## InvenTree API Token Provisioning

Via `inventree-token-provisioner` Job. SOPS-managed sandbox-agent password ->
Job execs into pod, creates user via Django ORM -> `inventree-api-token` Secret
in `claude-sandbox`.

## CPU limits policy (VPA)

For workloads with expensive cold starts (Python/JVM),
use `"controlledValues":"RequestsOnly"` so VPA only sets CPU requests and never
adds a CPU limit. CFS CPU limits are a hard rate limiter enforced by cgroups
regardless of node load — a pod capped at 60m gets 60ms/s of CPU even on a
completely idle node. Removing the CPU limit lets cold-start bursts use idle
capacity; when the node is contended, CFS shares CPU proportionally to requests
(compressible resource — no pod is killed). Memory limits remain useful
(`RequestsAndLimits`) since memory is incompressible.

See `cluster/k8s/agents/tana-mcp-facade/deployment.yaml` for a working example
(fastmcp takes ~6 CPU-seconds to import; at 60m limit this costs 100s wall time).

## Dropped Services

- **Capacitor** (2026-03-22): Removed. `ghcr.io/gimlet-io/capacitor-next` requires a
  proprietary license key despite the Apache 2.0 source license — the license check is
  injected in Gimlet's private build pipeline, not in the published source. Weave GitOps
  (the main alternative with dep graph visualization) has had no stable release since
  2023-12-06. Use Headlamp + `flux` CLI instead.
- **Kagent** (2026-07-21): Retired after noisy MCP results repeatedly exceeded the z.ai
  prompt limit and killed sessions. Kagent had no client-side tool-output budget, and its
  between-turn compaction could not prevent a single turn from overflowing context. See
  <../archive/2026_07_kagent/README.md>.

## Suspended Kustomizations

### Intentionally parked

Independent of the Proxmox outage — these stay suspended until explicitly revived,
and would **not** come back just because `atlas`/`wyrm2` returns.

- **ARC**: `arc-namespace`, `arc` — decommissioned 2026-04-11; GitHub runner
  pod/statefulset removed, resources deleted (`arc-secrets` still deployed).
- **BuildBuddy Executor**: `buildbuddy-executor` — scaled to 0; re-enable when needed.
- **Docker CI**: `docker-ci` — parked.
- **Firecrawl**: `firecrawl-{namespace,db,app}` — parked.
- **Google Workspace MCP**: `google-workspace-mcp` — parked 2026-05-13; resources + PVC deleted.
- **LiteLLM ChatGPT sub-instance** (`litellm-chatgpt`): scaled to 0 and then deleted on 2026-08-06,
  superseded by [CLIProxyAPI](../k8s/cli-proxy-api/README.md). It was a second LiteLLM
  Deployment inside the `litellm` namespace holding its **own** ChatGPT/Codex OAuth
  session on a PVC, serving the `*-chatgpt` models over the Responses API so Codex CLI
  could use the Codex subscription. CLIProxyAPI arrived later for a different client —
  Claude Code, which needs Anthropic-shaped tool calls that LiteLLM's Responses bridge
  mistranslates.
  [#3198] gave it its own single-replica `Recreate` deployment because LiteLLM builds the
  ChatGPT authenticator during ASGI startup: an expired token sends that call into the
  device-code flow, the pod never binds its port, and it dies to its own startup probe —
  which in the main proxy took Ollama, z.ai, Anthropic, Groq and Gemini down too. That
  split worked and is not why this was retired.
  **What stayed painful was re-authenticating it.** LiteLLM has no in-place login, so a
  new token meant: run `codex login` interactively somewhere with a browser, reshape the
  result into LiteLLM's _different_ flat `auth.json` schema (see the seed Secret's own
  description — "flattened from a Codex `codex login`"), SOPS-encrypt it, commit, push,
  wait for Flux, and then rely on an init-container guard to actually replace the copy on
  the PVC. That guard was wrong twice: `[ -f auth.json ]` could not replace a half-written
  file holding only `device_code_requested_at` ([#3199]), and its replacement
  `grep -q '"refresh_token"'` could not replace a present-but-revoked token (2026-08-06,
  ~3h of crashlooping). Both tested the file's shape; neither could test whether the
  credential works, which is the only property that decides whether the pod starts.
  CLIProxyAPI replaces that whole ritual with one command against the running pod
  (`-codex-device-login`), writing straight to its PVC, picked up by a file watcher
  without a restart and kept alive by a 15m refresh worker. Note it has SOPS secrets too
  (`config.sops.yaml`, `client-key.sops.yaml`) — but those hold the **inbound** client
  key, i.e. how LiteLLM authenticates _to_ it, which is static and rotates by editing two
  files. The **upstream** ChatGPT OAuth session is never in git there; its init container
  only does `mkdir -p /data/auth`. That is the whole difference: a credential that
  rotates on use and can only be minted interactively is a bad fit for a checked-in
  secret, which is why the copy-if-absent guard kept being wrong.
  CLIProxyAPI also serves `/v1/responses` directly, so it can front Codex CLI as well as
  Claude Code and this instance buys nothing.

  [#3198]: https://github.com/agentydragon/ducktape/pull/3198
  [#3199]: https://github.com/agentydragon/ducktape/pull/3199

  Deleted: the Deployment, Service, PVC, ServiceMonitor, auth-seed Secret, Gatus check,
  and the 6 `*-chatgpt` entries in `k8s/litellm/app/proxy-config.yaml`.

  The `*-chatgpt` model names came back the same day on a working backend: LiteLLM now
  serves them over CLIProxyAPI's native `/v1/responses` via the `openai/` provider, which
  is a passthrough rather than a bridge. The baked Codex configs
  (<../k8s/agents/agent-sandbox/workspace-image/codex-config.toml>,
  <../../x/codex_pod_image/home.nix>) and `oai_lane_models` in
  <../../tf/gitops/litellm-keys/main.tf> pin those names, so nothing downstream needed a
  rebuild. The `codex-*` entries stay on `anthropic/` → `/v1/messages` for Claude Code;
  the two lanes are the same pod reached through the wire each client speaks natively.

- **Harbor**: **removed 2026-08-11** (#3967) after two months parked — it was mostly a
  registry for props, which use the Forgejo registry; the replacement track is the
  `oci-cache` lighter-registry item in <plan.md>.
- **InvenTree**: `inventree-{namespace,secrets,token-provisioner}`,
  `authentik-blueprint-inventree-secret` — nice-to-have, parked under capacity pressure.
- **OpenClaw / OpenShell**: **removed 2026-07-31**, manifests deleted rather than
  parked. The gateway was unused and wedged (no exec traffic, idle orphaned
  sandboxes), the operator could not be egress-confined
  (`plans/personal_agents/findings/` F3), and `public-coder-agent` is now the
  reference agent — same OpenClaw image, plain Deployment, `sandbox.mode: "off"`.
  The `ghcr.io/agentydragon/openclaw` ImageRepository/ImagePolicy are deliberately
  **kept**: that image is what `public-coder-agent` runs. The former
  `openclaw-gateway` and `openclaw-sandbox` namespaces were retired after their
  retained credentials moved to `agents/shared-secrets`; see
  <../archive/2026_08_openclaw_namespace_retirement.md>. Rationale and the evaluated
  alternatives: `plans/personal_agents/verdicts.md`.
- **OpenHands**: `openhands`, `openhands-{namespace,secrets,sandboxes}` — experimental, not
  currently used.
- **Tandoor**: `tandoor`, `tandoor-{db,namespace}` — using Grocy instead.
- **claude-sandbox-firecracker** — parked to free resources.
- **listing-monitor-smoke** — parked. (thrive-scraper was un-parked by moving it to
  git-based storage in Forgejo — no PVC, no wyrm2 pinning; see gaffer-private
  `x/thrive_scrape/DESIGN.md`.)

### Still down

- **sdr** — suspended pending the radio re-set-up post-relocation.
