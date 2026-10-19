@README.md

## Migrations

Parallel branches edit `migrations/versions/`; a duplicate `revision` id or two children of one
parent merges green and stops the console booting on the next deploy.

- Choose the revision id and parent against `devel` **plus every open migration branch**, walking
  their union (a single-branch walk cannot see a sibling's claim).
- Build the chain from `revision`/`down_revision` in file contents, not filename order (numbering
  has gaps).
- Never reserve a number ahead; pick it when the branch goes out and re-check before every push.
- The one deployment migrates forward once, so tests of a specific migration live only while it
  lands and ~5 revisions after; delete one that blocks a new migration. Durable tests cover the
  current schema: fresh DB at head matches the ORM, and head re-applies idempotently.
