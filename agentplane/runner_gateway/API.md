# Runner Gateway API

The [Runner Gateway](README.md) serves `RunnerGateway`
([`runner_gateway.proto`](../proto/runner_gateway.proto)) over gRPC: Sessions, commands and Session
placement. Its replicas hold the runner connections; a Sandbox's
`<sandbox>-control` Service reaches the replica holding its runner. Callers use
[`RunnerGatewayClient`](client.py) over a channel to the gateway, and its `SandboxControlClient`,
which dials the Sandbox's `<sandbox>-control` Service for each call only the holder answers.

Callers authenticate under the gateway's own token audience and `caller_accounts`. Each request
checks the caller before resolving its Sandbox destination.

## Sessions and commands

The gateway keeps no Session history; runners push their Events to the
[History Service](../history_service/README.md)'s ingress, which serves reads.

- `ListSessions`: Sandbox destination; the runner's Sessions registered in this Sandbox
  incarnation. Runner sessions the registry does not know are left out.
- `CreateSession`: explicit Open with Sandbox destination, the client's fresh Session UUID and
  selected overrides. The UUID is the public Session ID and the runner's Session ID alike. The
  Session is registered in its Sandbox incarnation before runner contact; the gateway returns the
  UUID and an attachment snapshot with it. A `{session_id}` placeholder in a selected `cwd` override
  is expanded to that UUID, just like the stored default cwd. A retry with the same UUID and inputs
  returns the Session again; the runner refuses changed inputs and the gateway refuses another
  Sandbox; a request without a UUID is invalid. `ResumeSession` accepts the public ID of a Session
  registered within the pinned Sandbox UID. The UUID does not apply to runner-discovered native
  child sessions. The app opens every new Thread through it.
- `CreateSession`'s setup uses the runner's existing idempotence; the Sandbox's bootstrap runs in the runner's first `OpenSession`. The response is the
  native attachment snapshot, not a claim that all setup or a model turn has completed.
- `ResumeSession`: uses exactly the runner-retained spec, without applying today's
  defaults/instructions or rerunning setup. Missing sessions and failed/interrupted setup are
  refused.
- `SubmitCommand`: names the Session's Sandbox incarnation and public Session ID, like
  `ResumeSession`; the gateway refuses a Session not opened in that incarnation, and an incarnation
  that is gone or being torn down. It forwards
  an unchanged common-protocol `Command` only to a running harness and returns, as `admission`, the
  original `EventEntry` containing its exact matching `CommandAdmitted`. Like the lifecycle calls, it reaches
  the replica holding the runner's connection through the Sandbox's control Service, and any other
  replica refuses with `UNAVAILABLE`; the command travels over the
  [runner control stream](../docs/runner_control.md#command-delivery). The gateway records nothing:
  a caller whose reply was lost retries with the same command, and the runner answers a command it
  already journaled with its original receipt, even after the harness stopped or its journal was
  sealed. A runner refusal returns `FAILED_PRECONDITION` (`ALREADY_EXISTS` for a command ID already
  used for other work, `INVALID_ARGUMENT` for an invalid command). Admission is the runner
  journaling the command, not the harness acting on it; the Session's history in the History Service
  is where a caller reads it back.

Session listing and command RPCs never provision, resume, or wake a Sandbox or harness.

### Launch overrides and field presence

`CreateSessionRequest.spec` uses the runner `SessionSpec`. Its `override_mask` names the exact proto
fields to replace in stored defaults, including fields explicitly set to empty/default values. For
example, `paths: ["model", "instructions"]` selects `spec.model` and `spec.instructions`; an empty
instructions string clears the caller's inherited instructions, but not the runner's platform guidance.
Nested paths, unknown paths, duplicate paths, and supplied nondefault fields outside the mask are
refused. An empty mask means no overrides. Optional `setup_script` distinguishes omitted from empty.

The gateway sends the effective session `instructions`, stored or overridden, as the Session's task.
The runner adds the deployment-wide platform instructions and the Session's notification
destination ([runner SPEC](../runner/SPEC.md)). Stored specs are never rewritten. `CreateSession` persists the original effective settings under the caller's key, so
changed defaults cannot change a response-lost retry.

Resume also needs the native harness's retained conversation. The pinned Claude harness can refuse
resuming an empty conversation that never persisted a turn. The gateway surfaces that refusal; it
does not fabricate native history or claim runner admission proves native persistence.

## Events

The gateway serves no Events. Runners push their journals to the
[History Service](../history_service/README.md)'s ingress, which serves reads. To inspect a
session's harness and setup state, read its `ListSessions` summary. There is no separate inspection
or bootstrap RPC added for tests. Bootstrap runs in the runner's first `OpenSession`, which `CreateSession` makes; exact retries use the
runner's stored bootstrap result.

## Session placement

`BatchGetSessionSandboxes` and `FindSessions` are open to every `caller_accounts` caller and carry
no transcript. Each answers from the gateway's registry, deleted Sandboxes included: a Session's
public ID, Sandbox namespace and name, and incarnation UID (empty for imported Sessions without
one). `BatchGetSessionSandboxes` takes at most 1000 IDs and leaves out any the gateway never
registered; `FindSessions` returns every Session in a Sandbox with exactly the given name, or whose
name contains the given text, ignoring case.

## Errors and uncertain outcomes

Errors use the shared gRPC status mapping; `ALREADY_EXISTS` also indicates that a `SubmitCommand`
command ID already belongs to a different submission.

Neither a successful write nor a timeout proves admission/rejection. A mutation may commit before
its response is lost. Settle a command by retrying it with the unchanged ID/payload; admission is
neither harness consumption nor command completion nor inbox acknowledgement. A recorded submission
is not a queue: nothing delivers it later, and the runner journal stays the receipt authority.

## Server configuration

`//agentplane/runner_gateway:server` uses `AGENTPLANE_RUNNER_GATEWAY_*` environment variables or
kebab-case CLI flags (`AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` for its YAML file). Its token audience is
`agentplane-runner-gateway`; Sandbox lifecycle requests use the Kubernetes API and are not served here.

`port` defaults to 8080 for gRPC, which also serves the unauthenticated `grpc.health.v1.Health`.
`SubmitCommand` waits up to `runner_admission_ack_timeout_s` (default 15) for runner journal
admission. Configure the app client's `runner_gateway_command_admission_timeout_s` (default 20)
above that server budget. Management uses `lifecycle_timeout_s` (default 300).

`runner_control_port` (default 8082) is a separate gRPC listener serving only the
[runner control stream](../docs/runner_control.md), so the gateway's network policy can admit the
egress proxy to it alone. It accepts Pod tokens for `runner_control_audience` (default
`agentplane-runner-control`), and `commands_database_url` names the `sandbox_commands` database,
which holds its connection rows next to command admission.

Deployment must grant the gateway the appropriate Kubernetes and TokenReview permissions, and keep
it the sole production server of the runner control stream.
