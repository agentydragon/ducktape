# Action-request plaintext context

Status: **captured, not designed.** A submitting caller cannot say what an Action is _for_ today;
decide whether to add that, and where it renders, before touching schema or UI.

## Gap

`ActionRequestInput` (`action_service/models.py:200-209`) — the exact payload the `request_action`
MCP tool accepts (`mcp_frontend.py:239-249`) — carries only `idempotency_key`, `action` (group +
name), `arguments`, and opaque `origin`/`correlation` dicts. Nothing on it is a caller-authored
plaintext title, description, or rationale: `origin`/`correlation` are explicitly untrusted
metadata with no semantic weight (`action_service/README.md:48`, `action_service/SPEC.md:5` —
"Caller-provided provenance never assigns authority"). `ActionCard`
(`app/frontend/actions.tsx:90-119`) renders group/name, caller, and raw arguments JSON — there is
nothing else to render because the caller submits nothing else.

This is a different direction from `DecisionInput.decision_note` (`models.py:218-222`), which
already exists and is already rendered (`actions.tsx:107-110`, "Decision: **verdict** by issuer ·
note") — that's the **operator's** plaintext note attached when deciding, flowing to the caller.
What's missing is the reverse: the **caller's** plaintext context attached when submitting,
flowing to the operator deciding.

## Proposal to evaluate

Add an optional plaintext field to `ActionRequestInput` (a `title` and/or `description`, naming
TBD so it doesn't collide with `decision_note`'s vocabulary) that the calling client fills in when
it submits an Action, threaded through `ActionRequestView` to the approver. Render it in
`ActionCard` — both the live pending-approval surface and the decided-Actions history — alongside
group/name and arguments, the way the App Shell mock's approval cards already reserve a line for
it.

## Open questions

- Optional or required? Requiring it blocks existing clients until they're updated; leaving it
  optional means some requests still arrive with nothing to show.
- One field (a single caller note) or two (a short `title` for the compact list row, a longer
  `description` for the expanded view).
- Same untrusted-metadata handling as `origin`/`correlation`, or does a field meant for display
  need its own treatment (length limits, sanitization before rendering as plain text)?
