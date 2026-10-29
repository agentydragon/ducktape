# Synchronous-execution fallback

Narrow the blast radius of an incomplete synchronous execution. Dispatch order
is <../TODO.md>; this plan burns down as steps land.

## Problem

`facts/sync_effects/interpreter.rs` bounds the synchronous interpreter with
three cost budgets: invocation depth 64, invocation count 256, callback pool 64. A call site whose callback pool exceeds the budget, or a construction the
interpreter does not model, takes `bounded_invocation_fallback` over the whole
pool as one opaque batch and marks the statement incomplete. Graph build then
applies the conservative fallback to the incomplete statement: promoted at-init
edges to the read closure of everything the pool could reach
(`graph/build.rs::promote_at_init_calls`, `SourceUnitReadClosure`) and a strict
side-effect edge against every prior impure unit.

Measured on the canonical Tana input, `devel` 4377a57bae versus the pinned
engine ecb4a734 (<../perf/abstract_interpreter_step0_baseline.md>, the
pin-vs-devel perf note): 331 statements went from complete to incomplete,
`callback_pool_limit` on 297 and `unsupported_operation: construction` on
192, each gaining 500–2,700 promoted eager edges to 30–50% of all prior
declarations. Those edges form the 1,833-module SCC, add about 1 M edges, and
account for most of the 4.4× wall increase (main-chunk analysis 29 s → 180 s).
Two of the four Claude CLI eager-only pivots hit the same budget.

## Principle

The budgets stay as cost bounds. Raising one moves the cliff. The fix is what
an overflow produces: a sound summary instead of a havoc.

## Steps

### 1. Summarize an overflowing pool

When `callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL`, invoke each callback
whose body is known once with Top arguments and union the per-callback
summaries (reads, rebinds, uninitialized reads, effects, executed owners).
A summary over Top arguments covers invoking that callback with any argument
drawn from the pool, so the union is sound for any subset and order, and it is
linear in the pool instead of the capture cross product the budget guards
against. The statement stays complete when every callback body is known. This
is the abstract-interpreter plan's widening "argument shapes to Top after one
distinct re-summary per body" (<abstract_interpreter.md> § 6.2), landed early.

- Tests: a pool of 65 callbacks where one writes a binding a later statement
  reads at init keeps that edge; a pool of 65 pure callbacks produces no
  promoted edge; the same fixture at 64 callbacks is unchanged.
- SPEC: § Synchronous analysis limits describes the summary, not the limit.

### 2. Havoc only the unknown

A callback with no resolvable body still havocs, but the fallback's reach is
that callback's own `SourceUnitReadClosure`, never the union over the pool. The
precision loss names the unresolved callee.

- Tests: one unresolved callback among 65 orders only its reach; an escaped
  unknown callback still orders against every prior impure unit.

### 3. Model the unsupported construction

Classify the 192 construction statements from the perf note's list. If the
shape is `new LocalClass(args)` with a known constructor body, interpret the
constructor under the step 1 summary; this is <abstract_interpreter.md>
§ 5 step 5b narrowed to one row and should land as a builtin-table row, not a
bespoke certificate. If the constructor is unknown, step 2 applies.

- Tests: a local class whose constructor writes a module binding keeps the
  edge; one that only initializes own fields produces none.

### 4. Measure

Before and after each step, per <abstract_interpreter.md> § 6.1: public
controls, and canonical Tana on `devel`. Expected: edges fall from 3,146,382
toward the pinned 2,163,669; the 1,833-module SCC dissolves; main-chunk
analysis wall falls toward 29 s. Report the Claude CLI class B pivots
(`YC`, `Ujr`, `xe`, gaffer-private
`claude/cli/re/2.1.292/debug/2026-10-29-conflict-inventory.md`) as the
cross-check. A smaller graph is not a pass; the gate decision is reported as
observed.

## Not in scope

Changing a budget constant; any new author premise; the export-reentry policy
(<host_behavior.md>).
