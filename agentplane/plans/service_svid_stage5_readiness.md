# Service SVID cutover readiness

Stage 5 remains **not released**. This audit prepares the remaining caller work and the
receiver removal sequence; it changes no admission, settings, images or RBAC.
The selected decision and rollout gates remain in
[`PRIVILEGED_BACKEND_AUTH_DESIGN`](task_dag.md#privileged_backend_auth_design--one-service-authentication-mode).

## Evidence and limits

Source audit: devel `2b349f4116`, 2026-11-15. Live snapshot: `agentplane-staging`
Deployments, Pods and mounted-settings ConfigMaps read at approximately 07:08 UTC through the
read-only shared-diagnostics Kubernetes route. No Secret contents or database rows were read.

The eleven auth-related Deployments were Ready: Egress 3/3; App, Actions, sampler, History,
LLM ingress, MCP edge, Notifications, Policy Controller, Sandbox Controller and Runner Gateway
2/2 each. Their Pods reported zero restarts. The image pins and actual Pod tags agreed:
Runner Gateway ran `devel-20261115070016-fe1f0b6`; the other ten ran their respective
`devel-202611150427xx-08f5e49` tags, with App at `devel-20261115042801-08f5e49`.
Both Gateway Pods already had the Actions Dispatcher grant from #16467.

SVID client settings below point at mounted certificate Secrets and workload trust bundles.
This verifies deployment configuration, not successful execution of every RPC. The coordinator
accepted the central Session smoke and the empty post-switch ownership inventory at 04:20 UTC;
this audit did not repeat them. No claim that every caller has switched follows from Ready Pods.

## Remaining stage 4 callers

Stage 4 is **incomplete**. Two caller families still select Kubernetes bearer authentication in
both source and live settings. Keep their receiver bearer admission until their separate caller
cutovers are merged, applied and verified.

| Caller and target                                              | Live selection and source                                                                                                                                                                                                       | Remaining work                                                                                                                                                           |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Actions → Dispatcher, direct orchestration                     | `session_dispatcher.token_file=/var/run/secrets/actions/dispatcher`; server-only TLS. [`actions.py`](../../cluster/cdk8s/agentplane/actions.py), `dispatcher_peer`.                                                             | Switch this peer to Actions SVID after checking the direct Dispatcher grant; #16467 is merged and that grant is live.                                                    |
| Actions → History, direct orchestration                        | `history_service.token_file=/var/run/secrets/actions/history`; server-only TLS. Same composition root, `history_peer`.                                                                                                          | Switch to Actions SVID; History's live `reader_service_callers` already includes Actions.                                                                                |
| Actions → Dispatcher and History, delegated Session operations | `session_delegation_dispatcher` and `session_delegation_history` use those same token paths and server-only TLS. Same composition root, `delegated_*_peer`.                                                                     | Switch to Actions SVID and retain the exact typed Action/Identity envelope and method grants. Direct service authority must not become the effective caller's authority. |
| Central sampler → LLM ingress                                  | `connections.llm-ingress.auth.type=bearer`, `credential_file=/var/run/secrets/central-sampler/llm`; TLS verifies the server only. [`staging.py`](../../cluster/cdk8s/agentplane/staging.py), `CentralSamplerProps.connections`. | A separate caller PR selects `agentplane_ingress` with the sampler SVID and typed Session/Identity delegation. See the question below before opening it.                 |

The pinned Actions configuration code already supports `ClientTLS.UsesSVID` and validates the exact
Actions URI. The pinned sampler contains the SVID branch in
[`providerAuthorization`](../central_sampler/internal/residentruntime/protocol_adapter.go) and
[`ConnectionConfig.Validate`](../central_sampler/internal/settings/model.go). The sampler's
certificate/trust mounts exist. LLM ingress already has
`central_sampler_services=[spiffe://agentplane/central-sampler]` and
`central_sampler_identity_callers=true` in its live settings. These are preconditions, not a
substitute for the caller activation and its acceptance.

## Callers already configured for SVIDs

| Caller                     | Live outgoing SVID configuration                                                                                                                                                                                                  |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App                        | App SVID on Dispatcher, EnvironmentService, Runtime proxy, History, Notifications, Policy and Egress Admin. Actions and MCP ConnectionService additionally retain operator OIDC authority. No backend token-file setting remains. |
| Notifications              | Notifications SVID on Actions, Dispatcher, Runtime proxy and History.                                                                                                                                                             |
| MCP edge                   | MCP edge SVID on Actions with typed Connection delegation. Its ConnectionService TLS listener separately checks the App peer and operator OIDC.                                                                                   |
| Sandbox Controller         | Controller client SVID on Gateway control; direct Runtime control remains mTLS.                                                                                                                                                   |
| Dispatcher / Sessions role | `spiffe://agentplane/sessions` on Runtime proxy and sampler. Sampler ownership uses that same URI after the explicit ownership rewrite.                                                                                           |
| Central sampler            | Sampler SVID on Gateway Executor and History export, and certificate TLS on its Actions client. LLM ingress is the exception above.                                                                                               |

Keep the role identities separate through the planned binary split: Sessions owns the Dispatcher
client identity; Environments owns EnvironmentService; Runtime proxy keeps its own role certificate.
Do not collapse them into a new Gateway-wide identity or mechanism. Workload SVIDs still resolve
through `WorkloadCredential` to an Identity; these component service SVIDs do not.

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

All implementation in this section waits for the stage 5 release and completion of the caller
gates above. One receiver per PR, with its own rollback and relevant system suites, keeps the
operator's review bounded. The shared TokenReview implementation goes only after its last consumer.

| Receiver                                      | Remaining bearer surface                                                                                                                                                                 | Cutover constraint                                                                                                                                                                                                                                                           |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Policy Service                                | `grpc_audience`, `reader_accounts`, `writer_accounts`; mandatory TokenReview resolvers in [`policy_controller/cmd/main.go`](../policy_controller/cmd/main.go).                           | App is configured for SVID. Remove component account admission without changing the controller's Kubernetes API clients.                                                                                                                                                     |
| Egress Admin                                  | `admin_grpc_audience`, `admin_grpc_callers` and TokenReview resolver in [`admin_grpc.go`](../egress/cmd/admin_grpc.go).                                                                  | App is configured for SVID. Validator currently ties service callers to bearer fields; make service-only operation possible before retiring those fields. Preserve SVID-authenticated Rules and forward-proxy routes.                                                        |
| Notifications                                 | `rpc.auth` bearer mode, token audience, operator reader account and TokenReview dependency in [`main.go`](../notification_service/main.go).                                              | App already uses SVID. Preserve workload Identity routes, inbox ownership and automatic wake authorization.                                                                                                                                                                  |
| Actions                                       | `rpc.auth` bearer mode, account/delegation actor settings and mandatory resolver in [`cmd/main.go`](../action_service/cmd/main.go).                                                      | Current incoming App/Notifications/MCP edge/sampler paths use certificates or the separately retained operator/OAuth contracts. Preserve effective Identity and typed Connection/Session provenance; retain outgoing tokens until the Actions caller cutover.                |
| Central sampler                               | Account allowlist and audience on both serving paths, plus the plaintext 8080 listener, in [`cmd/main.go`](../central_sampler/cmd/main.go) and [`rpc`](../central_sampler/internal/rpc). | Gateway already uses the Sessions SVID on 8083. Preserve the rewritten service owner and retained Sessions. Plaintext-listener deletion requires checking probes, Services, observers and policies together.                                                                 |
| History                                       | `rpc.auth` bearer mode, reader/writer audiences, account grants and bearer delegation in [`cmd/main.go`](../history_service/cmd/main.go).                                                | Wait for Actions direct and delegated callers. `serve` synthesizes a bearer reader listener in code, so a manifest-only mode removal is insufficient. Preserve journal writer ownership and Sessions without a Sandbox.                                                      |
| LLM ingress                                   | `rpc.auth` bearer mode, `central_sampler_accounts`, token audience and resolver in [`runtime.go`](../llm_ingress/internal/server/runtime.go).                                            | Wait for the sampler provider cutover. Keep workload Identity admission and provider authorization; the actor certificate supplies no effective-caller grant.                                                                                                                |
| Gateway Dispatcher / Runtime proxy / Executor | Bearer listener modes, two audiences, account grants and bearer delegation in [`main.go`](../runner_gateway/main.go).                                                                    | Wait for Actions. Preserve separate peer grants for App, Notifications, Sessions, Controller and sampler, and effective Identity delegation. `rpcForMethodAudience` respects configured bearer modes; startup validation and the resolver dependencies also need retirement. |
| Extracted EnvironmentService                  | [`serving/server.go`](../environments/internal/serving/server.go) still constructs a TokenReview resolver.                                                                               | The live Controller-hosted 8082 listener is already SPIFFE-only. Retire the unused resolver/account machinery without adding environment requirements to Sessions or changing lifecycle grants.                                                                              |

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

The live Deployment templates still project the following service audiences. Some are now unused;
their presence alone does not prove that a caller still sends them. The Actions Dispatcher/History
tokens and the sampler LLM token remain selected as described above.

| Workload           | Projected service-token audiences                                                                                                                           |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App                | `agentplane-runner-gateway`, `agentplane-history-service`, `agentplane-egress-admin`, `session-dispatcher`, `agentplane-notifications`, `agentplane-policy` |
| Actions            | `session-dispatcher`, `agentplane-history-service`                                                                                                          |
| Notifications      | `agentplane-egress`, `agentplane-runner-gateway`, `agentplane-history-service`, `session-dispatcher`                                                        |
| MCP edge           | `agentplane-egress`                                                                                                                                         |
| Central sampler    | `agentplane-egress`, `agentplane-runner-gateway`, `agentplane-runner-ingress`                                                                               |
| Runner Gateway     | `agentplane-runner-gateway`, `central-sampler`                                                                                                              |
| Sandbox Controller | `agentplane-runner-gateway`                                                                                                                                 |

Remove the service projections, matching mounts, audience constants and service-token minting
permissions only after every affected caller image/configuration has switched. Keep provider API
keys, external MCP OAuth/static credentials and Kubernetes API authentication separate. In
particular, Egress's `local_api_server.token_file` authenticates to Kubernetes; it stays.

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

## Questions for Claude

- The sampler's SVID provider branch refuses a request without the recorded execution Identity
  name and UID; bearer mode did not require that proof. Before its config PR, confirm how to
  inventory and handle retained Sessions lacking it without dropping data or making a Sandbox,
  Sandbox UID or a single environment attachment mandatory. The selected
  [Session model](session_environment_model.md#a-central-session-can-use-zero-one-or-many-environments)
  permits zero or several central attachments.
  This audit adds no new refusal or required field and proposes no automatic rewrite.
- Release stage 5 only after the two remaining caller families have been activated and verified;
  the stage 5 receiver/RBAC implementation remains held.
