# Existing environment discovery

`EnvironmentDiscovery` is the read-only inventory surface for existing managed Sandboxes. It is a
separate gRPC service from the Session Dispatcher, co-hosted by the Runner Gateway on its existing
listener. Its method paths are:

- `/agentplane.sandboxes.discovery.EnvironmentDiscovery/ListEnvironments`
- `/agentplane.sandboxes.discovery.EnvironmentDiscovery/GetEnvironment`

Both methods accept direct TokenReview-verified ServiceAccount credentials and the explicitly
configured Actions delegation. Delegated calls use the existing Dispatcher audience and are opted
into by exact full RPC method name. The handler checks the admitted effective caller's namespace,
name, and immutable ServiceAccount UID, then returns only Sandboxes whose live, controller-owned
execution ServiceAccount has that exact identity. A Sandbox's namespace/name or labels alone do
not establish access. A target handle is the Sandbox namespace, name, and UID; get returns
`NOT_FOUND` for an absent, replaced, deleting, or foreign target.

List requires a limit from 1..100 and retains the paged request/response shape. The current
Sandbox-owned account mapping resolves the effective caller's same-name candidate directly, so it
returns no continuation token. The service never sends a namespace-wide Kubernetes continuation
token to a caller, because such a token can reveal the name of a filtered Sandbox.

Workspace paths and harness/model/effort choices are deployment-configured facts. They are returned
only when the target's exact SandboxTemplate has a published mapping; custom templates preserve
these fields as unavailable. The workspace descriptor says whether the configured root is enforced
by that template's Runner; it does not add a cwd restriction. The current Runner creates missing
requested cwd directories when it opens a Session.
Per-target instructions are absent because the Sandbox API has no such field; bootstrap scripts are
not treated as instructions. Platform guidance is a separate source.

Readiness, operating mode, stop stage, automatic wake-on-send status and explicit-resume eligibility
are separate current facts. Explicit-resume eligibility includes a denial reason and is evaluated
for the authenticated effective caller against the Sandbox's live same-ServiceAccount identity and
`resumePolicy`; it does not depend on `wakeOnSend`. An available descriptor includes the observed
wake generation and an opaque precondition for that exact Sandbox UID and generation. The backend
rechecks identity, policy and state when the token is used; it is not a capability.

`GetEnvironment.wait_for_ready_seconds` accepts 0..30. A positive value performs bounded,
side-effect-free reads until Ready or the bound expires; it returns the latest descriptor on timeout.
It never requests resume. Call `EnvironmentLifecycle.ResumeEnvironment` explicitly, then read or
wait again. `REQUESTED` confirms acceptance of desired running state, not readiness; no Session or
input is opened or replayed.

The CLI exposes `agentplane environments list`, `agentplane environments get NAMESPACE NAME UID
[--wait-seconds N]`, and `agentplane environments resume NAMESPACE NAME UID --resume-token TOKEN`;
all use `AGENTPLANE_SESSIONS_URL` and `AGENTPLANE_SESSIONS_CREDENTIAL`.

For a child Session, choose a separate path under the configured root, such as
`/state/workspaces/ci-latency-2026-10-31`. The Runner creates missing cwd directories when opening
the Session, so a prior `mkdir` is unnecessary. On templates that omit a configured workspace
boundary, this path is guidance on the mounted workspace volume; the Runner's existing path checks
remain authoritative. The target's workspace volume is shared by its Sessions; a distinct cwd is
workflow organization, not isolation.
