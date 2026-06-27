# `procedures/` — your own playbook of passes

These are **your** regular procedures — the recurring passes and worked patterns you run to
find useful things for the operator. They are seeded from ducktape's `state_template` as a
starting set; **they are now yours to read, refine, reorganize, and grow.** Your base
manual (`haku/base/`) defines _how you reason_ in general; this directory is the concrete,
evolving _how you currently work_. Add a file when you invent a pass worth keeping; prune
or rewrite one that stops earning its place.

They are **illustrations, not a checklist** and not a closed set — source-agnostic patterns
applied situationally. A run is never "ran every procedure, done"; the job is open-ended
synthesis (base manual → _How you reason_). The deliverable bar is the same throughout: do
the operator's work _in advance_ and hand over a one-click, approve-to-implement result
(base → _Hand over a finished solution_).

## Files

- [`worked_stories.md`](worked_stories.md) — capabilities in concert; the bar.
- [`triage_and_delegation.md`](triage_and_delegation.md) — inbox-like triage; delegation scans.
- [`finance.md`](finance.md) — financial anomalies & leaks.
- [`calendar_and_geo.md`](calendar_and_geo.md) — calendar prep; geo-temporal optimization; context.
- [`maintenance_and_synthesis.md`](maintenance_and_synthesis.md) — fix what's broken; overdue
  routines; generate (don't just detect); research blind spots; build the right medium; quiet runs.
