# public-coder-agent

## Paused

Public Coder OpenClaw is paused while model-limit ownership is being simplified.
OpenClaw has zero replicas. The KubeVirt devbox, its disposable Bazel cache, the
devbox SSH route, and the public-coder Iron proxy have been retired. This is a
parked OpenClaw deployment, not a deletion of its state, diagnostics, or backup.
The UI is unavailable while paused.

Keep the namespace and the existing Flux owners. OpenClaw state, diagnostics, and
the old SSH session recordings PVC remain declared with unchanged names/specs. The
VolSync backup's ReplicationSource is paused (`spec.paused`); its Restic repository
and controller-owned cache PVC are kept. The `public-coder-agent-app` Flux owner
remains active and annotated `ducktape.org/parked`: it keeps the Deployment at zero
replicas and prunes retired proxy resources while preserving the declared PVCs. The
backup owner stays suspended. The `public-coder-agent-devbox` owner temporarily points
to an empty cleanup artifact so Flux prunes its old inventory. The
`public-coder-agent-access` owner (`cluster/generated/agents/public-coder-agent/access/`)
stays active: it owns the Namespace and Roles that Haku and Agentplane Sandboxes still
read. A separate cleanup Kustomization waits for the credential sources to reconcile,
then deletes the retired, unowned Secret copies and private keys using a Role restricted
to their exact names. The devbox cache PVC was explicitly disposed of during retirement;
do not recreate it.

To resume, move the app directory back under `cluster/k8s` and drop the parked
annotation, per `cluster/AGENTS.md`; restore the OpenClaw replica count and
unsuspend the backup. The OpenClaw image workflow and smoke
script are parked in `cluster/parked/public-coder-agent/ci/`; move them back and publish
a fresh image before resuming. Verify model budgets and credentials first; do not
reset retained PVCs or change the embedding identity.

## Kubernetes authorization path

The Agent's kubeconfig points at `haku-kubeapi.allegedly.works` through the local
Agentplane egress relay. The Agent presents a non-secret placeholder; egress substitutes the
Agent's Haku bearer only for that hostname. The standalone Haku Kubernetes proxy authenticates the bearer with Console,
evaluates the deploy-owned `public-coder` standing SAR subject, strips all caller credentials, and
uses its own rotating projected ServiceAccount token upstream.

The synthetic, non-login `haku:access-profile:public-coder` group is the configured SAR
identity. Console selects it only from the deploy-owned `public-coder` access-profile configuration;
the caller cannot supply it. Console also includes Kubernetes's standard `system:authenticated`
group after it authenticates the Agent bearer, preserving normal API discovery without pretending
the subject is a ServiceAccount. `haku-kube-api-proxy` has an independent `cluster-admin` execution
ceiling, usable only when Console authorizes each request through standing SAR or an active
Agent-owned grant. This deliberately makes the inline Haku authorization path the temporary-access
policy boundary for this personal cluster; the Agent still has no standing cluster-admin authority
or direct kube-apiserver credential.

The proxy serves `watch` and log-follow alongside pod `exec` and `portforward`, and
rejects upgrades other than those two. Every long-running request is reauthorized
every five seconds and closes within the revalidation interval plus the Console
authorization timeout after a grant is released or revoked.
The standing group has no login credential or ServiceAccount token; only Console may evaluate it
through SubjectAccessReview.

The `public-coder-agent-app` Flux Kustomization owns the namespace and OpenClaw application.
It depends on External Secrets and Agentplane staging, and health-checks the OpenClaw Deployment.
Backup remains a separate owner. The devbox and its Iron dependency are retired; there is no
devbox rollback path to restore.

Promtail scrapes ordinary pod stdout/stderr from the application and Console into Loki. The current
Loki configuration enables compactor retention but sets no finite `retention_period`, so these
records are not subject to age-based deletion. Console logs map authorization decisions to the
Agent and grant, while the durable grant row maps the grant to its source ToolCall.

A second OpenClaw agent at <https://public-coder-agent.allegedly.works>, separate
from the personal agent at `openclaw.allegedly.works`. Its job is opening pull
requests against **public** repositories as `agentydragon-agent`, which has its
own GitHub account and pushes to its own forks.

## Layers

| Directory | Contents                                                                                                             |
| --------- | -------------------------------------------------------------------------------------------------------------------- |
| `app/`    | Namespace, OpenClaw Deployment, config, state/diagnostics/retained-recordings PVCs, credentials, and NetworkPolicies |
| `backup/` | Paused VolSync backup definition and retained repository                                                             |

The repository-owned tooling and approval operating instructions are in <TOOLING.md>. They cover
which local, GitHub, Kubernetes, Haku, and physical-host surfaces to prefer, how to inspect the live
authority, and how to handle the approval lifecycle without unnecessary stalls.

The agent also connects to Matrix as `@public-coder-agent:allegedly.works`.
Matrix is an official plugin baked into the derivative OpenClaw gateway's
trusted bundled-extension tree; loading it from an arbitrary config path would
deny the state-store capability it needs before sync. The initial policy is
deliberately DM-only and allowlisted to
`@agentydragon:allegedly.works`; add explicit room policy/config before using
it in group rooms.

## Egress model

Two layers, and the split matters:

1. **The app's `public-coder-agent-egress` NetworkPolicy is the fence.** The OpenClaw Pod may
   reach DNS, the Agentplane staging gateway on its proxy port, and in-cluster LiteLLM. It has no
   direct Internet or ClickHouse route. The `HTTP_PROXY` variables point to the local relay; the
   relay alone mounts the projected workload token.
2. **Agentplane's `public-coder-openclaw` policy is the egress allowlist.** It scopes destinations
   and credential substitution for the public-coder ServiceAccount. Every widening is a reviewable
   diff in `cluster/cdk8s/public_coder/egress.py` and the Agentplane policy controller.

The model path never leaves the cluster: LiteLLM is reached directly, bypassing
the egress relay, via `NO_PROXY`.

## Analytics reader

The Haku Console `public-coder-agent` account has a dedicated native
ClickHouse account, `public_coder_analytics`, for normalized and raw AIQuota
history. The agent receives `CLICKHOUSE_PUBLIC_CODER_USER` and
`CLICKHOUSE_PUBLIC_CODER_PASSWORD`, but the password is a non-secret Agentplane
placeholder. A request to the private ClusterIP HTTP endpoint through the
existing egress relay causes Agentplane to replace it in Basic authentication;
the agent never receives the actual password. The account is restricted to
`SELECT` on `aiquota.aiquota_windows` and `aiquota.raw_http_observations`, the
read-only ClickHouse profile/quota, and the Agentplane gateway is the only
public-coder-side Pod allowed to reach ClickHouse on 8123. `NO_PROXY`
deliberately names LiteLLM only, rather than all Service addresses, so ClickHouse
cannot bypass credential substitution.

The GitHub credential stays in the Agentplane egress credential namespace. The
agent sees only the configured placeholder; egress replaces it in the
authentication header and only on scoped GitHub hosts. Brave Search follows the
same mediation model: the real API key is held only by Agentplane egress and is swapped into
`X-Subscription-Token` only for `api.search.brave.com`. That key is not this
agent's: its canonical SOPS source is
`cluster/k8s/external-creds/brave-search-api-key.sops.yaml`, and an
ExternalSecret syncs it into the credential namespace. Workstations decrypt the
same source file directly through `ducktape.sopsEnv`.

The Haku Console credential is used only by the Kubernetes authorization proxy. Terraform
generates a dedicated `public-coder-agent` static-Agent bearer for Haku Console; Agentplane egress
copies it from the canonical Secret. The OpenClaw container sees an Agentplane placeholder; egress replaces it in
the `Authorization` header only for `haku-kubeapi.allegedly.works`. Haku Console checks the Agent's
identity and Kubernetes policy before the proxy forwards an authorized request to the Kubernetes
API. GitHub goes through the egress-mediated `agentydragon-agent` token directly; see <TOOLING.md>
for the operational playbook.

The Matrix bot password is generated and retained by the existing Matrix user
provisioner. It is stored in the Matrix namespace as a SOPS-managed Secret and
copied into Agentplane's isolated credential namespace. The OpenClaw container
sends a non-secret placeholder in its password login body; egress replaces it
only on Synapse's login endpoint. The
provisioner sets the password only when creating the account, so reconciliation
does not revoke the bot's cached access token. The Matrix channel names the
local Agentplane relay explicitly in `channels.matrix.proxy`; the plugin's
guarded fetch path uses that per-channel dispatcher rather than Node's generic
proxy environment handling.

## Deviations worth knowing

- **Plain `Deployment`, not `OpenClawInstance`.** The operator's generated
  NetworkPolicy always contains an egress rule for 443/TCP with no destination
  selector, and `spec.security.networkPolicy` exposes only additive fields
  (`additionalEgress`, `allowedEgressCIDRs`) with no way to disable it. Since
  Kubernetes NetworkPolicies are unions of allows, that rule cannot be
  subtracted, and an operator-managed instance cannot be egress-confined. The
  cost is losing `autoUpdate` and the CRD ergonomics.
- **Blueprint-managed SSO provider**, against the stated preference for
  Terraform in <../../docs/sso.md>. Every proxy provider is a blueprint and
  `embedded-outpost.yaml` owns outpost membership; a Terraform provider would
  split one object graph across two owners. Moves with the rest under issue #987.
- **Agentplane egress relay.** The OpenClaw Pod carries a short-lived projected
  token only in the relay container; its ServiceAccount has no automounted token
  and receives no Kubernetes RBAC from this path.
- **`gateway.bind: lan`**, unlike the loopback-bound lab rig, because the outpost
  reaches this pod over the cluster network. What makes that safe is
  the app's `public-coder-agent-ingress` NetworkPolicy, which admits only the outpost's pods —
  without it any pod could forge `x-authentik-username`.

## Known gaps

- **Matrix token auth.** TODO: replace the password-body substitution with a
  Matrix access token once the provisioner can mint and rotate one without
  revoking Haku Console's independent session. Egress only substitutes the
  password in the login request; the login response can contain a session token.
- The egress NetworkPolicy's DNS rule has no destination selector, so port 53
  reaches anywhere and DNS tunnelling is not prevented. Narrowing to kube-dns is
  the obvious tightening; it is left as a follow-up so the first deployment
  matches what was validated in the lab.
- With `sandbox.mode: "off"` there is no isolation _inside_ the boundary: the
  agent runs as the harness and can inspect its process environment. For
  GitHub, that environment contains only the egress placeholder; the real token
  remains in Agentplane's isolated credential namespace. That is the accepted cost of not using OpenShell — see
  <../../../docs/personal_agents/findings/openshell.md> F1 for why OpenShell was not
  used.
- **PVC capacity enforcement.** `local-path-ovh-hdd` does not enforce requested
  PVC sizes, so workspace growth can consume the worker disk beyond its claim.
  Before treating this as durable agent storage, evaluate extending the
  OpenEBS LVM provisioner to OVH workers and migrating this claim to a
  size-enforcing LVM-backed StorageClass (or another quota-enforcing design).
- **Runtime image closure.** Profile and reduce the OpenClaw image before its
  next substantial expansion. It includes the gateway, Matrix plugin, and
  formatter/pre-commit tooling. The dedicated KubeVirt build environment was
  retired; future manual acceptance work needs an explicitly chosen host and
  identity rather than an SSH route from this agent.
