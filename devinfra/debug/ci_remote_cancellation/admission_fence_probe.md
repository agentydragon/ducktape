# Hosted cancellation probe (2026-10-28 UTC)

Source under test: Ducktape `3268070162641f967615e89a4ed1e7d64b749f16`,
BuildBuddy CLI 5.0.466. Hosted BuildBuddy deployment revision is unknown.

## Cancellation is not an admission fence

Two controls establish distinct behavior on the hosted service:

| Case                                                                           | Invocation                                                                                                        | Observation                                                                                    |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Cancel a fresh ID before invocation creation                                   | `46ef865c-0c55-464c-a086-f6fcc167bbf7`                                                                            | HTTP 500, `record not found`, at 02:25:36.518771Z. No build was launched for this ID.          |
| Cancel an existing invocation with no submitted actions, then submit an action | [232b2641-97fe-4241-8185-18eb683055a0](https://app.buildbuddy.io/invocation/232b2641-97fe-4241-8185-18eb683055a0) | HTTP 200 `{}` at 02:35:36.533936Z; a subsequent uncached remote action completed successfully. |

The second case used a repository rule as a synchronous analysis barrier:

1. Establish that the invocation row exists and there are no executions.
2. Call `CancelExecutions` and wait for its HTTP response.
3. Write that response into `gate.json`, return from the repository rule, and
   expose a genrule that consumes the file.
4. Execute the genrule remotely with `--noremote_accept_cached`; its output
   contains the cancellation response and `remote-dispatch-completed`.

This gives causal ordering without a fixed readiness delay. The action cannot
consume the response file before cancellation returns. Its unique input includes
this probe's invocation ID and response timestamps. The probe disabled outer CLI
retries; it did not exercise action sharing, retries, or hedges.

The action was queued at 02:35:38.246246Z, ran from 02:35:53.894856Z to
02:35:53.943620Z, and exited successfully. These worker timestamps corroborate
the ordering; the synchronous file dependency establishes it independently of
cross-machine clock alignment. Action digest:
`979fa97c97d8a29648b745fec23a158fbb68a059db5fd65a6a8f7aadf1fdabc5/310`.
Outer runner: [ec5ceb53-ef29-41db-8ddb-4f94fb8e26e0](https://app.buildbuddy.io/invocation/ec5ceb53-ef29-41db-8ddb-4f94fb8e26e0).

Two earlier attempts failed during workspace setup before creating the child
invocation; they are excluded from the cancellation results.

## Current active-action control

The current wrapper was tested separately with the repository's runner/image
configuration and actual CLI 5.0.466. A Bash entry shell sourced
`devinfra/ci/bb_remote_log.sh` and launched `bb remote` directly. A temporary
standalone workspace supplied a unique uncached genrule whose command blocks
on `tail -f /dev/null`, with `timeout 120s` as a resource guard.

- Child invocation:
  [e44b6af9-8637-44ff-8723-3be7f978341c](https://app.buildbuddy.io/invocation/e44b6af9-8637-44ff-8723-3be7f978341c).
- At 02:40:51.715614Z, GetExecution reported its action `EXECUTING`. This is a
  server execution stage; it does not establish that the command had started.
- SIGTERM was then sent only to the owned Bash entry PID. The wrapper forwarded
  TERM to `bb` and reported that its child cancellation request succeeded.
- The entry shell exited 130 at 02:40:51.879520Z. The next child-state response
  reported `COMPLETED`, status code 1, `invocation cancelled`.
- Outer invocation
  [1528b11e-756c-4cdd-99e7-a0c06666b8a9](https://app.buildbuddy.io/invocation/1528b11e-756c-4cdd-99e7-a0c06666b8a9)
  was also subsequently observed `COMPLETED`, status code 1, `invocation cancelled`. Both invocation records were disconnected.

The CLI had not printed its outer invocation URL when the signal arrived.
Identity was recovered from this child's BES `buildMetadata` field
`PARENT_INVOCATION_ID`; `parentRunId` is a different identifier and is not a
valid invocation lookup key. This reinforces why the cancellation helper uses
preassigned child IDs instead of waiting for log-derived IDs.

Both canceled execution records contained only epoch-zero worker timestamps.
Together with the server's synthetic terminal-record behavior below, this
bounds the result to server-record cancellation. No independent worker-exit
observation was obtained. The control did not require a fixed readiness delay,
produce a retry/hedge, or exercise shared-action cancellation. It is one
controlled observation, not a cancellation-latency distribution.

One additional controller preparation failed locally before launching any
remote work because its Python environment lacked `pygit2`. The successful
control constructed the actual `bb` argv directly from `devinfra/bbr.json`.
The outer and child were terminal when inspected; no extra cancellation of
unrelated work was performed.

## Ownership and consequence for CI

GitHub signals the Actions entry shell. `bb_remote_log.sh` forwards TERM to
`bb`, whose cancellation path owns the outer hosted runner. In parallel,
`cancel_bb_invocations.py` requests cancellation for the workflow's preassigned
inner test and build invocation IDs. `bazel_ci.py` runs test and then build.
The build invocation may therefore not exist when its cancellation request
arrives; an invocation that does exist can still submit new work after an
acknowledged cancellation.

The probe proves those API semantics. It does not establish that this particular
race caused every historical post-cancellation action, nor measure fleet waste.

## Source contract and visibility boundary

Official BuildBuddy source at
[`a04caa741548187c77dc991c8d8384c6788e24a9`](https://github.com/buildbuddy-io/buildbuddy/tree/a04caa741548187c77dc991c8d8384c6788e24a9)
explains both observations; this is a source snapshot, not a confirmed hosted
server revision or exact source mapping for CLI 5.0.466.

- The RPC handler authorizes against an existing invocation row before calling
  the cancellation service:
  [handler and authorization](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/server/buildbuddy_server/buildbuddy_server.go#L320-L335).
- Cancellation enumerates currently known in-progress executions. An empty set
  returns success without installing a fence against future submissions:
  [Cancel](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/execution_server/execution_server.go#L2123-L2190).
- Cancellation can synthesize a terminal execution record before physical
  worker termination. `COMPLETED` in GetExecution is therefore not worker-exit
  acknowledgment:
  [terminal record](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/execution_server/execution_server.go#L1439-L1484).
- Shared actions are skipped when the action-merger count exceeds one. That
  count tracks submitted executions, not remaining live subscribers:
  [sharing check](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/action_merger/action_merger.go#L150-L185).

## Interpretation

This probe deliberately cancels the inner invocation while allowing Bazel to
continue. It establishes the API's behavior without claiming that normal outer
runner cancellation was ignored or that the missing future-submission fence
caused a historical incident. The [active investigation](investigation.md)
contains the verified continued-action case, competing explanations, and the
remote-stop/ownership evidence needed before selecting a change. Keep the
current cancellation of submitted child work while that diagnosis is open.
