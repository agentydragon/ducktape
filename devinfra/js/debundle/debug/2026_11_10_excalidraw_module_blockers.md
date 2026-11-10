# Excalidraw: what blocks module boundaries under the effect verdict (2026-11-10)

The current engine's side of the stage 2 producer comparison
(<../plans/target_architecture.md> § Open decision): which statements and
which missing rules leave swapped pairs unproven on the public corpus, for the
committed spec and for a decompose-style stress assignment.

## Result

- **Every unproven pair is havoc-only.** No pair has a modeled cell conflict
  or a premise, and the legacy gate orders none of them. A havoc statement is
  unproven against 93–100% of the statements it swaps with.
- **There is no concentration.** Covering the 100 statements that carry the
  most pairs removes 40–46% of the stress pairs. Module consequences do not
  move until nearly every havoc statement is covered: the unproven pairs
  connect 994 of 1,300 `main` modules and 1,065 of 1,270 `editor` modules into
  one component each.
- **The callback pool is not the cause.** 85% (`main`) and 99.6% (`editor`)
  of the unproven stress pairs remain if every pool-inherited havoc statement
  is assumed covered. Most havoc statements make an unknown call of their own.
- **The triggers are few, and Wave 3 has no row for the largest.** Calls into
  bundled vendor code through esbuild's CommonJS interop (the `require` thunk
  and `__toESM`, then `jsx`, `forwardRef`, `memo`, `createContext` on the
  resulting namespace) account for 395 of 721 havoc statements in `main` and
  167 of 356 in `editor`.
- **The landed kernel rows seal nothing here.** `root_attempts.sealed` is 0
  in both chunks.
- **The legacy gate accepts the stress assignment, and the tree runs.** The
  browser smoke passes on it, and neither a 30-pair sample nor a reference
  check over every pair found an order-dependent pair.

## Inputs and procedure

`devel` at `2e72444d`, `@excalidraw/excalidraw` 0.18.1, chunks `main` (1,628
source units) and `editor` (1,787).

- **Committed:** the `:debundle` spec with `effect_conflicts_jsonl`,
  `effect_conflicts_chunks = ["main", "editor"]` and `emit_on_rejection`.
- **Stress:** the same, with `unassigned_mode` of `main` and `editor` changed
  from `inline_in_entry` to `per_atomic_group` (1,300 and 1,270 modules).
- Both are scratch `debundle_pipeline` targets, not committed. BuildBuddy
  invocations `dbf25cec-1aa3-409e-bb25-4bc52fbcb54f` (both pipelines, 87 s
  including analysis) and `a65b7e08-c016-4f7c-bf7c-38caab5fa5e1` (smoke).
- Statement text, legacy purity and reference edges come from the
  `dependency_graphs` output group of `:debundle`.

Definitions used below:

- **Havoc statement:** an endpoint whose opacity record has unknown coverage,
  dataflow opacity, incomplete execution or an opaque heap.
- **Own loss:** a precision loss whose `statement_ordinal` is the statement
  itself. `callback_pool_limit`, `recursive_invocation` and the invocation
  limits are pool markers, not triggers: the pool runs only after an opaque
  event, and the loss list is not in execution order (the marker often
  precedes the member read and unknown call that caused it).
- **Trigger:** for an own `unknown_call_target`, the callee at the loss
  location, resolved through the chunk's imports and declarations.

## Pairs and statements

| Variant / chunk    | Modules | Swapped pairs | Unproven      | Legacy-ordered | Havoc statements in pairs (chunk total) | Both endpoints havoc |
| ------------------ | ------: | ------------: | ------------- | -------------: | --------------------------------------- | -------------------: |
| committed `main`   |       2 |         3,029 | 1,556 (51%)   |              0 | 616 (740)                               |                    0 |
| committed `editor` |       3 |         3,589 | 710 (20%)     |              0 | 370 (370)                               |                    0 |
| stress `main`      |   1,300 |       169,613 | 71,392 (42%)  |              0 | 721 (740)                               |                1,869 |
| stress `editor`    |   1,270 |       423,683 | 140,477 (33%) |              0 | 356 (370)                               |                5,986 |

Of the swapped pairs that touch a havoc statement, 100% are unproven in the
committed spec, 93.0% in stress `main` and 93.8% in stress `editor`.

Share of unproven pairs removed if the statements carrying the most pairs were
covered:

| Variant / chunk    | Top 10 | Top 50 | Top 100 | Top 200 |
| ------------------ | -----: | -----: | ------: | ------: |
| committed `main`   |   1.9% |   9.6% |   19.3% |   38.6% |
| committed `editor` |   5.6% |  28.2% |   56.3% |   76.1% |
| stress `main`      |   5.1% |  24.3% |   46.0% |   77.7% |
| stress `editor`    |   4.9% |  19.8% |   40.1% |   80.7% |

The partner of a havoc statement is almost always a definition the legacy
graph calls pure. In stress `main`, 79% of the clean partners are
function-valued `var` initializers and 12% primitives; in stress `editor`, 46%
and 45%.

## Module-level consequence

| Variant / chunk    | Modules holding a havoc statement | Modules in at least one unproven pair | Components (largest) |
| ------------------ | --------------------------------: | ------------------------------------: | -------------------- |
| committed `main`   |                        entry only |                         2 of 2, entry | 1                    |
| committed `editor` |                        entry only |                         3 of 3, entry | 1                    |
| stress `main`      |                               472 |                          994 of 1,300 | 1 (994)              |
| stress `editor`    |                                51 |                        1,065 of 1,270 | 1 (1,065)            |

The component is what the effect verdict would have to co-locate unless an
order search removes the swap, so it is an upper bound on merging.

In the committed spec no authored module holds a havoc statement. Each is
unproven because it is imported ahead of the entry and so swaps with every
havoc statement the entry runs before its source position:

| Authored module (chunk)                   | Distinct havoc partners |
| ----------------------------------------- | ----------------------: |
| `components/card` (`main`)                |                     616 |
| `components/color_picker/labels` (`main`) |                     470 |
| `data/blob` (`editor`)                    |                     370 |
| `element/text_wrapping` (`editor`)        |                     116 |
| `app_state/defaults` (`editor`)           |                     108 |

Each module's partners span every trigger family below, so no subset of rules
frees an authored module.

In the stress assignment the legacy atomic groups already co-locate most of
the strict chain: one `main` module holds 228 of the havoc statements, and two
`editor` modules hold 226 and 80.

## Origin of the havoc

Stress assignment, havoc statements by where their loss originates:

| Origin                                                         | `main` | `editor` |
| -------------------------------------------------------------- | -----: | -------: |
| Own unknown call                                               |    523 |      226 |
| Field store refused as a possible setter after an unknown call |    108 |        3 |
| Own unmodelled operation, no unknown call                      |     31 |       34 |
| Owner coverage refused, execution complete, no loss            |     27 |       62 |
| Losses only in a local callee the statement calls              |     24 |       31 |
| Dataflow opacity only (`global_object_alias_escape`)           |      8 |        0 |
| Losses only in pooled callbacks                                |      0 |        0 |

`callback_pool_limit` appears in the loss list of 551 of 721 `main` and 287 of
356 `editor` havoc statements, always beside a trigger of the statement's own.

**Counterfactual: no pool-inherited havoc.** Treating the possible-setter
stores as covered (the only class that exists because
`reachability.unknown_call_seen` is sticky,
<../plans/analysis_performance.md> § Monotone delta):

| Chunk    | Havoc statements left | Unproven pairs left | Modules in a pair | Components |
| -------- | --------------------- | ------------------- | ----------------: | ---------- |
| `main`   | 613 of 721            | 60,825 (85.2%)      |               951 | 1 (951)    |
| `editor` | 353 of 356            | 139,961 (99.6%)     |             1,065 | 1 (1,065)  |

This is the ceiling for a fix to the pool mechanism. It is an attribution from
the recorded losses, not a rerun with the mechanism disabled.

## Triggers, ranked

Stress assignment. "Alone" is the effect of covering only the statements whose
every trigger is in the family. "Freed" counts modules that would hold no
havoc statement.

| Family                             | Source shape                                               | Owner refusal, losses                                                                          | `main`: statements, pairs removed alone, freed | `editor`: statements, pairs removed alone, freed | Wave 3 row                                     |
| ---------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------ | ---------------------------------------------- |
| CommonJS interop `require`         | `var at=T(R(),1)`                                          | `unsupported_initializer`; `unknown_call_target`                                               | 321, 12.7%, 321                                | 8, 0.5%, 0                                       | none                                           |
| Vendor API on an interop namespace | `_((0,c.jsx)("path",{…}))`, `at.forwardRef((e,t)=>…)`      | `unsupported_initializer`; `member_read`, `unknown_call_target`                                | 74, 11.6%, 21                                  | 159, 61.5%, 0                                    | none                                           |
| Imported callee without a summary  | `wA=Y({name:"deleteSelectedElements",…})`, `A(ns,"set",…)` | `unsupported_initializer`; `unknown_call_target`                                               | 101, 37.9%, 1                                  | 11, 1.2%, 0                                      | 3, left                                        |
| Field store after an unknown call  | `ju.displayName="Slot"`                                    | `unproved_field_store`; `property_write`                                                       | 108, 14.8%, 40                                 | 3, 0.4%, 0                                       | 9, left (stores that add a property)           |
| Class expression, no loss          | `Ke=class{}`, `pj=class{constructor(){…}tick(){…}}`        | `unsupported_initializer`; none                                                                | 15, 5.4%, 14                                   | 28, 8.7%, 24                                     | 5, left                                        |
| Other initializer, no loss         | `cH=lH+oH+sH`, `An=Ae.Excalifont`, `Ui={Z_NO_FLUSH:0,…}`   | `unsupported_initializer`, `initializer_result_unproved`; none                                 | 12, 2.3%, 9                                    | 34, 6.1%, 23                                     | 11                                             |
| Local callee whose body is refused | `Gc=wt(null)`, `Ly=_("M433.941…")`                         | `unsupported_initializer`; callee `property_write`, `implicit_coercion`, `opaque_execution`    | 24, 5.3%, 6                                    | 31, 8.9%, 0                                      | 9 and 10, left (operations in callable bodies) |
| Own unmodelled operation           | `Yu=Math.floor`, `{min:oi,PI:Y5}=Math`, `wH={...Jf,…}`     | `unsupported_initializer`, `split_declaration`; `member_read`, `object_spread`, `array_spread` | 31, 3.2%, 23                                   | 34, 2.4%, 0                                      | 10, left (destructuring); otherwise none       |
| Native constructor                 | `Kp=new WeakMap`, `MU=new Uint8Array([…])`                 | `initializer_result_unproved`; `unknown_call_target`                                           | 14, 1.7%, 8                                    | 30, 3.2%, 1                                      | 2, left (`Set` only)                           |
| Host or builtin call               | `Gs=document.createElement("img")`                         | `unsupported_initializer`; `unknown_call_target`                                               | 6, 1.2%, 2                                     | 22, 1.9%, 0                                      | 4                                              |
| Call through a local value         | `[uz,wk]=xk(vy)`                                           | `split_declaration`; `unknown_call_target`, `array_spread`                                     | 12, 0.4%, 10                                   | 2, 0.1%, 0                                       | 10, left                                       |
| Global-object alias                | `var bk=gk`                                                | none; `global_object_alias_escape`                                                             | 8, 0.3%, 8                                     | 0                                                | none                                           |

Covering families cumulatively, best first, the module component barely
shrinks. In `main`, after the first four families (600 of 721 statements) 598
modules are still in one component. In `editor`, after six families (311 of
356 statements) 1,022 are.

**The rules that matter most:**

1. **Vendor code reached through the CommonJS interop.** `T` and `ra` are
   `__toESM`; `N`, `R`, `u1` and `mo` are `__commonJS` thunks for
   `react/jsx-runtime` and `react`. Each of 321 `main` statements is one
   namespace alias, and every later `jsx`, `forwardRef`, `memo` or
   `createContext` call reads a member of an unknown value. Together 395
   `main` and 167 `editor` statements. No Wave 3 row covers it; the nearest
   are row 3's unsummarized provider bodies and
   <../plans/analysis_performance.md> option 5.
2. **Cross-chunk callees without a summary.** `Y` is the `editor` chunk's
   `xp=e=>(r0=r0.concat(e),e)`, called by 90 `main` statements that carry 35%
   of the chunk's unproven pairs. `__publicField` and `__export` are the same
   kind. The chunk has one imported callable summary. Row 3, left.
3. **Field stores that add a property** (`X.displayName = …`): 108 `main`
   statements. Row 9, left, and the possible-setter exemption.
4. **Owner coverage for call-free initializers**: class expressions (43),
   arithmetic, aliases, template, object and array literals (46). Execution
   is complete and the certificate is still refused. Rows 5 and 11.
5. **Operations in local callee bodies and native constructors**: 55 and 44
   statements.

**Landed rows that did not matter here:** all of them. Rows 1, 2, 9, 10 and
root `new` seal no owner (`root_attempts`: 0 sealed; `main` rewinds 3,
`editor` attempts none). Row 3's landed half summarizes one import.

## Legacy gate on the stress assignment

The legacy gate accepts it. Three checks:

- **It runs.** `test_smoke.py` against the stress tree passes: the harness
  loads the 2,570 generated modules, the toolbar appears, a rectangle draws,
  undo enables, with no console, page or request errors.
- **"Swapped" is computed after legacy ordering.** The emitted order is the
  ESM loader model over the emitter's own import lists, which already include
  the ordering imports that satisfy the legacy edges. A legacy-ordered
  swapped pair would be a rejection, so 0 is by construction. The graph has
  220,432 `sequenced` strict-chain edges in `main` and 84,731 in `editor`, so
  impure statements keep their mutual order and every unproven pair has at
  least one legacy-pure endpoint.
- **No order-dependent pair found.** 30 pairs sampled across both chunks and
  the three endpoint combinations, read in source:
  - 14 obviously commute: a vendor `require`, JSX constant, atom, class
    definition or native constructor moved ahead of an unrelated function or
    constant, with no reference path in either direction;
  - 16 are a definition (constant, function, literal, class) hoisted ahead of
    a statement that references it lazily, which is the dependency-first
    direction and unobservable unless the source read an uninitialized `var`;
  - 0 plausibly or clearly order-dependent.

  Over every unproven stress pair, when a havoc statement is hoisted ahead of
  a source-earlier statement it has no reference path to that statement,
  through any use edge, in 19,507 of 19,507 `main` pairs and 30,466 of 32,537
  `editor` pairs. The other 2,071 are class expressions with complete
  execution and no loss, whose methods reference the partner lazily. This
  check does not follow callbacks escaped by earlier statements, so it bounds
  the statement's own closure, not the pool.

The acceptance therefore rests on one rule: impure statements keep their
mutual order while definitions the legacy graph calls pure move freely. What
the reference check leaves open, whether an unknown callee can invoke a
callback escaped by an earlier statement that reads the moved definition, is
the question a replacement producer answers with the set escaped as of that
statement.

## Reading

Under global reach, coverage is all-or-nothing at the module level as well as
per statement: one remaining havoc statement conflicts with every definition
it swaps with. On this corpus the unproven pairs are havoc statement against
pure definition, and the havoc statement almost never references the
definition. What decides module granularity here is what a havoc statement is
taken to reach (<../plans/synchronous_execution_fallback.md>), or a producer
that gives every statement a complete effect set, more than any list of
coverage rows.

## Tooling friction

- `bbr build` did not download the second of two tree-artifact targets
  (6,032 files); `bbapi artifact download <invocation> <path>` fetched the
  reports.
- The precision-loss list is not in execution order, so "first loss" from the
  export is not the trigger. The lexical `statement_ordinal` separates own
  from inherited losses, but not a direct callee from a pooled callback; the
  legacy purity reason (`impure_function_call`) was needed for that.
- `source_location.start_column` is 1-based.
