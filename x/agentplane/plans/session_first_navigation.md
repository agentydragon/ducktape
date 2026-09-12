# Session-first navigation

Status: **captured, not designed.**

## Current model: Sandbox-first, mirrors Kubernetes

The app was designed to map directly onto the Kubernetes objects underneath it, and the routing
still shows it literally: `/sandboxes/:name` and `/sandboxes/:name/sessions/:sessionId`
(`app.tsx:14-19,37-61`). The landing page is `SandboxList` (`sandboxes.tsx`), a table of Sandbox
resources with their Kubernetes-condition-derived state (`STATE_COLORS`:
running/suspended/waiting_for_pod\*, `sandboxes.tsx:37-42`). Sessions are one tab inside
`SandboxPage` (`sandbox_page.tsx:309`, the `"sessions"` tab), populated by calling `listSessions(name)`
against that one sandbox's runner (`sandbox_page.tsx:161-187`).

This is an accurate model, not an arbitrary one: a Session only exists as long as its Sandbox's pod
and runner do. But it means the natural thing an operator actually wants — "what conversations do I
have, and are they still usable" — requires drilling into a specific Sandbox first.

## Proposed: session-first home view

Make the primary landing view a list of conversations (Sessions), each row showing:

- the session itself (title/thread, matching what `SessionView`/`ThreadTitle` already track in
  `session.tsx`),
- which Sandbox it belongs to, and
- that Sandbox's current state — running / suspended / **no longer exists** — reusing the state
  vocabulary `sandboxes.tsx` already has (`STATE_COLORS`), extended with a terminal "deleted" case
  that isn't a live Kubernetes condition at all.

**Explicit non-goal:** don't make the UI lie about the underlying model. A Sandbox is a real
resource with its own lifecycle (suspend/resume/delete — see `lifecycle.tsx`'s `SuspendResume`/
`ConfirmDelete`/`deletable`) and should keep a real page for viewing/managing it directly
(`SandboxPage` stays; egress rules and raw sandbox state genuinely belong to the Sandbox, not to any
one Session inside it). This is about promoting Sessions to the default view, not hiding Sandboxes
as a concept.

## The gap is smaller than it looks: a durable store already exists

`listSessions` against a Sandbox's own runner is a live, request-scoped read with "no stream"
(`sandbox_page.tsx:150,161-187`) — that part of the worry holds. But the durable side is already
built: `app/trajectory.py` is a PostgreSQL-backed store (`Thread`/`Event` tables) that a leased
per-sandbox ingestion loop in `app/bridge.py` copies every runner event into as it happens, by
design so that "a deleted sandbox loses nothing" (`trajectory.py:1-7`; corroborated in
`app/README.md`: "PostgreSQL holds the copy of every event that outlives the sandbox"). `Event.payload`
holds the full proto-JSON of each event — turns, items, tool calls/output, reasoning — not just a
thread name or index entry, and `Thread` already carries `sandbox`, `session_id`, and `name`.

This is no longer speculative: `GET /threads/with-sandboxes` now exists, returning every Thread the
operator can see (paginated newest-first is still open) paired with each Thread's own still-existing
Sandbox — normalized, not fanning `listSessions` out across every Sandbox, and tolerant of a Thread
whose Sandbox no longer exists (a miss in the returned `sandboxes` map reads as deleted). What's
still open: the response doesn't yet carry cursor pagination (see `THREAD_BROWSE_PAGINATE` in
[the task DAG](task_dag.md) for when that's needed), and the trajectory store's schema is moving
onto real Alembic migrations (in flight) rather than the ad hoc idempotent-DDL pattern its docstring
used to call out as a staging-only stopgap.

## Open questions (not decided here)

- Does a session-first list replace `SandboxList` as the `/` route, or live alongside it as a
  second top-level view? (Settled direction, per [the App Shell mock](mocks/app_shell.html):
  replace it entirely as one atomic cutover — see `UISHELL_SIDEBAR` in [the task DAG](task_dag.md).)
- What happens to a session row when its Sandbox is deleted — kept as a read-only historical entry,
  or dropped once the Sandbox is gone? (Now answerable either way, since `Thread` rows already
  outlive the Sandbox.)
- Reuse `sandboxes.tsx`'s `STATE_COLORS` vocabulary and hover-condition-detail pattern
  (`conditionLine`, `sandboxes.tsx:44-46`) for the sandbox-state-per-session display, or does a
  session list need a simpler/collapsed state representation than the full Sandbox page does?
- Does a deleted Sandbox's session become read-only against its last-ingested `Event` rows (a real
  transcript replay with no live runner behind it), or just a dead list entry with no detail view?
  `trajectory.py`'s stored payloads make the former possible, not just a metadata stub.
