# Environment discovery and explicit resume

`EnvironmentService` discovers existing managed Sandboxes and accepts separately authorized
explicit-resume requests. It is a separate gRPC service from the Session Dispatcher, co-hosted by
the Runner Gateway on its existing listener. Its method paths are:

- `/agentplane.sandboxes.environments.EnvironmentService/ListEnvironments`
- `/agentplane.sandboxes.environments.EnvironmentService/GetEnvironment`
- `/agentplane.sandboxes.environments.EnvironmentService/WatchEnvironments`
- `/agentplane.sandboxes.environments.EnvironmentService/WatchEnvironment`
- `/agentplane.sandboxes.environments.EnvironmentService/ResumeEnvironment`

Methods accept direct TokenReview-verified ServiceAccount credentials. Delegated calls use the
existing Dispatcher audience and require exact full RPC method-name opt-in; `ResumeEnvironment`
must be opted in separately from reads. The handler checks the admitted effective caller's namespace,
name, and immutable ServiceAccount UID. Ordinary callers see only Sandboxes whose live, controller-owned
execution ServiceAccount has that exact identity; explicitly configured direct readers see the namespace. A Sandbox's namespace/name or labels alone do
not establish access. A target handle is the Sandbox namespace, name, and UID; get returns
`NOT_FOUND` for an absent or replaced target, and for a deleting or foreign target for ordinary callers.

`GetEnvironment` returns the current descriptor immediately. `WatchEnvironment` accepts the same
exact target and sends a current descriptor followed by changed snapshots. It observes Kubernetes
state even while the target is suspended, with no connection to its Runtime. Each snapshot repeats
the live target and execution-account checks. Metadata changes and observation timestamps alone
do not emit duplicate snapshots.

The watch is a current-state subscription: intermediate states may be coalesced, and reconnection
starts with a fresh snapshot. Target deletion/replacement or loss of access ends it with `NOT_FOUND`;
a lost Kubernetes source ends it with `UNAVAILABLE`. Clients retain their last observation as stale
and reconnect to observe again. Waiting for readiness is a client operation over this stream;
neither get nor watch requests resume. CLI and Action/MCP wait adapters are separate work.

List requires a limit from 1..100. Ordinary callers resolve their same-name candidate and receive no
continuation token; broad readers may page over the namespace using a Kubernetes continuation token.

`ResumeEnvironment` separately checks the exact target and execution account, resume opt-in policy,
and operator authority through the shared [lifecycle manager](../lifecycle/README.md). Listing or
reading a target does not authorize changing it. The observed wake generation and opaque resume
precondition bind an ordinary request to that Sandbox UID and generation; the backend rechecks
current identity, policy and state when the token is used.

The discovery `resume` field continues to describe the existing automatic-wake policy. The new
`resume_eligibility` and token fields describe explicit-resume authority. The DeliveryWorker remains
on its catalog-backed wake path until a separate automatic-wake integration switches it to the
lifecycle manager.

Workspace paths and harness/model/effort choices are deployment-configured facts. They are returned
only when the target's exact SandboxTemplate has a published mapping; custom templates preserve
these fields as unavailable. The workspace descriptor says whether the configured root is enforced
by that template's Runner; it does not add a cwd restriction. The current Runner creates missing
requested cwd directories when it opens a Session.
Per-target instructions are absent because the Sandbox API has no such field; bootstrap scripts are
not treated as instructions. Platform guidance is a separate source.

Readiness, operating mode, stop stage, automatic wake-on-send status and explicit-resume eligibility
are separate current facts. Explicit-resume eligibility includes a denial reason and is evaluated
for the authenticated effective caller against the live same-ServiceAccount identity and
`resumePolicy`; it does not depend on `wakeOnSend`. Automatic wake continues to use its separate
sender and configured wake-caller policy.

Call `EnvironmentService.ResumeEnvironment` explicitly, then observe the same target again.
`REQUESTED` confirms acceptance of desired running state, not readiness; no Session or input is
opened or replayed.

`ResumeEnvironment` reports acceptance, not readiness. Reconcile an uncertain reply before taking
another action. `GetEnvironment` returns immediately; callers can observe later state with
`WatchEnvironment`. CLI and Action/MCP adapters are separate integration work.

For a child Session, choose a separate path under the configured root, such as
`/state/workspaces/ci-latency-2026-10-31`. The Runner creates missing cwd directories when opening
the Session, so a prior `mkdir` is unnecessary. On templates that omit a configured workspace
boundary, this path is guidance on the mounted workspace volume; the Runner's existing path checks
remain authoritative. The target's workspace volume is shared by its Sessions; a distinct cwd is
workflow organization, not isolation.

`WatchEnvironments` sends an initial caller-visible inventory in bounded chunks and a completion
marker, followed by per-Sandbox upserts and UID-fenced removals. Reconnection starts a new snapshot;
intermediate changes may be coalesced. Direct ServiceAccounts named by the Gateway's
`environment_readers` setting can read all Sandboxes; the grant does not authorize writes and is
not inherited by delegated callers. Ordinary callers remain restricted to their exact owned Sandbox.
An idle stream stays open while its credential remains valid and closes when revalidation fails.
