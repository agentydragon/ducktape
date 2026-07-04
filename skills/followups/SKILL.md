---
name: followups
description: >
  Apply full judgment to "what would the user want to do, know, or reconsider
  next?" — from pending followups and incomplete migrations up to premortem
  risks, architectural rethinks, and owner's-eye strategy for the work lane.
  Verify session work is on disk. Use when wrapping up a task or session, or
  when user asks "what's next", "anything else", "what did we miss".
---

Surface what the user would most plausibly want to do, know, or reconsider next.

## Purpose

This skill's job is to apply **all available intelligence** — full session
context, repo knowledge, and genuine judgment — to one question: _given
everything that happened this session, what next?_ Think like a sharp colleague
who watched the whole session and also happens to co-own the repo.

The core questions. Everything else in this file is technique for answering
them:

1. **What did we miss?** The most important things not taken into account:
   assumptions never validated, consumers/stakeholders not considered, failure
   modes not discussed, angles skipped entirely (security, cost, scale,
   operability, rollback).
2. **Could this be the wrong approach?** Premortem: imagine it's three months
   later and what we did or planned turned out to be a mistake — what's the
   most likely reason? Is there a simpler or more standard mechanism that makes
   this work unnecessary?
3. **What would the owner do?** You own this repo / work lane and care about it
   shipping, staying healthy, and evolving well over the next year. From that
   seat, what should happen next — regardless of whether it came up this
   session? Direction, de-risking, debt paydown, sequencing, things to _stop_
   doing.
4. **What will the user want next?** If there's >10–20% chance the user wants
   something, surface it for a one-keypress decision — much cheaper than making
   them remember and type it. Catch loose threads: things discussed but
   abandoned in a pivot, half-finished work, components built but never wired
   up.

**Aim high as well as low.** The skill fails just as badly by only suggesting
`git push` when the honest answer is "this subsystem should be a reconciler"
as it does by missing the push. Small completions and big rethinks are both in
scope; they just have different output contracts (see Structural angles).

Answering rules:

- **Ground every answer in this session's specifics.** If an answer would apply
  to any session verbatim ("add more tests", "improve documentation"), it's
  boilerplate — drop it.
- **"Nothing significant" is a valid answer.** Don't invent concerns to fill
  space; a fabricated risk costs more attention than an empty section.
- **Disagree plainly.** Answers may contradict the session's direction — that's
  the point. Don't soften a real concern into a hedge.

## Process

`/followups` is a **loop, not a one-shot menu**: as long as the user keeps
selecting items, keep executing, re-thinking, and re-presenting.

1. **Ground** — establish what actually happened vs. what was merely discussed.
   Verify session work is on disk (not stashed/reverted by a parallel process):
   `git status` and `git log --oneline origin/HEAD..HEAD`. Classify:
   uncommitted / committed-not-pushed / pushed-not-deployed (e.g., Flux
   reconciliation pending).
2. **Generate** — work the core questions. Use the angle library below as an
   accelerant, and go beyond it wherever the session suggests an angle it
   doesn't list. Delegate read-only searches to parallel subagents when there
   are multiple independent checks.
3. **Filter** — verify each candidate is actionable right now (see below); drop
   what fails.
4. **Present** — probability-bucketed suggestions via AskUserQuestion, plus a
   "⚠ Reconsider" section for approach-risk flags (see Output).
5. **Execute** — do everything the user selected.
6. **Loop** — the work just executed changed the ground truth: new diffs, new
   loose ends, unblocked items, sometimes invalidated ones. Go back to step 1
   and **genuinely re-derive** the suggestion set — don't just re-show the
   leftover menu. First-iteration angles that were fully covered don't need
   re-scanning; focus regeneration on what the executed work touched, plus
   still-pending Skip items (re-verified). Exit when the user selects nothing,
   answers "stop"/"done", or only No/Skip remain with nothing new surfacing.

## Angle Library

Known-productive instantiations of the core questions. This is a **library of
examples, not a checklist that bounds the search** — pursue angles it doesn't
list; skip angles that obviously don't apply, without comment.

### Mechanical angles

The easy tier: each finding maps to a concrete command or small edit.

- **Loose threads**: "should/could/TODO/later" from the conversation; "let's do
  X" that never happened; questions left unanswered; `/later` items added to
  `TODO.md` / `plans/` this session.
- **Finish the migration**: moved pattern/config A→B in one place → find
  instances still on A; replaced a tool/approach → old one still used
  elsewhere; fixed a version/config mismatch in one layer → other layers still
  disagree. ("Moved `storageClass` for grocy — 3 other PVCs still on the
  default class.")
- **Propagate the pattern**: new helper → hand-rolled equivalents elsewhere;
  bug fixed → similar bugs; validation added → sites missing it; improvement to
  one instance of a pattern → its siblings.
- **Wire it up**: built but not connected — image no CI workflow builds,
  manifest nothing reconciles, module nothing imports, fixture no test uses,
  config option no doc mentions.
- **Workflow completion**: commit / push / test / docs / pre-commit for the
  session's changes.
- **Hygiene after change**: dead code, newly unused imports, comments
  referencing old code, file/test names stale after renames, tests that no
  longer match refactored interfaces (fix in the same change, not later).
- **Prevent recurrence** (after debugging): has this happened before? Check
  session logs, `debug/`, `lessons_learned/` — recurring means structural fix,
  not another one-off diagnosis. Worth a CI check, a guard/invariant that makes
  the bad state unrepresentable, better diagnostics, a workflow script, or a
  `lessons_learned/` entry? Concrete proposals only, never "consider adding
  tests".
- **Quality audit of the diff** (delegate to subagents): logic duplicating
  shared utilities, patterns repeated 3+ times wanting extraction, STYLE.md
  violations actually present in the diff — file:line, rule, concrete fix.

### Structural angles

The ambitious tier: step-change moves, not incremental cleanup. These fire
rarely, but when one is right it's the most valuable output this skill can
produce. Each must be grounded in friction actually observed — this session's
pain is the evidence.

- **Adopt instead of build**: this hand-rolled subsystem is a worse version of
  something standard — a framework, library, or platform capability already in
  the stack. "This app's routing/state handling is a buggy reimplementation of
  what any sane JS framework provides — switching to X deletes half the code
  and the whole bug class we just fought."
- **Wrong architectural shape**: the design fights the problem's nature; name
  the known-good shape that fits. Reconciler/controller (declare desired state,
  converge level-triggered, requeue on error — instead of imperative
  edge-triggered sync scripts that drift), state machine, queue + workers,
  append-only event log, pipeline of pure stages. "These sync scripts keep
  drifting and we keep patching them — this wants to be a reconciler."
- **Algorithmic headroom**: the hot path does avoidable work; estimate current
  and achievable complexity and say what closes the gap. "This is
  O(n² log m + nm); with an interval tree and incremental updates it should be
  near O((n+m) log n) — worth digging into before the dataset grows 10×."
- **Wrong data model**: the special cases we keep patching are symptoms; the
  schema forces the complexity. Restructure the model and the patches vanish.
- **Collapse layers**: services/processes/repos that should be one thing; a
  service that should be a library; indirection that no longer pays rent.
- **Delete the problem**: change something upstream so the whole component is
  unnecessary — the best version of this component may be its absence.
- **Promote a one-off to a capability**: this session built the third bespoke
  solution of its kind — build the general mechanism, delete all three.
- **Verify the invariant, not examples**: a tricky invariant defended by
  hand-picked example tests wants property-based testing, fuzzing, or
  exhaustive checking over the real input space.

**Output contract for structural items** (different from mechanical):

- **Claim + evidence + shape**: state the thesis crisply, name the friction
  from this session that motivates it, sketch the target shape and rough
  payoff.
- **Next step is a probe, not a leap**: a spike branch, a profiling run, a
  half-page design note — sized in hours, cheap to abandon. Never "rewrite it".
- **Cap 1–3 per session**, highest expected value first. These compete on
  probability × payoff, not raw probability — a 20% chance of killing a whole
  maintenance lane beats an 80% chance of a nice cleanup.

## Filtering

Before surfacing any suggestion, verify it's actionable right now:

- **Git push/commit**: re-run `git status` and
  `git log --oneline origin/HEAD..HEAD` fresh — the user may have committed,
  pushed, or staged since the last check
- **Run tests**: confirm the target exists and the runner is available
- **Code changes**: confirm the file/function still exists and wasn't changed
  by a concurrent agent
- **Cleanup**: confirm the dead code / unused import is actually still there

Drop suggestions that fail verification — a stale suggestion wastes more
attention than omitting it. If borderline ("bench.py might need updating" but
unchecked), verify or drop.

**Exempt from verification**: approach-risk flags (premortem) and structural
items — they're judgments and probes, not actions to validate. Their quality
gate is the grounding rule: real evidence from this session, or drop.

## Output and Interaction

### Priority buckets

For each mechanical action, estimate the probability the user wants it:

- **>80% — DO NOW** 🔴: commit modified files, fix breaking changes introduced,
  complete half-finished work
- **40–80% — LIKELY** 🟡: run tests after code changes, propagate new pattern
  to obvious sites, update related docs, push committed work
- **20–40% — MAYBE** 🟢: add tests for new feature, refactor similar code,
  improve error messages
- **10–20% — OPTIONAL** 🔵: nice-to-have cleanups, edge-case documentation
- **<10% — omit** (don't waste the user's attention)

Calibration anchors: 90% = user explicitly said "do this next"; 70% = standard
workflow step (commit after edits); 50% = natural followup (tests after code
change); 30% = improvement opportunity; 15% = nice-to-have.

**Outside the buckets** (not probability-gated):

- **⚠ Reconsider** — approach-risk flags from the premortem, surfaced even at
  low probability; suppressing a correct one costs far more than reading a
  wrong one.
- **Structural items** — ranked by expected value (probability × payoff),
  capped at 1–3, presented with their probe as the yes-action.
- Owner-view lane-level items: at most 1–2 per session, highest-leverage
  first — a steering nudge, not a backlog dump.
- Otherwise err toward over-suggesting: better five low-probability items than
  missing the one the user wanted.

### Phase output (text)

Print verification results and a brief summary as text:

```markdown
## Verification

✅ All session work verified on disk

- src/feature/ (3 files modified)
- config/settings.yaml (new validation added)

## ⚠ Reconsider

- <approach-risk flag, if any — plain statement of why the approach may be
  wrong, no hedging>

## Summary

Found 3 immediate actions, 4 likely followups, 1 structural proposal.
```

Omit the Reconsider section entirely when there are no approach-risk flags.

### Action selection (AskUserQuestion)

Present suggestions using AskUserQuestion. Each item needs a tri-state
response:

- **Yes**: do it now (for structural items: run the probe, not the rewrite).
- **Skip**: not this round — re-verify and resurface in later loop iterations
  (and on any later `/followups` call this session).
- **No**: don't do it, don't suggest it again this session — not in later
  iterations either. Track in conversation context only — do not save to
  memory.

Items not explicitly addressed default to **Skip**.

**Loop cadence**: after executing the Yes items, return to Process step 1 and
re-present (fresh items first, surviving Skip items after). Each round's
presentation should be cheaper than the first — only what's new or changed
needs re-derivation. Stop looping when the user selects nothing, picks
"stop"/"done" via Other, or a round produces no new items and only
Skip/No leftovers. On exit, print a one-line wrap-up of what was done across
all rounds and what remains parked.

Include the priority emoji (🔴🟡🟢🔵) in option descriptions; structural items
get 🏗 instead of a probability emoji.

**AskUserQuestion constraints:**

- 1–4 questions per call, 2–4 options per question
- Labels: 1–5 words; details go in description
- Headers: max 12 chars (chip/tag)
- `multiSelect: true` allows multiple selections
- "Other" freeform option is auto-provided
- Can call the tool multiple times sequentially

**Presentation strategy**: choose whatever pattern fits the suggestions —
multi-select by topic (selected=yes, unselected=skip) with a follow-up for
explicit "no"; per-item single-select when items are few or need individual
attention; batched by priority (DO NOW first); multiple sequential calls for
overflow. Optimize for quick decisions with minimal friction.

**Concrete next step**: every suggestion names its next step. For mechanical
items that's the exact command — `git push origin devel`,
`bbr test //path/to:target` — never "consider committing changes". For
structural items, where the work is not one command, it's the first probe:
"spike: swap the hand-rolled router for X in one page", "profile with n=10⁴ to
confirm the n² term dominates", "half-page design note on the reconciler
shape".
