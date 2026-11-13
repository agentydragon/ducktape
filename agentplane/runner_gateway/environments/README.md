# EnvironmentService

`EnvironmentService` owns managed Sandbox creation, template listing, discovery, watches, and
separately authorized lifecycle and policy mutations. It is a separate gRPC service from the Session Dispatcher,
co-hosted by the Runner Gateway on its existing listener. Its public contract is defined in
[`agentplane/environments/v1/environment_service.proto`](../../environments/v1/environment_service.proto).
Its discovery and resume method paths are:

- `/agentplane.environments.v1.EnvironmentService/ListEnvironments`
- `/agentplane.environments.v1.EnvironmentService/GetEnvironment`
- `/agentplane.environments.v1.EnvironmentService/WatchEnvironments`
- `/agentplane.environments.v1.EnvironmentService/WatchEnvironment`
- `/agentplane.environments.v1.EnvironmentService/ResumeEnvironment`

Methods retain direct TokenReview-verified ServiceAccount credentials. List/get and both watches
also admit workload credentials naming an Identity and explicitly configured Actions delegation.
Mutations keep their existing ServiceAccount gates; management requires its own direct Pod-bound
manager or launcher grant. Delegated calls use the existing Dispatcher audience and are opted into
by exact full RPC method name. Broad ServiceAccount readers are checked by namespace, name, and
immutable UID. Identity callers can list/get/watch only Sandboxes whose current
`spec.identity.name` names that Identity, with its exact current UID. A same-name replacement
follows the Sandbox's deliberate name-based binding, while a credential for the old UID is refused.
Ordinary ServiceAccount same-name self-discovery is retired: its list is empty and get/watch returns
`NOT_FOUND`. A target handle is the Sandbox namespace, name, and UID; get returns `NOT_FOUND` for an
absent or replaced target and for an unauthorized Identity.

`GetEnvironment` returns the current descriptor immediately. `WatchEnvironment` accepts the same
exact target and sends a current descriptor followed by changed snapshots. It observes Kubernetes
state, including current incarnation Pod diagnostics, even while the target is suspended, with
no connection to its Runtime. Each snapshot repeats the live target and caller-specific Identity
ownership check. Metadata changes and observation timestamps alone
do not emit duplicate snapshots.

The watch is a current-state subscription: intermediate states may be coalesced, and reconnection
starts with a fresh snapshot. Target deletion/replacement or loss of access ends it with `NOT_FOUND`;
a lost Kubernetes source ends it with `UNAVAILABLE`. Clients retain their last observation as stale
and reconnect to observe again. Waiting for readiness is a client operation over this stream;
neither get nor watch requests resume. CLI and Action/MCP wait adapters are separate work.

List requires a limit from 1..100. Ordinary ServiceAccount callers see an empty inventory. Identity
callers page through their visible Sandboxes with a lexical Sandbox-name cursor; it only skips
entries and cannot widen access. Broad direct readers may page over the namespace using a Kubernetes
continuation token.

`ResumeEnvironment` separately checks the exact target, resume opt-in policy, and direct operator
authority through the shared [lifecycle manager](lifecycle/README.md). Ordinary ServiceAccount
self-resume and Identity explicit resume are denied. Listing or reading a target does not authorize
changing it. The observed wake generation and opaque resume precondition bind an ordinary request to
that Sandbox UID and generation; the backend rechecks current identity, policy and state when the
token is used.

Workspace paths and harness/model/effort choices are deployment-configured facts. They are returned
only when the target's exact SandboxTemplate has a published mapping; custom templates preserve
these fields as unavailable. The workspace descriptor says whether the configured root is enforced
by that template's Runner; it does not add a cwd restriction. The current Runner creates missing
requested cwd directories while materializing a Session.
Per-target instructions are absent because the Sandbox API has no such field; bootstrap scripts are
not treated as instructions. Platform guidance is a separate source.

Readiness, operating mode, stop stage, automatic wake-on-send status and explicit-resume eligibility
are separate current facts. Explicit-resume eligibility includes a denial reason and is evaluated
for a direct ServiceAccount against its live UID and configured operator grant; it does not depend
on `wakeOnSend`. Automatic Identity wake has its own assessment
and uses the shared lifecycle manager under the `wakeOnSend` and existing
`resumePolicy.allowOwnServiceAccount` opt-in. Configured `wake_callers` remains an automatic-only
ServiceAccount broker grant.

Call `EnvironmentService.ResumeEnvironment` explicitly, then observe the same target again.
`REQUESTED` confirms acceptance of desired running state, not readiness; no Session or input is
opened or replayed.

The CLI exposes `agentplane environments list` and
`agentplane environments get NAMESPACE NAME UID`, and
`agentplane environments resume NAMESPACE NAME UID --resume-token TOKEN`; all use
`AGENTPLANE_SESSIONS_URL` with an optional `AGENTPLANE_SESSIONS_CREDENTIAL` bearer for forwarders that
require it. Workload SVID routes need no bearer.

The resume response reports acceptance, not readiness. Reconcile an uncertain reply before taking
another action; wait adapters for the CLI and Action/MCP surfaces remain separate work.

For a child Session, choose a separate path under the configured root, such as
`/state/workspaces/ci-latency-2026-10-31`. The Runner creates missing cwd directories while materializing
the Session, so a prior `mkdir` is unnecessary. On templates that omit a configured workspace
boundary, this path is guidance on the mounted workspace volume; the Runner's existing path checks
remain authoritative. The target's workspace volume is shared by its Sessions; a distinct cwd is
workflow organization, not isolation.

`WatchEnvironments` sends an initial caller-visible inventory in bounded chunks and a completion
marker, followed by per-environment upserts and UID-fenced removals. List, get, and both watches
use `EnvironmentDescriptor`, with configuration, observed state, caller-specific actions, and
Kubernetes diagnostics grouped into separate submessages. Reconnection starts a new snapshot;
intermediate changes may be coalesced. Direct ServiceAccounts named by the Gateway's
`environment_readers` setting can read all Sandboxes; the grant does not authorize writes and is
not inherited by delegated callers. Ordinary ServiceAccount callers no longer receive workload
self-discovery. Identity callers see only Sandboxes bound to that Identity; their descriptors
omit the execution ServiceAccount, resume eligibility, wake generation, resume token, and
automatic-wake eligibility. A delegated Action's component reader grant is never inherited by the
effective Identity.
An idle stream stays open while its credential remains valid and closes when revalidation fails.
