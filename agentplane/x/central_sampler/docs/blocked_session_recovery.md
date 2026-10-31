# Recovering a blocked Session

A blocked Session has an unresolved provider or compaction effect, lost JavaScript state, a required
one-shot setup failure, or another live runtime handle that cannot safely continue. Ordinary
executor connection and operation uncertainty is settled at the nested tool boundary and does not
by itself block the Session. The sampler never automatically replays a possibly accepted external
operation.

The Code Mode host binding is independent of executor reachability. Attaching JavaScript does not
open an executor stream or prepare the workspace; the sampler acquires execution when a command,
patch, configured setup script, or other executor RPC needs it. A failed initial connection or
reconnect rejects the current nested tool promise, records the initialization attempt separately,
and leaves the Session and JavaScript host available. The failed command was not dispatched. If no
native session ID was retained, a later explicit tool call may open a new empty native session; an
uncertain first open can leave an empty session until Codex's normal 30-second detached-session
expiry. If an ID was retained, a later call must resume that exact session under the original Sandbox
incarnation fence. A failed or ambiguous resume never silently clears the binding or substitutes a
fresh session.

An executor operation whose remote outcome is unknown rejects its own tool promise and records
`remote_outcome: unknown` and `automatic_replay: false`. The affected process handle is marked
abandoned with its last read cursor and observed output; this does not mean the remote command failed
or exited. The cell may catch the error and continue, or an uncaught rejection produces the normal
`Script failed` result. Later explicit executor calls may reconnect, but the failed operation is not
replayed. An abandoned `write_stdin` handle rejects before sending input. Reconcile any possibly
completed external effect before asking the model to repeat it.

An ordinary connection failure is catchable at the execution call:

```javascript
try {
  text(await tools.exec_command({ cmd: "pwd", yield_time_ms: 1000 }));
} catch (error) {
  text(String(error));
}
text("JavaScript continues even if the executor is unreachable.");
```

When following an existing process with `write_stdin`, a failure can include output already
observed. The pinned Code Mode host rejects with a primitive string, so `error.message` is
unavailable. For a process handle previously returned by `exec_command`:

```javascript
try {
  text(await tools.write_stdin({ session_id: processId, chars: "", yield_time_ms: 1000 }));
} catch (error) {
  const message = String(error);
  let failure;
  try {
    failure = JSON.parse(message);
  } catch {}
  if (failure?.observed_output) text(failure.observed_output.text);
  text(failure?.message ?? message);
}
```

`after_seq` is the last observed read cursor, not proof of process exit. The error view shows at most
8 KiB of retained bytes plus an omission marker and labels both retained and display omissions. To
inspect all retained bytes, call `FollowEvents` for the public Session and find the `tool` result
`BoundaryRecord` whose JSON payload
matches `failure.process_id` and, when present, `failure.intent_id`; its
`buffered_output_base64` contains the bytes. This
does not authorize repeating the failed operation. If the record has multiple fragments, concatenate
their payloads in `fragment_index` order for the same `record_id` before parsing the JSON.

If idle process reconciliation loses the transport while reading a process, it marks the process
abandoned with its last observed cursor and buffered output, then returns control to the scheduler.

Use the API to inspect the durable record:

1. Call `GetSession` and read `blocked_reason` for the unresolved intent or handle.
2. Call `GetCommand` for the active or last command. Its `failure_reason` explains the command
   outcome when a command was active.
3. Call `FollowEvents` from the Session cursor. Inspect `ExecutionProgress` and the linked
   `BoundaryRecord` entries; correlate fragments by record ID and operations by correlation ID.
4. Reconcile the named provider request, executor process, or JavaScript cell with the external
   system using its retained evidence. Confirm whether its side effects occurred before deciding
   what work to perform next.

Do not resubmit an uncertain operation or edit the database to clear `blocked` or `pending`. After
reviewing the evidence, an authorized caller may explicitly abandon a blocked cell/runtime failure:

```text
GetSession(session_id) -> attached.last_cursor
RecoverSession(session_id, expected_cursor=attached.last_cursor, operator_note=review_summary)
```

The caller must be authenticated as the same account that opened the Session. Recovery requires an
expired owner lease and the current attached cursor, so a stale review cannot clear a newer failure.
It supports unresolved cell, nested tool, and executor intents, and lost live cells or process
handles. Existing blocked Sessions are not cleared automatically by the executor uncertainty
contract; this explicit review remains available for older blocked executor boundaries and other
supported failures. It also repairs older rejected cell calls that retained a pending pointer: the
known rejected result is preserved, and the missing model output reports that no runtime operation
was dispatched for that call. Other live handles are still abandoned under the same operator review.
Pending provider sampling, compaction, and required one-shot setup failures remain blocked and
require a new Session.

If the Sandbox's Pod was replaced, the operator may also provide
`replacement_incarnation_uid` after verifying the new Pod belongs to the same Sandbox and retains
its workspace. This changes only the stored incarnation fence, never the Sandbox destination.
Recovery abandons the old execution handles, clears the retained native session, and records both
UIDs. Subsequent calls must authenticate the supplied incarnation; a stale or wrong UID is refused.
Omitting the field preserves the existing fence. This is explicit rebinding, not automatic failover.

Recovery preserves every request and boundary record, writes an `abandoned` result with
`remote_outcome: unknown` and `automatic_replay: false`, and completes unanswered model calls with
an aborted output matching each call ID. It retains provider history and adds a model-visible note
that remote effects may have occurred and prior `store()` / `load()` values and live-cell handles are
unavailable to the continuation. It does not kill remote processes or report them exited; their
retained status becomes `abandoned`. The owner generation and JavaScript session epoch advance
together, fencing late writes and ensuring a new logical JavaScript session on any replica. The old
shared host may retain inaccessible session memory until it exits; recovery removes the active
executor binding and creates no binding for that old session. Recreate required state in a new cell.
Reconcile external effects separately before deciding what work to perform next. Recovery never
replays an uncertain provider or executor operation.

A known rejection with `blocked` false has its command outcome recorded as failed, and later input can
continue without replaying the rejected operation.
