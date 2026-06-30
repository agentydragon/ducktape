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

- [`run_start.md`](run_start.md) — mandatory pre-run gates (operator-local date, bootstrap ready,
  bookmark sanity, open today's log). Run these first, every run.
- [`operate_ui_service.md`](operate_ui_service.md) — operate & evolve my own UI service: the
  standing health/evolution pass, driving every change to running in prod, and the bar for
  changing the surface. "Operating it is half the job."
- [`worked_stories.md`](worked_stories.md) — capabilities in concert; the bar.
- [`triage_and_delegation.md`](triage_and_delegation.md) — inbox-like triage; delegation scans.
- [`manage_gmail_labels.md`](manage_gmail_labels.md) — your one sanctioned world-write: organize
  Gmail with labels under `haku/` via the `gmail-labeling` MCP (base → _Hard rules_). Policy + knobs;
  conservative until a scheme is agreed.
- [`finance.md`](finance.md) — financial anomalies & leaks.
- [`calendar_and_geo.md`](calendar_and_geo.md) — calendar prep; geo-temporal optimization; context.
- [`maintenance_and_synthesis.md`](maintenance_and_synthesis.md) — fix what's broken; overdue
  routines; generate (don't just detect); research blind spots; build the right medium; quiet runs;
  garden the **Improvements** self-backlog (`improvements.yaml`, rendered in the UI's 💡 tab).

As I work for a real operator I'll add procedures for **their** specific sources and surfaces
(e.g. how I scan a particular note app, a kitchen/shopping board around their grocery stack) —
those live in that operator's `haku-state`, not in this generic ducktape starter.
