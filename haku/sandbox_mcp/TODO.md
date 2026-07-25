# TODO — sandbox MCP

## Don't invalidate live sandboxes the moment the config changes

Today any change to the server's `EnvironmentConfig` changes `contract_hash`
(<config.py>), and every claim annotated with the old hash is immediately unusable:
`exec_sandbox` and `get_sandbox_info` both fail with _"sandbox `<name>` was created with
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

When the mismatch does happen there is currently no way to see _what_ differs — the
error names neither hash, and `get_sandbox_info` fails the same way instead of
answering. The agent's only recourse is to guess from the recent deploy.

- `get_sandbox_info` should **always succeed** for a service-owned claim, and report the
  claim's stored contract hash, the server's current one, and which config fields
  diverge. A read tool that refuses to read when something is wrong is backwards — that
  is exactly when it is needed.
- Surface what the box was actually bootstrapped **with**: the bootstrap script's own
  hash (or a short digest), its exit status, and when it ran. Today `bootstrap_state`
  says `succeeded` without saying _what_ succeeded, so an old-image / new-config skew
  looks identical to a healthy claim (a 2026-07-24 rollout produced exactly that — a
  `succeeded` bootstrap that had silently done only half the setup, because the config
  had moved to calling a script the running image did not yet contain).
- `list_sandboxes` should mark stale-contract claims so the agent can see the situation
  without a per-claim probe.
