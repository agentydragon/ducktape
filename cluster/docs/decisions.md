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

| Location | Services                                                               | Rationale                                         |
| -------- | ---------------------------------------------------------------------- | ------------------------------------------------- |
| OVH      | Authentik, Grafana, Gateway, DNS automation, cert-mgr                  | Always-on, critical path                          |
| Home     | Ollama                                                                 | Storage-heavy, tolerates downtime                 |
| OVH      | SeaweedFS, attic-db, Forgejo, Nix cache chunks + Loki/Mimir/Tempo (S3) | Replicated across the OVH nodes (HDD ×3, NVMe ×2) |

CNPG: individual clusters per app. Two sanctioned profiles: OVH-HA (2 instances
pinned `zone: hil-ovh`, on `local-path-ovh` or `local-path-ovh-ssd`) and
Proxmox-single (1 instance, `local-path`). See <cnpg_conventions.md>. Known
deviation: `study-casino-db` runs 3 instances across OVH nodes.

## Control-plane scheduling

OVH Talos control-plane nodes carry the default
`node-role.kubernetes.io/control-plane:NoSchedule` taint. A workload may tolerate
that taint only as an explicit, owner-reviewed overflow exception in its GitOps
manifest. The exception uses `operator: Exists` with `effect: NoSchedule`; it does
not add a `NoExecute` toleration and does not remove the node taint.

An overflow exception must satisfy all of these rules:

- Keep the workload's OVH `region`/`zone` placement constraints explicit.
- Prefer non-control-plane nodes with soft node affinity on
  `node-role.kubernetes.io/control-plane` / `DoesNotExist`, so control planes are
  used only when ordinary worker placement cannot fit.
- Do not use `local-path-*`, `hostPath`, or `emptyDir` volumes. Those write to the
  node-local disk and can contend with etcd, especially on the HDD-backed
  `ovh-ns103656` control plane. Prefer stateless workloads or SeaweedFS-backed
  application PVCs instead.
- Record the workload-specific rationale next to the manifest's toleration and
  review any generated child pod template as part of the same change.

This is a scheduling fallback, not a general invitation to place application
workloads on control planes. The Langfuse web/worker pods and Paperless app are
explicit exceptions because their manifests use no local-path, hostPath, or
emptyDir storage.

## OVH-Only Resilience Invariants

**Rule**: These services MUST work with OVH only (Proxmox completely down). No
Proxmox-pinned storage (`lvm-proxmox-*`, `local-path-proxmox`) or Proxmox-pinned
workloads.

| Service   | Status | Storage            | Notes                                                                                                          |
| --------- | ------ | ------------------ | -------------------------------------------------------------------------------------------------------------- |
| DNS       | OK     | None in-cluster    | Zone is AWS Route 53; records reconciled by the `dns-records` Terraform CR (state in CNPG `tofu-state-db-ovh`) |
| Website   | OK     | None (stateless)   |                                                                                                                |
| Ingress   | OK     | None (hostNetwork) | Cilium Gateway on OVH                                                                                          |
| Authentik | OK     | OVH hdd tier       | CNPG `authentik-db-ovh` (OVH-HA); server + worker pinned to OVH                                                |
| Grafana   | OK     | OVH hdd tier       | CNPG `grafana-db-ovh` (OVH-HA); grafana-operator managed, JWT auth, no admin creds dependency                  |

The DB manifests still name `local-path-ovh` — the deprecated alias re-pinned to
the hdd tier (<cnpg_conventions.md> § R2).

**Compliance checklist** for critical-path changes:

1. No Proxmox-pinned PVCs (`lvm-proxmox-*`, `local-path-proxmox`) in dependency chain
2. No `topology.kubernetes.io/region: proxmox` affinity
3. Can schedule on OVH nodes
4. All upstream dependencies also pass 1-3

**Proxmox-dependent services** (tolerate downtime by design): Ollama,
ActivityWatch (central store on wyrm2, `local-path-proxmox`; device-side
importers buffer and re-push through downtime), and — both currently suspended —
BuildBuddy executor and InvenTree. Not the Nix cache: attic runs pinned to
`hil-ovh`, `attic-db` is CNPG OVH-HA, and chunks live in OVH SeaweedFS S3.

### Proxmox CSI removed (2026-07-16)

Proxmox CSI (`proxmox-csi-retain`) is gone. Two reasons: it hotplugs each PV as a
virtual SCSI disk onto the VM, capping PVs-per-node (`max-volume-attachments = 29`) —
too low, and OpenEBS LVM has no such limit; and only one k8s node runs on Proxmox any
more, so there is no reshuffling of PVCs between Proxmox nodes for a CSI to serve
(there was, when Proxmox hosted several nodes). It had also started crash-looping after
the network topology change broke its path to the Proxmox API, and with a single
physical Proxmox host LVM-local storage has the same failure domain anyway. Rationale
and full context: <lessons_learned/2026_07_16_disable_proxmox_csi.md>. All consumers
now use `lvm-proxmox-hdd`.

The `lifecycle { ignore_changes = [disk] }` rule on the wyrm2 VM is back since
2026-08-05 (tombstoned in `terraform/main/proxmox-vms.tf`): two orphaned CSI-era disks
(scsi1/scsi2) remain attached to the VM, and letting tofu reconcile the incomplete disk
state would remap live disks. Deliberately detach them, then delete the rule so the
declared `disk` blocks become authoritative again.

## Public Exposure: hostNetwork Gateway, No LB/VIP

**Decision**: the public `cluster-gateway` runs Cilium Gateway API in
`gatewayAPI.hostNetwork.enabled` mode — Envoy binds 80/443 directly on the OVH
nodes, and Route 53 wildcard/apex records point at those node IPs. There is no
provider `LoadBalancer`/VIP. Setting `Gateway.spec.addresses` to static node
IPs is not a substitute for one: it would not add failover or change internet
routing.

(Historical: upstream
[cilium/cilium#42786](https://github.com/cilium/cilium/issues/42786) left
hostNetwork Gateways reporting `Programmed=False`/`AddressNotAssigned` while
traffic worked, so Gateway status was suppressed from alerting for months;
fixed by [cilium/cilium#46350](https://github.com/cilium/cilium/pull/46350),
picked up here with the 1.19.6 upgrade (#4912) — Gateway status is trustworthy
again.)

Revisit if we introduce a normal external exposure layer: with Route 53
pointing directly at node IPs, a downed node's share of packets is simply lost
until the record set is edited — the hostNetwork shape has no failover and
cannot provide it. More normal Cilium exposure models, if we decide to leave
hostNetwork mode:

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
but only with the Kubernetes provider, mirroring a small number of secrets
cross-namespace — rotator-published tokens out of `flux-system`, shared agent
credentials out of `claude-sandbox`, Airlock OAuth tokens, CLIProxyAPI keys (stores in
`k8s/external-secrets/config/`). Stakater Reloader restarts pods on changes. Vault was
decommissioned 2026-04-19: much higher bootstrap operational complexity, raft being
annoying on a 3-replica Vault, and its extra features not actually helping — SOPS + ESO
and friends are enough.

## Google OAuth Client redirect URIs (blocked upstream)

The Google Cloud OAuth client backing Authentik's "Sign in with Google"
source (client_id `230253529789-…`, referenced from
`tf/gitops/sso-providers/source_google.tf`) is hand-managed in the
GCP Console. In principle each forward-auth app has its own callback
URI (`https://<app>.allegedly.works/source/oauth/callback/google/`)
that would need appending to the client's Authorized redirect URIs by
hand, `redirect_uri_mismatch` being the failure mode. The behaviour
itself is intentional in Authentik
(<https://github.com/goauthentik/authentik/issues/19883> closed as
not-planned); standalone proxy outposts run flows on the proxied
domain. Domain-Level forward-auth would centralise the callback but
sacrifices per-app group restrictions
(<https://docs.goauthentik.io/add-secure-apps/providers/proxy/forward_auth/>),
which is the entire reason for using forward-auth here.

**Observed in practice**: the per-app URIs have not been registered,
yet auth-proxied services keep working, with only occasional strange
Authentik errors. **Hypothesis** for the gap: flows normally run on
`auth.allegedly.works` against an existing Authentik session, so the
proxied-domain Google callback is rarely exercised; a fresh Google
sign-in started on a proxied domain is the case expected to fail with
`redirect_uri_mismatch`, and the occasional odd errors may be this
surfacing.

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
- [ ] Until then: when a per-app callback URI is added by hand, record
      it in the app's commit message so future audits can rebuild the
      list from git.

## Kubeconfig Endpoints (Current State)

| Consumer                      | Endpoint                   | Mechanism                                        |
| ----------------------------- | -------------------------- | ------------------------------------------------ |
| Talos nodes (kubelet)         | `localhost:7445`           | KubePrism (built-in Talos API proxy)             |
| NixOS workers (wyrm2, rugged) | `localhost:7445`           | haproxy -> all CP Nebula IPs                     |
| TF state / `cluster/.envrc`   | `api.allegedly.works:6443` | `terraform/main/kubeconfig`, written by OpenTofu |
| `~/.kube/config` (wyrm2)      | `localhost:7445`           | Via local haproxy                                |

`api.allegedly.works` exists on port 443 behind the cluster Cilium Gateway
(TLSRoute in `k8s/kube-api-proxy/` with TLS passthrough to the `kubernetes`
Service). This is what Claude Code web sandboxes use to reach the k8s API —
Anthropic's egress proxy only allows port 443 outbound, so a non-standard port
would not work. TLS passthrough preserves client certificates for x509 auth.

## OpenTofu State Backend

All 6 former TF roots consolidated into a single root at `cluster/terraform/main/` with
PG backend (CNPG `tofu-state-db-ovh`, schema `main`, OVH hdd tier via the
deprecated `local-path-ovh` alias). State
backups have been absent since the backup CronJob's removal 2026-06-01; restoring them
(CNPG replication or scheduled backups) is
[#4900](https://github.com/agentydragon/ducktape/issues/4900).

All in-cluster tofu-controller `Terraform` CRs also use the PG backend (one schema per CR
in the same `tofu-state-db-ovh` cluster; reflector mirrors PG creds into `flux-system`).
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

Via the `inventree-token-provisioner` image
(`cluster/provisioners/inventree_token_provisioner/`), run as a Job plus a weekly
renewal CronJob: admin credentials -> InvenTree REST API -> get-or-create the
`sandbox-agent` user, issue a named API token, write the `inventree-api-token`
Secret in the `inventree` namespace; an ESO `ClusterExternalSecret` mirrors it
into `claude-sandbox`. Renews when fewer than 30 days remain. Suspended together
with InvenTree.

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
- **ARC (GitHub Actions runner)**: decommissioned 2026-04-11; manifests deleted
  2026-08-05 (#3773).
- **Harbor**: parked 2026-06-02 (#1822); manifests archived under `x/harbor/` (#3967);
  deleted 2026-08-27 after #4856. Mostly a registry for props, which use the Forgejo
  registry; the replacement track is the `oci-cache` lighter-registry item in <plan.md>.
- **Kagent** (2026-07-21): Retired after noisy MCP results repeatedly exceeded the z.ai
  prompt limit and killed sessions. Kagent had no client-side tool-output budget, and its
  between-turn compaction could not prevent a single turn from overflowing context. See
  <../archive/2026_07_kagent/README.md>.
- **OpenClaw gateway / OpenShell** (2026-07-31): manifests deleted rather than parked.
  The gateway was unused and wedged (no exec traffic, idle orphaned sandboxes), and the
  operator could not be egress-confined (`docs/personal_agents/findings/` F3). OpenClaw
  as an agent runtime is alive: `public-coder-agent` (the reference agent — same
  `ghcr.io/agentydragon/openclaw` image, plain Deployment, `sandbox.mode: "off"`) and
  `haku-openclaw-spike` (its own OpenClaw build) run today, and the `openclaw`
  ImageRepository/ImagePolicy are kept for that image. The former `openclaw-gateway` and
  `openclaw-sandbox` namespaces were retired after their retained credentials moved to
  `agents/shared-secrets`; see <../archive/2026_08_openclaw_namespace_retirement.md>.
  Evaluated alternatives: `docs/personal_agents/verdicts.md`.
- **LiteLLM ChatGPT sub-instance** (`litellm-chatgpt`, deleted 2026-08-06): a second
  LiteLLM Deployment holding its own ChatGPT/Codex OAuth session on a PVC, serving the
  `*-chatgpt` models over the Responses API; superseded by
  [CLIProxyAPI](../k8s/cli-proxy-api/README.md). [#3198] had already isolated it so an
  expired token could not crashloop the main proxy; what retired it was
  re-authentication: LiteLLM has no in-place login, so every new token meant an
  interactive `codex login`, reshaping into LiteLLM's flat `auth.json`, a SOPS commit,
  and an init-container guard to replace the PVC copy — a guard wrong twice ([#3199];
  2026-08-06, ~3h crashloop) because it could only test the file's shape, never whether
  the credential works. **Lesson**: a credential that rotates on use and can only be
  minted interactively does not belong in a checked-in secret. CLIProxyAPI logs in
  against the running pod (`-codex-device-login`), keeps the session alive with a
  refresh worker, and never puts the upstream OAuth session in git. The `*-chatgpt`
  model names survived the swap: LiteLLM served them over CLIProxyAPI's native
  `/v1/responses` (`openai/` passthrough, not a bridge), so the baked configs pinning
  those names (<../k8s/agents/agent-sandbox/workspace-image/codex-config.toml>,
  <../../x/codex_pod_image/home.nix>, `oai_lane_models` in
  <../../tf/gitops/litellm-keys/main.tf>) needed no rebuild, and the `codex-*` entries
  stayed on `anthropic/` → `/v1/messages` for Claude Code. [#4823] later renamed the
  lanes to `chatgpt/oai-responses/*` / `chatgpt/ant-messages/*` and retired both
  legacy name families.

  [#3198]: https://github.com/agentydragon/ducktape/pull/3198
  [#3199]: https://github.com/agentydragon/ducktape/pull/3199
  [#4823]: https://github.com/agentydragon/ducktape/issues/4823

## Suspended Kustomizations

### Intentionally parked

These stay suspended until explicitly revived. The atlas/wyrm2 outage that idled
the Proxmox-pinned entries is over (both are back as of 2026-08), so for those
the open question is unsuspending, not hardware.

- **agent-box**: `agent-box`, `agent-box-namespace` — inactive while the unschedulable
  legacy VM is retired; the VM and its local disk stay untouched until explicitly
  deleted.
- **ArchiveBox**: `archivebox`, `archivebox-namespace` — retained suspended so Flux
  cannot recreate the retired objects.
- **Browsertrix**: `browsertrix`, `browsertrix-{namespace,retained}`,
  `seaweedfs-browsertrix-bucket` — manifests retained suspended (#4248).
- **BuildBuddy Executor**: `buildbuddy-executor` — scaled to 0; Proxmox-pinned, and
  atlas/wyrm2 being back removes that blocker — re-enable when needed.
- **claude-sandbox-firecracker** — pinned to wyrm2; parked to free resources, and wyrm2
  is back — unsuspending is now an open question.
- **egress-proxy-rugged** — decommissioned by operator request; configuration kept,
  reconciliation stopped.
- **Firecrawl**: `firecrawl`, `firecrawl-{namespace,db}` — parked.
- **gecko**: `gecko`, `gecko-namespace` — same legacy-VM retirement hold as agent-box.
- **Google Workspace MCP**: `google-workspace-mcp` — parked 2026-05-13; resources + PVC deleted.
- **InvenTree**: `inventree`, `inventree-{namespace,secrets,db,token-provisioner}` —
  nice-to-have, parked under capacity pressure; Proxmox-pinned, so atlas/wyrm2
  being back removes that blocker. Before unsuspending: mint the SOPS admin/db
  secrets (<../k8s/TODO.md>). The app's formerly dangling `dependsOn` now points
  at `sso-providers-tf` (#4908).
- **OpenHands**: `openhands`, `openhands-{namespace,secrets,sandboxes}` — experimental, not
  currently used.
- **props**: `props`, `props-{namespace,secrets,db,agent-rbac}` — suspended 2026-08-20
  for a temporary teardown.
- **Tandoor**: `tandoor`, `tandoor-{db,namespace}` — using Grocy instead.
- **Wayback cache**: `wayback-cache`, `wayback-cache-{namespace,agent-rbac}`,
  `wayback-archive-db` — decommissioned by operator request; configuration kept.

### Still down

- **sdr** — suspended pending the radio re-set-up post-relocation (not unblocked by
  atlas/wyrm2 returning).
