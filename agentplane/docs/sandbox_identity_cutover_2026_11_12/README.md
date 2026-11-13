# Sandbox Identity cutover, 2026-11-12

The switch [#15742](https://github.com/agentydragon/ducktape/pull/15742) and cleanup
[#15744](https://github.com/agentydragon/ducktape/pull/15744) are merged and serving on staging.
Live acceptance is complete, including the repaired web approval path: the operator reviewed and
approved the original Identity caller's broker request, its execution succeeded, and the caller's
exact-UID launcher read grant succeeded. All three owned test Sandboxes and their related resources
were cleaned up; retained History for the Pod's Codex and sampler Sessions remains readable.

[Machine-readable evidence](evidence.json) records source commits, serving image tags/digests,
applied Flux artifact revisions, exact test resource UIDs, turn IDs and completed tool outputs.
It contains no bearer tokens, private keys or Secret contents.

## Release and reconciliation

| Change         | Merge commit                               | Final-head CI                                                                    | Post-merge release                                                               |
| -------------- | ------------------------------------------ | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Switch #15742  | `642063435fcd7246e3b973a45de103264bdeb867` | [43190948740](https://github.com/agentydragon/ducktape/actions/runs/43190948740) | [43194005237](https://github.com/agentydragon/ducktape/actions/runs/43194005237) |
| Cleanup #15744 | `004efb6317a3760fef668781e8d3fb3ba4cd63b9` | [43194008931](https://github.com/agentydragon/ducktape/actions/runs/43194008931) | [43194719868](https://github.com/agentydragon/ducktape/actions/runs/43194719868) |

Both release workflows succeeded. The affected system suites passed, including all 21 Pod cases.
The final switch visual report was reviewed, including the changed policy and launch captures.
Its first publication attempt failed on GitHub HTTP 503; the retry succeeded.

The prepared declarative holds were **never applied**. The operator merged the two PRs, and normal
GitOps reconciliation advanced configuration/schema before the new images finished publishing.
The refreshed Sandbox inventory was empty during that interval; a prior unrelated
`sampler-storage-smoke-20261112` Sandbox had already disappeared before acceptance. No archived
sampler continuation was resumed, no database was reset, and no user workspace was deleted.

At 10:24 UTC, all three owners in `ducktape-flux` were unsuspended, Ready and Healthy:
`agentplane-crds`, `agentplane-staging`, and `agentplane-staging-delivery`. These consume
ExternalArtifacts; the applied artifact digests are recorded separately from Git commits.
All staging Deployments had their desired Ready replicas and observed generation. Services
changed by cleanup served `004efb6` images; unchanged switch services served `6420634` images.
The central sampler retained its separately published Identity-to-History fix.

The live Sandbox CRD has `spec.identity` and no `spec.policySets`. The Sandbox Controller's own
ServiceAccount cannot list ServiceAccounts. Both controller replicas started; the retained logs
contain no denied ServiceAccount/PolicyBinding watches or cache synchronization failure. They do
contain retried resource-version conflicts during reconciliation; acceptance converged afterward.

## Live checks

Identity and its exact selected PolicyBinding were created explicitly with the operator's
Kubernetes credentials, then the app's authenticated `CreateEnvironment` facade created each
Sandbox. This exercises the app API/controller route, **not** the browser's operator OIDC
Identity/grant creation flow. The selected Pod grants were only `basic-identity`,
`inference-experiments`, and `k8s-account-shared-diagnostics`; the controller added no grant or
per-Sandbox ServiceAccount.

- Pod `identity-cutover-pod-4ffacb44` reached Ready with the default ServiceAccount and token
  automount disabled. Its Sandbox-owned WorkloadCredential named the live Identity UID and
  current Pod UID.
- From inside that Pod, `agentplane egress rules` discovered the explicit Kubernetes placeholder.
  CA-verified requests through its normal egress proxy authenticated as
  `system:serviceaccount:agentplane-staging-k8s-accounts:shared-diagnostics`.
  Pod reads succeeded; deleting a nonexistent Secret with `dryRun=All` returned HTTP 403.
- An app-provisioned Codex/Luna Session completed two turns. Each recorded a successful execution
  tool output with the requested marker and a matching `TURN_STATUS_COMPLETED`, rather than only
  assistant prose.
- Operator suspend removed the old Pod and credential while retaining state/workspace PVCs.
  Resume kept the Sandbox and Identity UIDs, created a new Pod UID and credential ID, then the
  same Session completed a further tool turn.
- A fresh central-sampler/Luna Session completed a tool-using turn and its next input, with
  successful structured tool output and matching turn completion for both.
- Direct Identity `sessions create` returned `PERMISSION_DENIED`. The caller-delegated
  `sessions/create` Action was auto-approved by the existing basic policy but its execution
  failed with `EXECUTION_ERROR_KIND_BACKEND_REFUSED`. Neither attempt created its requested
  child Session. This is the operator-selected cutover behavior; lifecycle grants remain
  [separate work](../../plans/task_dag.md#session_subagent_delegation--a-session-creates-and-messages-a-sub-session).
- A missing Identity was refused before any Sandbox existed. A changed same-name launch returned
  conflict and preserved the existing Sandbox, Identity and grant. Replacing a live Identity
  was covered by CI; no live Identity replacement probe was performed.
- VM `identity-cutover-vm-c2dc1771` reached Ready and Gateway connected. Its admitted virt-launcher
  used default/no automount; its credential named the exact Identity and VMI UIDs and projected
  the current launcher Pod UID. Initial image downloads consumed most of the first six-minute
  observation window. The same launch subsequently reached Ready; it was not recreated.
- The disposable VM was suspended and deleted through the app. Its credential disappeared and
  its separate Identity remained. After checking references, that exact owned Identity was
  deleted with UID/resourceVersion preconditions; its binding and both owned PVCs disappeared.

## Operator approval, cleanup and test transport

[Broker request ca00e722-ec5b-46a1-a6a5-2e103f2f82d8](https://agentplane-staging.allegedly.works/#/actions/ca00e722-ec5b-46a1-a6a5-2e103f2f82d8)
requests one disposable `environment-tools` Sandbox, `identity-cutover-broker-5064d58b`, with only
`basic-identity` and shared diagnostics, plus `list_sessions` for the caller on that exact Sandbox
UID. The original caller error was repaired by
[#15860](https://github.com/agentydragon/ducktape/pull/15860), merged as
`abdc5a3d8aacb8390265dccadb6aea6a0b566271`. Its final-head checks and published visual review passed;
all changed captures were inspected, including desktop/mobile Identity name, UID and approval controls.
The successful post-merge release [43207589343](https://github.com/agentydragon/ducktape/actions/runs/43207589343)
contains that merge. Flux selected `devel-20261112112908-7f78327`; both frontend replicas became Ready,
and the public `main.js` contained the new Identity validation and no longer contained the old caller error.
Flux's first image-pin push failed with `object not found`; its normal retry succeeded. No manual image
pin or direct Deployment patch was used.

At 11:35:17 UTC the operator approved the preserved request using the web app. Its recorded operator
Decision and successful execution created Sandbox UID `32694684-d4e2-4462-aa35-2291d278eb5f` and
Identity UID `5b347d02-185b-480c-9224-a92afbb0fbd4`, with exactly the requested PolicySets.
The Sandbox reached Ready using default/no token automount and an exact Identity/Pod-bound credential.
Its Identity-owned selected-grants binding and dual-owned launcher binding were verified; the launcher
PolicySet allowed only `orchestration_v1/list_sessions` with this exact Sandbox name and UID.
The parent Identity's request `b52cd6b7-88c9-49c8-8d0d-5d474e69b4b2` was then auto-approved by that
specific launcher rule and returned the empty Session list. This approved platform-authority broker path
is separate from the correctly denied direct and caller-delegated Identity Session-creation attempts.

The broker and resumed parent were suspended and deleted through the app with exact Sandbox UIDs.
Each separate Identity survived Sandbox deletion, then was explicitly deleted only after reference checks,
with UID/resourceVersion preconditions. A final inventory verified no owned Sandbox, Identity,
WorkloadCredential, PolicyBinding, PolicySet, Pod, VM/VMI, ServiceAccount or disposable PVC remained,
including the dual-owned launcher binding. Both test Sessions' retained History remained readable.
The earlier VM cleanup is recorded above. No unrelated resources or History were deleted.

The first manual live Ollama test stopped before submitting a model turn: Python gRPC expected a
Dispatcher DNS SAN while the serving certificate intentionally has only
`spiffe://agentplane/session-dispatcher`. This was a host test transport failure, not a failed
Identity tool turn. [#15834](https://github.com/agentydragon/ducktape/pull/15834) repairs only manual
live fixtures using the production Go CA/SPIFFE verifier through a loopback TLS proxy. The common
k3s path remains unchanged. Separate focused Go/Python checks and its final-head PR CI passed. The selected manual rerun
reached Dispatcher admission and input delivery, then timed out after 300 seconds awaiting the
first Ollama/Gemma output ([run](https://app.buildbuddy.io/invocation/ca3866a7-bb62-4b7c-bf32-ea17481841c2)).
Its test Sandbox was cleaned up. This is a passing transport observation, not a passing Ollama
end-to-end test; the successful Luna turns above remain separate evidence.

Rollback must restore schema/configuration, controller RBAC/watch behavior and compatible images
together. The pre-cutover Deployment image tags and Flux artifact snapshot are included in the evidence;
no rollback was performed.
The operator's archived-thread waiver does not cover the new acceptance Sessions or unrelated new
work. Preserve their History and any later user data during a forward repair or reviewed rollback.

The prompt/schema guidance repair [#15844](https://github.com/agentydragon/ducktape/pull/15844)
merged as `012f1dccc1051bff8595fdf281b48f2751e1b046` after its generated-manifest and final-head
checks passed. It describes bound Identity credentials and complete explicit launch grants, and
distinguishes denied Identity lifecycle calls from operator provisioning and approved broker Actions.
It changes no authorization rule. Serving the updated guidance remains a separate rollout observation.

Live LLM Ingress and History mount `policy_namespace: agentplane-staging`; the Runner Gateway receives
that namespace through its existing `AGENTPLANE_RUNNER_GATEWAY_POLICY_NAMESPACE` environment setting.
Their serving images, observed generations and configuration sources are recorded in the evidence.
The source-level removal of optional verifier inputs remains separately open in `SANDBOX_IDENTITY_SPEC`;
this record accepts the live cutover and does not claim that cleanup has landed. The unused rollout hold
plan and patch were retired. Lasting recovery guidance is in
[workload authentication](../workload_authentication.md#sandbox-rollout-and-recovery).
