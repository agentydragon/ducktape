# Splitting a bundle into ES modules, with a Lean proof

A flat JavaScript bundle goes in. A spec says which top-level statement goes to which module.
The experimental splitter emits ES modules after Lean certifies the requested
CoreJS guarantee. The [target theorem](PLAN.md#target) covers every input and spec accepted by the constructor, with its proof checked when building the splitter. [Certification commands and guarantees](#request-a-corejs-guarantee)

[`SUPPORT.md`](SUPPORT.md) inventories the syntax, standard library and host
features the intended inputs require. [CoreJS coverage](model/README.md) tracks
the fragment exercised by the model tests.

From the repository root, run the same checks as CI:

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/..." --config=ci
```

Bazel supplies Lean, Node, Python, and the JavaScript dependencies. For focused checks and longer experiments, use the following commands with the same `p` (`out` is an absolute output path):

| Task                                                | Command                                                                               |
| --------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Audit the shared proofs                             | `bbr test "$p:axioms_test" "$p:corejs_axioms_test"`                                   |
| Compile the extra Lean proofs                       | `bbr test "$p:experiments_test"`                                                      |
| Compare module evaluation order across 500 graphs   | `bbr test "$p/es_order:es_order_sweep_test"`                                          |
| Check translation fixtures and effect cases         | `bbr test "$p/translation:fixtures_test" "$p/effects:effects_cases_test"`             |
| Run a larger translation fuzz sample                | `bb run "$p/translation:translation_fuzz" -- --count 150 --seed 1 --require-accepted` |
| Run the extended CoreJS comparison                  | `bbr test "$p/model:corejs_differential_sweep_test"`                                  |
| Run the debundle case corpus                        | [Corpus commands](debundle_cases/README.md#run-it)                                    |
| Lower public library bundles and check their splits | [Bundle commands](experiments/README.md#6-lower-the-ast-into-lean)                    |

## Request a CoreJS guarantee

```sh
bb run //devinfra/js/debundle/x/split_proof:lowersplit -- deployment.json out
bb run //devinfra/js/debundle/x/split_proof:lowersplit -- deployment.json out --check-output
```

Use a single source chunk with an authored, sequential placement. For example, for
`const seed = 7; export const result = seed;`, write these two files beside the source:

```json
{ "entry": "entry.mjs", "chunks": [{ "path": "entry.mjs", "source": "source.mjs", "spec": "placement.json" }] }
```

```json
{
  "entry": "entry.mjs",
  "modules": { "worker.mjs": ["#0"], "entry.mjs": ["#1"] },
  "order": ["worker.mjs"],
  "sequential": true
}
```

Lean constructs the modules from the source and placement. Supported rules cover
ordered named declarations and indexed local exports. A success reports
`compiled-constructor` and `all-outcomes`, including when the caller requests
only normal completion. The CLI stages and reparses output before publication.
An unsupported request exits nonzero without creating or replacing the destination.
The same constructor must accept before `--check-output` can verify an existing tree.

For a flat owned Node package, the
[callable provider example](integration/library_splits/LODASH_SORTED_UNIQ.md#ordinary-cli)
shows how to declare the package root, provider and a caller that only
snapshots the imported entry default. This route requires an explicit
`--require all-outcomes` and a fresh destination for publication. The
verdict's `proof_scope` is the linked entry and provider, not general
package behavior.

Host-dependent, structural and multi-chunk checks cannot publish output.
The [construction roadmap](CONSTRUCTION_ROADMAP.md) tracks coverage. For the
scope of earlier artifact certificates, see the
[historical checker guide](https://github.com/agentydragon/ducktape/blob/b88098ac2a1cbaa970a7019f50ec78df94054629/devinfra/js/debundle/x/split_proof/README.md).
Use `--diagnostic` for plan or conflict reports; it does not write modules.

## What is proved

The abstract theorem below applies to a plan and its stated footprints. The planner uses its `Valid` checks. For the artifact guarantee of
the normal splitter, see [CoreJS certification](#request-a-corejs-guarantee).

[`EsmSplit/Correct.lean`](EsmSplit/Correct.lean):

```lean
def Correct (B : Bundle) (P : Plan) : Prop :=
  (P.execOrder.Nodup ∧ ∀ x, x ∈ P.execOrder ↔ x < B.length)      -- every statement runs exactly once
  ∧ Hygienic B P                                                   -- every name means what it meant
  ∧ ∀ (V W : Type) (M : Meaning V W) (s0 : St V W),                -- whatever the statements mean:
      B.exec M s0 P.execOrder = B.exec M s0 (List.range B.length)  -- same values, same world

theorem split_correct {B : Bundle} {S : Spec} {P : Plan} (h : Valid B S P) : Correct B P
```

For any statement semantics satisfying `Meaning`, an accepted plan executes
each statement once, preserves binding resolution, and leaves the same
top-level values and world state.

`Valid` is a conjunction of decidable checks ([`Plan.lean`](EsmSplit/Plan.lean)):

| Check                       | Says                                                                                                                  |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `Faithful`                  | each statement sits in exactly the module the spec names; no selector is dangling                                     |
| `Linkable`                  | each import names a binding another module declares and exports; no name is bound twice; each glue export is declared |
| `ExportsUnique`             | no module exports a name twice: its glue keeps clear of the names its own `export` statements publish                 |
| `Hygienic`                  | each name a statement mentions resolves, through imports, to the declaration it had in the bundle                     |
| `AssignsHome`               | code that assigns a name sits in the module that declares it (ES imports are read-only)                               |
| `Permutes`                  | the split runs every statement exactly once                                                                           |
| `OrderOk`                   | in the order the split runs them, conflicting statements keep their bundle order                                      |
| `UniqueDefs`, `ReadsClosed` | side conditions: one flat scope; read sets are closed under calls                                                     |

A statement **binds** the names it declares and the names it may assign (`writes`: reassigned variables, objects whose contents it changes,
including what the functions it calls change; unknown code lists them all). These named mutable things are _cells_.
A statement may also read names it does not mention, through a function it calls that it reached through an object (`deps`); they order it like `eager` names do.
Two statements **conflict** ([`Bundle.lean`](EsmSplit/Bundle.lean)) if both have effects,
or one reads a name the other binds (either direction), or both bind the same name.
A hoisted `function` declaration never conflicts: it exists before any code runs.
Everything else may be reshuffled freely. `assigns` is a different list: where the code that assigns a name physically sits, which decides where it must live (`AssignsHome`).

The order the split runs in is computed by `Plan.visit`: a model of ES module evaluation.
It goes depth first over `import`s in source order, runs each module once, and skips a module that is already being entered.
That last rule cuts import cycles.

[`Semantics.lean`](EsmSplit/Semantics.lean) proves pairwise commutation of
nonconflicting statements (`step_comm`) and lifts it to the plan's execution
order (`exec_reorder`).
`lean:axioms_test` checks axiom dependencies;
`leanchecker` replays the declarations.

## What is not proved

The abstract theorem assumes footprint soundness for the supplied program.
[`Lower.lean`](EsmSplit/Lower.lean) scans declarations, eager and deferred
names, and assignment sites. [The Effects analysis](effects/README.md) supplies
writes, indirect reads and world effects under its printed premises.

`Meaning` models one flat environment. Using that theorem for JavaScript assumes
that `Hygienic` justifies flattening and that `Plan.visit` agrees with module
evaluation. The [module-order experiment](experiments/README.md#10-does-the-models-module-order-match-node)
compares that order with Node. In a structural preview, external imports count as
preexisting bindings. The diagnostic admission check keeps source imports and
exports in the entry. The emitter writes source imports before generated imports. Preserving external initialization order assumes synchronous dependencies
and unchanged resolution of relative specifiers. Diagnostic glue exports may widen
the entry's public surface.

For the normal splitter, the [CoreJS artifact guarantee](#request-a-corejs-guarantee)
states the parser, binding annotation, lowering, printer, model and compiler premises. The
[footprint proof inventory](docs/footprint_soundness.md#current-proof-inventory)
lists the Effects summaries already covered by separate CoreJS certificates.
For validation of a separately authored tree, see the
[translation validator's renaming premise](translation/README.md#trusted).

## Further reading

- [Production acceptance and proof work](PLAN.md)
- [Structural checker experiments and Node comparisons](experiments/README.md)
- [Historical scale measurements](experiments/TIMINGS.md)
- [Lessons from the experiment](docs/lessons_learned.md)
- [Excalidraw: certified placement, upstream ownership, and browser replay](integration/excalidraw/README.md#certified-placement)
