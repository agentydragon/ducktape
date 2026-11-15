# Service SVID cutover readiness

Stage 5 was released for implementation on 2026-11-15 and is in flight. This plan holds the caller
audit it rests on and the receiver removal sequence with each receiver's progress. The release
authorizes implementation under the existing rollout gates.

Stage 4 is complete and was verified on staging on 2026-11-15: Actions' direct and delegated
Dispatcher and History peers (#16620; a live delegated `sessions/get` Action at 07:59 UTC), and the
sampler's LLM ingress peer (#16624; a central Session command at 09:00 UTC exercised the provider
call, Executor and History export with no auth or TLS errors). A read-only inventory before the
sampler switch found every retained sampler Session carrying its execution Identity name and UID;
no data was written.
The selected decision and rollout gates remain in
[`PRIVILEGED_BACKEND_AUTH_DESIGN`](task_dag.md#privileged_backend_auth_design--one-service-authentication-mode).

## Refreshed audit, 2026-11-15 09:34 UTC

Source: fresh devel `aaa39328e439c7ceda96588ef7bd9b0d55c0125e`. Read-only Kubernetes
snapshot: Deployments, Pods and mounted-settings ConfigMaps at 09:34 UTC, and Services at
09:40 UTC. No credential values, Secret contents or database rows were read. No workloads,
settings, admission, RBAC or stored data were changed.

The eleven auth-related Deployments and their 23 Pods were Ready, with zero container restarts.
Their image tags matched the devel pins: Actions, Egress, History, LLM ingress and Notifications
on `08f5e49`; App and Policy on `0b85888`; sampler and MCP edge on `2b349f4`; Gateway and Sandbox
Controller on `2ceca57`. Every certificate/trust path found in these mounted service settings
was covered by a corresponding Pod volume mount. This checks wiring, not Secret contents or
whether every RPC has executed.

No first-party backend hop in these settings selects a Kubernetes bearer:

| Caller                   | Selected outgoing authentication                                                                                                                                                                                                            |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App                      | App SVID on Dispatcher, EnvironmentService, Runtime proxy, History, Notifications, Policy, Egress Admin, Actions and MCP ConnectionService. Actions and ConnectionService retain operator OIDC authority. No backend token-file key is set. |
| Actions                  | Actions SVID on direct Dispatcher and History clients, delegated Dispatcher and History clients, and EnvironmentService. All five peer `token_file` values are empty; delegated clients retain their typed envelopes.                       |
| Notifications            | Notifications SVID on Actions, Dispatcher, Runtime proxy and History; no token-file setting.                                                                                                                                                |
| MCP edge                 | MCP edge SVID on Actions, with typed Connection delegation; no Actions token-file setting.                                                                                                                                                  |
| Sessions role in Gateway | `spiffe://agentplane/sessions` on Runtime proxy and central sampler; no token-file setting.                                                                                                                                                 |
| Central sampler          | Sampler SVID on Gateway Executor, History export, Actions and LLM ingress. LLM auth is `agentplane_ingress`; its external Anthropic API key is a separate provider credential.                                                              |
| Sandbox Controller       | Controller SVID on Gateway control; direct Runtime transport remains certificate-authenticated. No Gateway token-file setting.                                                                                                              |

The App's default token paths are cleared for configured SVID peers when their keys are absent
(`app/internal/settings/config.go`). Shared `ClientTLS.ValidateCaller` rejects combining a token
path and service certificate, and a partial certificate configuration selects validation failure,
not bearer fallback. `DialDelegated` emits only the typed envelope in SVID mode. An obsolete
mounted token does not establish an active bearer caller.

The first-party service-token projections this snapshot found (19 in templates) were removed in
#16650; every affected Deployment rolled Ready at 10:27 UTC with no auth or TLS errors. Standard
Kubernetes API token projections are separate and stay.

The sampler has **no plaintext application listener configured**: `listen` is absent, its pinned
loader supplies no default, and its Service exposes only TLS port 8083. Its health-only listener
is 8081. The earlier removal table's plaintext-listener prerequisite was stale. The TLS API still
admits the configured Gateway account bearer as well as the Sessions SVID; admission removal is
still required. The Controller-hosted EnvironmentService on 8082 is already SPIFFE-only, but
constructs the shared TokenReview resolver and retains account settings.

### Positive bearer clients recorded in the 09:34 UTC source snapshot

These are callers of disposable test services, not evidence of a selected bearer on staging's
backend hops. This is the historical source inventory from the audit above; receiver retirement
PRs have since migrated or removed some of these callers. Refresh the rows when auditing the
remaining test callers. Keep explicit bearer-rejection tests for the final boundary; do not convert
rejected Kubernetes tokens into accepted callers.

| Fixture or scenario                                                                                                      | Remaining bearer use                                                                                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `system_tests/driver/dispatcher_transport.py`                                                                            | `bound_caller` mints a Pod-bound App token; `native_client` sends it to Dispatcher. Used by VM/Pod suites, MCP Actions/delegation and the shared Dispatcher fixture.                                                                                                                                           |
| `system_tests/driver/egress_decisions.py`                                                                                | Mints an App bearer for Egress Admin decision reads.                                                                                                                                                                                                                                                           |
| `system_tests/scenarios/dispatcher.py`, `session_deletion.py`                                                            | Mint App, Notifications and Gateway fixture account tokens for Dispatcher, Runtime proxy and History calls, including lifecycle/deletion checks.                                                                                                                                                               |
| `app/testing/gateway_fixtures.py`, `testing/native_gateway_fixtures.py`, `session_dispatcher/testing/gateway_process.py` | Positive native Gateway/Dispatcher fixtures retain account tokens; recovery and continuity tests consume them.                                                                                                                                                                                                 |
| `central_sampler/testing.py` and receiver Go fixtures                                                                    | Sampler's synthetic TokenReview endpoint and positive receiver fixtures still exercise bearer admission. Envtest token helper references span Actions, Egress Admin, EnvironmentService, History, LLM ingress, Notifications and Gateway; assess each positive or negative assertion with its receiver change. |

`system_tests/x/shared_cluster/allocation.py` now mints only the observer's Kubernetes API token
with an empty audience list; that token is not presented to an Agentplane receiver. Live suites
use caller-supplied operator ID tokens. The App's `kubernetes_auth` fixture and literal
`agentplane-agent` rejection test exercise refusal, not the retired special-token login.

### External caller and visibility limits

A tracked-source search outside `agentplane/` found no active Kubernetes-bearer caller of an
Agentplane listener. Haku's managed-agent token is for Kubernetes, and the Haku CI relay presents
a standing workload SVID with token automount disabled. Public Coder's relay uses SVID proxy auth;
its application is paused at zero replicas with token automount disabled. Its old Action HTTP
policy rule still references the credential name `agentplane-workload`, for which no current
credential declaration was found. That stale policy and the older projected-token cutover prose
need reconciliation; they do not prove a currently mintable bearer or an active caller.

The shared-diagnostics account refused the exact permission checks for cross-namespace Pod
listing, ClusterRole listing and staging ServiceAccount listing. No alternate credential or
additional grant was requested. Independently deployed scripts and other namespaces therefore
remain outside this live audit; the eight receiver TokenReview RBAC grants are a source finding,
not a live ClusterRole inventory. The App special-token ServiceAccount absence is likewise
source-verified, not rechecked through a live ServiceAccount listing.

### Remaining cutover work

No staging caller-configuration blocker was found. All receiver retirements are merged, including
History (#16728), which was the final production `auth.NewResolver`/TokenReview consumer. At the
2026-11-15 14:57 UTC gate check, History had two Ready replicas on `d817b0a`, containing that
retirement. Its mounted YAML still carried the ignored legacy settings. Manifest omission is the
next step; deleting the compatibility decoders waits for that omission to roll out.

Gateway manifest omission (#16709) and decoder cleanup (#16722), Policy omission (#16699) and
decoder cleanup (#16718), and sampler omission (#16724) and decoder cleanup (#16729) are merged.
Shared TokenReview API retirement still has test consumers; TokenReview RBAC remains until the
receiver images and settings retirements are verified deployed.

For each receiver, migrate its positive test clients with its admission change, land and pin code
accepting reduced settings before changing manifests, roll out key omission before deleting
settings fields, and remove obsolete projections and mints before RBAC last. Preserve
operator OIDC/MCP OAuth, Kubernetes API access, workload Identity admission, exact role service
allowlists and typed delegation. No new Session field, Sandbox requirement or data rewrite is
needed. This audit ran source searches and live read-only checks; it did not rerun system tests or
the previously accepted service smoke.

## External callers and the retired App token

A source search across `haku/`, `tf/` and `cluster/cdk8s/` outside Agentplane found no active
caller minting or presenting a Kubernetes bearer directly to an Agentplane receiver. This is
bounded source evidence; external namespaces and independently deployed scripts were not inspected
live. The existing operator OIDC and MCP OAuth contracts remain unchanged.

- OpenClaw's current [relay settings](../../cluster/cdk8s/public_coder/egress.py) use
  `SpiffeProxyAuth`. Its [Deployment](../../cluster/cdk8s/public_coder/app.py) disables ServiceAccount
  token automount and is paused at zero replicas. Only the relay mounts its SVID. The projected-token
  description in the [older cutover document](../../cluster/docs/public_coder_openclaw_egress.md#transport-and-credentials-at-cutover)
  is historical and does not describe current source.
- Haku's managed-agent [entrypoint](../../haku/runtime/x/managed_agent/self_hosted/entrypoint.sh)
  uses its mounted ServiceAccount token for in-cluster `kubectl`. Its configured MCP endpoints are
  Haku services. The Claude.ai Agentplane connection uses the
  [OAuth provider](../../tf/gitops/sso-providers/provider_agentplane_mcp.tf), with logical Connection
  identity `claude-ai`; it is not a direct backend ServiceAccount bearer caller.
- No active `agentplane-agent` token mint, projection, ServiceAccount or token-request grant was
  found in the searched source. App code rejects Kubernetes bearers. Remaining literal references
  include rejection tests, the rejected alternative in the orchestration ADR and historical plans;
  these do not establish a caller. Live test scripts take a caller-supplied operator ID token via
  `AGENTPLANE_SYSTEM_TEST_TOKEN`. Provisioning that issuer remains parked in the
  [freezer](task_freezer.md#live_smoke_operator_token--provision-a-live-test-operator-token).

## Receiver-specific removal sequence

Implement one receiver per PR, with its own rollback and relevant system suites. The shared
TokenReview implementation goes only after its last consumer. Shared groundwork merged: direct
workload SVID admission refuses requests that also carry bearer or delegation metadata (#16657).

Receiver retirement is merged for Notifications (#16667), LLM ingress (#16668), Egress Admin
(#16678), Actions (#16680), Gateway/Dispatcher (#16690), EnvironmentService (#16681), central
sampler (#16688) and History (#16728). History's `d817b0a` image is Ready on staging. Its manifest
omission (#16757) is live; compatibility decoder deletion follows. TokenReview RBAC removal stays last.

| Receiver                                      | Remaining bearer surface                                                                                                                                                                      | Cutover constraint                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Policy Service                                | Receiver omission (#16699) and documentation (#16710) are merged; decoder cleanup (#16718) is merged and green.                                                                               | Keep Kubernetes API clients and controller behavior unchanged.                                                                                                                                                                                                                                                           |
| Egress Admin                                  | Receiver retirement is merged (#16678).                                                                                                                                                       | Preserve SVID-authenticated Rules and forward-proxy routes.                                                                                                                                                                                                                                                              |
| Notifications                                 | Receiver retirement is merged (#16667); legacy keys remain decoded and ignored.                                                                                                               | Preserve workload Identity routes, inbox ownership and automatic wake authorization.                                                                                                                                                                                                                                     |
| Actions                                       | Receiver retirement is merged (#16680); Dispatcher and History callers are verified in SVID mode (#16620).                                                                                    | Preserve effective Identity and typed Connection/Session provenance, plus operator/OAuth contracts.                                                                                                                                                                                                                      |
| Central sampler                               | Receiver retirement (#16688), manifest omission (#16724) and decoder cleanup (#16729) are merged. The optional plaintext application listener is disabled in staging.                         | Preserve exact method/role admission, service actor versus effective Identity provenance, the rewritten service owner and retained Sessions; preserve the separate health-only listener.                                                                                                                                 |
| History                                       | Receiver retirement (#16728) and manifest omission (#16757) are live. Legacy audience, reader-account, delegation and sampler-account/Identity decoders are removed by the follow-up cleanup. | Runtime admission is workload SVID or an exact service SVID rule. Preserve method/role and actor/effective caller checks, and journal ownership. Retained data must allow absent or multiple attachments and must not require a Sandbox or execution Identity. TokenReview RBAC cleanup remains a separate rollout step. |
| LLM ingress                                   | Receiver retirement is merged (#16668); legacy settings remain decoded and ignored pending manifest omission and rollout.                                                                     | The sampler provider cutover is verified (#16624). Keep workload Identity admission and provider authorization; the actor certificate supplies no effective-caller grant.                                                                                                                                                |
| Gateway Dispatcher / Runtime proxy / Executor | Receiver retirement is merged (#16690); manifest key omission (#16709) and decoder cleanup (#16722) are merged and green. TokenReview RBAC cleanup remains separate rollout work.             | Preserve separate peer grants for App, Notifications, Sessions, Controller and sampler, and effective Identity delegation. Keep operator/MCP OAuth contracts and typed Connection/Session provenance unchanged.                                                                                                          |
| Extracted EnvironmentService                  | Receiver bearer admission is retired in merged code (#16681); rollout and TokenReview RBAC cleanup are tracked separately.                                                                    | Preserve the SPIFFE-only Controller-hosted 8082 path without adding environment requirements to Sessions or changing lifecycle grants.                                                                                                                                                                                   |

Settings reject unknown keys. For every receiver, determine whether the currently pinned image
accepts omission of each legacy field. Where it does not, land and pin code accepting the reduced
configuration first. Remove manifest keys and roll them out before deleting their settings fields.
Keep any temporary reader narrowly scoped to that named rollout; do not infer authentication from
the presence of unrelated fields. Test actual bearer rejection at the method, not only YAML output.

Before receiver removal, rollback may restore a caller's former bearer configuration while its
receiver still supports it. After admission removal, use a compatible SVID-capable receiver image
and settings. A coordinated receiver rollback must restore any required fields, tokens and RBAC;
never make a certificate error silently select a bearer. Gateway→sampler rollback to account-owned
bearers additionally needs the exact reverse ownership Job from the sampler maintenance procedure.
Do not run that Job as part of this audit.

## Token and RBAC cleanup, last

Service-token projections, their mounts and the callers' token settings are gone (#16650). Keep
provider API keys, external MCP OAuth/static credentials and Kubernetes API authentication
separate; in particular, Egress's `local_api_server.token_file` authenticates to Kubernetes and
stays. Audience constants and service-token minting permissions go with their receivers.

Remove the shared `kubernetes_bearer` mode, resolvers and obsolete account admission paths when
their final consumers are gone. EnvironmentService's code residue does not establish a ninth
deployed TokenReview RBAC grant. Last, delete the receiver TokenReview
ClusterRole/ClusterRoleBindings for Actions, sampler, Egress, History, LLM ingress, Notifications,
Policy and Gateway, after their images no longer construct or call TokenReview. Do not remove
unrelated policy subjects or Kubernetes clients merely because they mention ServiceAccounts.

## Acceptance to attach to the implementation PRs

Reuse the certificate/rotation coverage in `//agentplane/net/peertls:peertls_test`, service/actor
coverage in `//agentplane/identity/auth:auth_test`, and delegated unary/stream coverage in
`//agentplane/identity/auth/grpc:grpc_test`. Each receiver also needs its own method-table tests and
negative case proving a valid Kubernetes bearer cannot authenticate a missing or invalid
certificate and causes zero TokenReview calls. Keep exact peer/method denial,
expired/wrong-EKU/wrong-CA certificates, rotation,
Identity UID replacement and delegation authority separation. Retained operator OIDC/OAuth methods
must continue to work under their own authorization contract.

Run the changed cdk8s library/render tests and manifest generation. Select existing system suites
for each affected boundary: `test_pod`/`test_vm` and Dispatcher/Gateway integration for control paths;
MCP `test_actions`, `test_services` and `test_delegation` for Action/delegation paths; sampler roll,
History outage and Gateway recovery suites for central Sessions. Use envtest where the receiver
depends on Kubernetes behavior. Update focused system-overview edges with each actual cutover.
No unchanged reruns, timeout increases or skipped assertions. This documentation audit itself
does not execute a receiver cutover or re-run the accepted live smoke.
