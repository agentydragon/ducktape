# Synchronous-execution fallback

Narrow the blast radius of an incomplete synchronous execution. Dispatch order
is <../TODO.md>; this plan burns down as steps land.

## Problem

Measured on the canonical Tana input, `devel` 4377a57bae versus the pinned
engine ecb4a734 (<../perf/pin_vs_devel_tana_2026_10_29.md>): 331 statements
went from complete to incomplete, each gaining 500–2,700 outgoing edges to
30–50% of all prior declarations. Those edges form the 1,833-module SCC and
add about 1 M edges.

The edges are the statement's synchronous read set, not the S-chain. Of the
growers' outgoing edges, 387,894 are `promoted_at_init` eager uses whose
callee is the statement itself, and 379,206 are `Sequenced`. On a
representative statement the two target sets overlap 490 of 506: each
`Sequenced` edge orders a promoted read against the read binding's nearest
preceding writer (`graph/build.rs::promote_at_init_calls`). The read set is
what the interpreter's havoc paths promote for a construction or call it cannot
model: class-member promotion of a rejected construction, the callback pool
fallback (`bounded_invocation_fallback`), and the foreign-record promotion.

Every grower carries a loss other than `callback_pool_limit`:

| Loss alongside the pool limit       | Statements | Site                                                   |
| ----------------------------------- | ---------: | ------------------------------------------------------ |
| `unsupported_construction`          |        192 | one local class, three factory wrappers (`new C(...)`) |
| `unsupported_operation/member_read` |         71 | descriptor alias chains, Map/Set registries + freeze   |
| `unknown_call_target`               |         31 | opaque calls                                           |
| array spread, membership protocol   |          3 |                                                        |

### Dropped: summarizing an overflowing pool

The earlier steps 1–2 summarized a pool above `MAX_SYNCHRONOUS_CALLBACK_POOL`
by invoking each body once with `Unknown` arguments, and narrowed unresolved
callees to their own read closure. Both premised that the statement could stay
complete. It cannot: every caller of `invoke_possible_callbacks` (opaque call,
rejected construction, own-data read, implicit coercion, callable field write,
protocol steps) records its own loss before the pool is consulted, so the pool
is reached only on an already incomplete statement. Re-summarizing the escaped
pool at each statement also replaces the fallback's incremental closure cache:
the prototype timed out `opaque_callback_scale_test`.

## Steps

### 1. Unblock the rejected construction

All 192 construction losses sit at the three wrapper sites of one local class,
and all reject at `field_may_exist_on_object_prototype` (the first field
checked; every field fails). The whole-program prototype census holds 63,067
entries with no named property (49,124 escapes, 10,987 reflective writes, 2,785
computed writes, 171 deletes; 61,877 of all 88,617 entries are in the vendor
chunk), so no field name can be proved absent from `Object.prototype`. The
pinned engine predates this check.

The engine cannot prove the premise. The audited author input that supplies
exactly it already exists: `retained_record_factory: true` on each wrapper
(SPEC § Synchronous analysis limits). This is a spec change in the private
Tana spec, gated on that audit; measure its effect before landing it.

### 2. Classify the member-read losses

Group the 71 `member_read` growers by the operation the interpreter declined
(descriptor alias, registry `Map`/`Set`, `Object.freeze` input) and decide per
group between a builtin-table row (`facts/abstract_domain/builtins.rs`), an
existing author input, or nothing.

### 3. Bound what an incomplete statement promotes

An incomplete statement promotes the whole havoc read set. The open soundness
question is whether graph build may instead order the statement against only
the reach the interpreter can name (the owners it actually could not model and
their synchronous read closure), keeping the strict fallback for losses with no
computable reach (`eval`, `with`, foreign records). This needs every loss path
to carry its reach. Not implemented; decide before any code.

### 4. Measure

Before and after each step, per <abstract_interpreter.md> § 6.1: public
controls, and canonical Tana on `devel`. Report edges, SCC sizes, atomic
conflicts, wall and RSS, and the Claude CLI class B pivots (`YC`, `Ujr`, `xe`,
gaffer-private `claude/cli/re/2.1.292/debug/2026-10-29-conflict-inventory.md`)
as the cross-check. A smaller graph is not a pass; the gate decision is
reported as observed.

## Not in scope

Changing a budget constant; the export-reentry policy (<host_behavior.md>).
