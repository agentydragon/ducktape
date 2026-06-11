# Debundle full review — 2026-06-11

Full-stack review of `devinfra/js/debundle/` (~77.5K lines Rust across 145
files), from low-level code up through architecture and approach. Produced by
a fan-out of subsystem reviewers (core graph/facts, realizability gate, purity,
peel planner, lowering/emission, vendor, spec/CLI/pipeline, tests/build/docs)
plus direct verification of contested findings. Line numbers are as of
`e818267`; re-check before acting.

## Verdict

The approach is right and the formal core is genuinely strong: the
owner-graph-quotient framing, the two-pass realizability gate with the ESM
Phase-2 simulator, the fully-explicit spec decision, and the
soundness-over-completeness contract are all well-argued and mostly
well-implemented. The differential tests between the incremental
`RealizabilityIndex` and the pure `check_realizability` are real, and the
e2e-first test discipline is genuinely followed (339 black-box tests, 152
executing emitted output under Node).

The problem is uneven enforcement of that contract. The gate itself is sound,
but (a) the **analyses feeding it** under-approximate edges in multiple
concrete ways (purity classifier, fact extraction, at-init promotion), (b) the
**emitter behind it** diverges from the gate's simulated model at two seams
and contains a string-keyed rename pipeline with real miscompilers, and (c)
several **load-bearing doc claims are false** — including the precondition
list that is presented as the audit checklist making the dataflow S-chain safe
to enable. The recurring failure mode across every subsystem is the same:
_invariants held by convention rather than structure_ (simulator "mirrors" the
emitter by parallel implementation; the materializer's accept decision
recomputes over a different graph view than the verdict; gate-by-default is
dispatcher convention; module identity has three wire spellings; design.md
cites safety nets — "the validator's strict rule", `mark_potentially_cyclic`,
`BindingId` interning — that do not exist in code).

The repo's own self-review docs (CODE_REVIEW.md, ARCHITECTURE_BACKLOG.md) are
structurally useful but skewed to hygiene; they contain essentially none of
the soundness-class findings below, several stale entries, and one
recommendation (swc DCE replacing the vendor sweep) that should be retired as
unsound.

---

## 1. Approach level

**What's right.**

- Treating debundling as _graph quotient + scheduling with a static gate_ is
  the correct framing, and the theorem/lemma discipline in docs/design.md is
  far beyond what code of this kind usually gets. The "why a static gate, not
  a runtime check" argument and the rejected-closure-pass argument
  (spec stays the source of truth) are both correct calls.
- "Soundness over completeness" with the conditionally-correct-optimization
  rule (checked precondition + conservative fallback) is the right contract
  for this domain.
- The single-realizability-primitive invariant ("no bespoke parallel walks")
  is the right architectural rule — the violations found below are violations
  _of_ it, not evidence against it.
- The agent-oriented operating layer (skills/, machine-readable reports,
  CLI gate-by-default, selector-debt reporting) matches how the tool is
  actually driven.

**Where the approach under-delivers.**

- The theorem is only as good as the `R`/`S`/`I` edges feeding it, and the
  burden of proof applied to whitelist entries (ECMA-262 citations, paired
  tests) was never applied to the _classifier and fact-extractor around
  them_. The soundness findings in §3 are almost all of the shape "construct
  X fires user code / creates a dependency, and the analysis is silent."
- A1/A3/A4/A5 are called "statically checkable" in design.md but nothing
  checks them and no test pins behavior on violating input. A cheap
  chunk-admission linter (grep-level: top-level `eval`, internal dynamic
  `import()`, `with`, namespace reflection) would convert assumptions into
  verified preconditions the way A2 already is.
- Doc-claims drift on soundness-relevant statements is itself a soundness
  hazard here, because AGENTS.md instructs users to audit bundles against the
  README list (§8).

## 2. Architecture level

- **Layering is right**: facts → owner graph → atomic units (Stage A);
  partition → quotient → gate → lower → emit (Stage B). The single projection
  point (`graph.rs::partition_endpoints`) and the canonical constraining-edge
  set are good design, consistently used.
- **The gate of record is the pure form** — `validate_factorization` reruns
  `check_realizability`, so incremental-index drift cannot by itself ship a
  broken bundle. Good defensive structure.
- **But three structural seams undermine "one implementation":**
  1. The emitter's import ordering is a _parallel implementation_ of what the
     gate's `EsmEvaluationSimulator` assumes (§3.1). Mirroring is convention.
  2. `validate_factorization` derives its accept decision (`cycles`) from the
     **Lenient**-view quotient SCCs rather than from `verdict.unrealizable_sccs`
     (validation.rs:316-358) — a bespoke recomputation; if the views ever
     diverge, a gate-rejected spec is silently accepted.
  3. The peel kernel's hot boolean merge gate
     (`merge_creates_new_constraining_cycle`, quotient.rs:705-709, 1030-1054)
     is a bespoke constraining-only Pearce–Kelly walk that does **not** route
     through the realizability primitive — design.md:514-519's claim that it
     uses `verdict_after_moving_owners_touching` is false. The constraining-only
     view is blind to asymmetric I-cycles; the mitigation is a single post-seed
     check. Either sanction this in design.md as the documented trade-off or
     route the gate through the index.
- **Type proliferation** (confirmed): four "unrealizable, here's why" types;
  `CycleReport`/`QuotientSccReport` should become projections of
  `SccDiagnosis` (this also fixes seam 2 above and the over-reporting bug
  B2-gate). `ChunkManifest` literally embeds `ChunkAnalysisReport` field-by-
  field (artifact.rs:334-383) — `#[serde(flatten)]` collapses the layer with
  zero wire change, answering the backlog's open decision.
- **Module identity has three wire spellings**: `ModuleKey` (`logical:N`) in
  owner_graph.json, `LogicalModule.id` (`{chunk}::path`) in cycles.json, and
  `ModulePath`. This is the root cause of `gate describe` being structurally
  broken (§4, C4-cli). WIRE_FORMAT.md's "one canonical identity" convention
  needs cycles.json brought into it.
- **`RealizabilityVerdict` mixes graph views** (lenient `scc_partition` +
  gate-view `unrealizable_sccs` in one type, realizability.rs:96-104) — this
  is what made seam 2 possible.
- The Bazel `:analysis` target is a god-crate (25 srcs spanning graph, gate,
  facts, purity, validation, reports — BUILD.bazel:254-300); splitting it
  would make the wished-for module boundaries compiler-enforced.

## 3. Soundness findings

The contract: any spec the validator accepts must emit a bundle that runs
correctly. Each item below has a concrete counterexample shape in the
underlying review transcripts.

### 3.1 Gate ↔ emitter seams (found independently by two reviewers)

- **S1. Phantom-import placement diverges from the simulator.** Emitter places
  phantom side-effect imports first in every moved module regardless of
  `linker_position` (lower.rs:743-750); the simulator sorts all successors in
  one `linker_position` list (realizability.rs:493-513). Inside an asymmetric
  I-SCC this can flip the runtime DFS post-order — an accepted spec that TDZs
  under Node. Nothing structurally prevents it. Also: tie-breaking for
  missing `linker_position` is opposite between simulator (largest-first) and
  emitter (smallest-first). **Fix**: one shared sort for phantoms + cross
  imports (phantoms only need to precede the _entry_ import), plus a Node-
  execution differential e2e over accepted asymmetric-SCC specs.
- **S2. Entry imports are a superset of the simulator's model.** The entry
  imports every binding-owning plan (lower.rs:257-292) and
  `trim_dead_named_specifiers` deliberately keeps unreferenced side-effect
  directives, while the simulator models only modules residual _references_.
  Direction is over-rejection (and is the likely cause of the open gaffer
  over-rejection `#[ignore]`), but it falsifies the "mirrors exactly" claim.
- **S3. Anonymous-only modules lose their side effects.** A logical module
  containing only anonymous statements has no bindings ⇒ no entry import
  (lower.rs:258 skips), and phantom imports skip residual as a source ⇒ the
  emitted file is never imported; its side effect never runs; the gate
  accepts. Needs a minimal e2e (`anonymous_statements: ['console.log("a");']`
  as the chunk's last impure statement + `assert_entry_output`).
- **S4. Materializer accept decision is a parallel walk** (architecture seam
  2 above; validation.rs:316-358). Derive `cycles` from
  `verdict.unrealizable_sccs`, or at minimum assert the implication.

### 3.2 Owner-graph fact extraction (default strict path)

- **S5. At-init promotion has no conservative fallback for indirect/method
  calls, and the documented safety net doesn't exist.** `at_init_calls`
  records only bare-`Ident` callees; promotion silently `continue`s on
  unresolvable callees (graph.rs:1043-1047). design.md:628-634 claims these
  are "caught by the validator's strict rule (see below)" — dangling
  reference, no such rule. Counterexample: `const g = readB; … const r = g();`
  split across modules → gate accepts, runtime TDZ. The method-call variant
  (`api.read()` namespace objects) is a mainstream minified shape.
- **S6. Nested-closure cross-module rebinds escape rebind-legality.** Only
  `first_order_lazy_rebinds` emit edges (graph.rs:704-723); full
  `lazy_rebinds` never reach the cross-rebind check, whose justification
  (ESM imports are read-only) is time-independent. The existing
  `e2e/at_init_promotion_nested_closure_test.rs` fixture is exactly the broken
  shape — it only asserts init-time output; a post-init probe
  (`globalThis.__updateState()`) flips it red (TypeError on assignment to
  import). **Fix**: a deferred-rebind edge kind that participates in
  cross-rebind rejection but not init-order SCCs.
- **S7. Block-hoisted `var` is invisible.** `collect_declared_names` handles
  only direct `Stmt::Decl` (facts/mod.rs:843-861); `try { var impl = … } catch
{ var impl = … }` (classic feature detection) declares nothing ⇒ readers get
  no edges; also defeats `compute_shadowed_globals` (A8). Collect vars with a
  hoisting-aware walk or reject the shape.
- **S8. `export default <expr>` / `export default class` are unconditionally
  `Pure`** (facts/mod.rs:621-622, 826-841) — `export default sideEffect()`
  drops out of the S-chain in both modes.
- Cross-destination rebind enforcement has no defense-in-depth:
  `verdict.cross_rebinds` is computed but never consulted on the accept path;
  the only guard is `atomic_units.rs:98-101`'s bidirectional rebind edges.
  Assert `cross_rebinds` empty on accept.

### 3.3 Dataflow-aware S-chain (opt-in) — recommend treating as unsafe to enable until fixed

- **S9. Missing write-after-read edges.** Last-writer-only emission covers
  RAW/WAW but not WAR: a writer is never ordered after prior readers
  (graph.rs:848-890). Three-statement counterexample reorders an observable
  read past a later write across modules; gate accepts.
- **S10. Opaque calls don't bail.** Any plain call is as opaque as `eval` to
  cell summaries, but doesn't flip `dataflow_summarizable`: `console.log`
  pairs carry no ordering (I/O isn't a cell); `f()` whose body writes
  `globalThis.x` records no write (`record_global_prop` gated on
  `lazy_depth == 0`; promotion propagates only binding cells). Minimal sound
  fix: any statement containing a non-whitelisted call/`new` forces the
  conservative fallback — which honestly asks whether the relaxation pays for
  itself beyond the fixture shapes.
- **S11. Member writes record only reads** (`obj.x = 1`, `globalThis.count++`
  via `visit_update_expr`, `const g = globalThis; g.tag = …` aliasing).
- **S12. README precondition list over-claims** (verified directly):
  `(0, eval)` is **not** detected (`strip_parens`, binding_targets.rs:219-225,
  unwraps `Paren` only — the callee of `(0, eval)(…)` is a `SeqExpr`);
  `window[expr]` / `self[expr]` are not tracked anywhere (zero matches in
  facts/ and graph.rs); the "dynamic member-key reads/writes … bail" does not
  exist. design.md's shorter list matches the code; README.md:239-248 is the
  drifted copy — and it's the one presented as the audit checklist.

### 3.4 Purity classifier (S edges)

The whitelist tables themselves audit clean (admission rule honored,
citations real). The classifier around them does not apply the same
discipline:

- **S13. Operator/template coercion**: `a + 1`, `` `${a}` ``, `instanceof`,
  `in` classify Pure but fire `valueOf`/`toString`/`Symbol.toPrimitive`/
  `Symbol.hasInstance`/proxy `has` on object operands (purity/mod.rs:1552-1590)
  — pinned as intended by classifier_tests.rs:149, contradicting the
  admission philosophy. Fix is op discrimination (logical/strict-eq/`typeof`
  safe; arithmetic/relational/loose-eq/`in`/`instanceof`/interpolation only on
  syntactically-primitive operands).
- **S14. Destructuring** (`const {a} = o`, `const [x] = o2`) fires getters/
  iterators never classified (purity/mod.rs:786-794).
- **S15. Parameter evaluation invisible**: default-value expressions and
  param destructuring never classified (visit_body_with walks only the body).
- **S16. Body-level shadowing handled only for PlainData** (purity/mod.rs:
  1367-1372): a param shadowing a short chunk-top function name (`f`, `m`)
  still resolves through global tables — the most likely of these to fire on
  real minified corpora. The PlainData fix shows the mechanism; extend it to
  function bindings, whitelist receivers, and spec annotations.
- **S17. Shadow tracking for `new Map/Set/WeakMap/WeakSet` is dead code**:
  `compute_shadowed_globals` only inserts `WHITELIST_RECEIVERS`
  (whitelists.rs:227-228), which excludes those names; `const Map = class {…};
new Map()` classifies Pure. Derive the tracked-name set from the union of
  all whitelist tables.
- **S18. Class-definition-time evaluation under-checked**: `extends <expr>`
  side effects, computed member keys, static accessors all bypass
  `class_has_static_observable` (purity/mod.rs:2536-2563).
- **S19. PlainData has no escape analysis**: alias + `defineProperty` defeats
  the literal-first-arg hostile-write scan (purity/mod.rs:1326-1340);
  subsequent reads stay Pure. Disqualify escaping candidates.
- **S20. `for…of`/`for…in` in bodies fire protocol code with no purity
  contribution**; computed member keys checked for evaluation purity, not
  primitiveness (ToPropertyKey).
- Also: undocumented intrinsic-integrity assumption (prototype pollution of
  `Set.prototype.add` etc. defeats whitelists — needs an A-assumption and
  ideally a scan); `typescript_decorate_helper` accepts member-chain decorator
  references whose evaluation fires getters (facts/mod.rs:747-768);
  inconsistent doctrine on engine-emitted throws (fromEntries rejection vs
  hoisted-`var` TypeError admission); 18/22 `PURE_STATIC_FUNCTION_REFS`
  entries lack the AGENTS.md-mandated paired tests; the AGENTS.md-referenced
  inline TODO of gated-admissible patterns doesn't exist.

### 3.5 Vendor

- **S21. Partial swap rewrites only `ImportDecl`** (vendor/mod.rs:1927-1961):
  `export { x } from "vendor"` re-exports and `import * as M` namespace
  consumers of swapped names are never rewritten while strip removes the
  names — link error, or silent `undefined` via `M.x`. Vite facade chunks are
  this shape. No post-strip cross-chunk gate exists; add one (scan retained
  files for references to stripped names, bail).
- Boundary rename can rebind a caller import to the wrong value on
  local/export name collision (mod.rs:577-658). Wrapper synthetic locals
  (`_d`, `__vendor_default__`) lack collision checks against upstream bodies
  (`unique_synthetic_ident` exists, unused here). `named_from_module_default`
  aliases every named export to the default with no validation
  (wrappers.rs:205-211). `export { x as default }` asymmetry in
  `collect_exported_names(include_default=false)` (mod.rs:847-879, duplicated
  strip.rs:1604). VendorPrune local-effect detection accepts non-local side
  effects (`record_member_write` unconditional; `ns.x = sideEffect()` marked
  Pure-with-local-effect) — facts/local_effects.rs:340-409.
- **Retire the CODE_REVIEW.md swc-DCE suggestion**: the sweep must delete
  _referenced, side-effectful_ swap-private statements that DCE retains, and
  its split-brain gates are exactly what DCE lacks. Replacing it would weaken
  the guarantees. The two deliberate split-brain bypasses (multi-package
  items; `shareable_helper`) should be documented in design.md.

### 3.6 CLI edit gate

- **S22. The edit gate is blind to `source_match` members and
  `binding_groups`** (spec_modules.rs:134-145 → edit_gate.rs:205-210): their
  owners are treated as residual during gating, so the CLI can green-light an
  edit the authoritative `debundle run` gate rejects — under-restriction by
  the project's own classification.

## 4. CLI / data-integrity bugs (the spec is the RE source of truth)

- **C1. Every mutating edit destroys author YAML comments/formatting in
  touched files** (cli/yaml_edit.rs:27-39): edits round-trip through
  `serde_yaml::Value`, which carries no comments/anchors/style. Even
  `modules merge`'s own `# merged from:` header is destroyed by the next
  assign. The "YAML-shape preserving" claim (cli/binding.rs:12) is only true
  for untouched files. Biggest data-loss vector in the tool; needs a
  comment-preserving editor (or honest docs + preservation of known headers).
- **C2. `bindings assign/unassign` delete unrelated pre-existing empty
  modules** (binding.rs:726-752, 951-973): the drained-module sweep iterates
  all loaded docs; the code comment states the intended restriction ("only if
  source of a move") that the code never checks.
- **C3. Batch corruption when one member is addressed by both spellings**
  (minified + readable): dedupe keys on the raw `sym` string; the second
  `take_member` pushes a literal `- null` member that later fails to parse
  (binding.rs:548-615, 717-721).
- **C4. `gate describe` evidence is structurally empty on production data**
  (gate.rs:412-449): intersects `cycles.json` module names
  (`{chunk}::path`) with `owner_graph.json` destinations (`logical:N`) —
  disjoint vocabularies; e2e passes only because its fixture matches strings.
- **C5. Pass-2 (asymmetric/TDZ) rejections produce an empty `cut`** and no
  binding-pair blame (`compute_realizability_cut` runs FAS over the
  constraining-only subgraph, acyclic by definition for this class;
  validation.rs:478-560 + the defensive continue at :188-192). The subtlest
  rejection class has the worst diagnostics. Also B2-gate: rescued SCCs are
  reported as blockers alongside real ones (validation.rs:354-358).
- `modules merge` drops source `comment:` and `binding_groups:` then deletes
  sources (module.rs:291-292) — docs promise comment concatenation that was
  never implemented. Member `comment:` is silently dropped whenever the
  member is renamed (`binding_comments` keyed by original name; matching runs
  post-naturalization — plan_builder.rs:247-256 / js_ast.rs:440-476).
- No write-temp-then-rename atomicity anywhere (yaml_edit.rs:37) despite
  docs/cli.md:285-289 claiming batch atomicity. `bindings assign`'s rename-
  collision check is weaker than `rename`'s (binding.rs:624-675 vs 315-371).
  `DEBUNDLE_SOURCE_ROOT` means two different things (`run --tree-source-root`
  vs snapshot root for queries) — wrong-input runs from a session env var.
  Documented `--format json` structured rejection output doesn't exist
  (edit_gate.rs:311-323 prints prose to stderr) — worth building for agent
  consumers. Gate enforcement is dispatcher-convention; make it structural
  (`Gate::Run(&Path) | Gate::Skip` parameter). `--dry-run` rejections leave no
  `cycles.json` for the documented `gate list/describe` follow-up.
- Case-normalization asymmetry: `ModulePath::parse` lowercases but CLI file
  resolution is case-sensitive → `UI/Widgets.yaml` beside `ui/widgets.yaml`.

## 5. Rename pipeline (lowering) — the concentrated weak spot

Findings 1, 2, 4, 6 of the lowering review are one architecture problem:
flat `BTreeMap<String, String>` renames applied by visitors that know some
identifier-position hazards but not others.

- **R1 (ship-blocking miscompiler). Shorthand props / destructure patterns**:
  no `Prop::Shorthand` / `ObjectPatProp::Assign` handling in
  `impl_rename_visit_mut!` (visitors.rs:143-228). Rename `a→b` turns
  `f({ a })` into `f({ b })` (property key silently changes) and
  `const { a } = o` into `const { b } = o` (reads the wrong property).
  Reachable from every rename path; minifiers emit shorthand aggressively;
  zero e2e coverage. CODE_REVIEW.md's own SWC table already notes
  `IdentRenamer` handles these — the gap is half-known.
- **R2. Rename-target capture**: scope stack suppresses on shadowed _sources_
  only; `a→b` inside `function f(b){ return a + b }` yields `b + b`. Target
  validation consults top-level names only (lower.rs:183, 217-252), and
  naturalization applies `export_name`s module-wide with no target check.
- **R3. Export-local remapping through scope-local heuristic entries**
  (lower.rs:813 / imports_cross.rs:189-205): a heuristic rename whose source
  name collides with a top-level exported binding remaps the export specifier
  while the declaration keeps its name → SyntaxError.
- TODO.md's collect→validate→execute-once RenameLedger is the right fix and
  should be promoted to the top of the queue; R1 deserves a minimal e2e and a
  fix now (or adopt `swc_ecma_utils::IdentRenamer` semantics for these
  positions).

## 6. Peel planner

- Invariant violation + design.md falsehoods covered in §2. Additionally:
  reachable release-mode panic — `topo_order.rs:533-539` `assert_eq!` window-
  Kahn fires on gate-bypassing seed merges (`merge_classes_unchecked` with a
  cycle-creating group; explicitly anticipated input per topo_order.rs:211-214);
  the documented safety valve `mark_potentially_cyclic` (topo_order.rs:161-163)
  was never implemented. Degrade to `is_dag = false` instead.
- design.md:521-525's "rare multi-target case" fallback is **unreachable dead
  code** (`compute_merge_deltas` only emits single-target `MoveOwners`).
- The flagship differential test shares the kernel's own
  `project_partition_for_tests` as its reference — blind to projection bugs;
  only `replay_partition` rebuilds independently and checks only
  `cycle_set()`. "Property tests" are fixed ≤6-owner corpora, no
  randomization; gate-residual promotion has no differential coverage; the
  `GAFFER_OWNER_GRAPH` benchmark test is vacuously green in CI (forbidden
  skip-shape).
- `factorize_tests.rs` is misnamed — its ~40 tests exercise
  `chunk_factorization.rs`/`validation.rs`/purity, not `factorize.rs`.
  `factorize.rs` hygiene: dead `owner_to_class` map (:482-485), write-only
  constant `status: PeelableNow` with `BlockedCycle` never assigned and a
  size-cap diagnostic mislabeled `BlockedResidualDependency` (:662-663,
  :849-852), inconsistent size-cap denominator for extension proposals
  (:521 vs :822-829).
- plan.rs: `explain` drops `--source-root` when building the factorize report
  (:750-756); deprecated `peel` aliases accept and ignore `--format`
  (:462-489); per-row single-element calls into the _batched_ anonymous-claims
  API re-parse the whole bundle N times (:1091-1101); owner_graph.json parsed
  twice per command (no way to pass a parsed report into
  `PeelFactorizeOptions`).
- topo_order's complexity header overstates the bound (re-Kahns the whole rank
  window, not Δ); `is_dag = false` is a permanent degradation (documented
  recovery never invoked).
- `RollbackDiGraph`: release-mode `undo` LIFO only `debug_assert`ed —
  add a release check; journals grow unbounded on committed work — add
  `commit()`/truncate; `scc_containing` is full Tarjan + full SCC
  materialization per query, contradicting the "localized reachability" doc
  (reuse the overlay walker).

## 7. Tests

Strong overall; the two real problems are silent holes, not bad tests:

- **T1. `e2e/asymmetric_non_residual_cycle_test.rs` has never run** — exists
  since #2008, never wired into e2e/BUILD.bazel (its two Lemma-2 siblings
  are). Two-line fix; verify it compiles/passes.
- **T2. A1/A3/A4/A5 have no enforcement and no tests** (§1). A8 is enforced
  but unit-only; A10 unit-only. Lemmas 1/3/4/5 have no named pinning tests
  (functionally exercised, no tripwire).
- Stale "RED test" doc headers on five now-green files (and pinned line
  numbers inside them); one mangled redaction comment in
  accepted_spec_runs_under_node_test.rs:225-243.
- CODE_REVIEW P5 confirmed: dead `NodeOutput` (support.rs:881, zero refs);
  cycle-forcing fixture helper never landed (33 of 43 tests still inline).
- support.rs nits: no-op `*_with_syntactic_holes` aliases; dead
  `FixtureAnonymousStatement.note`; `assert_generated_module_after_entry_script`
  hardcodes `./static/app/entry.js` (breaks with `with_chunk_id`); seven
  `logical_module_with_*` constructors want a builder.
- `unification_eliminates_cell_pipeline` greps `factorize.rs` source via
  `include_str!` — change-detector; replace with nothing or a golden.
- Missing e2e shapes called out above: S1 differential under Node, S3
  anonymous-only module, S6 post-init probe, R1 shorthand rename, vendor C1
  re-export/namespace consumers, `named_from_json_default` wrapper (zero
  tests), full swap with a caller chunk, `boundary_rename`/`suppress` e2e.

## 8. Docs

Doc set is unusually good; drift concentrates on soundness-relevant claims:

- README S-chain precondition list over-claims (S12) — **worst drift**, fix
  first. README also misattributes the gate to `bindings rename` and omits
  `unassign`/`delete` (docs/cli.md is correct).
- design.md falsehoods: "find_top_level_await before fact analysis" (it runs
  inside `analyze_chunk`; bail after — stage_one/mod.rs:91-101; enforcement
  outcome equivalent); "validator's strict rule" dangling reference (S5);
  "Factorize uses explicit push/undo" (kernel does, factorize.rs doesn't);
  "merge_preserves_invariants routes through verdict_after_moving_owners_touching"
  (it doesn't — §6); "only derived state is advisory cached_cycles" (TopoOrder
  decides; cached_cycles rejects merges); multi-target fallback described as
  live (dead); `BindingId`/`BindingTable` interning described as existing
  (never built — everything keys cloned `Id`s); "they cannot drift"
  (ARCHITECTURE_BACKLOG:147) — falsified by the phantom-first rule.
- cli.md cites a WIRE_FORMAT.md section that doesn't exist ("Cross-process
  scope: not a goal"); stale `<facts.rs>`/`<purity.rs>` paths in several docs;
  README's fallback description ("adjacent impure pair") is weaker than the
  actual (every-prior-impure barrier).
- Housekeeping: TODO.md's Excalidraw subsections stranded under "Structural
  selector language" (lines 129-161); comment-CLI workflow triplicated
  (README/guide/cli.md); RENAME.md belongs in plans/; WIRE_FORMAT.md belongs
  in docs/; CLI_DOGFOOD has resolved items.

## 9. Self-review docs: corrections

CODE_REVIEW.md updates needed: Top-5 #2 (analysis_tests split) **done** —
delete; P0 vendor "wrapper generation" clause **done** (`vendor/wrappers.rs`);
"266-line import block" now ~95/295 lines; "God Modules" omits
`realizability.rs` — now the largest at 3265 lines (split: perf counters ~480,
simulator ~200, IncrementalQuotient ~700, tests ~920); the swc-DCE item should
be **retired as unsound** (§3.5); `StatementFacts` hazard framing overstated
post-single-pass-collector (real fix: a `PositionBucketed<T>` triple, one
vocabulary across `StructuralStatementFacts`→`StatementFacts`, derive
`effects`); P1 fixture duplication spans **three** sites with a
same-name/different-arity `owner()` trap; `generated_by_selected_module_lowering`
investigation is moot — the reorder already happened, the flag is dead,
delete it; output_layout accessor item: push back (typed accessors are fine).

BUILD.bazel:436-439 + quotient.rs:2794-2804 "peel_test is broken" — **stale,
verified empirically** (both targets pass on RBE); delete the comments and the
empty scaffolding mod, and consider folding kernel unit tests back in-crate.

Dead code to delete: `program_analysis.rs` write-only access payload
(`IdentifierAccesses`, `OwnerRecord.accesses`, `SideEffectRecord.*` —
lines 84-156, 246-252; burns a full extra AST walk per statement);
`js_ast.rs:255-347` weaker duplicates of source_match/ordinal machinery;
`SourceMatch.kind` and `ChunkRenames.id` write-only spec fields;
`selection_with_proposal` sentinel hack (cli/mod.rs:687).

## 10. Prioritized actions

**P0 — soundness/miscompilers (each per AGENTS.md bug-fix discipline: minimal
e2e first):**

1. R1 shorthand-prop rename miscompiler (+ R2 target capture).
2. S1 phantom-import order: unify the sort shared by gate simulator and
   emitter; Node-differential e2e.
3. S16/S17/S13/S14 purity classifier (shadowing extension, derived
   shadow-name set, op discrimination, destructuring) — these protect every
   S edge.
4. S5/S6/S7/S8 strict-path graph holes (S6's fixture exists; add the probe).
5. S12 + S9–S11: fix README list to match code **now**; treat dataflow
   S-chain as unsafe until WAR + opaque-call bail land.
6. S21 vendor post-strip cross-chunk gate; S22 edit-gate claims model;
   S3 anonymous-only module; S4/`cross_rebinds` assertions.

**P1 — data integrity & diagnostics:** C1 comment-preserving YAML editing;
C2/C3 batch fixes + tmp-rename writes; C4 unify cycles.json on `ModuleKey`;
C5 Pass-2 cut/blame; merge comment/binding_groups preservation;
renamed-member comments.

**P2 — structural:** derive `CycleReport`/`QuotientSccReport` from
`SccDiagnosis` (fixes S4+B2 as a side effect); decide the PK-gate question
(sanction in design.md or route through the index) + fix the topo_order
release assert; RenameLedger; split `realizability.rs` and the `:analysis`
Bazel crate; flatten `ChunkManifest`/`ChunkAnalysisReport`.

**P3 — quick wins (≤30 min each):** wire the orphaned e2e (T1); delete
`generated_by_selected_module_lowering`, `NodeOutput`,
`program_analysis` dead payload, `js_ast.rs` duplicates, dead spec fields;
fix stale peel_test/RED-test/doc-path comments; TODO.md section nesting;
retire the DCE item; A1/A3/A4/A5 admission linter.
