# L4c discrepancy audit, first pass (2026-11-09)

Legacy gate verdict vs effect verdict (`effect_translation_validation.json`
`legacy_gate_accepts` vs `decision.status`) for every candidate evaluation, on
`devel` at `fa49ae5ed1` (L2a, L2b, array spread, native rows, L2c PR 1 and 2).

## Inputs and procedure

- **e2e:** `bazelisk test //devinfra/js/debundle/e2e/... --test_env=DEBUNDLE_TV_AUDIT=1`
  (`DEBUNDLE_TV_ORACLE=1` is already on in every target). Invocation
  `a90c2b2a-4ef0-4398-8363-02967b5a76de`: 213/213 targets pass, 203 targets
  evaluate a candidate. Each evaluation writes
  `debundle_tv_audit/<pid>-<n>.{json,conflicts.jsonl}` (legacy, edge-TV and
  effect reports, plus every conflict row) and `<pid>.sources.json` under
  the target's undeclared outputs. The e2e support layer adds
  `debundle_node_parity/`: Node output of the source and the emitted entry
  for every accepted fixture, and for every gate-refused fixture rerun with
  `--emit-on-rejection`, tagged with that run's effect verdict.
- **Canonical:** gaffer-private `39d87303e` with
  `patches/ducktape_cargo_source_labels.patch`,
  `--override_module=ducktape=<devel fa49ae5ed1>`, the five accepted product
  targets (`DUCKTAPE_PINNING.md` § Validation, plus Quest). Invocations
  `c6138a7b-a217-4efb-9e92-93a3e3757baf` (build) and
  `51b8b683-6977-4ec7-b1df-bcdc55beb3f6` (`--remote_download_toplevel`).
  Per-chunk `effect_translation_validation.json` only; no full conflict
  export (Tana Web alone has 5.18M conflicting pairs).
- One evaluation = one distinct (target, test, chunk, emitted order, legacy
  verdict, effect status).

## Totals by class

| Input                     | Evaluations | Agree | Sharper effects | Missing coverage | Unsafe legacy | Defect |
| ------------------------- | ----------: | ----: | --------------: | ---------------: | ------------: | -----: |
| e2e (203 targets)         |        2540 |  2104 |              76 |              360 |             0 |      0 |
| Claude CLI 2.1.292 subset |   76 chunks |    62 |               0 |               14 |             0 |      0 |
| Claude Web 2026-10-23     |  807 chunks |   785 |               0 |               22 |             0 |      0 |
| Tana Web 78d928dca7       |     1 chunk |     0 |               0 |                1 |             0 |      0 |
| Tana Desktop v1.515.0     |     1 chunk |     0 |               0 |                1 |             0 |      0 |
| Quest 2026-11-08          |   44 chunks |    44 |               0 |                0 |             0 |      0 |

The "Unsafe legacy" column is what this pass's fixtures showed. It is zero
because no fixture had the unsafe shape, not because the gate had none
(§ Unsafe legacy acceptance).

e2e breakdown:

| Class                                                                       | Evaluations | Targets |
| --------------------------------------------------------------------------- | ----------: | ------: |
| Agree: both accept                                                          |        1375 |     180 |
| Agree: both refuse (effect `conflict` or `incomplete`)                      |         709 |      99 |
| Agree: legacy refuses; effect accepts the order; placement blocks           |          20 |      12 |
| Sharper effects: no reordering, legacy veto is a semantic-order SCC         |          41 |       9 |
| Sharper effects: swapped pairs commute, legacy veto is a semantic-order SCC |          35 |      11 |
| Missing coverage: uncovered havoc                                           |         350 |      83 |
| Missing coverage: premise-covered havoc reported `incomplete`               |          10 |       3 |

Every legacy-accepted / effect-refused case, e2e and canonical, is an
uncovered **havoc** pair (one of them unsafe: § Unsafe legacy acceptance): no case has an uncovered modeled cell conflict
(e2e: 841 uncovered rows, all `havoc`, `cell_count` 0,
`legacy_edge_conflict` false; canonical: `pairs == havoc_pairs`, 0 cell
incidences, 0 premise contradictions). The effect gate never names a
concrete conflicting cell that the legacy gate let through.

## Node evidence

`debundle_node_parity/` probes, by the verdict of the run that emitted the
probed tree. "Unrunnable" = a multi-chunk fixture whose sibling chunk is
not in the snapshot (`ERR_MODULE_NOT_FOUND`).

| Run kind | Legacy | Effect     | Swaps | Same | Diverges | Unrunnable |
| -------- | ------ | ---------- | ----- | ---: | -------: | ---------: |
| accepted | accept | accepted   | no    | 1153 |        2 |          6 |
| accepted | accept | accepted   | yes   |  269 |        1 |         12 |
| accepted | accept | incomplete | yes   |  394 |        0 |          5 |
| rejected | refuse | accepted   | no    |   64 |        1 |          0 |
| rejected | refuse | accepted   | yes   |   88 |        1 |          4 |
| rejected | refuse | conflict   | yes   |  412 |      529 |         14 |
| rejected | refuse | incomplete | yes   |   62 |       16 |          3 |
| rejected | accept | accepted   | no    |    0 |        1 |          0 |

Every divergence outside the "both refuse" rows:

| Case                                                                                                                   | Cause                                                                                                                                | Class                      |
| ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------- |
| `passed_to_call_lowering_test::{constrains_by_object,constrains_by_arg_index,pins_registry_distinguished_empty_class}` | Class rename changes `Function.prototype.name`; the test asserts the renamed output                                                  | Outside the gate (renames) |
| `peel_factorize_landability_test::proposer_does_not_emit_binding_only_proposal_for_rebound_split_let`                  | `TypeError: Assignment to constant variable` (cross-module rebind); status `blocked_by_placement`                                    | Agree (placement)          |
| `unknown_call_intermediate_availability_test::split_is_rejected_when_callback_value_precedes_a_later_var_reassignment` | Same; `cross_destination_rebinds` blocks                                                                                             | Agree (placement)          |
| `loader_closed_reentry_test::closed_commonjs_request_keeps_the_static_importer_proof`                                  | The probed `static/consumer` verdict is accept/accept; the refusal and the divergence are `static/provider`, where both gates refuse | Agree (probe attribution)  |

## Unsafe legacy acceptance

One class, found after the first pass and reproduced on `cfba98e22e` and
`3cb00014f2`: **a read of another chunk's mutable state moved across a
writer of that state.**

```js
// static/peer.js
let count = 0;
export function next() {
  return ++count;
}
export function peek() {
  return count;
}
// static/app.js
import { next, peek } from "./peer.js";
const first = next();
const second = peek();
console.log(first, second);
```

With `second` in a logical module and the rest inline, the legacy gate
accepted, the effect verdict was `incomplete` (uncovered havoc), and Node
printed `1 0` for the emitted entry against `1 1` for the source.

Mechanism: the strict S-chain and the dataflow-aware chain order only
statements whose purity is not `Pure` (`emit_s_chain`, `graph/build.rs`). The
cross-module purity oracle classified `peek` as `Pure` because its body
writes nothing, and the importer's graph has no node for `count`, so nothing
ordered `second` against `first`. The same held for a direct read of an
imported live binding (`import { count }`), which the classifier treats as a
pure identifier read. Rule and fixtures: <../docs/design.md> § Reads of
another chunk's cells, `e2e/foreign_cell_read_order_test.rs`.

Shapes the legacy gate accepted unsoundly before that rule, each with a Node
divergence: a reader of a rebound `let` or reassigned `var`; a reader of an
array's contents (`items.length`); writer and reader in two chunks sharing a
third chunk's state; a reader behind a local function; a writer invoked
through a provider callback; a direct read of a live binding, also through a
re-export or as a default export. Already refused: readers reached through a
namespace import or a re-exported function, provider state in an object
property, and reader and writer as methods of an exported object.

### Why the first pass reported none

The first pass probed only the fixtures the suite already had: all 394
runnable legacy-accepted / effect-`incomplete` fixtures matched their source
under Node. That bounds the fixtures, not the gate. No fixture had this
shape, because it needs four things at once and each test was written for
one feature:

- a second analyzed chunk (most fixtures are one chunk);
- mutable state in that chunk (`provider_summary_effect_test` was the first
  with a counter);
- a reader of the state the classifier calls `Pure` (the existing
  counter test only called the writer twice, and a writer is impure, so the
  chain ordered it);
- a split that moves the reader across a writer.

Whether the canonical inputs depend on the old acceptance is not measured:
the canonical-spec check has not been run on the new rule.

Probing accepted fixtures cannot find a hole no fixture exercises. The
systematic check is a generated differential: enumerate provider state
shapes, the paths an importer takes to their reader and writer, and both
statement orders, and require that every split the gate accepts runs like
its source under Node.

## Defects

The legacy gate had one under-restriction (§ Unsafe legacy acceptance); none
found in the effect gate. One policy decision is open for L4d (below).

## Sharper effects (76 e2e)

The legacy gate refuses only through an atomic-group conflict classified
`semantic_order_scc` (`legacy_gate_ordering_veto: true`). The edge TV
rejects with `forward_edges_kept > 0`, and the effect sweep accepts the same
assembled order (`no_reordering` 41, `statement_order_commutation` 35).
Node: every legacy-refused / effect-accepted run probed under
`--emit-on-rejection` matches its source (152 same, 4 unrunnable) except the
two placement-blocked rebinds above. Targets: `bundle_owned_globals_test`
(12), `reentry_free_operations_test` (11), `imported_callback_timing_test`
(7), `native_slot_integrity_test` (7), `native_call_summary_test` (6),
`math_round_timing_test` (5), `namespace_wrapper_test` (5),
`captured_value_input_test` (4), `descriptor_copy_factory_test` (4), and 9
others with 1–3 each. L4d note: on the flip these tests' refusals become
acceptances of the _assembled_ candidate, not of the authored split.

## Missing coverage (360 e2e, 38 canonical chunks)

### Premise-covered havoc (10 e2e): L4d policy

`commuting_statements_gate_test` (8), `commuting_statements_test` (1),
`propose_commutations_cli_test` (1). Every swapped pair is havoc but
`covered_by_existing_premise`. The decision returns
`Incomplete { premise_covered: true }`
(`realizability/effect_translation_validation.rs`, the
`has_uncovered_havoc || has_premise_covered` arm), whereas the translation
validation gate's definition accepts a swapped pair "covered by a
`commuting_statements` premise". Decided 2026-11-09: the premise is an axiom
and discharges the pair; L4d makes this arm an acceptance
(<../plans/convergence_landing.md> § Wave 4, L4d).

### Uncovered havoc, by missing rule (350 e2e)

Each opaque statement of an uncovered pair (`first_opacity`/`later_opacity`
with any loss, opacity or unknown domain) is classified from its source text
and loss/refusal labels by first-match pattern rules. This is heuristic:
treat counts as ±10 %. "Touching" counts evaluations with at least one
statement under the rule. "Sole" counts evaluations that the owner alone
would recover.

| Owner                                                                                                                                                                               | Touching |                  Sole | Targets |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------: | --------------------: | ------: |
| **Unowned: anonymous-statement owner coverage** (`unsupported_owner_kind`: global publications, heap writes, bare assignments; abstract_interpreter step 7)                         |      110 |                    18 |      28 |
| **S3** provider activation, imported factory bodies, callbacks and retained factories                                                                                               |      106 |                    56 |      30 |
| **Unowned: whole-owner seal of a completed call initializer** (local calls, IIFEs, destructured call results, comma initializers; `unsupported_initializer` with no execution loss) |       75 |                    24 |      27 |
| **S4** host timing and reentry (`console.*`, `globalThis.f()`, `new globalThis.Date`, `Promise.*`, global-object aliases)                                                           |       72 |                    21 |      24 |
| **S5** class construction                                                                                                                                                           |       46 |                    18 |      19 |
| **S2** array iteration rows and spread                                                                                                                                              |       36 |                    12 |      15 |
| **Unowned: non-invoking initializer coverage** (object/array literals with callables or getters, templates, member reads, arithmetic: `ValueExpr::from_expr_with_coverage` refuses) |       28 |                     7 |      16 |
| **S1** remainder: native constructor/call rows (`new Map([...])`, `Object.defineProperty` aliases, builtin replacement)                                                             |       25 |                    10 |      16 |
| **S7** purity-annotation authority (`purity_test` only)                                                                                                                             |       22 |                    22 |       1 |
| **Conditional/logical IR slice** (`?.`, `??=`, `                                                                                                                                    |          | `, `!function(){}()`) |      13 | 0   | 6   |
| **S6** qualified module cells (imported primitive operands, export linkage owners)                                                                                                  |        9 |                     1 |       3 |
| Unclassified / no source                                                                                                                                                            |        8 |                     4 |       5 |

Rules within the owners that Wave 3 slices 3, 4 and 6 hold:

| Rule                                 | Owner | Touching | Sole | Example                                  |
| ------------------------------------ | ----- | -------: | ---: | ---------------------------------------- |
| Call with a callback/record argument | S3    |       85 |   44 | `const wrapped = api.make(() => later);` |
| Anonymous call statement             | S3    |       16 |    7 | `da([taga], Alpha.prototype, "x", 1);`   |
| Unknown or imported callee           | S3    |        9 |    3 | `const before = external();`             |
| Host call or host constructor        | S4    |       57 |   19 | `const startup = new globalThis.Date();` |
| Global-object alias                  | S4    |       18 |    1 | `const root = globalThis;`               |
| Membership on the global object      | S4    |        1 |    0 | `'cache' in globalThis;`                 |
| Imported primitive operand           | S6    |        7 |    0 | `const computed = 4 * bytes + 256;`      |
| Export linkage owner                 | S6    |        2 |    1 | `export { shared } from './peer.js';`    |

The anonymous-statement owner rule is led by global publication
(`globalThis.saved = () => later;`: 98 touching, 13 sole) and overlaps S4.
14 evaluations need exactly {anonymous-statement coverage, S4}, and 8 need
{anonymous-statement coverage, S3}.

Cumulative recovery of the 350 evaluations, taking each owner set alone:

| Owners landed                                     | Recovered |
| ------------------------------------------------- | --------: |
| S3                                                |        56 |
| S4                                                |        21 |
| S6                                                |         1 |
| S3 + S4                                           |        82 |
| S4 + anonymous-statement coverage                 |        53 |
| S3 + S4 + anonymous-statement coverage            |       123 |
| … + call-initializer seal                         |       179 |
| … + S5                                            |       220 |
| … + S1, S2, non-invoking literals, conditional IR |       311 |
| … + S6, S7                                        |       342 |

Ordering input for slices 3, 4 and 6: S3 has the largest sole recovery.
S4 recovers 21 alone and 53 once anonymous-statement coverage exists, so
that coverage is S4's real prerequisite. S6 touches 9 evaluations in 3
targets (`imported_primitive_test`, `saved_analysis_reports_test`,
`effect_tv_global_provenance_test`). No Wave 3 slice owns the three
unowned rules, which together touch 175 of the 350 evaluations.

### Canonical (38 chunks, all legacy-accepted, all effect `incomplete`)

| Product (disagreeing chunks) | Uncovered havoc pairs | Havoc statements / statements | Owner refusals among statements (top)                                                              |
| ---------------------------- | --------------------: | ----------------------------: | -------------------------------------------------------------------------------------------------- |
| Claude CLI 2.1.292 (14)      |               232,040 |                3,315 / 25,612 | `unsupported_initializer` 2,746, `unsupported_owner_kind` 357, `missing_initializer` 165           |
| Claude Web 2026-10-23 (22)   |               354,627 |                5,483 / 33,678 | `unsupported_initializer` 3,673, `initializer_result_unproved` 1,384, `unsupported_owner_kind` 209 |
| Tana Web 78d928dca7 (1)      |             5,179,380 |                 2,300 / 9,709 | `unsupported_initializer` 1,317, `unsupported_owner_kind` 937                                      |
| Tana Desktop v1.515.0 (1)    |                21,286 |                 2,296 / 4,413 | `unsupported_initializer` 1,718, `missing_initializer` 425, `unsupported_owner_kind` 130           |

The bounded examples (at most 20 pairs per chunk, the first swept) were read
by hand; counts are opaque example sides among the 25 most frequent
statements. The pattern rules do not apply to minified lines. Leading shapes
per product:

- **Claude Web:** esbuild export registration `f(ft,{default:()=>os})` (20,
  anonymous-statement/S3); `new Map`/`new WeakMap` initializers with
  `initializer_result_unproved` (49, S1 remainder: the application
  keyed-collection lane named in L2a's tombstones); `Object.freeze({...})`
  (19, S1); imported calls `var P=i(void 0)` (15, S3); class expressions
  extending a local class or `Error` (20, S5); `new Set([ids])` (17, S1/S2);
  literal data with computed keys, templates, spread, `-1/0`, `2**20` (≈60,
  non-invoking literals); `typeof Element<"u"&&…` (10, conditional IR + S4).
- **Tana Web:** `let TA = { REACT_APP_ENV: mr.REACT_APP_ENV, … }` member-read
  literals (13), and literal data `{ input: 10 / 1e3 }`, `[m.a, m.b]` (6)
  (non-invoking literals); `Object.defineProperty`/`getOwnPropertyDescriptor`
  aliases (4, S1).
- **Tana Desktop:** template-literal initializers (9, non-invoking);
  anonymous `!(function(){try{…}})()` (9, anonymous-statement/conditional
  IR); class expressions with constructors (6, S5);
  `var __originalRequire = require` (2, S4).
- **Claude CLI:** extracted sources are generated, so examples were not
  read; refusal counts only.

## Effect of concurrent work

- **L2c** (`roots/` phases): `initializer_result_unproved` is produced in
  `facts/sync_effects/roots/finalize.rs`. The two unowned owner rules
  (anonymous statements; call-initializer seal) live in the owner
  finalization phase L2c is moving, so they should land on L2c's `roots/`,
  after `complete: bool` is deleted.
- **Wave 3 slice 5** (class construction): 46 e2e evaluations touching, 18
  sole, 19 targets; canonical class expressions in Claude Web and Tana
  Desktop.
- **Conditional/logical IR slice:** 13 e2e touching, 0 sole: each case also
  needs S3, S4, the call-initializer seal or non-invoking coverage.
  Canonical guards (`typeof X<"u"&&…`) show up in Claude Web and Tana
  Desktop examples.

## Rerun after Wave 3 slice 3 PR 2 (2026-11-10)

`debundle_node_parity/` records only (one per fixture run), the PR head
against its `devel` merge-base `a7006bf26e`; the pattern-rule scripts were not
available, so the per-owner counts above are not recomputed.

| Legacy  | Effect status | Base | Branch |
| ------- | ------------- | ---: | -----: |
| accepts | accepted      | 1512 |   1535 |
| accepts | `incomplete`  |  393 |    394 |
| refuses | accepted      |  159 |    153 |
| refuses | `conflict`    |  955 |    955 |
| refuses | `incomplete`  |   81 |     81 |

Compared run by run (target, test, source output): no existing fixture
changed its legacy verdict or its emitted order. Two changed effect status,
both `imported_callback_timing_test`, from `incomplete` to accepted
(`returned_wrapper_does_not_invoke_its_callback_at_creation`,
`successive_factories_do_not_publish_callbacks_to_the_next_factory`). The
other differences are new or renamed tests: `provider_summary_effect_test`,
and six `export_availability_test` runs that now emit on rejection and write
no parity record. Most S3 havoc is untouched: it is namespace-member and
author-asserted factories, which the ordered lane does not substitute, and
invocations of a retained result.

## Re-running

The scripts are heuristic one-offs, kept outside the repository at
`/var/cache/agentydragon/l4c/scripts/`, with a copy in
`~/code/ducktape-scratch/l4c/`. The baseline records for this pass are
`/var/cache/agentydragon/l4c/e2e_records_fa49ae5ed1.jsonl`. The pattern
rules are in `rules.py`.

Record schema, per target under `bazel-testlogs/<target>/test.outputs/`:

- `debundle_tv_audit/<pid>-<n>.json` has `argv`, `chunk_id`,
  `translation_validation` (edge-TV report), `candidate`
  (`CandidateRelocationAssessmentReport`) and `effect`
  (`EffectTranslationValidationReport`).
- `debundle_tv_audit/<pid>-<n>.conflicts.jsonl` holds one
  `EffectConflictDiagnosticRow` per inverted conflicting pair.
- `debundle_tv_audit/<pid>.sources.json` maps each source path to its text.
- `debundle_node_parity/<pid>-<n>.json` has `test`, `kind`
  (`accepted`/`rejected`), `source` and `emitted` (`{code, stdout, error}`),
  and `effect` (`{legacy_gate_accepts, status, swapped_pairs,
emitted_order}`) for the run that emitted the tree.

Steps:

```sh
bazelisk test //devinfra/js/debundle/e2e/... --test_env=DEBUNDLE_TV_AUDIT=1 --keep_going
S=/var/cache/agentydragon/l4c/scripts
TL=$(readlink -f bazel-testlogs)/devinfra/js/debundle/e2e
python3 $S/collect.py $TL records.jsonl   # one line per evaluation
python3 $S/final.py records.jsonl         # class totals, owner and rule counts
python3 $S/classify.py records.jsonl      # owner combinations; writes per_eval.json, statements.txt
python3 $S/cumul.py                       # cumulative recovery from per_eval.json
python3 $S/parity2.py $TL                 # Node parity by verdict, and divergences
# Canonical: build the five targets with --remote_download_toplevel, then
python3 $S/canon.py <product>.out ...           # per-chunk verdicts
python3 $S/canon_examples.py <gaffer-root> <product>.out ...   # bounded examples with source text
```

## What remains

- Full conflict export (`--effect-conflicts-jsonl`) for the canonical chunks
  under 1M pairs, with span-level source text, to replace the hand-read
  bounded examples.
- Repeat this pass after each of S3, S4, S5 and the conditional IR slice
  lands; the audit records are a single `--test_env` away.
