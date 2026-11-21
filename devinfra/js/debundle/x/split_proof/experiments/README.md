# Experiments: what it costs to go further

Run it from the project root: `./run.sh --experiments` (§ 1–4), `--lowering` (§ 6), `--es-order` (§ 10), `--splits` (§ 7), `--debundle-cases` (§ 11), `--translation` (§ 12). § 5 and § 8 have their own scripts. The [ESMeta spike](esmeta/README.md) investigates a fuller JavaScript semantics.
Line counts are Lean code lines (no comments, no blank lines).

## 1. Sound plans without a spec

[`EsmSplit/Correct.lean`](../lean/EsmSplit/Correct.lean) defines `Sound` and proves `sound_correct`:
if a recovered plan passes the structural and ordering checks, it satisfies `Correct`. The translation
validator (§ 12) uses those checks on a plan recovered from a bundle and an output tree. Its pairing,
renaming and JavaScript front end are not covered by that theorem.

An earlier verbatim-output checker exposed a callback/TDZ bug in the extractor: it treated a callback's
read as lazy even when the callback ran during initialization. The regression now checks the extractor's
eager read and the translation validator's verdict, and runs the unsafe output in Node.

## 2. A real semantics for a small JS-like language

[`MiniJS.lean`](../lean/Experiments/MiniJS.lean): numbers, one-argument closures, application, recursion through module-level
names, and `log`. It is Turing-complete (self-application diverges). Evaluation is fuel-bounded call-by-value.
Divergence means "no result at any fuel", and the theorem never reasons about it.

It _derives_ what `Semantics.lean` assumes: a statement's behavior depends only on the names in its footprint.

| Part                                                    | Lines   |
| ------------------------------------------------------- | ------- |
| language, substitution, evaluator                       | 41      |
| locality + coverage lemma (the semantic content)        | 66      |
| statement footprints, computed from the syntax          | 21      |
| read closure: proved in `Bundle.lean` now; helpers here | 6       |
| reachable-state invariant                               | 35      |
| concrete step, abstract meaning, helpers                | 49      |
| adequacy: the abstract `Meaning` is the evaluator       | 37      |
| invariant preserved by every step                       | 56      |
| run correspondence + final theorem                      | 26      |
| **total**                                               | **337** |

Final theorem, for the real evaluator, at every fuel:

```lean
theorem mjs_reorder (hc : ReadsClosed (toBundle P)) (hwf : WF P) (fuel : Nat) (ord : List Nat) …
    (hok : ord.Pairwise fun a b => a < b ∨ (toBundle P).conflict a b = false) :
    ord.foldl (fun s j => cstep P fuel j s) (G0 P, []) = (List.range P.length).foldl (fun s j => cstep P fuel j s) (G0 P, [])
```

[`MiniJSExample.lean`](../lean/Experiments/MiniJSExample.lean) runs a 7-statement program through it: higher-order `twice`,
a closure over a constant, two logs. A reordering is proved equal, both orders print `[3, 3]` in the kernel,
and a bad order is rejected and prints something else.
The theorem's axioms are Lean's three standard ones, as for the main development.

What a real fragment adds beyond this, and I did not do: a heap (allocation order must be shown unobservable, or allocation
counts as an effect), objects and prototypes, exceptions, and the module system itself.

## 3. Two tiers: automatic for the formulaic pairs, custom Lean for the rest

[`TwoTier.lean`](../lean/Experiments/TwoTier.lean): the reordering theorem with the commutation test as an oracle (68 lines; it is the proof
of `Semantics.lean` over any step function). Tier 1: footprints do not conflict. `step_adequate` + `step_comm` give the commutation (14 lines).
Tier 2: the user lists extra pairs and proves `SemComm a b` in the real semantics (19 lines for the theorem).

[`TwoTierExample.lean`](../lean/Experiments/TwoTierExample.lean): `((x) => x)(1);` is a call, so the analysis calls it an effect and tier 1
refuses to move a `log` past it. The custom proof that it is the identity step is one line, because the call is closed and `simp`
just evaluates it. That is the easy end. A call to a function defined elsewhere, shown log-free, needs an effect analysis proved
sound once per language (my estimate: 40–60 lines for this language). Symbolic facts (for all `n`, `isEven n` …) need real induction.

## 4. How much does Lean's own automation write?

[`Automation.lean`](../lean/Experiments/Automation.lean), the lemmas that took the most iterations by hand:

| Lemma                                         | `grind` unaided | with a little scaffolding                                                    |
| --------------------------------------------- | --------------- | ---------------------------------------------------------------------------- |
| nodup + pairwise of the shortened list        | works           | –                                                                            |
| membership bookkeeping                        | fails           | `simp`, then `grind`                                                         |
| moved element conflicts with nothing after it | fails           | unfold `Pairwise`/`Nodup`, then `grind`                                      |
| commutation of non-conflicting statements     | fails           | case split + the two frame facts by hand, then one generic `first \| … \| …` |

Tactics finish leaf goals. The ideas (the frame fact, "slide the maximum to the end") were mine.

## 5. How far does the check scale?

Times and conditions: [`TIMINGS.md`](TIMINGS.md). Raw output: [`records/`](records).

**The first version was slow in the wrong places.** The checks were plain statements (`∀ i ∈ range n, …`) and `decide` ran them as written.
A profile of real bundles found the cost at once: a closure recomputed inside a loop, list scans for membership, and `B.stmt i`, which is a list lookup.
Each check now has an algorithm over `TreeSet`/`TreeMap` tables and a theorem that it computes the statement
([`Fast.lean`](../lean/EsmSplit/Fast.lean), [`Bundle.lean`](../lean/EsmSplit/Bundle.lean)). The statements in `Plan.lean` did not change.
The `Decidable` instances run the fast algorithms, compiled and in the kernel. The equivalence proofs are about 450 lines of Lean, more than everything else in the core.

| Check                                 | Before                                                   | Now                                                                                                           |
| ------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| read closure (`ReadsClosed`, `reads`) | list closure with `eraseDups`, recomputed for every name | worklist over a `TreeMap` index of what each declaration mentions; proved closed and least (`closeFrom_spec`) |
| `UniqueDefs`                          | all pairs of statements                                  | one pass with a set                                                                                           |
| `Hygienic`, `Linkable`                | scan the module's statements for every name              | one table per module: declared, exported, imported from                                                       |
| `Permutes`                            | list membership                                          | sets                                                                                                          |
| `Faithful`                            | claimants recomputed for every module                    | claimants once per statement, one statement set per module                                                    |
| `OrderOk`                             | `reads` recomputed for every pair                        | read sets and statements looked up in tables built once                                                       |
| `mkPlan` (untrusted)                  | list scans                                               | index maps; the same modules as before on 30 real splits                                                      |

**Compiled, real bundles**, split into contiguous blocks (a timing workload; § 7 shows which block splits are valid). "Whole run" is parse JSON, footprints, `mkPlan` and the seven checks; every stage is in [`records/real_bundles_3_final.md`](records/real_bundles_3_final.md).
The minified rows were measured with a declarator list as one statement; `dump.mjs` now writes these bundles as 447 and 1,072 statements.

| Input             | Statements | Modules | Whole run before        | Whole run now | `ReadsClosed` now | Verdict                          |
| ----------------- | ---------- | ------- | ----------------------- | ------------- | ----------------- | -------------------------------- |
| lodash, first 100 | 100        | 4       | 113 ms                  | 5 ms          | 0 ms              | accepted                         |
| lodash, first 200 | 200        | 8       | 717 ms                  | 8 ms          | 1 ms              | accepted                         |
| lodash, first 300 | 300        | 12      | 2,561 ms                | 19 ms         | 1 ms              | accepted                         |
| semver            | 59         | 4       | 153 ms                  | 19 ms         | 1 ms              | accepted                         |
| lodash            | 670        | 6       | 53,927 ms               | 42 ms         | 6 ms              | accepted                         |
| acorn             | 447        | 5       | 4,176 ms                | 63 ms         | 2 ms              | accepted                         |
| acorn, minified   | 374        | 5       | 5,394 ms                | 67 ms         | 4 ms              | rejected: OrderOk front-end rule |
| zod, minified     | 805        | 8       | did not finish in 240 s | 320 ms        | 61 ms             | rejected: OrderOk front-end rule |
| zod               | 1,072      | 8       | not run                 | 218 ms        | 19 ms             | rejected: OrderOk front-end rule |

**Compiled, K renamed copies of lodash** (timing only; [`lowering/scale.sh replicate`](lowering/scale.sh)). The total after each stage of the work:

| K   | Statements | Modules | Indexed closure, module tables | + shared tables, `Permutes` sets, `mkPlan` indexes | + `Faithful` tables | Final run: `mkPlan` | `ReadsClosed` | `Faithful` | `OrderOk` | all checks |
| --- | ---------- | ------- | ------------------------------ | -------------------------------------------------- | ------------------- | ------------------- | ------------- | ---------- | --------- | ---------- |
| 1   | 670        | 6       | 129 ms                         | 82 ms                                              | 59 ms               | 3 ms                | 5 ms          | 2 ms       | 11 ms     | 20 ms      |
| 2   | 1,340      | 12      | 486 ms                         | 230 ms                                             | 115 ms              | 9 ms                | 12 ms         | 6 ms       | 33 ms     | 54 ms      |
| 4   | 2,680      | 24      | 2,108 ms                       | 1,009 ms                                           | 276 ms              | 30 ms               | 25 ms         | 20 ms      | 150 ms    | 205 ms     |
| 8   | 5,360      | 48      | 10,640 ms                      | 5,268 ms                                           | 841 ms              | 79 ms               | 51 ms         | 76 ms      | 433 ms    | 594 ms     |
| 16  | 10,720     | 96      | 57,772 ms                      | 35,248 ms                                          | 2,853 ms            | 264 ms              | 112 ms        | 312 ms     | 1,736 ms  | 2,290 ms   |
| 32  | 21,440     | 192     | did not finish (cap 300 s)     | 252,080 ms                                         | 10,482 ms           | 1,026 ms            | 297 ms        | 1,293 ms   | 7,014 ms  | 9,107 ms   |
| 64  | 42,880     | 384     | –                              | –                                                  | 42,036 ms           | 3,570 ms            | 458 ms        | 5,165 ms   | 26,390 ms | 33,955 ms  |

- **Still quadratic: `OrderOk`.** It checks every pair of the execution order: 7 of the 10 s at 21,440 statements, 28 of 42 s at 42,880.
  The fix is known and not done. `OrderOk` holds iff every conflicting pair `a < b` has `pos a < pos b`. The conflicting pairs are few:
  the effect statements form a chain, each name a statement reads points at its declaration, and two declarations of one name conflict.
  That is one lookup per read instead of one per pair. The proof must show that this implies the `Pairwise` condition `exec_reorder` consumes.
- **`Faithful`** still compares every selector with every statement (5 s at 42,880); an index from names to declaring statements would remove that.

**Kernel.** The kernel is a different cost model, and these changes help it much less. Synthetic bundles ([`scaling/gen.py`](scaling/gen.py)),
`decide +kernel`, one process each ([`scaling/nat_names.sh`](scaling/nat_names.sh), [`scaling/peak.py`](scaling/peak.py)):

| Statements | Before the indexed checks | Names as strings                                  | Names as numbers                                  |
| ---------- | ------------------------- | ------------------------------------------------- | ------------------------------------------------- |
| 12         | 0.7 s                     | 1.2 s, peak 650 MB                                | 0.8 s, peak 612 MB                                |
| 24         | 1.4 s                     | 2.9 s, peak 866 MB                                | 1.9 s, peak 746 MB                                |
| 48         | 5.0 s                     | 7.4 s, peak 1459 MB                               | 4.4 s, peak 1117 MB                               |
| 96         | 19.7 s                    | 22.7 s, peak 3416 MB                              | 12.8 s, peak 2400 MB                              |
| 144        | 51 s                      | 44.9 s, peak 6158 MB                              | 26.8 s, peak 4267 MB                              |
| 192        | killed after 88 s         | 69.2 s, peak 9464 MB                              | 41.6 s, peak 6557 MB                              |
| 288        | –                         | 66.6 s, peak 12031 MB, killed at the memory limit | 63.6 s, peak 12027 MB, killed at the memory limit |

- **Memory, not time, is the limit.** The kernel keeps what it evaluates. 144 statements need 6 GB, and 192 need 9 GB. At 288 the 12 GB limit I set killed it.
  `decide +kernel` on the lodash split (670 statements) reached the 12 GB limit after 62 s and was killed ([`records/`](records)).
- **Numeric names help 1.6–1.8×, not 10×.** `Name := Nat` makes comparisons cheap in the kernel (it has big-number support; it has none for strings).
  I had guessed it was most of the cost. It is not. [`nat_names.sh`](scaling/nat_names.sh) builds a scratch copy of the core Lean files with `Name := Nat` and times the same bundles.
- **For a big split, use `native_decide`** (2.3 s for lodash; trusts Lean's compiler, § 8), or a kernel certificate that carries the derived tables as data and lets the kernel
  _check_ them (closed, and least by derivation order) instead of computing them. That is how CompCert's validators work (§ 9). I have not built it.

The semantic theorem does not depend on bundle size.

## 6. Lower the AST into Lean

The idea: acorn's ESTree goes into Lean as data, and Lean computes the footprints. Then the thing analyzed and the thing emitted are the
same nodes, and the analysis that was my weakest trusted part (including the callback soundness hole in § 1) becomes Lean code that can be tested,
and later proved.

[`dump.mjs`](lowering/dump.mjs) writes ESTree as JSON, with an independent oracle (`eslint-scope`) for each statement's free variables.
A top-level `var`, `let` or `const` list of several declarators becomes one statement per declarator, so a spec can place them apart.
[`Lower.lean`](../lean/Experiments/Lower.lean) (306 lines) reads it as `Lean.Json`, which is untyped, so **every** node type of JavaScript is representable.
It scans what each statement declares, mentions and assigns. The scan is total and sound by construction:

- a name is local only if a _recognized_ binder introduces it;
- any node type it does not recognize goes through a fallback: every identifier below is a reference. That can add dependencies, never drop one.

Whether a statement touches the world, and which names it may change, is not decided here: [`Effects.lean`](../lean/Experiments/Effects.lean) does that (§ 13), and the model uses no other classification.

Adding precision is the only way to get something wrong. So I compared against the oracle on real code
([`compare.mjs`](lowering/compare.mjs)), on four packages bundled by esbuild (readable and minified) and on a hand-written scoping stress file:

| Bundle                                                      | Statements | Inert | Effect | Hoisted functions | Missed free names | Mis-classified eager | Exactly the oracle's set |
| ----------------------------------------------------------- | ---------- | ----- | ------ | ----------------- | ----------------- | -------------------- | ------------------------ |
| lodash (subset)                                             | 670        | 636   | 34     | 167               | 0                 | 0                    | 99.3%                    |
| acorn                                                       | 447        | 155   | 292    | 42                | 0                 | 0                    | 99.8%                    |
| semver (CJS)                                                | 59         | 57    | 2      | 0                 | 0                 | 0                    | 100%                     |
| zod                                                         | 1,072      | 873   | 199    | 560               | 0                 | 0                    | 100%                     |
| acorn, minified                                             | 447        | 155   | 292    | 42                | 0                 | 0                    | 99.8%                    |
| zod, minified                                               | 1,072      | 873   | 199    | 560               | 0                 | 0                    | 100%                     |
| scoping stress file ([`torture.mjs`](lowering/torture.mjs)) | 28         | 28    | 0      | 10                | 0                 | 0                    | 100%                     |
| the 12-statement example                                    | 12         | 10    | 2      | 4                 | 0                 | 0                    | 100%                     |

That is 3,767 real statements with no missed free variable. A minified bundle has the statements of the readable one: its declarator lists are split. "Inert" and "effect" are the verdicts of `Effects.lean`; the oracle has none. On the example, `extract.mjs` (the first prototype, which counts every call as an effect) and the Lean analysis give identical footprints for 10 of 12 statements: the other two are the calls `register(…)`, where Lean sees that the callee only writes `registry`.
No real bundle contained a node type the analysis does not recognize, so the fallback was exercised on a synthetic unknown node: it recorded the type and still collected every identifier below it.

**Does a zero mean anything?** I injected bugs into `Lower.lean` and scored them against the oracle.
All six _unsound_ mutants were caught: skipping a member expression's object, `extends`, computed keys (caught by the real bundles),
and ignoring pattern defaults, letting parameters see body-level names, binding a catch parameter over the whole `try` (caught only by the stress file).
One mutant, forgetting a catch parameter is local, is on the safe side and is correctly not flagged. The oracle is only as strong as its corpus.

**What real bundles look like**, from the same runs. `Effects.lean` makes 1,018 of 3,767 statements effects (27%); the allow-list of the first version made 1,272 (37%):

- acorn: 292 (minified too), of which 272 are `proto.method = function …`: a store into the prototype of an exported class, and the names a bundle exports start out held by the world;
- semver: 2 (a call and a member read);
- zod: 199 (minified too), of which 183 are `var X = factory(…)` calls;
- lodash: 34: 17 calls, 9 logical expressions, 4 conditionals, 3 member reads and an `if`.

The bundles also contain statements that assign to a top-level name (lodash 1, acorn 2, zod 7), which the model cannot place in another module.

**End to end in one program.** [`LowerSplit.lean`](../lean/Experiments/LowerSplit.lean) (198 lines) takes the JSON and a spec, computes footprints in Lean,
plans, checks, writes the modules, and with `--lean` writes a Lean file for the kernel. On the example the modules are identical to the ones produced through `extract.mjs`
except the order of names inside two `export { … }` lists (Lean's JSON parser sorts object keys, so the spec's file order is lost).
The generated certificate is accepted by the kernel; its axioms are `propext`, `Classical.choice` and `Quot.sound`. Whole run, compiled: 2 ms.

**What lowering does not buy.**

- The parser. acorn is still trusted.
- A proof about the analysis. `Lower.lean` is `partial` over untyped JSON, so the kernel cannot run it and Lean cannot prove things about it.
  The principled endpoint is a typed ESTree with structural recursion: then `decide` can run the analysis inside the kernel and the certificate covers parse output → footprints → plan.
  That is a bigger build; section 2 is the template.
- A semantics. The frame assumption is unchanged. Only its _footprint side_ moved into Lean.
- Imports from outside modules, top-level reassignment, and `import.meta`: rejected or guarded, not modeled. `lowersplit` keeps a bundle's `import` statements in the entry and writes them first; that this keeps the order of evaluation is argued in the [README](../README.md#what-is-not-proved), not proved.

Run it: `./run.sh --lowering` (installs esbuild, lodash-es, acorn, zod, semver, eslint-scope into `experiments/lowering/`).

## 7. Real splits: halves, cuts, blocks, random placements

A bundle cannot be cut anywhere. This section runs the checker on 2,892 splits of six real bundles and runs each result in Node.
Run it with [`splits/run_all.sh`](splits/run_all.sh) (about 10 minutes, most of it the footprint analysis of `zod`, which runs once per bundle; after `./run.sh --lowering`).

For every split, `lowersplit --force` checks it and writes the modules even when a check rejects it. Then [`splits/smoke.mjs`](splits/smoke.mjs) calls the
entry module's exported API and prints the result as JSON. The split behaves like the bundle when that line equals the bundle's own.
The calls: lodash `chunk`, `get`, `set`, `merge`, `cloneDeep`, `groupBy`, `sortBy`, `uniqBy`; acorn `parse` of ES2022 programs, tokens and four syntax errors;
semver `valid`, `compare`, `satisfies`, `inc`, `coerce`, `Range`; zod `parse` and the issues for bad input.
One JSON line per split is in [`splits/results/`](splits/results); [`results/summary.md`](splits/results/summary.md) has the per-bundle table.

| Strategy                                                                           | Splits    | Accepted  | Accepted, Node differs | Rejected, Node breaks | Rejected, Node fine | Failing checks (splits)                                  |
| ---------------------------------------------------------------------------------- | --------- | --------- | ---------------------- | --------------------- | ------------------- | -------------------------------------------------------- |
| halves: `[0, n/2)` and `[n/2, n)`, the second module is the entry                  | 6         | 6         | 0                      | 0                     | 0                   | –                                                        |
| every single cut `[0, c)` and `[c, n)` (every 4th for zod, every cut for the rest) | 2,153     | 2,136     | 0                      | 14                    | 3                   | `AssignsHome` 17                                         |
| 2–8 contiguous blocks                                                              | 42        | 26        | 0                      | 16                    | 0                   | `OrderOk` 16, `AssignsHome` 4                            |
| each statement to a random one of 2, 3, 5 or 8 modules                             | 480       | 2         | 0                      | 476                   | 2                   | `OrderOk` 478, `AssignsHome` 378                         |
| single cuts with the _first_ module as the entry                                   | 211       | 0         | 0                      | 211                   | 0                   | `ExportOutsideEntry` 211, `OrderOk` 211, `AssignsHome` 2 |
| **all**                                                                            | **2,892** | **2,170** | **0**                  | **717**               | **5**               |                                                          |

- **No accepted split behaved differently.** That is the soundness claim, tested on 2,170 accepted splits. It holds relative to the footprints and the front-end rules, and debundle's corpus later showed those incomplete (§ 11): a `var` nested in a top-level block, and an entry export name that collides with a glue export.
- **Halving is mostly fine, which I did not expect.** With the entry in the second half, the entry imports the first half. If the first half refers back to the second
  (a function calling a helper declared later), it imports it too, and the cycle is cut at the entry. The order of the bodies stays the bundle's order.
  99.2% of the single cuts are accepted.
- **A cut can still be invalid.** All 17 rejected single cuts fail `AssignsHome`: a statement assigns a top-level binding that now lives on the other side
  (lodash cut at 422: statement 422 assigns `getTag`, declared by statement 421; acorn cut at 41: `warnedAboutEcmaVersion`). ES imports are read-only, so the assignment would throw.
  Three of the 17 run fine in my smoke test, because the assignment sits in a path the test does not take.
- **Three or more modules break the order.** A forward reference from block 0 to block 1 makes block 1 run first, because an import runs before the importer.
  Lodash and semver survive all 7 block splits. Acorn survives 5 of 7 and zod 1 of 7: the `proto.method = function …` stores of acorn and the `var X = factory(…)` calls of zod are effects (§ 13), and effects keep their order.
- **Arbitrary placement almost never works:** 2 of 480 accepted (two semver placements, and Node agrees), and 476 break in Node. The other 2 are semver splits that reorder effects which do not show in the smoke test.
- **Putting the entry in the wrong half loses the API.** The `export { … }` statement is a statement like any other; I had not checked that it stays in the entry module.
  `lowersplit` now rejects it (`ExportOutsideEntry`). Before this experiment the checker would have accepted a split with the exports in a non-entry module.
- **The checker is conservative in 5 of 722 rejections** (0.7%): 3 reassignments that are not on the executed path and 2 random placements.

## 8. How a proof is produced

**The shape.** `lowersplit --lean` writes a Lean file for a split. The example's is [`Generated.lean`](../lean/Experiments/Generated.lean): data, then two lines of proof.

```lean
def bundle : Bundle := [
  { src := "const PI = 3.14159;", defs := ["PI"] },
  { src := "const area = (r) => PI * square(r);", defs := ["area"], lazy := ["PI", "square"] }, …
]
def spec : Spec := { entry := "main.mjs", modules := [("utils.mjs", [.name "PI", …]), …] }
def plan : Plan := mkPlan bundle spec

theorem plan_valid : Valid bundle spec plan := by decide +kernel
theorem plan_correct : Correct bundle plan := split_correct plan_valid
```

No proof term is searched for. `Valid` is decidable (§ 5), so `decide +kernel` makes the kernel _evaluate_ the decision procedure and check that it returns `true`.
`split_correct` was proved once, for every bundle, spec and plan ([`Correct.lean`](../lean/EsmSplit/Correct.lean)). Lean's reply is "accepted" or an error.
For a bad split `decide` says the proposition is false. `¬ OrderOk …` is itself a kernel-checked theorem ([`Controls.lean`](../lean/EsmSplit/Controls.lean)),
and the compiled report (`lowersplit`) names the failing check.

**Strategies.** A strategy says how Lean runs that computation.

| Strategy         | Runs it in     | Trusts                                  | Seen here                                                      |
| ---------------- | -------------- | --------------------------------------- | -------------------------------------------------------------- |
| `decide`         | the elaborator | the kernel                              | `maximum recursion depth` at 59 statements (5.5 s)             |
| `decide +kernel` | the kernel     | the kernel                              | 17 s at 59 statements; 23 s at 96 synthetic ones; 12 GB at 670 |
| `native_decide`  | compiled code  | the kernel, Lean's compiler and runtime | 2.3 s at 670                                                   |

`native_decide` adds one axiom per proof, `plan_valid._native.native_decide.ax_1_1`: "the compiled evaluation returned `true`".
(The [Lean reference](https://lean-lang.org/doc/reference/4.27.0-rc1/ValidatingProofs/) lists the trust each one adds. A [February 2026 Zulip post](<https://leanprover-community.github.io/archive/stream/270676-lean4/topic/Heads.20up.3A.20changes.20to.20native_decide.20(and.20bv_decide).html>)
announces that the axiom machinery behind `native_decide` is being restructured, so the names printed by `#print axioms` may change.)

[`Ladder.lean`](../lean/Experiments/Ladder.lean) writes the ladder inside Lean:

```lean
macro "decide_split" : tactic => `(tactic| first | decide | decide +kernel | native_decide)
theorem ladder_valid : Valid bundle spec plan := by decide_split
```

[`proof_search/prove.mjs`](proof_search/prove.mjs) does it from outside for a generated file, with a time limit per strategy. It rewrites the tactic after `by`, runs
`lake env lean --json`, and stops at the first strategy Lean accepts:

```text
$ node prove.mjs Semver.lean --ladder "decide;decide +kernel;native_decide"   # 59 statements, 4 modules
decide             failed: maximum recursion depth has been reached         5.5 s
decide +kernel     proved                                                  16.6 s  [propext, Classical.choice, Quot.sound]

$ node prove.mjs Lodash.lean --ladder "decide;native_decide"                # 670 statements, 6 modules
decide             failed: maximum recursion depth has been reached         1.8 s
native_decide      proved                                                   2.3 s  [propext, Classical.choice, Quot.sound, plan_valid._native.native_decide.ax_1_1]

$ node prove.mjs AcornMin.lean --ladder "native_decide"                     # a split that OrderOk rejects
native_decide      failed: Tactic `native_decide` evaluated that the proposition … is false   1.4 s
no strategy proved it
```

(`decide +kernel` is left out of the lodash ladder: it reaches 12 GB in 62 s, § 5. Raw output: [`records/ladder_*.txt`](records).)

**Custom proofs for the residue.** When the footprints cannot justify a pair, the file lists the pair and a hand-written lemma proves it ([`TwoTierExample.lean`](../lean/Experiments/TwoTierExample.lean), § 3).
The custom part there is two lines, `stmt2_is_identity`: `rcases fuel with _ | _ | fuel <;> simp [cstep, prog2, eval, Expr.subst]`.

**What this is not.** It is not proof search. The strategy space is small enough to enumerate, so the ladder does.
For a statement that is not decidable, the loop is the usual one: write a tactic script, run `lake env lean --json`, read the goals and errors, try `simp`, `omega`, `grind`, `exact?`.
§ 4 did that by hand. I did not build an automatic loop.

## 9. What already exists, and what I would reuse

I found no formalization of JavaScript or of ESTree in Lean. A search for one returned only
[`autoform`](https://github.com/mbhatt1/autoform), which maps source code onto a generic interpreter through Joern's code property graph.
That is a different approach, not a semantics of JavaScript.

Elsewhere (from my earlier searches, not rechecked): JSCert (Coq, ES5, with an executable reference interpreter), ESMeta (an executable IR
extracted from the specification text, not a proof-assistant model), KJS, λJS/S5 and JaVerT (all ES5-era). ES5 has no modules, so none of them covers the
feature this experiment depends on. A port would cost months and would still need a theory of footprints on top.
The footprint abstraction is what avoids it: the proof never looks inside a statement.

Used here:

| Tool                                                                             | For                                                                                                           |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Node (V8)                                                                        | the oracle for module semantics: the fuzzer (§ README), the split matrix (§ 7), the module-order check (§ 10) |
| `eslint-scope`                                                                   | an independent oracle for scoping and free variables ([`compare.mjs`](lowering/compare.mjs), § 6)             |
| acorn, esbuild                                                                   | parser (trusted) and bundle generator                                                                         |
| Lean: `decide +kernel`, `native_decide`, `Std.TreeMap`, `grind`, `simp`, `omega` | the checks and the proofs                                                                                     |

Worth reusing, not done:

- **test262**, `language/module-code` and `language/import`: a much larger oracle for the module evaluation order and for cycles than my 500 random graphs.
- **`eslint-visitor-keys`** ([npm](https://npmjs.com/package/eslint-visitor-keys)) or `@babel/types`: tables of the child keys of every ESTree node.
  They would generate a _typed_ ESTree for Lean. Then the analysis (`Lower.lean`, `partial` over untyped `Json` today) can use structural recursion,
  and the kernel certificate can cover parse output → footprints → plan.
- **CompCert's validation architecture**: an untrusted transformation plus a verified checker
  ([Tristan and Leroy, POPL 2008](https://xavierleroy.org/bibrefs/Tristan-Leroy-scheduling.html)). That is the structure here: `mkPlan` is untrusted, `Valid` is checked.
  Their symbolic-execution validator was exponential until [Six et al.](https://hal.archives-ouvertes.fr/hal-03200774) added verified hash-consing.
  The speed-ups in § 5 are the same kind of change: share work through an index, prove the fast version equal to the plain statement.

## 10. Does the model's module order match Node?

`Plan.visit` models how an engine orders module bodies: depth-first over `import`s in source order, a module runs after the modules it imports,
an entered module is skipped (that is how cycles end). `OrderOk` rests on it, and the proof does not cover it.

[`es_order/check.mjs`](es_order/check.mjs) builds random import graphs (2–8 modules, 0–3 imports each, cycles and unreachable modules included).
Every module prints its name. The order Node prints must equal `Plan.evalOrder`, which the `evalorder` executable ([`EvalOrder.lean`](../lean/Experiments/EvalOrder.lean)) prints.

| Run                                                                         | Graphs | With a cycle | Mismatches |
| --------------------------------------------------------------------------- | ------ | ------------ | ---------- |
| `node check.mjs 500 1`                                                      | 500    | 262          | 0          |
| `node check.mjs 100 1 --control` (Node sees each module's imports reversed) | 100    | 59           | 24         |

The control shows that the test can fail.

## 11. debundle's e2e cases

debundle does the same job without Lean. Its corpus ([`e2e/cases`](../../../e2e/cases)) has 207 entries: a bundle, a spec, and the verdict debundle must give.
[`debundle_cases/run_cases.mjs`](debundle_cases/run_cases.mjs) translates each spec, runs `lowersplit --force`, runs the original and the emitted entry in Node, and compares.
Run it with `./run.sh --debundle-cases` (about 10 s). Details, causes and the plan: [`debundle_cases/README.md`](debundle_cases/README.md), [`PLAN.md`](../PLAN.md).

| Outcome                                                                                    | Entries |
| ------------------------------------------------------------------------------------------ | ------- |
| both accept, same stdout                                                                   | 69      |
| both reject (`atomic_conflict` 8, `cycle` 7)                                               | 15      |
| I reject, debundle accepts (6 would run, 1 does not load)                                  | 7       |
| I accept and the output differs                                                            | 0       |
| refusal tests (debundle's error text)                                                      | 7       |
| not run: the adapter cannot translate the spec (selectors, chunks, imports, comma lists)   | 103     |
| refused by `lowersplit`: a direct `eval` or a top-level `await`; debundle rejects them too | 6       |

- **It found two soundness gaps in my front end,** both fixed: a `var` nested in a top-level block (4 entries accepted, then they fail with `ReferenceError` or `SyntaxError`), and an entry export name that collides with a glue export (1 entry accepted, then `SyntaxError`; the glue now exports under a fresh name, and `ExportsUnique` checks it).
- **14 of the 15 expected rejections fail `OrderOk`,** and 14 of the 15 break when written anyway. Where we disagree, debundle models more: purity (3), repeated `var` (3), and an import for evaluation order (1).

## 12. Translation validation: is this tree the bundle, renamed and reshuffled?

The experiments so far start from a spec and a splitter. [`translation/`](translation/README.md) starts from the other end: a bundle and _any_ tree of modules, however it was made.
A front end annotates every identifier with what it means (`eslint-scope`). A pairing tool matches output units to bundle units. Lean checks that each pair is equal up to renaming,
that every reference resolves, through the imports and exports, to the right declaration, that the entry exports what the bundle exports, that a unit assigning a module-scope name sits with its declaration (ES imports are read-only), and that the plan the tree describes is `Sound`.
Three rewrites besides renaming are accepted as listed premises (an outside import swapped for a member chain, a moved dynamic-import path, a worker URL made relative to the module). Anything else is a rejection.

Run it with `./run.sh --translation`. The fixtures' tampered trees (a changed literal, a reference to the wrong declaration, a parameter that captures a module-scope name, a missing import, an assignment moved away from its declaration, …) and a mutation test are all rejected.
A tree with renamed locals is accepted. Two effects run in the other order give obligations instead of a verdict of "invalid".

[`translation/fuzz.mjs`](translation/README.md#fuzzer) asks the converse: does the checker ever accept a tree that behaves differently from its bundle? It generates random bundles and random splits of them, runs both in Node and compares with the verdict. It found two holes that the fixtures missed (a pure read of a reassigned variable moving across the assignment, and a unit using `import.meta` moving to another module), and both are fixed with a fixture.

On a large production bundle that is not in this repository, the checker accepts the translation, with every unit equal up to renaming and every link resolved, and reports only effect-order obligations.
What justifies those is the next step: [`translation/PLAN.md`](translation/PLAN.md).

## 13. Footprints by abstract interpretation

`Effects.lean` decides what each unit touches by running the bundle's top level once over abstract values. [`effects/`](effects/README.md) has the rules, the premises and the tests. `lower`, `translate` and `lowersplit` use it and no other classification: there is no allow-list of inert shapes and no recognizer for calls it can clear. The model has `Stmt.deps` (names a statement reads through functions it calls). A hoisted declaration does not conflict with a unit that binds the same name, since running it changes nothing (`step_hoisted`); that halved the obligations on the large bundle.

Evidence, none of it a proof:

- 201 small bundles in [`effects/cases.mjs`](effects/cases.mjs) and [`effects/cases_general.mjs`](effects/cases_general.mjs): the classification of every unit of the first 110, and 182 reordered programs. Node runs 180 of them differently from the original, and `translate` must not accept those; it must accept the other two, which Node runs the same. `node run.mjs --permutations N` adds N random permutations of every case's units and fails on a wrong accept; CI runs 3 per case.
- The translation fuzzer ([`translation/fuzz.mjs`](translation/README.md#fuzzer)) generates random bundles and random splits of them, runs both in Node and compares with the verdict. CI runs the random catalog, the AST corpus, the callback corpus and the `--leaks` templates on fixed seeds.
- The suites that existed before ran on it: the fixtures, the emission tests, the Shift grammar fuzzers, the real-bundle splits (§ 7: no accepted split behaved differently) and debundle's e2e cases (§ 11).
- 71 mutants of the checker, run by [`translation/fuzz_mutate.py`](translation/fuzz_mutate.py): each removes one rule of the analysis or of the model, and the cases, the fuzzer or a proof that stops building must catch it. In the last run 66 were caught by a case or the fuzzer, 4 by a proof, and 1 survives (`no Hygienic`, redundant with the link check).

**What reading the code found that the fuzzer did not.** The first version had passed seven fuzz seeds. Reading every place where the interpreter does not run code, and asking what that code could still do, gave eleven programs it accepted wrongly (Node prints something else): a generator body, which runs when the iterator is advanced, in a later unit; a call deeper than the analysis follows, or through more candidates than it follows, whose callee was not marked as run; the same for a constructor; an `await`, whose continuation runs after the program in the order the awaits were reached; a read of state that a module outside the tree keeps; a getter, a setter and an inherited accessor; and an object assigned to a variable that the world's handler reads. The fuzzer's catalog had been written by the hand that wrote the analysis. The rules that came out of it: a give-up must escape what the skipped code can reach, and a rule that assumes code runs now must ask whether it runs later. Each is a case with the reordered program, and a mutant that removes it.

**What running the older suites on it found.** A bundle that defines a getter on `Object.prototype` and later reads `{}.missing` was accepted (`fixtures/inherited_getter`): the analysis assumed the intrinsics unpatched, and the bundle patched one itself. A patch the bundle makes by name is seen now. A change to a static member (`Symbol.dispose ??= …`) makes the units that read it effects. A change to a prototype, to an unknown key or to a conversion hook makes every unit that is not a function declaration an effect. The first version treated every patch that way, and the one polyfill in the large bundle turned 2,059 effect units into 6,571. A value that may also be another object does not count as a patch, or every library that walks a prototype chain would. `zod`'s constructor helper overflowed the stack, because a bound function stored where its target came from calls itself. No case covered the rule that exported names start out held by the world. Fixtures and cases that expected a refusal where Node prints the same were limits of the earlier recognizers: they are accepted now.

**An independent attempt to break the patch rules.** A fresh agent with no stake in the analysis tried to break it with about 400 small bundles and found 24 minimal programs that were accepted wrongly: a bare assignment to an intrinsic global; `Function` and `Reflect`, which were not intrinsic globals; the prototype chain of an intrinsic and of a primitive; `__defineGetter__`; the code of a replaced static member; `Error.stackTraceLimit`, which `new Error` reads; two paths to one object (`TypeError` and `Error`); a getter that `Object.assign` or `Object.prototype.toString` runs (not a patch: any getter that writes state); and patches in code the analysis does not run. 22 of the 24 are refused now, each with a case, and the text of code that is not run is read for the names of intrinsics. The fix also showed a regression on minified `zod`: a value that only may be a primitive got the prototypes of the five wrappers, and a guarded `Object.defineProperty(Object.getPrototypeOf(e), …)` became a patch of all of them (117 effect units became 245). The wrappers' prototypes apply to a value that is a primitive for certain now. Five programs stay accepted wrongly and are kept as the limits of P1 in [`effects/debug/p1_known_gaps`](effects/debug/p1_known_gaps/README.md).

**A second independent attempt, on the general rules.** Another fresh agent wrote 63 small programs, in 15 causes, that were accepted wrongly. A callback got the wrong parameters, `reduce` did not return its initial value, `new Map(map)` and `Object.entries` had the wrong shape, a spread was one argument position, a computed key was not converted to a string, the object a class constructor returns was dropped, an async function that returns a thenable did not call its `then`, a chain of 11 prototypes was cut short, `arguments` in an arrow function was its own, and the iterator of a `Map` was a copy with no cursor. Standard functions also call methods through a protocol: `test` calls `exec`, `replace` calls the `Symbol.replace` of its pattern, `instanceof` calls `Symbol.hasInstance`, and the error constructors read `cause`. 58 of the 63 are refused now, each a case in [`effects/cases_general.mjs`](effects/cases_general.mjs). Five stay accepted wrongly and are kept as the limits of the general rules in [`effects/debug/general_known_gaps`](effects/debug/general_known_gaps/README.md).

**A third independent attempt, on the core language.** A third fresh agent went after the interpreter's handling of the language itself, not the standard library: about 400 programs, 29 minimal wrong accepts in 11 causes. The call memo replayed a call that had grown the heap, and dropped what the call threw. A named read of an array or a collection missed what was stored under a computed key. A `Symbol.iterator` or `toString` installed on an array by `Object.assign`, or found on a replaced prototype, never ran. The fields of a derived class ran before `super()`, and a static field before the methods defined after it. The object a parent constructor returns was not `this`, and the object `super(args)` builds in `Map` or `Set` was not held by the instance. Parameter defaults saw the declarations of the body, and a function declared in a `switch` case was never bound. Two closures made at one site shared one `this`. A standard function used as a getter or a `valueOf` was never called. All 29 are refused now, each a case in [`effects/cases_general.mjs`](effects/cases_general.mjs).

**On the large production bundle** (private; numbers only; the committed readable tree, 9,707 units):

| footprints                                              | effect units | obligations | analysis | whole run |
| ------------------------------------------------------- | -----------: | ----------: | -------: | --------: |
| no premises                                             |        2,059 |     865,930 |    114 s |     324 s |
| premise P3                                              |        1,583 |     669,532 |    114 s |     288 s |
| P3 and 40 imports assumed to only allocate (unreviewed) |          730 |     344,781 |    132 s |     219 s |

The obligations are effect-effect pairs the tree runs in the opposite order from the bundle: 1,583 effect units make 1.25 million pairs, and 1,527 of the units run unknown code (`EFFECTS_WHY=1 lower …` lists the first reason for each). One vendor import is called, directly or through a local wrapper, by 511 units; 451 read or assign a property of a value the world returned, which may run a getter or setter the world made (premise P3 does not cover those); 527 units apply a decorator helper to a class or a prototype (`f([decorators], C.prototype, "member", …)`); a few dozen other imports account for most of the rest. What would clear them is a reviewed table of vendor functions that only allocate, keyed by package and export, and an audited claim that decorators on different prototypes commute ([`translation/PLAN.md`](translation/PLAN.md)). The table in the last row is keyed by minified names and was written to see what a table could do: it is an illustration, not a result.

## 14. An executable model of a core of JavaScript

[`CoreJS`](../lean/Experiments/CoreJS/) is the model that footprint rules are to be proved against ([`PLAN.md`](../PLAN.md) § Direction). It is an interpreter: total functions by recursion on a fuel bound, no `partial`, no `sorry`. A program has one right answer, so the model is tested against Node.

| File                                                    | Holds                                                                                                                                                                       | Lines |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----: |
| [`Syntax.lean`](../lean/Experiments/CoreJS/Syntax.lean) | the typed syntax tree, opaque statements, modules, which names a scope declares, and the free names of a piece of code                                                      |   306 |
| [`Number.lean`](../lean/Experiments/CoreJS/Number.lean) | `Number::toString`, `StringToNumber`, `%`, `ToInt32`, exactly, on the bits of a `Float`                                                                                     |   170 |
| [`Value.lean`](../lean/Experiments/CoreJS/Value.lean)   | values, the heap, bindings, conversions, operators, property operations, the world, its table of functions and its conversation                                             |   866 |
| [`Eval.lean`](../lean/Experiments/CoreJS/Eval.lean)     | the interpreter: twelve mutually recursive functions, and how a program is linked and run                                                                                   |   621 |
| [`Frame.lean`](../lean/Experiments/CoreJS/Frame.lean)   | the proof of the allocation rule below                                                                                                                                      |   551 |
| [`Fuel.lean`](../lean/Experiments/CoreJS/Fuel.lean)     | more fuel never changes a run that finished                                                                                                                                 |   307 |
| [`Scope.lean`](../lean/Experiments/CoreJS/Scope.lean)   | code sees a scope only through its free names                                                                                                                               |   607 |
| [`Split.lean`](../lean/Experiments/CoreJS/Split.lean)   | a split that keeps the order ends as the bundle does                                                                                                                        |   815 |
| [`Decode.lean`](../lean/Experiments/CoreJS/Decode.lean) | reads a program; [`Run.lean`](../lean/Experiments/CoreJS/Run.lean) is the `corejs` executable and its world                                                                 |   651 |
| [`model/to_core.mjs`](model/to_core.mjs)                | the front end: acorn's trees to the model's syntax, or a refusal with the node type                                                                                         |   803 |
| [`model/differential.mjs`](model/differential.mjs)      | the test against Node, with [`model/cases.mjs`](model/cases.mjs), [`model/module_cases.mjs`](model/module_cases.mjs) and [`model/opaque_cases.mjs`](model/opaque_cases.mjs) | 1,589 |

**What it covers.** Strict-mode module code. Numbers, strings, booleans, `undefined`, `null`. `var`, `let`, `const` with hoisting and the temporal dead zone. Functions, arrows and methods as closures; calls with `this`, method calls, `new`. Object literals with data properties, getters, setters and `__proto__`; array literals (read only). Classes without `extends`: methods, accessors, instance and static fields. Property read, write, `delete` and `in` along the prototype chain, with getters and setters run on the receiver. `Object.defineProperty` with the specification's validation, `Object.create`, `Object.getPrototypeOf`, `Object.setPrototypeOf`. `throw`, `try`, `catch`, `finally`; `if`, `for`, `while`, `break`, `continue`, `return`. The operators except `**` and `instanceof`; `typeof`; template literals; `console.log` of primitives.

**Modules.** A program is a set of modules and an entry. A module has `import { a as b, c } from "./m.mjs"`, `import d from`, bare `import "./m.mjs"`, `export { a as b, c }`, `export` before a declaration, and `export default`. That is what `Module.render` writes and what an entry chunk has, and a little more.

- _Linking comes first._ Every binding of every module the entry reaches is made before any statement runs. An imported name is the exporter's binding itself, so the importer sees every later assignment. The declared functions of all modules get their values then; `let`, `const` and class names are uninitialized until their declaration runs, and a `var` is `undefined`.
- _Importing a name the module does not export_ ends the run with a `SyntaxError` before anything is printed, as in Node.
- _An import cannot be assigned._ Which declaration a name means is decided by the text, so the front end marks an assignment whose target is an imported name (`Target.frozen`), and the model throws a `TypeError` there, after the right side ran.
- _The order of evaluation_ is the specification's for modules without a top-level `await`: depth-first over the `import`s in source order, a module after the modules it imports, a module that was entered already skipped. It does not depend on what the modules do, so the model computes it first (`Program.evalOrder`) and then runs the bodies. A throw ends the run.
- A program of one module is the case `Program.single`.

**Unknown code is an outcome, not a guess.** A run ends with a value, a thrown value, out of fuel, or `unknown`: it reached code the model does not define. A value of the host can be stored and passed; converting or comparing it ends the run as `unknown`. So do a call of a host function the world does not answer for (below), the methods of `Object.prototype`, `Function.prototype` and `Array.prototype` (which exist in the model's heap, as host values under their real names), a write to an array, the conversion of an object to a primitive (`valueOf` and `toString` are not run), and `console.log` of an object. What unknown code may change is not modelled yet.

**The world is a parameter.** A host function that works on primitives is in the model without a definition. The model runs in a `World`:

```lean
structure World where
  σ : Type
  init : σ
  step : (fn : String) → (this : Primitive) → (args : List Primitive) → σ → HostResult × σ
```

- A world has a state of its own and one deterministic step: the host function `fn`, called with a receiver and arguments, ends with a primitive, a thrown primitive or a thrown error of some kind, and leaves the next state. The state of the model holds the state of the world (`State W`).
- _Which functions the model asks the world about_ is a table in [`Value.lean`](../lean/Experiments/CoreJS/Value.lean) (`worldFunction`), 88 of them: 32 methods of strings, 5 of numbers, 2 of booleans, which take their receiver; the 35 functions of `Math`, 6 of `Number`, 2 of `String`, and `Boolean`, `Number`, `isFinite`, `isNaN`, `parseFloat`, `parseInt`, which do not. The claim for each is: called with a primitive receiver and primitive arguments, it returns a primitive or throws, and it reads and changes nothing the program can reach. Left out on purpose: `match`, `matchAll` and `split`, which return objects.
- _Anything that would hand the host an object_ ends the run as `unknown` in this stage: an object as receiver or argument, and a host function that is not in the table.
- _How a program gets such a function._ A property that a string, a number or a boolean does not have itself is a member of its prototype, a host value under its real name, when the prototype has that name; otherwise it is looked up on `Object.prototype`, which the program can change, and a getter found there runs with the primitive as `this`. `Math.abs` is a read of a member of a host global. A program that had replaced one of these would have ended as `unknown` at the write.
- _Strings._ A string the world returns with a character outside the Basic Multilingual Plane ends the run as `unknown`, for the reason the front end refuses such a literal.
- **Everything proved about the model holds for every world**: `fuel_mono`, `Ends.unique` and the allocation rule take `W` as a parameter and say nothing about it.

**What the front end refuses**, each with the node type: destructuring, spread, default and rest parameters, `arguments`, generators, `async`, `class … extends`, private names, static blocks, labels, `switch`, `do`, `for`-`in`, `for`-`of`, optional chains, tagged templates, regular expressions, BigInt, `import * as`, `export … from`, an import attribute, an import of anything but a relative path, `import()`, `import.meta`, `**`, `instanceof`, a string outside the Basic Multilingual Plane (the model counts code points, JavaScript code units), a function under a computed key (its `name` would be computed at run time), and any name the program does not declare and the host defines, except the eight globals the model has a rule for (`Math`, `Number`, `String`, `Boolean`, `isFinite`, `isNaN`, `parseFloat`, `parseInt`). The model ends `unknown` for a module that exports a name it imports. Given a scope analysis, the front end makes a refused top-level unit opaque instead (below).

**Addresses, and the identity of a statement.** An object or a binding lives at `(region, index)`. JavaScript cannot observe an address, so any choice of unused ones is faithful.

- Every top-level statement of a program carries an _identity_, a number no other statement of the program has. The statement with identity `i` owns region `i + 1`. Its bindings and, for a function declaration, its function are made there when the program is linked, and what it allocates when it runs goes there too.
- So where a statement allocates depends on its identity and on nothing else: not on its module, and not on what ran before it. Two statements that only allocate end in equal states in either order, with no renaming of addresses.
- A program that is split into modules keeps the identities of its statements. The front end numbers the top-level statements of a file as [`lowering/dump_core.mjs`](lowering/dump_core.mjs) numbers a bundle's (a declaration list counts once for each name), and takes other numbers for a file when told: `lowersplit --plan` writes, for each module, the bundle statements it holds.
- A closure keeps only the bindings of the names its code can mean (`Env.capture` of `Func.closes`, the free names of the function). So two scopes that give those names the same bindings give the same closure, and a function made in a module of a split is the object it is in the bundle.
- Linking visits the statements of all modules in the order of their identities, not module by module. No program code runs while a program is linked, so that order cannot be observed, and it makes the state after linking independent of how the statements are divided into modules.

**Checked against Node** by `./run.sh --model` (about a minute), and in CI by `corejs_differential_test` (72 generated programs and 24 split programs, about 100 seconds). Both sides must print the same lines and end the same way (a thrown primitive by value, an error by its kind).

**The world of the test is Node, as a recording.** Every program runs in Node first, with the 88 functions wrapped; each call that a file of the program makes with a primitive receiver and primitive arguments is written down with how it ended. The model then runs in the world that answers exactly those calls in that order (`replay` in [`Run.lean`](../lean/Experiments/CoreJS/Run.lean)). So the model has to make the calls Node made, the same function with the same receiver and arguments in the same order, and it gets Node's answers. A call that is not the next recorded one fails the test; so does a recorded call the model did not make before it ended, and a listed function that returns an object. `Math.random` needs nothing special: its answers are in the recording.

- 41 hand-written cases for what the generator rarely writes: a getter three prototypes up, a setter that throws, `finally` overriding `return`, `delete` of a missing and of a fixed property, a method with another `this`, closures over a loop variable, `var` hoisting, the dead zone, descriptor validation, a getter put on `Object.prototype`, class fields, number printing; and for the world: methods of strings and numbers, `Math`, the parsing functions, a host function that throws, a property of a string found on `Object.prototype`, a host function stored and called later. Eleven of them must end `unknown`, among them a host function that is handed an object and one that returns a string outside the Basic Multilingual Plane.
- 19 hand-written programs of several modules: a live binding read after the exporter assigned it, a function called before the body of its module ran, the dead zone and `var` across modules, bare imports, a cycle of three, export and import under other names, a module imported twice, a missing export, an assigned import (also in its dead zone), default exports, a module that throws, modules in directories. For each, the order in which the model runs the modules is also compared with `Plan.evalOrder` of the proved checker (the `evalorder` executable): they agree on all 19.
- Programs of [`translation/gen`](translation/gen/). The generator leans on the standard library, so the front end accepts no whole program. A program is cut down to the units the front end accepts alone and whose names are still declared; a unit where the model then reaches unknown code is dropped and the program is run again. Of 2,040 programs (340 for each of the six sets of families, seeds 1000 to 1339): 52,652 units, 24,890 accepted alone (47%), 9,000 left in the programs that ran (17%). 123 programs had no unit left. The other 1,917 agree with Node, all of them, and the model made every host call Node made (3,529 in this run, those of the hand-written cases included). Before the world was a parameter, 4,665 units (9%) were left and 387 programs had none: a method of a string ended the run. What ends it most often now is a method of an array, which takes an object.
- **Splits by the real tool.** Programs the model runs to the end are split by `lowersplit` under three specs each: consecutive pieces run in order, a random module for each statement with `sequential`, and the same with the order left to the imports. For every split it accepts, four runs must agree: the bundle in Node, the bundle in the model, the split in Node, the split in the model. The front end must also give every statement of the split the tokens it gave that statement of the bundle, and the model must run the modules in the order of the plan. With `--count 40 --seed 1 --splits 150`: 135 programs, 405 specs, 219 accepted (127 of the consecutive ones, 46 with `sequential`, 46 without). On 218 the four runs agree, 196 of those with the statements in the bundle's order. The model never differed from Node, and no accepted split of a bundle that ends normally differed from its bundle. No accepted split asked the world for anything else than its bundle did, or in another order. Every accepted split that runs the statements in the bundle's order was also run in the world its bundle recorded, and ended in the bundle's state: every object and binding at the same address, by a hash of the whole state (189 of 189 in a run of `--count 40 --seed 1 --splits 150`).
- The members the model gives the six prototypes are compared with Node's own, and every function of the table has to be a function of this Node.

**A split that moves code is compared with its bundle only if the bundle ends normally.** The tool's verdict is for a bundle whose evaluation completes: premise P4 of [`effects/README.md`](effects/README.md). Two named cases (`splitCases` in [`model/cases.mjs`](model/cases.mjs)) hold the line between what the tool orders and what the premise covers:

```js
console.log("printed");
throw 1.5;
```

`lowersplit` must reject a spec that puts the `throw` in a module that runs first: a `throw` it reaches is unknown code and keeps its place.

```js
const o = { a: undefined };
console.log("printed");
const w = o.a.b;
```

Here the spec puts the first and the third statement in a module of their own, which the entry imports, so the `TypeError` is raised first and nothing is printed. `lowersplit` accepts: an exception that the language raises itself in pure code is not an effect to it. The model agrees with Node on both programs. So the theorem for a split that moves code has P4 as a hypothesis ([`PLAN.md`](../PLAN.md) § Direction). A split that runs the statements in the bundle's order needs none, and the test compares such a split with its bundle however the bundle ends.

The low share of generated units is the finding of [`SUPPORT.md`](../SUPPORT.md) again: no small fragment covers what programs use. The model is for proving rules about a core, not for running the inputs.

One disagreement was found on the way: `x ** 2` differed from Node in the last bit for one operand. The specification leaves `**` to the implementation, and the C library's `pow` is not V8's. `**` is refused now.

**What is proved** ([`Frame.lean`](../lean/Experiments/CoreJS/Frame.lean), on Lean's three standard axioms). The shape `Expr.allocOnly`: a literal, a name, a function, an object literal of named data properties, an array literal, nested.

```lean
theorem allocOnly_extends {W : World} (h : e.allocOnly = true) (fuel : Nat) (env : Env) (s : State W) :
    (evalExpr fuel env e s).Keeps s
theorem decl_allocOnly {W : World} (h : e.allocOnly = true) (fuel : Nat) (env : Env) (kind : DeclKind) (x : Name)
    (s : State W) : (execStmt fuel env (.decl kind x e) s).KeepsBut (env.vars.lookup x) s
```

However the expression ends, every object and binding that existed is unchanged, nothing is printed and the world is in the state it was in; what it made is new. A declaration with such an initializer changes no binding but its own. This is the footprint "binds its name, no effect", proved for that shape.

[`Fuel.lean`](../lean/Experiments/CoreJS/Fuel.lean) proves that the bound on the depth is only a bound:

```lean
theorem fuel_mono {W : World} (h : runProgram W n P = r) (hr : r ≠ .fuel) (hnm : n ≤ m) : runProgram W m P = r
def Ends (W : World) (P : Program) (r : Res W Unit) : Prop := ∃ n, runProgram W n P = r ∧ r ≠ .fuel
theorem Ends.unique {W : World} (h : Ends W P r) (h' : Ends W P r') : r = r'
```

A run that ends with a value, a throw or unknown code ends the same way with any larger bound, so a program ends in at most one way. The proof compares the two runs rule by rule; no rule of the interpreter had to change for it.

[`Scope.lean`](../lean/Experiments/CoreJS/Scope.lean) proves that code sees a scope only through the names that are free in it. `Agree N env env'` says that two scopes have the same `this` and give every name of `N` the same binding, or none.

```lean
theorem same : ∀ n, Same W n
theorem runUnit_agree (h : Agree s.free env env') (fuel id : Nat) : runUnit fuel env id s = runUnit fuel env' id s
theorem capture_faithful (n : Nat) (env : Env) (s : Stmt) (N : List Name) (hN : ∀ x ∈ s.free, x ∈ N) :
    execStmt n (env.capture N) s = execStmt n env s
```

`Same W n` is that statement for each of the nine functions of the interpreter that take a scope: with `Agree` on the free names of the code, the two computations are equal, for every bound and every world. They are equal as functions of the state, results and final states included, not related up to something: a closure made on either side keeps the same bindings. `capture_faithful` is the reason closures may keep so little: code runs in the kept bindings as it runs in the scope.

[`Split.lean`](../lean/Experiments/CoreJS/Split.lean) proves the first slice of the theorem of [`PLAN.md`](../PLAN.md), a split that keeps the order:

```lean
def ScopesAgree (P P' : Program) : Prop :=
  ∀ frames, FramesOf P.tops frames → ∃ L L', P.scopes frames P.loaded = .ok L ∧ P'.scopes frames P'.loaded = .ok L' ∧
    SameUnits (unitsOf L) (unitsOf L') ∧ (unitsOf L).Pairwise fun a b => a.1 ≤ b.1
theorem order_kept_preserves {P P' : Program} (hregions : P.regions = P'.regions) (htops : P.tops = P'.tops)
    (hscopes : ScopesAgree P P') : (∀ r, Ends W P r → Ends W P' r) ∧ (∀ r, Ends W P' r → Ends W P r)
```

- _What it says._ Two programs that hold the same statements under the same identities (`htops`, `hregions`), run them in the same order, the order of the identities, and give every statement a scope that agrees with its scope in the other program on the names it mentions (`ScopesAgree`), end the same way in every world. A bundle and a split of it that keeps the order are two such programs.
- **`≈` is equality.** A result is how the run ended (normally, with a thrown value, at unknown code) and the state it ended in: what was printed, the state of the world, every object and every binding. The theorem says the two results are equal. Nothing is related up to a renaming. What differs between the two programs, the scopes of their modules, is not part of a result.
- _The hypotheses._ `htops` and `hregions` are equalities of lists and numbers that the tool can compute. `ScopesAgree` speaks about every choice of addresses for the bindings, so it is not a check as it stands.
- **For a plan that `Valid` accepts**, `valid_order_kept_preserves` is the theorem with no further hypotheses than footprints that say what the text says (`FootprintsOfSyntax`). It rests on two facts. `split_holds_statements`: `splitProgram`, the program `Module.render` writes for a plan, has the bundle's statements when the plan keeps the order; the model's order of evaluation is `Plan.visit` (`split_visit`). `split_scopes_agree`: the scopes of `splitProgram` agree with the bundle's. The second is the substance: an import resolves, through the export it names, to the binding of the one statement that declares the name. It needs the exporting module to declare the name, which `Linkable` asks: the model does not follow an `export` statement that publishes a name its module imports.
- _What the test adds._ The conclusion, the same final state, holds on every order-kept split the real tool produced in the test (above). It also checks the model against `Valid`: a split the test finds wrong would be a counterexample to `valid_order_kept_preserves` or to `FootprintsOfSyntax`.

Not proved: the allocation rule for classes (the model runs them; the proof does not cover them yet), that such an expression never ends `unknown`, and that two such declarations commute. The last is written down as `AllocOnlyUnitsCommute`. It needs one more lemma, that the value depends only on the bindings the expression reads.

**What the proofs showed about the model's shape.**

- Stating every lemma against a fixed earlier state (`Extends base s`) made a change to a new object free: an address that is unused in `s` was unused in `base`.
- The property operations take "what a call does" as a parameter, so they are outside the recursion and their lemmas need no induction.
- Lean's `do` notation copies the rest of a block into both branches of an `if` in the middle of it. Rules written that way did not match the sequencing lemma, and three helpers (`selfScope`, `initSelf`, `addPrototype`) were split out. New rules should keep each `if` inside a helper.
- `Float` has no equations in Lean. That costs nothing here, since a frame fact never looks at a number.
- A proof that walks every rule of the interpreter (`fuel_mono`) is short when it is one tactic that takes a rule apart into sequences and case splits and closes each part with the hypothesis for the smaller bound. It has to match terms without unfolding definitions (`with_reducible`), or the elaborator unfolds the interpreter itself.
- A relation between two runs is cheap when it can be equality. The scope theorem compares runs in two different scopes, and would have needed a relation between heaps, because a closure holds its scope. Making the closure keep only the bindings its code names turned the relation into equality, and the proof into the same walk over the rules as `fuel_mono`.
- Where the rest of a rule depends on what the first part returned (the name an assignment target gave, the names a new frame declares), the sequencing lemma has to hand the continuation that fact (`M.bind_congr_ok`); a plain congruence does not.
- Taking the world as a parameter cost no proof: `State W` threads through every definition, and the lemmas never look at `W`.

**The world, stage two: opaque code.** Stage one stops where the host would be handed an object. Stage two lets the world run code that the model does not define, and that code may be handed objects and functions. It is built for one use: a statement outside the model's syntax becomes an _opaque statement_, and the world runs it. That is what lets the order-kept theorem reach real bundles ([`PLAN.md`](../PLAN.md) § State when parked).

- _An opaque statement_ is `Stmt.opaque code decls funs free`: a number for its code, the names it declares (with their kind), the functions it declares (with their `length` and whether they construct), and the other names it mentions. The front end takes all of them from the scope analysis. The statement's free names are these names (`opaqueNames`), so `Same` and `capture_faithful` hold for it with no new argument.
- _The world names an object only by its position in the list of objects it was handed_ (`Host.handed`, part of the state). An object goes on the list when it is given to the world, as an argument, a `this`, or the answer to a request. A handle that is not on the list ends the run as `unknown`. So the world reaches only what it was handed and what it read from that, by construction.
- _A run of opaque code is a conversation_ (`World.talk`, `converse` in [`Eval.lean`](../lean/Experiments/CoreJS/Eval.lean)). The model tells the world what to run. The world answers with a request, the model carries it out on a smaller bound and tells the world how it ended, until the world ends the code with a value, a throw or `unknown`. The requests: read, write or initialize a binding it was handed (by its position in `opaqueNames`), read, write or delete a property, `in`, the own keys of an object, call or construct a function, make an object or a function, and the model's primitive operations. A request that throws is told to the world as a throw, so opaque code can catch it.
- _Functions of opaque code._ A function the statement declares is made when the program is linked, as for any function declaration. The world can also make one (`newFunction`). Each is a part of the statement's code, closed over the bindings the statement was handed. When the program calls it, the world runs that part with the callee, `this` and the arguments as handles.
- _Every proof holds for every world, opaque code included._ `fuel_mono`, `Same`, `order_kept_preserves` and `valid_order_kept_preserves` did not change their statements. The proofs needed one more case each: the conversation is monotone in the bound (`Mono.converse`), and an opaque statement sees the scope only through the bindings of its names (`opaqueCells_agree`).
- _What the meaning trusts._ The world may do anything with what it can reach, so a real statement fits it if the front end listed every name the statement can mean. That reading of the scope analysis is trusted, as the reading of declared names was before.

**The front end makes opaque statements** when it is given a scope analysis (`toCore(source, { opaque: eslintScope })`; `coreOf` in [`split_dump.mjs`](translation/split_dump.mjs)). A top-level unit it refuses becomes one opaque statement. Some units it still refuses, because no statement of the model can stand for them: a top-level `await` or `using`, a direct `eval`, `import.meta`, and `import()`, whose meaning depends on the file the unit is in. The option `census` makes these opaque anyway and lists them in `refused`. A name that no statement declares and that is not one of the host globals the model knows is refused in the model's syntax, so the unit is opaque and the world holds that name.

**Checked against Node, with scripts for the world.** The test needs a world that does what the real code does. Six hand-written programs ([`model/opaque_cases.mjs`](model/opaque_cases.mjs)) have statements outside the model's syntax, and each comes with a script: for each part of the opaque code, the requests the world makes. The scripted world (`replay` in [`Run.lean`](../lean/Experiments/CoreJS/Run.lean)) answers with them, in order, and fails the test on a request that does not fit. The cases: a destructuring declaration that reads an object of the program, an opaque function that the program calls and that calls back, a throw of opaque code that the program catches, opaque code that writes a binding and a property, opaque code that makes an object and a function, and a getter of the program that runs when opaque code reads the property. All six agree with Node. Each is also split into two consecutive modules; of the six splits, `lowersplit` accepts five, and all five end in the bundle's state. A wrong script fails the test.

**Footprints against the syntax.** `corejs --footprints program.json footprints.jsonl` checks `FootprintsOfSyntax` ([`Split.lean`](../lean/Experiments/CoreJS/Split.lean)) on a real bundle: for each statement, the names it declares are the footprint's `defs`, and every name it mentions is in `defs`, `eager` or `lazy`. On the large bundle, with its import line blanked and the vendor module as part of the world, it holds for all 9,708 statements. That run took the 41 units with `import()` as opaque too, which is faithful only if the split leaves every module in the directory of the chunk, as `lowersplit` does: a specifier then resolves to the same file.

**How much of a bundle is opaque** ([`translation/opaque_census.mjs`](translation/opaque_census.mjs)). A unit is a top-level statement, or one declarator of a list.

| Bundle                                |  Units | In the model's syntax |         Opaque |    Refused |
| ------------------------------------- | -----: | --------------------: | -------------: | ---------: |
| the large bundle, application chunk   |  9,707 |         4,071 (41.9%) |  5,595 (57.6%) |  41 (0.4%) |
| a second web application, 62 chunks   | 45,615 |        19,388 (42.5%) | 26,046 (57.1%) | 181 (0.4%) |
| a command-line application, 75 chunks | 35,009 |        11,929 (34.1%) | 22,702 (64.8%) | 378 (1.1%) |

The front end accepts 98.9 to 99.6% of the units, in the model's syntax or opaque. The refusals are `import()` (41; 179; 183) and `import.meta` (none; 2; 195).

The stage-two design also asked for the following, and it is not built:

- _A frame lemma for opaque code._ A conversation changes only what the world can reach and what the functions it calls change. That holds by construction, and it is not proved. It is what a split that moves code past opaque code needs ([`PLAN.md`](../PLAN.md) step N5).
- _Node's side of a conversation as a recording_, with a proxy around every object that is handed over. The test's scripts are written by hand.
- _Host calls of stage one as conversations._ The 88 functions of the table still take primitives only.
- _A class for each function, not only a list._ Stage one has two classes: takes its receiver, or does not. The table should say per function what it may do: "a function of its arguments", "reads its receiver's own properties", "calls its argument". The step of a function of the first class would not see the state of the world at all.
- _That class is what a split that moves code needs._ With one world state, two host calls can never change places: the test compares the calls a split makes with its bundle's, in order, for that reason. Moving `Math.abs` past `Math.max` is sound only because neither reads or changes the world, and the world type has to be able to say so.
- _Host functions should be objects._ `Val.host` is a name, so `typeof Math.abs` and `Math.abs === Math.abs` end as `unknown`. In stage two a host function is on the heap, with host code, so it has an identity and properties and can be handed back to the host.
- _Strings should be code units._ The boundary showed the gap: the host returns strings the model cannot hold. With strings as sequences of UTF-16 code units, nothing the host returns is outside the model, and the front end no longer refuses literals.
- _Errors are objects the host makes._ Stage one keeps them opaque (`Val.error`), which is why a caught error cannot be inspected. In stage two they are objects the world asks the model to make.
