@README.md

## Adding a migration while other branches hold one

Several agents work `migrations/versions/` at once, and the chain is the one thing a green PR can
break silently: a duplicate `revision` id or a second child on one parent merges clean and stops
the console booting on the next deploy.

- **`devel`'s head is necessary and not sufficient.** An in-flight branch holds a revision id and a
  parent claim that `devel` cannot show, so pick both against `devel` plus every open migration
  branch — never against `devel` alone.
- **Walk the composite, never a single branch.** A walk over one tree reports "no forks" whether or
  not a sibling has already claimed the parent being attached to; the fork exists only in the union
  of the branches, so that is what has to be walked.
- **Parse `revision` and `down_revision` out of file contents.** The filenames' numbering may have
  gaps, which say nothing about the link structure, so a filename sort is not the chain.
- **Never reserve a number in advance.** One that was free at authoring time is taken by merge
  time, so choose it as the branch goes out and re-check before every push.

## Do not keep tests for old migrations

The cluster holds the only deployment, and its database is migrated once, forward. Nobody will ever
re-run an old migration against real data, so a test that pins one is testing a path that cannot be
taken again — and it will not merely rot, it will actively block: a later migration that changes or
empties the rows it builds fails it, and the fix is to contort the test rather than to learn
anything.

**Test a migration while it is landing and for roughly five revisions after, then delete the
test.** Nothing needs to be kept for the record; git has it. When an old migration test stands in
the way of a new migration, deleting it is the expected move, not a last resort.

What is worth testing instead is the _current_ schema: that a fresh database migrated to head
matches the ORM, and that head re-applies idempotently. Those stay true as the chain grows.

## Conversation data may be dropped; tool-call data may not

**A standing operator allowance, revocable at any time.** Nothing in the prod database's
conversation tables is worth keeping — the console is still in development and what is there is
test traffic. A migration that would otherwise need expand/contract, a backfill, or a read-only
compatibility variant to keep stored conversation rows readable may **delete the rows instead**,
and should: take the simpler schema.

`conversation` is the root, and every conversation-scoped table cascades from it —
`channel_attachment`, `sessions`, `conversation_event`, `conversation_item`, `conversation_turn`,
`conversation_prompt`, `session_frames`, and the per-attachment channel state — so deleting there
is the whole of it.

**Nothing else is covered.** `mcp_tool_calls` and `mcp_tool_call_principals` are the audit record
for privileged execution and are kept; so are identity, credentials, grants, and OAuth state. A
migration touching any of those gets the full expand/contract treatment.

Say in the PR that a migration drops conversation rows, so a reviewer sees it was chosen rather
than overlooked. When the allowance is withdrawn this section goes, and the exemption with it.
