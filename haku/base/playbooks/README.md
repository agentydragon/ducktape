# Example playbooks

These are **examples** — concrete starting points showing the _kind_ of value to
look for and how to gather it. They are **not a closed set**: your scope is
open-ended (see `../instructions.md`). Read them for the pattern, run the ones whose
sources you currently have, adapt them, and grow your own over time — record
playbooks you develop in your state `memory/`, not here (this directory is
read-only base).

Available now: [`plaid_anomalies`](plaid_anomalies.md),
[`gmail_triage`](gmail_triage.md), [`calendar_prep`](calendar_prep.md),
[`drive_activity`](drive_activity.md), [`keep_notes`](keep_notes.md), and
[`ducktape_git_review`](ducktape_git_review.md) (your ducktape checkout — always
reachable). The Google ones share the read-only Google token; other Google products
(Docs, Tasks, …) are fair game the same way when the token carries their scope — if
a call 403s, the scope isn't granted, so note the gap in your log and move on.

[`tana_review`](tana_review.md) (read-only Tana via the cluster-internal
`tana-mcp-ro` facade) is **newly deployed** — its `haku-tana-ro-token` secret and
pod-based connection aren't paved yet, so confirm it's on your wire before relying
on it (and record the working recipe in your `memory/`).

Designed but **not yet wired** — the tools aren't on your wire; don't attempt
them, just note the gap in your log if one appears: PostScanMail (unopened mail →
open/discard), Grocy stock (expiring / below-minimum). Each is blocked until it
sits behind a read-only filter facade; see `../../PLAN.md`.
