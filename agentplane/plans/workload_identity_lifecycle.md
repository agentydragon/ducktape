# Workload identity lifecycle

The workload identity source cutover is complete. Agent workloads receive SVIDs through
Identity-subject `WorkloadCredential`s; the issuer requires an Identity subject, and the live
resolver rejects retained ServiceAccount-subject records. Sandbox source and live acceptance are
recorded in the [rollout evidence](../docs/sandbox_identity_cutover_2026_11_12/README.md).
Deployment-specific standing-workload proxy observation remains tracked separately in the
[task DAG](task_dag.md#standing_workload_identity--standing-workloads-run-as-identities).

## Remaining paths

These are distinct from the completed workload credential cutover. In particular, ServiceAccount
authentication between Agentplane components and ServiceAccount use for Kubernetes API access are
platform credentials with retained attribution, not agent Identity credentials.

| Path                                   | Current contract                                                                                                                                                                                            | Remaining work                                                                                                                                                                                                       |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| App bearer for `agentplane-agent`      | The app accepts operator OIDC and a TokenReview bearer for `agentplane-agent` (`app/internal/auth/auth.go`). This is not a workload SVID path.                                                              | Decide whether the remaining staging and hand-run callers can use real-issuer ID tokens (`APP_AGENT_TOKEN_DECISION`). Keep the current configuration until the callers move or the exception is explicitly retained. |
| Backend service callers                | Agentplane components continue authenticating to shared listeners as themselves, using SVID or their configured audience through TokenReview.                                                               | Retained by operator decision. Preserve component ServiceAccount auth and actor/writer/sender provenance.                                                                                                            |
| Bearer-only component listeners        | The egress admin, Policy Service, sampler and selected Gateway methods accept component TokenReview; the Policy Service listener has no TLS.                                                                | Retained by operator decision; this is service-to-service auth, not an agent bearer migration.                                                                                                                       |
| Delegated workload calls               | The component actor proves itself through its own credential; the delegation envelope names the exact Identity effective caller. Receivers check the actor per method and authorize the delegated Identity. | No ServiceAccount effective-caller removal remains. Keep actor proof and workload Identity authority distinct.                                                                                                       |
| Off-cluster workloads presenting SVIDs | No current off-cluster workload delivery path is recorded. External MCP clients use the edge's OAuth grants bound to an Identity.                                                                           | Revisit SVID delivery when an actual off-cluster workload requires it.                                                                                                                                               |

## Workload credential authorization

RBAC grants `create`, `update` and `delete` on `WorkloadCredential`s to exactly three accounts: the
Sandbox Controller, the Action Service and Flux's applier. Records name an Identity in the policy
namespace. The upstream gates stay distinct:

- for a Sandbox, who may set `Sandbox.spec.identity`;
- for an Action box, the Identity caller authenticated by the Action Service;
- for a standing workload, git.

Flux's kustomize-controller already holds `*`, so a narrower grant to Flux would record intent but
not enforce it. Haku cannot author a record, because the `haku-state` Kustomization impersonates
`haku-state-reconciler`, which holds no right on `WorkloadCredential`s. A narrower requester can be
added later without changing anything here: a `ValidatingAdmissionPolicy` calling the CEL
`authorizer` for a custom `use` verb on the named Identity.

## Open questions

1. **The app's `agentplane-agent` bearer.** Staging live suites, the shared-cluster allocation probe
   and hand-run scripts still use this TokenReview path through the public route, where TLS ends at
   the Gateway, so a client certificate cannot reach the app as mTLS. PR #15448 moved k3s fixtures
   to short-lived test-minted OIDC ID tokens and added operator ID-token bearer support to the app;
   it did not move these staging callers. The remaining decision is tracked by
   [`APP_AGENT_TOKEN_DECISION`](task_dag.md#app_agent_token_decision--the-apps-agentplane-agent-token-under-the-target).
2. **Off-cluster workloads presenting SVIDs.** A Secret mount does not reach them. Revisit delivery
   when one exists; the record and resolver need no change for the current in-cluster workloads.

3. **Per-incarnation credential IDs.** The current design uses a fresh `WorkloadCredential` ID for
   each accepted binding, so a replacement cannot reuse credentials left on a terminating Pod,
   VM disk or Firecracker snapshot. Revisit whether the binding checks alone make this redundant
   or whether lookup latency makes a stable ID with a Pod-binding check preferable. This is a
   deferred design question, not a cutover blocker.

The serving contract lives in the
[`workload_authentication.md`](../docs/workload_authentication.md). Revisit off-cluster SVID
delivery if an actual workload needs it; this inventory does not gate the completed workload
identity cutover.
