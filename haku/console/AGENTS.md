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
- **Parse `revision` and `down_revision` out of file contents.** The filenames' numbering has real
  gaps — there is no `0053` and no `0076` — which say nothing about the link structure, so a
  filename sort is not the chain.
- **Never reserve a number in advance.** One that was free at authoring time is taken by merge
  time, so choose it as the branch goes out and re-check before every push.
