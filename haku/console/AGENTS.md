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
