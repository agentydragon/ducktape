# Runner Gateway API

The [Runner Gateway](README.md) serves `RunnerGateway`
([`runner_gateway.proto`](../proto/runner_gateway.proto)) over gRPC: Sessions and Session
placement. Its replicas hold the runner connections; a Sandbox's
`<sandbox>-control` Service reaches the replica holding its runner. Callers use
[`RunnerGatewayClient`](client.py) over a channel to the gateway, and its `SandboxControlClient`,
which dials the Sandbox's `<sandbox>-control` Service for each call only the holder answers.

Callers authenticate under the gateway's own token audience and `caller_accounts`. Each request
checks the caller before resolving its Sandbox destination.

## Sessions

The gateway keeps no Session history; runners push their Events to the
[History Service](../history_service/README.md)'s ingress, which serves reads.

- `ListSessions`: Sandbox destination; the current runner's retained Sessions. The request verifies
  the Sandbox namespace, name, owner and UID before reading that incarnation's runner. This is live
  runner inventory and includes Sessions opened through the Dispatcher; it does not require a row in
  the Gateway's public Session registry.
- `CreateSession`: explicit Open with Sandbox destination, the client's fresh Session UUID and
  selected overrides. The UUID is the public Session ID and the runner's Session ID alike. The
  Session is registered in its Sandbox incarnation before runner contact; the gateway returns the
  UUID and an attachment snapshot with it. A `{session_id}` placeholder in a selected `cwd` override
  is expanded to that UUID, just like the stored default cwd. A retry with the same UUID and inputs
  returns the Session again; the runner refuses changed inputs and the gateway refuses another
  Sandbox; a request without a UUID is invalid. The legacy Gateway `ResumeSession` accepts the public
  ID of a Session registered within the pinned Sandbox UID. The UUID does not apply to
  runner-discovered native child sessions. The app opens new Threads through Session Dispatcher;
  repository search found no production caller of this Gateway `CreateSession` RPC. Because Gateway
  `CreateSession` is the registry's only writer, Dispatcher-opened Sessions do not get a registry row.
- `CreateSession`'s setup uses the runner's existing idempotence; the Sandbox's bootstrap runs in the runner's first `OpenSession`. The response is the
  native attachment snapshot, not a claim that all setup or a model turn has completed.
- `ResumeSession`: uses exactly the runner-retained spec, without applying today's
  defaults/instructions or rerunning setup. Missing sessions and failed/interrupted setup are
  refused. This legacy Gateway wrapper requires a matching public registry row. The app's explicit
  Resume action instead calls `Runner.ResumeSession` through the Sandbox control Service, carrying
  the live Sandbox name, UID and owner to the Runner Proxy; its live-list and retained-spec checks
  do not depend on the Gateway registry.

Commands go through Session Dispatcher, which records durable intent and calls the matching
per-operation Runner RPC through the control proxy. Session listing never provisions, resumes, or
wakes a Sandbox or harness.

### Complete launch spec

`CreateSessionRequest.spec` is the complete runner `SessionSpec`; the Gateway reads no launch defaults
from the Sandbox. The caller resolves its configured preset before opening and sends every required
field, including the cwd with any session ID template already expanded. Explicit empty values remain
part of that spec. Optional `setup_script` distinguishes omitted from an explicitly empty script.

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

`BatchGetSessionSandboxes` is open to every `caller_accounts` caller and carries no transcript. It
answers from the gateway's registry, deleted Sandboxes included: a Session's public ID, Sandbox
namespace and name, and incarnation UID (empty for imported Sessions without one). It takes at most
1000 IDs and leaves out any the gateway never registered. The Gateway does not provide name-based
Session search; the History Service's `FindSessions` searches its retained event-backed association,
which is a separate source and does not replace the registry's incarnation records.

## Errors and uncertain outcomes

Errors use the shared gRPC status mapping. Runner RPC status codes and details pass through unchanged.
In particular, `UNAVAILABLE` after the control stream ends before a reply means the mutation's
outcome is unknown; it does not establish that the destination was unavailable before the runner
acted.

## Server configuration

`//agentplane/runner_gateway:server` uses `AGENTPLANE_RUNNER_GATEWAY_*` environment variables or
kebab-case CLI flags (`AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE` for its YAML file). Its token audience is
`agentplane-runner-gateway`; Sandbox lifecycle requests use the Kubernetes API and are not served here.

`port` defaults to 8080 for gRPC, which also serves the unauthenticated `grpc.health.v1.Health`.
The Agentplane app sends Thread commands through Session Dispatcher. Management uses
`lifecycle_timeout_s` (default 300).

`runner_control_port` (default 8082) is a separate gRPC listener serving only the
[runner control stream](../docs/runner_control.md), so the gateway's network policy can admit the
egress proxy to it alone. It accepts Pod tokens for `runner_control_audience` (default
`agentplane-runner-control`), and `commands_database_url` names the `sandbox_commands` database,
which currently stores the Gateway Session registry.

Deployment must grant the gateway the appropriate Kubernetes and TokenReview permissions, and keep
it the sole production server of the runner control stream.
