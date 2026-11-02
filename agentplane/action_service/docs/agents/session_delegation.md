# Delegate work to a Session

Use this workflow to run a task in a separate Agentplane Session, then read the Session's result.
It works from an authorized workload shell or an external MCP connection. The caller needs an
eligible **existing** environment; an external MCP caller can have no local environment or parent
Session of its own. Authorization stays with the authenticated caller and the owning backends; a
target or parent relationship grants nothing. This workflow creates no environment or grants.
Explicit resume is available only when the receiving backend allows the authenticated effective
caller under the target's current policy; it does not grant access to other callers or to a new
environment. Automatic wake-on-send remains a separate backend policy.

## Discover the tools and an existing target

For MCP, call `tools/list` on the current connection and use the offered tools' current input
schemas. Direct names use `<group>__<action>`, such as `environments__list`; the server only lists
direct tools allowed by the current policy. If using generic Action tools, discover identifiers with
`list_actions`, then request `input_schema` from `get_action` using
`include_fields:["input_schema"]`. Submit with `request_action` using that schema, and keep its
Action idempotency key separate from the Session and command UUIDs. If it returns a pending request
or you requested a receipt, follow that same request with `get_action_result`. Do not guess tool names
or fields.

For shell clients, use `agentplane --json environments list`, `agentplane environments get` with the
returned namespace/name/UID, and `agentplane actions show environments/list`. Use
`agentplane sessions --help` for direct Dispatcher/History commands when their credentials are
configured. Otherwise use the Action-backed Session reads described below.

Start with `environments__list` (or `agentplane environments list`). Its MCP input is:

```json
{ "limit": 20 }
```

The list is filtered to existing environments whose execution account matches the authenticated
caller, including its ServiceAccount UID. If it is empty, stop and ask the operator to prepare or
identify a suitable environment. There is no create-environment action in this workflow.

Choose a returned `target` and refresh it with `environments__get` (CLI: `agentplane environments get`).
Keep the complete namespace/name/UID tuple; a replacement with the same name is a different target.
Check the returned execution account, readiness, workspace, and
`configurations`. Proceed only with a configured workspace root and offered harness/model; choose a
listed reasoning effort when supplied. Use an absolute working directory under that root, with a
separate directory per Session. If workspace or runtime choices are absent or unavailable, ask the
operator for a suitable target and settings. Do not substitute guessed runtime settings or another
target's UID.

If the exact environment is suspended or otherwise not Ready, inspect `resume_eligibility`. Only
continue when its state is `AVAILABLE`; use its fresh `resume_token` with `environments__resume`.
The service rechecks the effective caller's current policy and the complete target. A `REQUESTED`
result is accepted desired state, not readiness. Then call `environments__get` with
`wait_for_ready_seconds` from 1 through 30, and continue only when the returned readiness is
`READY`. If permission is denied or the wait expires, stop and ask the operator. This works for an
external MCP-only caller without a local environment or parent Session.

## MCP call recipe

Replace every `<...>` placeholder with a returned value: copy `target` from an
`environments__list` item, and use `configurations` and `workspace.root` from `environments__get`.
Placeholders are not literal inputs. The UUIDs below are explicit caller-minted examples; mint unique
IDs for each real Session and input. Omit `reasoning_effort` if the selected configuration offers none.

Refresh the exact target from discovery with `environments__get`:

```json
{
  "target": {
    "namespace": "<target.namespace from discovery>",
    "name": "<target.name from discovery>",
    "uid": "<target.uid from discovery>"
  }
}
```

When that descriptor reports resume eligibility `AVAILABLE`, copy its current `resume_token` and
request resume on the same exact target:

```json
{
  "target": { "namespace": "<target.namespace>", "name": "<target.name>", "uid": "<target.uid>" },
  "resume_token": "<resume_token from the fresh environments__get>"
}
```

Use that input with `environments__resume`. `REQUESTED` means the backend accepted the desired
running-state update; it does not prove readiness or prior command delivery. Read again and wait
only for a bounded interval:

```json
{
  "target": { "namespace": "<target.namespace>", "name": "<target.name>", "uid": "<target.uid>" },
  "wait_for_ready_seconds": 30
}
```

Pass this input to `environments__get`. If readiness is still not `READY` when the bound expires,
stop and reconcile with the operator before opening or submitting new work. An uncertain resume
reply is not retried; refresh the same target and use only a fresh descriptor's token. Never replay
an earlier input or execution request.

Call `sessions__open` with `harness` and `model` from one returned `configurations` entry, and put a
unique directory below the returned `workspace.root` in `cwd`:

```json
{
  "target": { "namespace": "<target.namespace>", "name": "<target.name>", "uid": "<target.uid>" },
  "session_id": "11111111-1111-4111-8111-111111111111",
  "spec": {
    "harness": "<configurations[0].harness>",
    "model": "<configurations[0].model>",
    "cwd": "<workspace.root>/<unique-session-directory>",
    "reasoning_effort": "<configurations[0].reasoning_efforts[0]>"
  }
}
```

Opening returns a Dispatcher receipt; admission alone does not prove the runtime accepted the model.
`additional_instructions` can add standing context without replacing the harness/provider prompt or
configured platform guidance; it does not start a model turn. An optional `setup_script` may run
during opening but is not model input.

Call `sessions__submit_input` to start the task explicitly with a separate input:

```json
{
  "target": { "namespace": "<target.namespace>", "name": "<target.name>", "uid": "<target.uid>" },
  "session_id": "11111111-1111-4111-8111-111111111111",
  "command_id": "22222222-2222-4222-8222-222222222222",
  "text": "<your task>"
}
```

Call `sessions__observe_input` for that exact command (`wait_seconds` is 0–30):

```json
{
  "session_id": "11111111-1111-4111-8111-111111111111",
  "command_id": "22222222-2222-4222-8222-222222222222",
  "wait_seconds": 30
}
```

If observation returns `resume_token`, pass that opaque token on the next observation for the same
IDs. A timeout ends only the read; `not_observed` does not prove work failed or finished. For the
independent opening and History summary, call `sessions__get`:

```json
{ "session_id": "11111111-1111-4111-8111-111111111111" }
```

Call `sessions__read_turns` for retained turns; if a result has `next_cursor`, pass it as
`page_cursor` on the next call:

```json
{ "session_id": "11111111-1111-4111-8111-111111111111", "count": 20 }
```

For another instruction, submit a new command UUID and observe it. Inputs can share a turn, so
correlate by command ID. Preserve source states, gaps, and errors when interpreting partial reads.

## CLI equivalent

The environment commands use the existing Dispatcher endpoint and credential configured by
`AGENTPLANE_SESSIONS_URL` and `AGENTPLANE_SESSIONS_CREDENTIAL`. Refresh, resume, then wait for
readiness before opening a Session or submitting input:

```sh
agentplane environments get '<target.namespace>' '<target.name>' '<target.uid>'
agentplane environments resume '<target.namespace>' '<target.name>' '<target.uid>' --resume-token '<fresh token from get>'
agentplane environments get '<target.namespace>' '<target.name>' '<target.uid>' --wait-seconds 30
```

`--timeout` must exceed `--wait-seconds` to receive the final readiness descriptor. `REQUESTED`
does not mean Ready. With direct Session/History credentials configured, continue with the CLI
sequence below only after readiness is confirmed:

```sh
agentplane sessions open '11111111-1111-4111-8111-111111111111' --sandbox '<target.name>' --sandbox-uid '<target.uid>' --spec '{"harness":"<configurations[0].harness>","model":"<configurations[0].model>","cwd":"<workspace.root>/<unique-session-directory>","reasoning_effort":"<configurations[0].reasoning_efforts[0]>"}'
agentplane sessions submit-input '11111111-1111-4111-8111-111111111111' --sandbox '<target.name>' --sandbox-uid '<target.uid>' --command-id '22222222-2222-4222-8222-222222222222' --text '<your task>'
agentplane sessions observe-input '11111111-1111-4111-8111-111111111111' --command-id '22222222-2222-4222-8222-222222222222' --wait-seconds 30
agentplane sessions get '11111111-1111-4111-8111-111111111111'
agentplane sessions read-turns '11111111-1111-4111-8111-111111111111' --count 20
```

Replace placeholders before running. See `agentplane sessions open --help` and
`agentplane sessions observe-input --help` for available flags. If direct Session/History credentials
are absent but Action access is available, use the Action-backed operations instead. They take the
same JSON inputs as the MCP recipe; discover each schema with `agentplane actions show GROUP/ACTION`:

```sh
agentplane actions submit sessions/open --title 'Open a task Session' --arguments @open-session.json
agentplane actions result '<request ID from submit>'
```

Put the substituted `sessions__open` input in `open-session.json` first. Then use `sessions/submit_input`,
`sessions/observe_input`, `sessions/get`, and `sessions/read_turns` with their respective recipe inputs.
Retain each submission's Action idempotency key and request ID as well as the Session and command IDs.

## Stop or reconcile safely

- `sessions__cancel_input` cancels only the named input while its exact Dispatch is still queued,
  before delivery starts. It needs the Session and command IDs; it cannot undo delivered input.
- `sessions__interrupt_turn` targets the exact active `turn_id` you observed, in a Ready target. It
  also needs the full target UID and a fresh command UUID. Interruption is not rollback of effects.
- If opening's reply is uncertain, read `sessions__get` with the same Session UUID. If input's reply
  is uncertain, call `sessions__observe_input` with the same Session and command UUIDs. Continue
  read-only reconciliation and inspect any observed effects; never send the uncertain work again
  under a new ID. `not_observed`, a timeout, or a failed source read leaves the outcome unresolved.

## Read the outcome at the right level

An Action receipt describes the Action request's decision and execution lifecycle. A succeeded Action
means its executor returned; its `execution.result` is the Session Action's Dispatcher/observation
result. A Dispatch result may only establish that input was stored or queued. `turn_completed` and a
final assistant message are evidence of a completed model turn, not independent proof that the task
was correct or complete. Inspect the retained turn, its status/error, and the actual requested
artifacts before reporting success.

Direct MCP tools normally return the Action's structured result after their bounded wait. If the
structured response instead contains an Action `request_id` and `state`, that is a lifecycle receipt,
not the Session result; continue with `get_action_result` for that same request. Generic Action calls
and CLI workflows likewise need their Action result read after the receipt. For Action lifecycle
details use [/docs/agents/action_lifecycle.md](/docs/agents/action_lifecycle.md); for lost responses
and unknown outcomes use [/docs/agents/recovery.md](/docs/agents/recovery.md). An MCP-only client can
fetch the recovery guide with `get_guide({"name":"recovery"})`.
