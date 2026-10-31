# Havoc reach: what fills the callback pools

Question: would bounding an unknown call's reach by provider (an import-acyclic
chunk) or by provider export region shrink the `callback_pool_limit` pools that
drive the Tana and Claude SCCs below `MAX_SYNCHRONOUS_CALLBACK_POOL` (64)?
Answer: no, on either input. The pools are filled by escapes that no provider
scoping can exclude.

## Method

[`havoc_reach_pool_probe/probe.patch`](havoc_reach_pool_probe/probe.patch)
applies on #13151 (`f986bf0af3`). It is diagnostic only, gated on
`DEBUNDLE_HAVOC_PROBE_DIR`. It records:

- each module's static imports and exports, and for import-free provider
  chunks the per-statement declared, read, rebound and member-written
  bindings;
- at each `callback_pool_limit` event, the pool members and the opaque call
  in progress. For each escaped member it records the route that first put
  it in `global_escaped_callbacks`. That route is the call it was an argument
  of (callee root binding, plus that root resolved through top-level
  assignments) or the escape site (global-object write, untracked-receiver
  write, bounded fallback).

[`pool_regions.py`](havoc_reach_pool_probe/pool_regions.py) recomputes each
pool under three scopings. Exports and the call's own arguments always stay in
the pool. An escaped member stays when its route's scope overlaps the call's.
A route or call that does not resolve to an import of an import-acyclic
provider has the whole scope.

- `per_chunk`: the scope is the provider chunk's static import closure.
- `region_untagged`: the scope is the provider export's region, meaning the
  non-function top-level statements in the read/rebind/member-write closure of
  the export's declaration. Only direct calls of an import, or calls of a
  member of an import, resolve.
- `region_tagged`: as `region_untagged`, but a call chain (`Z.string().min()`)
  or a local binding initialized from one resolves to its root import. This
  approximates perfect tagging of provider results.

First-route attribution and name-based roots overstate what scoping can
remove, so the results below are upper bounds.

Inputs: canonical Tana (gaffer `25938756b` spec, `synchronous_module_loader:
none`) and Claude CLI 2.1.292, both replayed with the probe binary.

## Tana

There are 8,022 pool-limit events at 839 statements (927 `callback_pool_limit`
losses). Those statements carry 2,525,160 constraining out-edges. Pools have a
median of 617 members and a maximum of 741. No module exports enter them; 340
distinct callbacks enter as arguments.

The 399 distinct escaped callbacks, by first route:

| Route                                                            | Callbacks |
| ---------------------------------------------------------------- | --------: |
| Argument of a call whose root is a vendor import                 |        64 |
| `new Fx(...)`: the rejected local construction (fallback step 1) |        86 |
| `Object.*` calls (`keys`, `entries`, `assign`, …)                |        75 |
| Array methods on `lV`, an array of 20 classes                    |        64 |
| Other local, parameter or global callees                         |        92 |
| Global-object and untracked-receiver writes                      |        16 |
| Bounded fallback of a truncated invocation                       |         2 |

| Scoping           | Events under 64 | Median pool | Statements with every event under 64 | Their out-edges |
| ----------------- | --------------: | ----------: | -----------------------------------: | --------------: |
| `per_chunk`       |               0 |         617 |                                    0 |               0 |
| `region_untagged` |               0 |         615 |                                    0 |               0 |
| `region_tagged`   |               0 |         615 |                                    0 |               0 |

`per_chunk` is exactly today's pool. The vendor chunk is the main chunk's only
static provider, and an unknown non-import callee may reach it. Regions can
drop at most the 64 vendor-routed callbacks. The rest escaped through local
operations the interpreter does not model.

## Claude

There are 6,163 pool-limit events at 3,163 statements (3,322 losses), with
5,886,249 constraining out-edges. Pools have a median of 1,241 members and a
maximum of 3,677. The median event's pool consists entirely of export
callbacks: the spec's export reentry is conservative, so every exported
callable is a pool member. All three scopings leave 0 events under 64.

Without the export callbacks, 5,378 events (87%) would be under 64. Every
event would be under 64 at 2,614 statements, which carry 5,491,056 out-edges.
Of the 2,070 distinct escaped callbacks, 1,087 first escaped as arguments of
an imported callee. That is the only share per-chunk scoping could act on.

## Conclusions

- Provider scoping (per chunk or per export region, with or without tagging)
  does not pay on either input. Don't build it.
- Tana's pool is local: the `Fx` wrappers (fallback step 1's
  `retained_record_factory` premise), builtin rows for the `Object.*` calls,
  and array iteration over class arrays account for 225 of 399 escapes.
- Claude's pool is the export-reentry set. The export-reentry policy
  (`plans/host_behavior.md`) is the lever there, not havoc reach.
- R1's report shows how much R2 could bound. Tana has 1 `bounded` construction
  out of 120. Claude has 4 out of 88; the rest are `borrowed_receiver`,
  `tainted_input` or `unknown_callee`.

Caveats: roots are matched by name, so a nested binding that shadows an import
name is misattributed. Regions are a syntactic closure over top-level
statements, not a heap analysis.
