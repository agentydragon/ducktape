# ducktape_git_review (example)

You have the **ducktape repo** checked out (your base lives in it, and you already
`git log` it to adopt base updates). Its recent history is a rich source of
follow-up work the operator may want surfaced. Look at roughly the last 1–2 weeks
(`git -C <ducktape> log --since='2 weeks ago' --stat`, and `git log -p` where the
diff matters), resuming from a bookmark in `memory/` so you don't relitigate:

- **New `TODO`/`FIXME`/`XXX`** in code, and new entries in `TODO.md` / `PLAN.md` /
  `plans/` — especially ones that read like the author meant to come back.
- **Commits that flag their own follow-up** — "quick fix", "temporary", "stop-gap",
  "revert once …", "will clean up", or a `CLEANUP(`/tombstone marker whose stated
  condition now looks met.
- **Reverts and immediate re-fixes** — something that broke and may still be fragile.
- **Half-finished threads** — a feature whose commits trail off, a `plans/` doc
  added but never tombstoned, a migration started but not carried across all callers.

These rarely carry a deadline, so they're backlog-tier `value` unless something
makes one urgent. File `suggestion` items for "worth doing", and `prepared_prompt`
items where you can frame a concrete change for a full-access agent (name the files,
the commit, and the desired end state). Put evidence in `body`: commit SHA(s), file
paths, the line. Skip anything a later commit already resolved, and don't refile
items you've filed before (check your dedup keys / `memory/`).
