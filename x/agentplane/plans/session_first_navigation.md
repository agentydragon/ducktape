# Session-first navigation

Status: **captured, not designed.** A bigger, later redesign than
[mobile density](mobile_density.md), which this complements: mobile density is about decluttering
the views that exist today; this is about which view is the _default/primary_ one.

## Current model: Sandbox-first, mirrors Kubernetes

The app was designed to map directly onto the Kubernetes objects underneath it, and the routing
still shows it literally: `/sandboxes/:name` and `/sandboxes/:name/sessions/:sessionId`
(`app.tsx:14-19,37-61`). The landing page is `SandboxList` (`sandboxes.tsx`), a table of Sandbox
resources with their Kubernetes-condition-derived state (`STATE_COLORS`:
running/suspended/archived/waiting_for_pod\*, `sandboxes.tsx:36-41`). Sessions are one tab inside
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
- that Sandbox's current state — running / suspended / archived / **no longer exists** — reusing
  the state vocabulary `sandboxes.tsx` already has (`STATE_COLORS`), extended with a terminal
  "deleted" case that isn't a live Kubernetes condition at all.

**Explicit non-goal:** don't make the UI lie about the underlying model. A Sandbox is a real
resource with its own lifecycle (suspend/resume/delete — see `lifecycle.tsx`'s `SuspendResume`/
`ConfirmDelete`/`deletable`) and should keep a real page for viewing/managing it directly
(`SandboxPage` stays; egress rules and raw sandbox state genuinely belong to the Sandbox, not to any
one Session inside it). This is about promoting Sessions to the default view, not hiding Sandboxes
as a concept.

## The real gap: there is no cross-sandbox session listing today

This is not just a layout change. `listSessions` is called per Sandbox name and talks to that
Sandbox's own runner directly, with "no stream" (`sandbox_page.tsx:150,161-187` — re-polled, not
pushed). Two consequences for a session-first home view:

1. **Building the list requires fan-out or a persisted index.** Fanning `listSessions` out across
   every Sandbox from `SandboxList`'s existing fetch would work for _running_ sandboxes, but:
2. **A suspended/archived/deleted Sandbox has no runner to ask.** The operator explicitly wants to
   see "does the sandbox still exist" for a session whose sandbox might be gone — that information
   can't come from asking a runner that no longer exists. That implies some durable,
   server-persisted record of "this session belongs to this sandbox" (and probably a last-known
   thread title/summary) that outlives the runner, not just a live fan-out at render time.

Whether that durable index already exists elsewhere in the backend (the runner/store split
mentioned in `sandbox_page.tsx`'s "Thread names by session id: the store's copy, which outlives the
runner's list", line ~145) or needs new persistence is an open question for whoever picks this up —
worth checking before assuming new backend work is required.

## Open questions (not decided here)

- Does a session-first list replace `SandboxList` as the `/` route, or live alongside it as a
  second top-level view (e.g. the nav-overflow split from [mobile density](mobile_density.md))?
- What happens to a session row when its Sandbox is deleted — kept as a read-only historical entry,
  or dropped once the Sandbox is gone?
- Reuse `sandboxes.tsx`'s `STATE_COLORS` vocabulary and hover-condition-detail pattern
  (`conditionLine`, `sandboxes.tsx:44-46`) for the sandbox-state-per-session display, or does a
  session list need a simpler/collapsed state representation than the full Sandbox page does?
