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
- [`garden.md`](garden.md) — the knowledge garden: link syntax, standard view widgets, and the
  affordance library (`<handoff>`, `<signal-toggle>`, `<choices>`, …) you embed in item/note bodies.
- [`worked_stories.md`](worked_stories.md) — capabilities in concert; the bar.
- [`triage_and_delegation.md`](triage_and_delegation.md) — inbox-like triage; delegation scans.
- [`maintenance_and_synthesis.md`](maintenance_and_synthesis.md) — fix what's broken; overdue
  routines; generate (don't just detect); research blind spots; build the right medium; quiet runs;
  garden the **Improvements** self-backlog (`memory/improvements/<id>.md`, rendered by the
  `<improvement-board/>` widget).
- [`propagation/`](propagation/README.md) — per-domain propagation checklists (the surfaces to
  reconsider when something in a domain changes): [`items.md`](propagation/items.md) for
  state-changing events, [`intake.md`](propagation/intake.md) for operator feedback.

## Adding bespoke per-source surfaces and procedures

As I work for a real operator I'll grow **their** specific sources and surfaces the same way —
each is a new procedure here (how I scan it, what I reconcile) plus, where it deserves its own
shape, a bespoke UI surface (see [`operate_ui_service.md`](operate_ui_service.md)). Illustrative
future extensions, described not implemented: a recipe/pantry board over a household inventory
tool; a finance tracker over transaction data; an email-labeling pass as a sanctioned world-write;
or a calendar/geo surface for schedule and travel prep. Those live in that operator's `haku-state`,
not in this generic ducktape starter.
