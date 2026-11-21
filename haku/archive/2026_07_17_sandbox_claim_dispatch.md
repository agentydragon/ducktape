# Dispatch via SandboxClaim instead of Job — archived design probe

Historical probe (2026-07-17), no implementation. The original Haku dispatch plane it would have
changed is archived and not deployed (<2026_08_multi_agent.md>), so this document does not describe a
currently supported launcher. Kept for two constraints, in case warm-pool dispatch is ever wanted
again: the zone perimeter and the per-job-secret blocker.

The idea: the dispatcher stamps a `SandboxClaim` against a per-zone `SandboxWarmPool` (the
agent-sandbox controller, <../../cluster/k8s/agents/agent-sandbox/>) instead of a validator-gated
`Job` plus a per-job `Secret`. It buys warm starts (no image pull or pod scheduling per job),
pause/resume (`Sandbox.spec.operatingMode: Suspended`) and deadline GC (`lifecycle.shutdownTime`).
Worth pursuing only when warm-start latency or pause/resume becomes a felt need.

## What must hold (zone perimeter)

The perimeter is namespace-scoped and owner-agnostic, so it carries over
unchanged: Kyverno mitmproxy egress injection and zone NetworkPolicies match
pods in the zone namespace regardless of whether a Job or a Sandbox owns them.
The workspaces lane ran `networkPolicyManagement: Unmanaged` (trusted,
personal; since retired); zone pools MUST keep the zone's managed policies —
verify the controller's injected defaults don't fight Kyverno on pool pods
before anything else.

Exactly-once creation maps cleanly: claim name derived from the idempotency
key, `409 AlreadyExists` = the other racer won.

## The blocker to solve first: per-job secrets

In the Job design the per-job `Secret` (prompt, per-job LiteLLM key, result token)
is mounted into the Job pod at creation. A warm-pool pod **pre-exists the
claim**, so per-job material cannot ride pod env/volumes — it must arrive
post-adoption. Options, roughly in order of appeal:

1. **Fetch-by-claim**: worker starts idle, watches for its adoption, then
   redeems a short-lived bootstrap token (projected into the pool template)
   against the gate-validator for its job payload. Inverts the flow: the
   validator authenticates the _pod_, not the Job manifest.
2. **Secret-update + inotify**: mount a per-pool Secret the dispatcher patches
   after adoption. Racy across concurrent claims on one pool; probably a dead
   end.
3. **No pool for secret-bearing zones**: standalone `Sandbox` per job (own
   podTemplate, env at creation like a Job) — keeps pause/resume and
   shutdownTime but forfeits warm starts. A valid incremental first step.

A warm pool waits on option 1; option 3 needs no new secret flow.
