# Runner Gateway API

The [Runner Gateway](README.md) serves `RunnerGateway`
([`runner_gateway.proto`](../proto/runner_gateway.proto)) over gRPC: Sessions, commands, Session
placement and teardown's seal. Its replicas hold the runner connections; a Sandbox's
`<sandbox>-control` Service reaches the replica holding its runner. Callers use
[`RunnerGatewayClient`](client.py) over a channel to the gateway, and its `SandboxControlClient`,
which dials the Sandbox's `<sandbox>-control` Service for each call only the holder answers.
Teardown in the Sandbox Service uses `SandboxControlClient` alone.

Authentication and destinations work as in the
[Sandbox Service API](../sandbox_service/API.md#authentication-and-destinations), under the gateway's
own token audience and `caller_accounts`.

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
- `CreateSession`'s bootstrap and setup use the runner's existing idempotence; the response is the
  native attachment snapshot, not a claim that all setup or a model turn has completed.
- `ResumeSession`: uses exactly the runner-retained spec, without applying today's
  defaults/instructions or rerunning setup. Missing sessions and failed/interrupted setup are
  refused.
- `SubmitCommand`: names the public Session ID alone; the gateway routes it to the Sandbox
  incarnation the Session was opened in, and refuses one that is gone or being torn down. It forwards
  an unchanged common-protocol `Command` only to a running harness and returns the original
  `EventEntry` containing its exact matching `CommandAdmitted`. The submission is recorded in the
  `sandbox_commands` database before the runner is contacted, keyed by Session and command ID with
  the caller's identity. An exact retry by the same caller returns the retained receipt without
  contacting the runner once the admission is known; a different command or caller under the same
  ID is `ALREADY_EXISTS`. A submission whose reply was lost is settled only by delivering it again:
  an exact retry sends the same command, and the runner answers a command it already journaled with
  its original receipt. Nothing reads the runner journal or delivers it again in the background, so
  a submission nobody retries stays `PENDING_ADMISSION`. The command reaches the runner over its
  [runner control stream](../docs/runner_control.md), through the submission's dispatch attempt;
  with no live runner connection the call is `UNAVAILABLE`. A runner refusal returns
  `FAILED_PRECONDITION` (`ALREADY_EXISTS` for a conflicting command, `INVALID_ARGUMENT` for an
  invalid one) and leaves the submission pending; an exact retry reopens the attempt and is answered
  with the original receipt even after the harness stopped or its journal was sealed. A recorded
  admission or rejection never changes, and submissions are kept forever: nothing expires or deletes
  them.
- `GetCommand`, `ListCommands`: what the gateway retained for a public Session's submissions, for
  any allowed caller, without contacting the runner: the command, its state (`PENDING_ADMISSION`,
  `ADMITTED` with the same receipt `SubmitCommand` returns, or `REJECTED` with the runner's refusal),
  and when it was accepted. This is how a caller whose `SubmitCommand` response was lost, or that
  reconnects, learns the outcome; a pending command is settled only by an exact retry or a late
  receipt for a delivery already sent, and `PENDING_ADMISSION` does not say whether the runner
  journaled it. Admission is the runner journaling the command, not the harness acting on it. They
  name the public Session ID alone, and answer after its Sandbox is gone: the submissions outlive
  it. `ListCommands` pages in acceptance order and filters by state.

Session listing and command RPCs never provision, resume, or wake a Sandbox or harness.

### Launch overrides and field presence

`CreateSessionRequest.spec` uses the runner `SessionSpec`. Its `override_mask` names the exact proto
fields to replace in stored defaults, including fields explicitly set to empty/default values. For
example, `paths: ["model", "instructions"]` selects `spec.model` and `spec.instructions`; an empty
instructions string clears the caller's inherited instructions, but not backend platform guidance.
Nested paths, unknown paths, duplicate paths, and supplied nondefault fields outside the mask are
refused. An empty mask means no overrides. Optional `setup_script` distinguishes omitted from empty.

`platform_instructions` in deployment configuration is the complete deployment-wide platform
instruction block; deployment construction supplies service URLs and assembles all shared guidance
into this value. The Runner Gateway passes it through unchanged. On `CreateSession`, the gateway
prepends the explicit Sandbox/session destination (and notification destination when configured),
then appends the effective session `instructions`, stored or overridden. A non-empty
`platform_instructions` value is required; there is no runtime default. Stored specs are never
rewritten. `CreateSession` persists the original effective settings under the caller's key, so
changed defaults cannot change a response-lost retry.

Resume also needs the native harness's retained conversation. The pinned Claude harness can refuse
resuming an empty conversation that never persisted a turn. The gateway surfaces that refusal; it
does not fabricate native history or claim runner admission proves native persistence.

## Events

The gateway serves no Events. Runners push their journals to the
[History Service](../history_service/README.md)'s ingress, which serves reads. To inspect a
session's harness and setup state, read its `ListSessions` summary. There is no separate inspection
or bootstrap RPC added for tests. Bootstrap runs through `CreateSession`; exact retries use the
runner's stored bootstrap result.

## Session placement

`BatchGetSessionSandboxes` and `FindSessions` are open to every `caller_accounts` caller and carry
no transcript. Each answers from the gateway's registry, deleted Sandboxes included: a Session's
public ID, Sandbox namespace and name, and incarnation UID (empty for imported Sessions without
one). `BatchGetSessionSandboxes` takes at most 1000 IDs and leaves out any the gateway never
registered; `FindSessions` returns every Session in a Sandbox with exactly the given name, or whose
name contains the given text, ignoring case.

## Sealing

`SealSandbox` is teardown's seal ([Sandbox Service teardown](../sandbox_service/API.md#teardown)).
Only the gateway's `sealer_accounts`, the Sandbox Service's account, may call it.

## Errors and uncertain outcomes

Status codes are those of the
[Sandbox Service API](../sandbox_service/API.md#errors-and-uncertain-outcomes), plus
`ALREADY_EXISTS` when a `SubmitCommand` command ID already belongs to a different submission.

Neither a successful write nor a timeout proves admission/rejection. A mutation may commit before
its response is lost. Settle a command by retrying it with the unchanged ID/payload; admission is
neither harness consumption nor command completion nor inbox acknowledgement. A recorded submission
is not a queue: nothing delivers it later, and the runner journal stays the receipt authority.

## Server configuration

`//agentplane/runner_gateway:server` uses `AGENTPLANE_RUNNER_GATEWAY_*` environment variables or
kebab-case CLI flags (`AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` for its YAML file). It accepts the
Sandbox Service's account for `SealSandbox` through `sealer_accounts`. Its token audience is
`agentplane-runner-gateway`, so a token for the Sandbox Service is refused here.

`port` defaults to 8080 for gRPC, which also serves the unauthenticated `grpc.health.v1.Health`.
`SubmitCommand` waits up to `runner_admission_ack_timeout_s` (default 15) for runner journal
admission. Configure the app client's `sandbox_service_command_admission_timeout_s` (default 20)
above that server budget. Management uses `lifecycle_timeout_s` (default 300).

`runner_control_port` (default 8082) is a separate gRPC listener serving only the
[runner control stream](../docs/runner_control.md), so the gateway's network policy can admit the
egress proxy to it alone. It accepts Pod tokens for `runner_control_audience` (default
`agentplane-runner-control`), and `commands_database_url` names the `sandbox_commands` database,
which holds its connection rows next to command admission.

Deployment must grant the gateway the appropriate Kubernetes and TokenReview permissions, and keep
it the sole production server of the runner control stream.
