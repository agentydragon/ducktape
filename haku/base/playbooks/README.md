# Example playbooks

These are **examples** — concrete starting points showing the _kind_ of value to
look for and how to gather it. They are **not a closed set**: your scope is
open-ended (see `../instructions.md`). Read them for the pattern, run the ones whose
sources you currently have, adapt them, and grow your own over time — record
playbooks you develop in your state `memory/`, not here (this directory is
read-only base).

Available now: [`plaid_anomalies`](plaid_anomalies.md),
[`gmail_triage`](gmail_triage.md), [`inbox_cleanup`](inbox_cleanup.md),
[`calendar_prep`](calendar_prep.md), [`drive_activity`](drive_activity.md),
[`tasks`](tasks.md), and
[`ducktape_git_review`](ducktape_git_review.md) (your ducktape checkout — always
reachable). The Google ones share the read-only Google token; other Google
products (Docs, Slides, …) are fair game the same way when the token carries their
scope — if a call 403s, the scope isn't granted (or the API isn't enabled on the
project), so note the gap in your log and move on.

[`tana_review`](tana_review.md) reads the operator's Tana (read-only) via the
cluster-internal `tana-mcp-ro` facade — reached **directly from the web home** with
the `fastmcp` CLI carrying the `haku-tana-ro-token` bearer (no pod), or with plain
`curl` over MCP-HTTP when `fastmcp` is missing (the playbook has both recipes). Tana
is the operator's primary task store — a must-scan source, not optional; keep the
working recipe in your `memory/`.

[`delegation_scan`](delegation_scan.md) is the cross-source pass for the operator's
top standing goal: find what a capable AI agent could take off his plate — today, or
given one specific affordance (an API key, an MCP server, a credential) — and surface
it ranked. Maintain a delegation register in `memory/` so it compounds.

Designed but **not yet wired** — the tools aren't on your wire; don't attempt
them, just note the gap in your log if one appears: PostScanMail (unopened mail →
open/discard), Grocy stock (expiring / below-minimum). Each is blocked until it
sits behind a read-only filter facade; see `../../PLAN.md`.
