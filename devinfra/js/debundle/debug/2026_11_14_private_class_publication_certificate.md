# Private class publication certificate experiment

Report-only implementation in `x/summary_effects/publication{,_main,_test}.rs`.
It changes no production gate, effect classification, inference, or spec.
The frozen Excalidraw editor run discharges all 34 optimistic prototype conflicts;
52,184 of 53,448 Conservative prototype conflicts remain unproved. This is a
bounded private-allocation certificate, not a whole-chunk producer or a speedup.

## Source rule and boundary

One canonical resolved SWC parse feeds a source-only index. Source-unit ordinals
use `js_ast::split_var_declarators`; assignment/update/destructuring/loop writes
use `binding_targets`. The index visits all lazy bodies and uses full resolved
`Id`, counts declarations, and inventories direct readers, writes, and exports.
It does not consult the requested module assignment to select candidates.
`modules.json` is read only for the module count in the report.

A candidate is a single private class declaration/initializer, declared once and
never rebound anywhere. Exported cells are excluded, even when their original
prefix value is `undefined`. The class has no heritage, computed keys, fields,
static blocks, private members, accessors, or decorators. Ordinary noncomputed
static and instance methods and constructor bodies are deferred; their allocation
runs no body. This rule says nothing about the effects of **invoking** a constructor.
The experiment does not classify `new Factory()` as pure.

Every direct reader includes lazy capture owners. Any reader before initialization,
or any module-level hoisted function reader anywhere, refuses the certificate.
Direct `eval`/`with`, incomplete/default-export/ambient forms refuse the census.
The census conservatively also excludes a shadowed `eval` reference.

For each eligible initialization D and direct reader/publication owner U, generate
D -> U and B -> U for **every source unit B before U**. This treats every earlier
unit as a possible effect or throwing barrier; the unsound tier-0 pure classifier
supplies no omitted-barrier premise. The nonthrowing private allocation can move,
but its first direct value copy or cell capture cannot cross the original prefix.
There are no eager class reads in the admitted syntax; the D -> U edges retain
initialization before every eager read and conservatively before lazy captures.

The source-derived candidates and constraints are constructed before inspecting the
proposed positions. A certificate contributes to pair discharges only if all its
constraints hold in that schedule. The baseline TSVs supply only measured conflicts
for comparison; their unlisted pairs do not acquire proof from this experiment.

## Frozen result

Input source SHA-256:
`fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`.
The emitted order is the exact frozen `combined-pipeline-pairs/c3` report:
1,787 unique ordinals, a full permutation of `0..1786`, and 253,092 inversions.
No source/spec/output schedule was changed.

There are 28 top-level class candidates. Eleven satisfy the source rule and
schedule constraints. Four additional source-eligible classes fail the conservative
fences: units 604, 1462, 1463, and 1472 (1,045, 2,710, 1,389, and 1,389 violations).
This observed overconstraint is retained. The other candidates are exported,
read by an earlier owner, or have excluded eager class syntax.

| Initialization | Resolved binding | Direct reader owners | Constraints | Violations |
| -------------- | ---------------- | -------------------- | ----------: | ---------: |
| 1386           | `_e##2`          | 1387, 1388           |       2,775 |          0 |
| 1692           | `xl##2`          | 1693, 1694           |       3,387 |          0 |

The export label `_e` in `NE as _e` is not the private resolved `_e` binding.
The canonical-ID census excludes that label from both its reads and its exports.

| Prototype baseline | Baseline conflicts | Discharged | Remaining |
| ------------------ | -----------------: | ---------: | --------: |
| Optimistic         |                 34 |         34 |         0 |
| Conservative       |             53,448 |      1,264 |    52,184 |

Optimistic discharges: six pairs involving unit 1386 and 28 involving unit 1692;
29 `reach_escaped` and five `reach_own`. Conservative discharges by later class:
unit 881: 295; unit 1386: 314; unit 1464: 319; unit 1692: 336.
The two requested target classes alone remove 650 Conservative baseline conflicts.
The all-class total contains 1,259 `reach_escaped` and five `reach_own` discharges.
These are conditional replacement proofs for those private-allocation conflicts.
The Conservative count remains a prototype baseline comparison, not a prediction
of the exact constraints a future sound temporal whole-chunk producer needs.

The report explicitly writes `whole_chunk_proved: false`. Neither the future
exported-cell observation case nor opaque host reentry is waived. In particular,
the exported-initializer pairs `(5,475)` and `(1200,1753)` have no certificate here.
There is no new no-reentry premise for helper `T`, `new Set`, or host access.

## Checks

`publication_test` parses real source and selects the typed candidate certificate.
Its executable Node oracles reorder the SWC AST, not source text. Moving only
`Hidden` preserves `undefined/true` for a `var` alias and `ReferenceError/true`
for a `let` alias. Moving `Hidden` with its alias is refused and the corresponding
Node schedules print `function/true`. Another oracle uses an ordinary constructor
that observes the alias; that invocation remains a publication barrier.

Other negatives cover later textual hoisted readers, already captured readers,
later lazy captures moved past their fence, exported cells/renamed export labels,
heritage/computed keys/fields/static blocks/accessors, assignment/update/pattern/
loop/closure writes, redeclarations, dynamic scope, and shadowed resolved IDs.

These are certificate and Node tests, not evidence that the production CLI selected
this report-only path. Separate real CLI bundle+spec publication/export oracles
are maintained by the sibling worker in `e2e/publication_order_test.rs`. Its
observed legacy cyclic-export defect must be fixed independently; this producer
never admits exported class cells or non-class callback initializers.

First checked implementation commit:
`325d89dbd30a9ccab379b44393afcc4c67dd163d`.
Eight initial focused tests passed on exact-commit RBE invocation
[7f1ffa92-37eb-443a-93ff-4039e858dbd6](https://app.buildbuddy.io/invocation/7f1ffa92-37eb-443a-93ff-4039e858dbd6).
The frozen report binary compiled with Clippy and ran successfully under invocation
[094300c7-d562-4eb5-b047-83f21d78747b](https://app.buildbuddy.io/invocation/094300c7-d562-4eb5-b047-83f21d78747b).
A ninth constructor-observation test was added afterward; its final result is
recorded in the task handoff. Changed-file pre-commit passed before the first
commit. No native timing loops, production acceptance claims, PR, or benchmark
speedup are part of this experiment.

## Artifacts and reproduction

Task scratch root:
`/tmp/agents/2026_11_14/excalidraw-speed/publication-certificate`.
`editor-publication-report.json` contains the complete candidate/violation inventory
and every discharged baseline pair. `editor-run.log` and `test.log` retain output.
The worktree and experiment branch preserve the implementation.

From that worktree after `direnv allow`:

```sh
task_root=/tmp/agents/2026_11_14/excalidraw-speed
direnv exec . bb run //devinfra/js/debundle/x/summary_effects:publication_certificate -- \
  --chunk "$task_root/frozen/devinfra/js/debundle/excalidraw/chunks/editor.js" \
  --effect-report "$task_root/combined-pipeline-pairs/c3/out/reports/tree/editor/effect_translation_validation.json" \
  --modules "$task_root/combined-pipeline-pairs/c3/out/reports/tree/editor/modules.json" \
  --optimistic-pairs "$task_root/eligibility/editor-base-unproven.tsv" \
  --conservative-pairs "$task_root/eligibility/editor-conservative-base-unproven.tsv" \
  --out "$task_root/publication-certificate/editor-publication-report.json"
```

Stop at this bounded certificate. A whole-chunk implementation still needs sound
base completion/eager-read proofs, publication/cell observation constraints for
exports, and checked source/host reentry effects. The remaining 52,184 conflicts
cannot be removed through timeless export-root suppression or spelling-based
purity. A speed claim additionally needs root-serialized matched measurements
once that admission boundary is sound.
