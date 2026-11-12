# Sandbox Identity cutover, 2026-11-12

The switch [#15742](https://github.com/agentydragon/ducktape/pull/15742) and cleanup
[#15744](https://github.com/agentydragon/ducktape/pull/15744) are merged and serving on staging.
Pod and VM acceptance passed the checks below. **Operator approval acceptance remains open:**
the web app throws `The Action Service omitted ActionRequest.caller.` for an Identity caller's
pending Action. The backend receipt correctly contains `callerIdentity` and no ServiceAccount
`caller`. Do not describe the entire rollout as accepted until that path is repaired and exercised.

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

## Remaining acceptance and test transport

[Broker request ca00e722-ec5b-46a1-a6a5-2e103f2f82d8](https://agentplane-staging.allegedly.works/#/actions/ca00e722-ec5b-46a1-a6a5-2e103f2f82d8)
requests one disposable `environment-tools` Sandbox, `identity-cutover-broker-5064d58b`, with only
`basic-identity` and shared diagnostics, plus `list_sessions` for the caller on that exact Sandbox
UID. It remains pending. The operator tried to review it and reported the frontend caller error.
Fix the caller presentation, verify its published visuals and rollout, then obtain a real operator
decision and verify the broker-created Identity/grants/Sandbox. Kubernetes admin credentials are
not an operator OIDC decision. Keep the parent acceptance Identity until that request is settled;
then remove only the recorded owned acceptance resources and preserve their retained evidence.

The first manual live Ollama test stopped before submitting a model turn: Python gRPC expected a
Dispatcher DNS SAN while the serving certificate intentionally has only
`spiffe://agentplane/session-dispatcher`. This was a host test transport failure, not a failed
Identity tool turn. [#15834](https://github.com/agentydragon/ducktape/pull/15834) repairs only manual
live fixtures using the production Go CA/SPIFFE verifier through a loopback TLS proxy. The common
k3s path remains unchanged. Separate focused Go/Python checks passed; its selected manual live
smoke and PR CI are tracked independently of the live checks above.

Rollback must restore schema/configuration, controller RBAC/watch behavior and compatible images
together. The pre-cutover image snapshot was taken before deployment; no rollback was performed.
The operator's archived-thread waiver does not cover the new acceptance Sessions or unrelated new
work. Preserve their History and any later user data during a forward repair or reviewed rollback.
