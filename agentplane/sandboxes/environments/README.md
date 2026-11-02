# Existing environment discovery

`EnvironmentService` is the read-only inventory surface for existing managed Sandboxes. It is a
separate gRPC service from the Session Dispatcher, co-hosted by the Runner Gateway on its existing
listener. Its method paths are:

- `/agentplane.sandboxes.environments.EnvironmentService/ListEnvironments`
- `/agentplane.sandboxes.environments.EnvironmentService/GetEnvironment`
- `/agentplane.sandboxes.environments.EnvironmentService/WatchEnvironments`
- `/agentplane.sandboxes.environments.EnvironmentService/WatchEnvironment`

All methods accept direct TokenReview-verified ServiceAccount credentials and the explicitly
configured Actions delegation. Delegated calls use the existing Dispatcher audience and are opted
into by exact full RPC method name. The handler checks the admitted effective caller's namespace,
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

Workspace paths and harness/model/effort choices are deployment-configured facts. They are returned
only when the target's exact SandboxTemplate has a published mapping; custom templates preserve
these fields as unavailable. The workspace descriptor says whether the configured root is enforced
by that template's Runner; it does not add a cwd restriction. The current Runner creates missing
requested cwd directories when it opens a Session.
Per-target instructions are absent because the Sandbox API has no such field; bootstrap scripts are
not treated as instructions. Platform guidance is a separate source.

Readiness, operating mode, stop stage, wake-on-send and resume eligibility are current observed
Sandbox state. Resume eligibility reflects the existing wake policy: both the Sandbox's
`wakeOnSend` setting and the caller's current configured wake-caller status must allow it. This
service does not add lifecycle or wake authority.

The CLI exposes `agentplane environments list` and
`agentplane environments get NAMESPACE NAME UID`; both use
`AGENTPLANE_SESSIONS_URL` and `AGENTPLANE_SESSIONS_CREDENTIAL`.

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
