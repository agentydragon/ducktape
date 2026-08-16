# Dispatch via SandboxClaim instead of Job — archived design probe

**Moved out of `plans/` on 2026-08-16, unchanged.** It called itself an archived probe from the
first line while sitting where pending work lives. The dispatch plane it would have changed is
retired (<../plans/multi_agent.md>), so nothing here is scheduled; the per-job-secret problem in
§ The blocker is the part worth re-reading if warm-pool dispatch is ever wanted again.

This is a historical probe. The original Haku dispatch plane is archived and
not deployed; this document does not describe a currently supported launcher.

Status: probe note (2026-07-17), no implementation. Motivated by the
agent-sandbox workspaces lane (<../../cluster/k8s/agents/agent-sandbox/>)
going live: the same controller could give haku workers warm starts and
pause/resume.

## What would change

`haku/x/dispatch/k8s_jobs.py` stamps a validator-gated `Job` + per-job `Secret`
into the zone namespace. The sandbox variant stamps a `SandboxClaim`
referencing a per-zone `SandboxWarmPool`; the controller hands the claim an
already-running pod.

## What it buys

- **Warm starts**: today every job pays image pull + pod scheduling (the zone
  worker image is large). A warm pool amortizes that to near-zero claim
  latency — the workspaces lane measured seconds.
- **Pause/resume**: `Sandbox.spec.operatingMode: Suspended` parks a
  long-running job (pod gone, `/workspace` PVC kept) — no Job equivalent.
- **Deadline GC for free**: `lifecycle.shutdownTime` replaces the dispatcher's
  own cleanup of finished/stuck Jobs (`activeDeadlineSeconds` only covers the
  running phase).

## What must hold (zone perimeter)

The perimeter is namespace-scoped and owner-agnostic, so it carries over
unchanged: Kyverno mitmproxy egress injection and zone NetworkPolicies match
pods in the zone namespace regardless of whether a Job or a Sandbox owns them.
The workspaces lane runs `networkPolicyManagement: Unmanaged` (trusted,
personal); zone pools MUST keep the zone's managed policies — verify the
controller's injected defaults don't fight Kyverno on pool pods before
anything else.

Exactly-once creation maps cleanly: claim name derived from the idempotency
key, `409 AlreadyExists` = the other racer won (same arbiter as
`ZoneJobStamper.create`).

## The blocker to solve first: per-job secrets

Today the per-job `Secret` (prompt, per-job LiteLLM key, result token) is
mounted into the Job pod at creation. A warm-pool pod **pre-exists the
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
   podTemplate, env at creation like today) — keeps pause/resume and
   shutdownTime but forfeits warm starts. A valid incremental first step.

## Verdict + next step

Worth pursuing when either warm-start latency or pause/resume becomes a felt
need; not before the per-job secret flow (option 1) is designed, since it
touches the gate-validator trust story in <../docs/security.md>. Next probe:
prototype option 3 (standalone Sandbox, no pool) behind a dispatch flag — it
is nearly mechanical from `render_job` and de-risks the CRD dependency without
the bootstrap-token design.
