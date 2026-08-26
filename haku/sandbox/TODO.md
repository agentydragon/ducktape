# TODO — Agent Sandbox client

## Don't invalidate live sandboxes the moment the config changes

Today any change to the `SandboxEnvironmentConfig` changes `contract_hash`
(<config.py>), and every claim annotated with the old hash is immediately unusable:
`provision_sandbox` and `exec_sandbox` both fail with _"sandbox `<name>` was created with
different server configuration; dispose and recreate it"_ (<kubernetes_client.py>). The
running pod is fine — its checkout, caches, and any in-flight work are all still there —
but the only supported move is to throw it away.

Hit live on 2026-07-24: a bootstrap-script edit rolled out mid-session and locked the
agent out of a warm claim that had a populated `haku-state` checkout and a 30-minute-old
Bazel disk cache, purely because the _next_ claim would bootstrap differently.

What would be better, roughly in order of value:

- **Let an existing claim keep working under the contract it was created with.** The
  hash's real job is "don't hand this claim to a caller expecting a different
  environment" — that is satisfied by reporting the mismatch, not by refusing service.
  A stale claim could stay fully usable and simply be flagged, with recreation the
  caller's decision.
- **Distinguish the fields that actually matter.** A `bootstrap.script` change only
  affects claims not yet bootstrapped; `max_output_bytes` or `max_exec_timeout_seconds`
  are per-call and safe to apply to a running box; `warm_pool`/`container`/`default_cwd`
  genuinely describe the pod. One hash over `model_dump()` cannot tell these apart, so
  the cheapest correct change is a narrower hash over only the pod-describing fields,
  with the rest read live.
- **Consider refusing only on `provision_sandbox`.** Resume-an-existing-claim is where a
  contract mismatch is a real hazard; `exec_sandbox` against a box the caller already
  holds is not.

## Make running-vs-configured inspectable

The read tools say _that_ a claim is stale — `get_sandbox_info` and every entry of
`list_sandboxes` report `state: stale_config` with `reason: ConfigurationChanged` — but not
_what_ differs. Neither hash is in the response and neither is in the provision/exec error, so
the agent's only recourse is to guess from the recent deploy.

- `get_sandbox_info` should report the claim's stored contract hash, the current
  one, and which config fields diverge.
- Surface what the box was actually bootstrapped **with**: the bootstrap script's own
  hash (or a short digest), its exit status, and when it ran. Today `bootstrap_state`
  says `succeeded` without saying _what_ succeeded, so an old-image / new-config skew
  looks identical to a healthy claim (a 2026-07-24 rollout produced exactly that — a
  `succeeded` bootstrap that had silently done only half the setup, because the config
  had moved to calling a script the running image did not yet contain).

### A `warnings` field: "this pod is running a stale spec"

The shape both of the above want is a **non-fatal warning list** on the read tools, not a
refusal and not a new synthetic state. `get_sandbox_info` (and a compact form in
`list_sandboxes`) should carry something like `warnings: ["pod image
…043101-57961d1 is behind the template's …052952-ad87431", "claim contract hash predates
the current config"]` — the claim stays usable, and the agent decides whether the
skew matters for what it is about to do.

**Image skew is a real case, not a hypothetical, and it is separate from contract drift.**
Observed 2026-07-25: Flux rolled a new `haku-sandbox-image` pin, and every pod _created_
after that used it — but the pool's already-idle warm pod was **not** recycled, and a claim
made three minutes after the bump adopted it, still on the previous image. Meanwhile
`get_sandbox_info` reported `state: ready, bootstrap_state: succeeded` with nothing to
suggest the box was a version behind. The convergence is by turnover — pods age out and
claims expire — so it is correct within a claim TTL, but silent throughout.

Note the two skews need different treatment, which is the argument for a warning list over
a single boolean:

- **Claimed pods keeping their image is correct.** Yanking a box mid-run to roll an image
  would be worse than the skew. Warn, never act.
- **An idle warm pod being handed out stale is arguably a pool bug**, not just a reporting
  gap — `updateStrategy: Recreate` on the `SandboxWarmPool` did not replace it when the
  template changed. Worth checking whether the upstream controller intends to, before
  building anything on the assumption that a fresh claim implies a fresh spec.
