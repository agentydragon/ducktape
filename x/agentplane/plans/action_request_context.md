# Action-request plaintext context

Status: **decided 2026-09-12; not yet implemented.**

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

## Decision

Add two fields to `ActionRequestInput`:

- `title: str`, **required**, `Field(max_length=60)`. A short, plain-language summary of what the
  action does, written to fit on one line on mobile — roughly a git-commit-subject-line length,
  e.g. "delete crashlooping backend pod." Required rather than optional: the caller is always an
  LLM invoking `request_action` as an MCP tool, so there is no legacy application code to break —
  the required field's own `Field(description=...)` is what prompts the calling model to fill it
  in, not a breaking change to any caller's source. A title that's sometimes present and sometimes
  missing is a worse approval-review experience than one that's always there.
- `description: str | None = None`, optional, `Field(max_length=250)`. Added detail or
  justification building on the title, not restating it — a documentation-level instruction to the
  calling model via its own `Field(description=...)` text, not something a validator can enforce.

Each field's own `Field(description=...)` should say explicitly that `title` is not `Action.name`
(the tool name) and is not the reverse-direction `DecisionInput.decision_note` (the operator's note
attached when deciding) — the vocabulary is close enough that a calling model could otherwise
conflate them.

Thread both through `ActionRequestView` to the approver and render them in `ActionCard` — both the
live pending-approval surface and the decided-Actions history — alongside group/name and
arguments, the way the App Shell mock's approval cards already reserve a line for it.

Render both as plain text, verbatim — no sanitization pass, no markdown interpretation, nothing
clever beyond the length limits already enforced at submission. Leave an inline `// TODO:` at the
`ActionCard` render call noting that markdown rendering is a possible later addition, not a
decision made now.
