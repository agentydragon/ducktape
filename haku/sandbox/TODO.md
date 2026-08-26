# TODO — Agent Sandbox client

## Say what the bootstrap actually did

`bootstrap_state: succeeded` does not say _what_ succeeded. The claim already stores the
started/completed timestamps and the digest of the script that ran, but `SandboxInfo`
surfaces none of them, and the bootstrap's exit status is not stored at all. An
old-image / new-config skew therefore still looks identical to a healthy claim — a
2026-07-24 rollout produced exactly that, a `succeeded` bootstrap that had silently done
only half the setup, because the config had moved to calling a script the running image
did not yet contain.

## Report pod image skew

`get_sandbox_info` should warn when the running pod's image is behind the one the pool's
template now names. The pod's image is in its spec and needs no annotation, but the
template side is out of reach: Console's Role in `haku-sandbox` grants only
`sandboxclaims`, `sandboxes`, `pods`, and `pods/exec`
(<../../cluster/k8s/haku/workspaces/app/haku-console-sandbox-role.yaml>). Reading the
`SandboxWarmPool` or its template needs a new rule, so this is an RBAC change plus a
comparison, not a comparison alone.

Observed 2026-07-25: Flux rolled a new `haku-sandbox-image` pin, and every pod _created_
after that used it — but the pool's already-idle warm pod was **not** recycled, and a claim
made three minutes after the bump adopted it, still on the previous image. Meanwhile
`get_sandbox_info` reported `state: ready, bootstrap_state: succeeded` with nothing to
suggest the box was a version behind. The convergence is by turnover — pods age out and
claims expire — so it is correct within a claim TTL, but silent throughout.

**Claimed pods keeping their image is correct.** Yanking a box mid-run to roll an image
would be worse than the skew, so this warns and never acts.

**An idle warm pod being handed out stale is arguably a pool bug**, not just a reporting
gap — `updateStrategy: Recreate` on the `SandboxWarmPool` did not replace it when the
template changed. Check whether the upstream controller intends to, before building
anything on the assumption that a fresh claim implies a fresh spec.
