# TODO — Agentplane operator frontend

Gaps to close if a workflow needs them; none is committed.

## Per-Action widget schemas generated from the servers' models

The SSH `exec` widgets' zod schemas (`actions/rendering/ssh.tsx`) and the stored result in
`visual/harness.tsx` mirror `x/ssh_mcp_server/server.py`'s `exec` signature and `ExecResult` by
hand. The schemas are strict, so a field the server adds sends real results to the generic view
while every test still passes. Consider generating them from the pydantic models, for example from
the JSON Schema FastMCP publishes as the tool's `inputSchema` and `outputSchema`, converted to zod at
build time.

## Thread view UX

- **Chronological-debug link renders full width**: `ChronologicalDebugLink` (`chronological_debug.tsx`) is a compact
  Mantine `Button`, but the evidence page view in `projected_session.tsx` places it inside a `Stack`, whose default
  `align="stretch"` stretches every child to the container's width -- the button ends up as a full-width bar
  instead of a small pill.
- **Thread id shown redundantly once a thread has a title**: `ThreadTitle` (`thread_title.tsx`) uses the thread id
  both as the title field's placeholder when unset and as an always-visible dimmed caption next to the title once
  one is set. Once a title exists, the id line adds clutter for no benefit; it should move somewhere less
  prominent instead -- the composer's overflow menu (`Menu.Dropdown` in `projected_session.tsx`, currently "Debug
  history" / "Shut down harness") is one candidate.
- **Mobile topbar's only content is an inert hamburger**: `.agentplane-mobile-topbar` (`app.tsx`, `shell.css`) shows
  below 560px width and holds only the `IconMenu2` button that opens the sidebar -- nothing else fills that bar's
  width. Consider moving `ThreadTitle` (`thread_title.tsx`) into it on mobile, since `ProjectedSession` currently
  renders its own title/id row inline in the thread page rather than sharing the shell's topbar.
- **Per-message evidence icon is always-on visual noise**: `EvidenceToggle` (`projected_session.tsx`, the
  magnifying-glass `IconZoomCode` button) renders unconditionally at every one of its 7 call sites, one per
  message/entity, whether or not a reader is looking at that row. Consider a per-message overflow affordance
  instead -- e.g. a vertical-dots button, shown only on hover (desktop) or tap (mobile), holding this and other
  message-level debug actions -- mirroring the composer's existing `Menu` + `IconDotsVertical` pattern
  (`projected_session.tsx`, "Debug history" / "Shut down harness").
- **Consider squashing the "observation" naming layer**: not remembered as a deliberate design choice. `Event`
  (`protocol/event.proto`)'s payload is a `oneof` field literally named `observation`; that name then propagated
  outward into `runner/observation.py`'s `Observation` type, the archive/API layer
  (`app/agent_runtime/events/event_log.py`'s `observations()`/`observation_entry()`, `api.py`'s
  `/observations/{cursor}` and `/evidence/{observation_cursor}/frames` routes), and the debug UI
  (`chronological_debug.tsx`'s "Observation N raw frames"). Worth revisiting whether this is a distinction worth
  keeping or whether it should just say "Event" everywhere a stored `Event` is meant.

## Hidden characters in what the operator approves

Bidi controls, zero-width and other default-ignorable characters, and control characters render
invisibly, so a command or argument can read differently from what runs. highlight.js escapes only
markup characters and `JSON.stringify` only C0 controls, so neither the highlighted views
(`syntax_highlight.tsx`) nor plain text (titles, descriptions, stdout, drawn arguments) show them.
One way: in `highlight()`, between highlight.js and DOMPurify, wrap each
`[\p{Bidi_Control}\p{Default_Ignorable_Code_Point}]` match and each control character other than tab
and newline in a span showing its code point; do the same in plain text through a small React helper;
and warn on the card, as GitHub does for bidi text. Bidi controls and tag characters warrant a loud
marker; emoji joiners and variation selectors a quiet one.
