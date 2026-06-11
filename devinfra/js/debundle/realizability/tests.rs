use std::collections::BTreeSet;

use super::*;
use analysis::OwnerId;
use analysis::facts::analyze_chunk;
use analysis::graph::build_owner_graph;
use analysis::ids::{LogicalModuleIndex, ModuleId};
use analysis::partition::Partition;
use analysis::{AnalysisHints, OwnerGraph};
use swc_common::{FileName, SourceMap, sync::Lrc};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

fn module_id(index: usize) -> ModuleId {
    ModuleId(LogicalModuleIndex(index))
}

fn parse_and_build(source: &str) -> OwnerGraph {
    let cm: Lrc<SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom("test.js".into()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        Syntax::Es(Default::default()),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    let module = Parser::new_from(lexer)
        .parse_module()
        .expect("parse module");
    let facts = analyze_chunk(&module, &AnalysisHints::default(), None, |_| None).facts;
    build_owner_graph(&facts).unwrap()
}

/// Two top-level constants in different modules, with one reading
/// the other at-init across the module boundary acyclically. No
/// cycle, no rebind — verdict is empty.
#[test]
fn acyclic_cross_module_at_init_read_is_realizable() {
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    // Owner 0: const a = 1 → module 0.
    // Owner 1: const b = a + 1 → module 1.
    // Edge owner_1 → owner_0 (eager_use of `a`).
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        verdict.is_realizable(),
        "verdict should be empty: {verdict:#?}"
    );
}

/// Same setup but flipped to create a constraining cycle: both
/// statements live in different modules and mutually at-init read
/// the other. Quotient has a 2-cycle of constraining edges →
/// unrealizable.
#[test]
fn constraining_cycle_across_two_modules_is_unrealizable() {
    // Two top-level constants whose initializers eager-read each
    // other. Real JS would TDZ at runtime, but the analyzer just
    // records the structural graph: two `eager_use` edges in
    // opposite directions. Placing them in different modules
    // forms a constraining-edge SCC of the quotient — exactly
    // what clause 3 rejects.
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        !verdict.is_realizable(),
        "verdict should report an SCC: {verdict:#?}"
    );
    let modules: BTreeSet<ModuleId> = verdict.modules_in_unrealizable_sccs();
    assert!(modules.contains(&module_id(0)));
    assert!(modules.contains(&module_id(1)));
    assert!(
        verdict
            .unrealizable_sccs
            .iter()
            .all(|scc| !scc.constraining_owner_edges.is_empty()),
        "every SCC must carry owner-edge evidence"
    );
}

/// The touching-filtered reference predicate
/// (`plans/incremental_gate_unification.md` §2): an SCC diagnosis is
/// kept only when the queried module participates in it. A module
/// outside every diagnosis sees a realizable verdict even though the
/// full verdict is unrealizable.
#[test]
fn touching_filter_keeps_only_diagnoses_involving_the_queried_module() {
    // Mutual eager cycle between mod 1 and mod 2; owner 2 (`const c`)
    // is an unrelated clean module 3.
    let source = "const a = b + 1; const b = a + 1; const c = 1;";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(1), module_id(2));
    partition.set(OwnerId(2), module_id(3));
    assert!(!check_realizability(&owner_graph, &partition).is_realizable());
    let touching_cycle = check_realizability_touching(&owner_graph, &partition, module_id(1));
    assert!(
        !touching_cycle.is_realizable(),
        "module 1 is in the SCC; the diagnosis must survive the filter: {touching_cycle:#?}",
    );
    let touching_clean = check_realizability_touching(&owner_graph, &partition, module_id(3));
    assert!(
        touching_clean.is_realizable(),
        "module 3 touches no diagnosis; pre-existing violations \
         elsewhere must not surface: {touching_clean:#?}",
    );
}

/// Clause-2 side of the touching filter: a cross-module rebind is
/// kept iff the queried module is one of its endpoints.
#[test]
fn touching_filter_keeps_only_cross_rebinds_at_the_queried_module() {
    // owner_0: let a = 1 (residual). owner_1: a = 2 (mod 1 — a
    // cross-module top-level rebinding write). owner_2: const z
    // (mod 2, unrelated).
    let source = "let a = 1; a = 2; const z = 3;";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1));
    partition.set(OwnerId(2), module_id(2));
    let full = check_realizability(&owner_graph, &partition);
    assert!(
        !full.cross_rebinds.is_empty(),
        "fixture must produce a cross-module rebind: {full:#?}",
    );
    let touching_writer = check_realizability_touching(&owner_graph, &partition, module_id(1));
    assert!(
        !touching_writer.cross_rebinds.is_empty(),
        "module 1 is the rebind's writer side: {touching_writer:#?}",
    );
    let touching_clean = check_realizability_touching(&owner_graph, &partition, module_id(2));
    assert!(
        touching_clean.is_realizable(),
        "module 2 is on neither rebind endpoint: {touching_clean:#?}",
    );
}

/// A pure lazy-read cycle (mutual references inside function
/// bodies) is realizable: ESM evaluates the lazy side first, no
/// TDZ. Verdict must be empty even when the modules form a cycle
/// in the *full* quotient.
#[test]
fn pure_lazy_cycle_is_realizable() {
    let source = "function a() { return b(); } function b() { return a(); }";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        verdict.is_realizable(),
        "lazy-only cycle should be realizable: {verdict:#?}"
    );
}

/// Asymmetric I-cycle `{mod_dep, mod_dependent}` with eager
/// `mod_dependent → mod_dep` and lazy `mod_dep → mod_dependent`.
/// Residual (`module_id(0)`) at-init-reads both, so residual has
/// I-edges into the SCC and Lemma 2 rescues — the simulator's
/// post-order puts mod_dep's body before mod_dependent's body.
/// Verdict must be empty.
#[test]
fn lemma_two_rescues_asymmetric_cycle_when_residual_imports_scc() {
    // owner_0 (residual): const a = 1; (also reads b, lazy_reader at-init via console.log)
    // owner_1 (mod_dep): const dep_value = "alpha"
    // owner_2 (mod_dep): function lazy_reader() { return cross_value; }
    // owner_3 (mod_dependent): const cross_value = dep_value + "-beta"
    // owner_4 (residual): console.log reads dep_value, cross_value, lazy_reader at-init
    let source = "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; function lazy_reader() { return cross_value; } console.log(dep_value, cross_value, lazy_reader());";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    // dep_value (owner 0) → mod_dep, cross_value (owner 1) →
    // mod_dependent, lazy_reader (owner 2) → mod_dep,
    // console.log (owner 3) stays in residual (= module_id(0)).
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(1), module_id(2));
    partition.set(OwnerId(2), module_id(1));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        verdict.is_realizable(),
        "Lemma 2 should rescue this shape; verdict: {verdict:#?}",
    );
}

/// Same SCC shape but residual's own statements have NO direct
/// I-edge into the SCC — they reach it only through
/// `mod_mediator`. Still realizable: the emitted entry imports
/// EVERY logical module (not just the ones residual's statements
/// reference), in Lemma 2's source-import order, so ESM DFS
/// enters the SCC at `mod_dependent` (the dependent) before the
/// mediator's dependency-first imports could reach it at
/// `mod_dep`. The simulator's universal residual fan-out models
/// this; the matching Node-anchored pin is
/// `e2e/mediator_reaches_asymmetric_cycle_test` (the emitted
/// output runs cleanly and prints the mediator-derived value).
///
/// Simulated post-order: `mod_dep` → `mod_dependent` →
/// `mod_mediator` → residual; the constraining pair
/// `(mod_dependent → mod_dep)` is satisfied.
#[test]
fn mediator_only_entrant_into_asymmetric_cycle_is_rescued_by_entry_imports() {
    // owner_0: const dep_value = "alpha"
    // owner_1: const cross_value = dep_value + "-beta"
    // owner_2: function lazy_reader() { return cross_value; }
    // owner_3: function mediator_helper() { return dep_value + lazy_reader(); }
    // owner_4: const mediator_init = mediator_helper(); (at-init promotes
    //          to a constraining edge into the dep_value owner —
    //          mediator → mod_dep eager)
    // owner_5: console.log(mediator_init); (residual at-init)
    let source = "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; function lazy_reader() { return cross_value; } function mediator_helper() { return dep_value + lazy_reader(); } const mediator_init = mediator_helper(); console.log(mediator_init);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1)); // dep_value → mod_dep
    partition.set(OwnerId(1), module_id(2)); // cross_value → mod_dependent
    partition.set(OwnerId(2), module_id(1)); // lazy_reader → mod_dep
    partition.set(OwnerId(3), module_id(3)); // mediator_helper → mod_mediator
    partition.set(OwnerId(4), module_id(3)); // mediator_init → mod_mediator
    // owner_5 (console.log) stays in residual.
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        verdict.is_realizable(),
        "entry's universal per-plan imports DFS into the SCC at the \
         dependent first (Lemma 2); the mediator path never wins. \
         verdict: {verdict:#?}",
    );
}

/// Regression test for the gaffer over-rejection. Asymmetric
/// I-cycle where residual's own statements reach the SCC only
/// via the constraining edge's **target** (the dependency), not
/// the source (the dependent).
///
/// Shape (gaffer's `domains/system/ids` ↔ `domains/system/schemas`
/// minimal repro):
///   - `mod_schemas` owns `schemas_target` (the eager-read target)
///     and `lazy_back` (whose body lazily references `ids_val`).
///   - `mod_ids` owns `ids_val`, whose initializer eager-reads
///     `schemas_target` from `mod_schemas`.
///   - residual reads ONLY `schemas_target` — no direct
///     reference to `ids_val`.
///
/// I-graph cross-module edges:
///   - `mod_ids → mod_schemas` `EagerUse(schemas_target)` (forward, constraining)
///   - `mod_schemas → mod_ids` `LazyUse(ids_val)` (back, non-constraining)
///   - `residual → mod_schemas` `EagerUse(schemas_target)` (constraining)
///
/// I-graph SCC: `{mod_ids, mod_schemas}`. Residual is NOT in the
/// SCC; residual's statements only reference `mod_schemas`.
///
/// The historical over-rejection: the simulator modeled residual's
/// DFS fan-out as only the modules residual's statements
/// reference, entered the SCC at `mod_schemas`, followed the
/// emitted lazy-read import back to `mod_ids`, and flagged
/// `post_order[mod_schemas] > post_order[mod_ids]` as TDZ. The
/// emitted entry, however, has always imported every plan —
/// `mod_ids` included — in Lemma 2's source-import order, which
/// puts the dependent `mod_ids` first; the runtime DFS unwinds
/// through `mod_schemas` and evaluates it before `mod_ids`. The
/// simulator now models the entry's universal imports and
/// accepts.
#[test]
fn pass_two_simulator_models_entry_universal_imports_for_runtime_dfs() {
    // owner_0: const schemas_target = "v"     (mod_schemas)
    // owner_1: function lazy_back() { return ids_val; }
    //                                         (mod_schemas; lazy_use ids_val)
    // owner_2: const ids_val = schemas_target (mod_ids; eager_use
    //          schemas_target — a PURE initializer, so no
    //          sequenced edges hand residual an incidental
    //          direct edge to mod_ids)
    // owner_3: console.log(schemas_target);   (residual; eager_use schemas_target)
    let source = "const schemas_target = \"v\"; function lazy_back() { return ids_val; } const ids_val = schemas_target; console.log(schemas_target);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1)); // schemas_target → mod_schemas
    partition.set(OwnerId(1), module_id(1)); // lazy_back     → mod_schemas
    partition.set(OwnerId(2), module_id(2)); // ids_val       → mod_ids
    // owner_3 (console.log) stays in residual.
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        verdict.is_realizable(),
        "gaffer-shape asymmetric cycle must accept: entry imports \
         every plan in Lemma 2's source-import order, so the runtime \
         DFS enters the SCC at mod_ids (the dependent) and evaluates \
         mod_schemas first. verdict: {verdict:#?}",
    );
}

/// Differential pin: the simulator's predicted Phase-2 post-order
/// for the gaffer shape equals the evaluation order Node produces
/// for the emitted tree. The Node side is pinned by
/// `e2e/asymmetric_non_residual_cycle_test::`
/// `dependency_only_residual_reference_into_asymmetric_cycle_runs_under_node`
/// — the same shape, which TDZ-crashes under Node unless
/// `mod_schemas`' body evaluates before `mod_ids`'. Emitter and
/// simulator both consume `EsmImportOrder`, so this pin guards
/// the shared-ordering contract from the gate side.
///
/// This pin keeps the hand-derived expected order at the unit level;
/// `e2e/simulator_node_differential_sweep_test` generalizes it into a
/// live differential (simulator prediction vs instrumented Node run)
/// across the accepted asymmetric / phantom / tie-break family.
#[test]
fn simulator_post_order_matches_emitted_evaluation_order() {
    // owner_0: const schemas_target = "v"      (mod_schemas)
    // owner_1: function lazy_back() { return ids_val; } (mod_schemas)
    // owner_2: const ids_val = schemas_target  (mod_ids)
    // owner_3: console.log(schemas_target)     (residual)
    let source = "const schemas_target = \"v\"; function lazy_back() { return ids_val; } const ids_val = schemas_target; console.log(schemas_target);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1)); // schemas_target → mod_schemas
    partition.set(OwnerId(1), module_id(1)); // lazy_back     → mod_schemas
    partition.set(OwnerId(2), module_id(2)); // ids_val       → mod_ids
    // owner_3 (console.log) stays in residual.
    let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
    let pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
    let simulator =
        EsmEvaluationSimulator::build(&canonical.i_successors, &pairs, partition.residual());
    // Node evaluates: mod_schemas body, mod_ids body, then the
    // entry (residual) body — entry's imports are
    // [mod_ids, mod_schemas] (intra-SCC reversal), DFS unwinds
    // through mod_schemas first, and the root body is last.
    let expected: BTreeMap<ModuleId, usize> =
        [(module_id(1), 0), (module_id(2), 1), (module_id(0), 2)]
            .into_iter()
            .collect();
    assert_eq!(
        simulator.post_order, expected,
        "simulated post-order must match the emitted tree's actual Node evaluation order",
    );
}

/// Residual is the source of a constraining edge into the SCC,
/// but the SCC also has a constraining-target-residual edge.
/// Lemma 2 fails: residual is the DFS root and evaluates last in
/// post-order; the SCC member reading residual's binding TDZs.
#[test]
fn constraining_edge_into_residual_inside_scc_is_unrealizable() {
    // owner_0: class Backend { ... } (residual, TDZ-locked target)
    // owner_1: let currentLogger; (mod_logger)
    // owner_2: function setLogger(impl) { currentLogger = impl; ... } (mod_logger)
    // owner_3: setLogger(new Backend()); (mod_logger, at-init reads Backend)
    // owner_4: console.log(currentLogger.tag); (residual, lazy read of currentLogger from mod_logger via re-export)
    let source = "class Backend { constructor() { this.tag = \"B\"; } } let currentLogger; function setLogger(impl) { currentLogger = impl; globalThis.__tag = impl.tag; } setLogger(new Backend()); console.log(currentLogger);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    // Backend (owner 0) stays in residual.
    partition.set(OwnerId(1), module_id(1)); // currentLogger → mod_logger
    partition.set(OwnerId(2), module_id(1)); // setLogger → mod_logger
    partition.set(OwnerId(3), module_id(1)); // setLogger(new Backend()) → mod_logger
    // owner 4 (console.log) stays in residual.
    let verdict = check_realizability(&owner_graph, &partition);
    // mod_logger → residual EagerUse (constraining target = residual)
    // residual → mod_logger LazyUse (re-export / console.log)
    // SCC = {residual, mod_logger}. Constraining edge target = residual.
    // Residual is DFS root; mod_logger body runs first, reads Backend → TDZ.
    assert!(
        !verdict.is_realizable(),
        "constraining edge target=residual must TDZ; verdict: {verdict:#?}",
    );
}

/// Namespace-aggregator split: a module-level `const ids = {...sub1, ...sub2}`
/// gets sub1 and sub2 extracted into separate modules. The aggregator's
/// initializer carries at-init reads of sub1 and sub2 (the spread RHS
/// reads them). If a sub-module also reads back into the residual or
/// aggregator at-init, the resulting cross-module SCC must be detected
/// by the gate or the emitted ESM will TDZ at runtime under Node.
///
/// Shape used here: sub1 reads `seed` declared in residual at-init;
/// residual reads `ids` at-init. Cycle:
///   residual --EagerUse--> mod_ids   (`const consumed = ids.foo`)
///   mod_ids  --EagerUse--> mod_sub1  (`const ids = {...sub1, ...sub2}`)
///   mod_sub1 --EagerUse--> residual  (`const sub1 = { foo: seed }`)
/// The gate must reject this partition.
#[test]
fn namespace_aggregator_with_back_edge_through_sub_is_unrealizable() {
    // owner_0: const seed = "S"           (residual)
    // owner_1: const sub1 = { foo: seed }  (mod_sub1) — eager_read of seed
    // owner_2: const sub2 = { bar: 1 }     (mod_sub2) — no cross-module reads
    // owner_3: const ids = {...sub1, ...sub2} (mod_ids) — eager reads sub1, sub2
    // owner_4: const consumed = ids.foo + ids.bar (residual) — eager read of ids
    let source = "const seed = \"S\"; const sub1 = { foo: seed }; const sub2 = { bar: 1 }; const ids = {...sub1, ...sub2}; const consumed = ids.foo + ids.bar; console.log(consumed);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1)); // sub1 → mod_sub1
    partition.set(OwnerId(2), module_id(2)); // sub2 → mod_sub2
    partition.set(OwnerId(3), module_id(3)); // ids  → mod_ids
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        !verdict.is_realizable(),
        "namespace-aggregator split with sub→residual back edge \
         must be flagged by the gate; verdict: {verdict:#?}",
    );
}

/// Same aggregator shape but with sub1 and sub2 INDEPENDENT of residual
/// (pure literal initializers). The split is realizable: ESM evaluates
/// sub1, sub2, then ids, then residual.
#[test]
fn namespace_aggregator_with_pure_subs_is_realizable() {
    // owner_0: const sub1 = { foo: 1 }
    // owner_1: const sub2 = { bar: 2 }
    // owner_2: const ids = {...sub1, ...sub2}
    // owner_3: console.log(ids)
    let source = "const sub1 = { foo: 1 }; const sub2 = { bar: 2 }; const ids = {...sub1, ...sub2}; console.log(ids);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1)); // sub1 → mod_sub1
    partition.set(OwnerId(1), module_id(2)); // sub2 → mod_sub2
    partition.set(OwnerId(2), module_id(3)); // ids  → mod_ids
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        verdict.is_realizable(),
        "pure namespace-aggregator split must be realizable; verdict: {verdict:#?}",
    );
}

/// **RED regression test** for the namespace-aggregator TDZ hole.
///
/// The cycle goes through a *promoted* edge — the sub-module's at-init
/// `readSeed()` call has its body's read of `seed` (in residual) promoted
/// to a sub→residual eager edge. The lenient projection view
/// (`EndpointView::Lenient`) drops it under
/// `EdgeRole::is_cross_module_promotion` because the call target
/// `readSeed` lives in `mod_helpers`, not `mod_sub1`. With the
/// drop, the gate sees no cycle. Without the drop, the cycle
/// `residual→mod_ids→mod_sub1→residual` is closed.
///
/// ESM runtime DFS from residual:
///   residual → mod_ids → mod_sub1 → mod_helpers (eval helpers)
///                                 → residual (on stack, skip).
///   Post-order: helpers, then mod_sub1.
///   When `mod_sub1`'s body evaluates `readSeed()`, the call reads
///   `seed` from residual — residual is mid-DFS, `seed` is TDZ-locked.
///   ⇒ `ReferenceError: Cannot access 'seed' before initialization`.
///
/// The gate-side view (`EndpointView::Gate`) keeps the promoted
/// edge so the cycle is detected; the test pins that behaviour.
#[test]
fn promoted_edge_in_aggregator_cycle_is_unrealizable() {
    // owner_0: const seed = "S"                  (residual)
    // owner_1: const readSeed = () => seed       (mod_helpers)
    // owner_2: const sub1 = { foo: readSeed() }  (mod_sub1) — at-init call into mod_helpers
    // owner_3: const ids = sub1.foo + "x"        (mod_ids)
    // owner_4: const consumed = ids              (residual)
    let source = "const seed = \"S\"; const readSeed = () => seed; const sub1 = { foo: readSeed() }; const ids = sub1.foo + \"x\"; const consumed = ids; console.log(consumed);";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(1), module_id(1)); // readSeed → mod_helpers
    partition.set(OwnerId(2), module_id(2)); // sub1 → mod_sub1
    partition.set(OwnerId(3), module_id(3)); // ids → mod_ids
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(
        !verdict.is_realizable(),
        "promoted-edge aggregator cycle must be flagged by the gate \
         (mod_sub1's readSeed() at-init call reads `seed` in residual; \
         residual reads `ids` in mod_ids; mod_ids reads `sub1` in \
         mod_sub1 — closes a cycle through the promoted edge); \
         verdict: {verdict:#?}",
    );
}

/// All owners in the same module → no cross-destination edges of
/// any kind → empty verdict.
#[test]
fn single_module_is_always_realizable() {
    let source = "const a = 1; const b = a + 1; const c = a * b;";
    let owner_graph = parse_and_build(source);
    let partition = Partition::new(&owner_graph, module_id(0));
    let verdict = check_realizability(&owner_graph, &partition);
    assert!(verdict.is_realizable());
}

/// Pushing a delta on the index and reading the verdict matches
/// the pure function on the post-push partition. Undo restores the
/// pre-push verdict exactly.
#[test]
fn index_push_undo_roundtrips_verdict() {
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);

    let baseline = Partition::new(&owner_graph, module_id(0));
    let baseline_verdict = check_realizability(&owner_graph, &baseline);
    assert!(baseline_verdict.is_realizable());

    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());
    let handle = index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    // After push: matches the explicitly-built post-delta partition.
    let mut hypothetical = baseline.clone();
    hypothetical.set(OwnerId(1), module_id(1));
    let hypothetical_verdict = check_realizability(&owner_graph, &hypothetical);
    assert_eq!(
        index.verdict().unrealizable_sccs.len(),
        hypothetical_verdict.unrealizable_sccs.len(),
    );
    assert!(!index.verdict().is_realizable());

    index.undo(&owner_graph, handle);
    // After undo: matches the baseline exactly.
    assert!(index.verdict().is_realizable());
    for owner_id in 0..owner_graph.num_nodes() {
        assert_eq!(
            index.partition().of(OwnerId(owner_id)),
            baseline.of(OwnerId(owner_id)),
            "partition slot {owner_id} should be restored by undo",
        );
    }
}

#[test]
fn duplicate_owner_ids_are_journaled_once() {
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());

    let handle = index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1), OwnerId(1)],
            to: module_id(1),
        },
    );
    assert_eq!(index.partition().of(OwnerId(1)), module_id(1));

    index.undo(&owner_graph, handle);
    assert_eq!(index.partition().of(OwnerId(1)), baseline.of(OwnerId(1)));
    assert_eq!(
        normalize_verdict(index.verdict()),
        normalize_verdict(check_realizability(&owner_graph, &baseline)),
    );
}

#[test]
fn commit_drops_journal_state_and_index_stays_queryable() {
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    // Permanent push (the commit_merge shape: no matching undo).
    index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    let committed = normalize_verdict(index.verdict());
    index.commit();

    // Committed state is intact, and subsequent scoped
    // speculative work (push + undo) still balances correctly
    // against the new journal baseline.
    assert_eq!(index.partition().of(OwnerId(1)), module_id(1));
    assert_eq!(normalize_verdict(index.verdict()), committed);
    index.scoped(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(0)],
            to: module_id(2),
        },
        |idx| idx.verdict(),
    );
    assert_eq!(normalize_verdict(index.verdict()), committed);
    assert!(
        index.journal.is_empty(),
        "scoped work must not leak entries"
    );
}

#[test]
fn move_overlay_matches_scoped_verdict_touching() {
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());
    let before = normalize_verdict(index.verdict());

    let overlay =
        index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(1));
    let scoped = index.scoped(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
        |idx| idx.verdict_touching(module_id(1)),
    );

    assert_eq!(normalize_verdict(overlay), normalize_verdict(scoped));
    assert_eq!(
        normalize_verdict(index.verdict()),
        before,
        "overlay query must not mutate the working partition",
    );
    assert_eq!(index.partition().of(OwnerId(1)), baseline.of(OwnerId(1)));
}

#[test]
fn move_overlay_reports_cross_rebinds_like_scoped_verdict() {
    let source = "let a = 0; function b() { a = 1; }";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    let overlay =
        index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(1));
    let scoped = index.scoped(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
        |idx| idx.verdict_touching(module_id(1)),
    );

    assert_eq!(
        normalize_verdict(overlay.clone()),
        normalize_verdict(scoped)
    );
    assert!(overlay.unrealizable_sccs.is_empty());
    assert_eq!(overlay.cross_rebinds.len(), 1);
}

#[test]
fn move_overlay_masks_removed_current_edges() {
    let source = "const a = b + 1; const b = c + 1; const c = 1;";
    let owner_graph = parse_and_build(source);
    let mut baseline = Partition::new(&owner_graph, module_id(0));
    baseline.set(OwnerId(0), module_id(1));
    baseline.set(OwnerId(1), module_id(2));
    baseline.set(OwnerId(2), module_id(3));
    let mut explicit = baseline.clone();
    explicit.set(OwnerId(1), module_id(4));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    let overlay =
        index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(4));
    let scoped = index.scoped(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(4),
        },
        |idx| idx.verdict_touching(module_id(4)),
    );
    let pure = filter_verdict_touching(&check_realizability(&owner_graph, &explicit), module_id(4));

    assert_eq!(
        normalize_verdict(overlay.clone()),
        normalize_verdict(scoped)
    );
    assert_eq!(normalize_verdict(overlay), normalize_verdict(pure));
}

/// `scoped` runs the closure with the delta applied and undoes on
/// return — even when the closure returns a value.
#[test]
fn index_scoped_isolates_per_call_state() {
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);

    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());

    let inside_verdict_realizable = index.scoped(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
        |idx| idx.verdict().is_realizable(),
    );
    assert!(
        !inside_verdict_realizable,
        "inside the scope the cycle exists"
    );

    // After scoped: state restored exactly.
    assert!(index.verdict().is_realizable());
    assert_eq!(index.partition().of(OwnerId(1)), module_id(0));
}

#[test]
fn incremental_index_matches_pure_verdict_through_nested_push_undo() {
    let source = "const a = b + 1; const b = a + 1; function c() { return a; }";
    let owner_graph = parse_and_build(source);

    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut explicit = baseline.clone();
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());

    assert_eq!(
        normalize_verdict(index.verdict()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );

    let first = index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    explicit.set(OwnerId(1), module_id(1));
    assert_eq!(
        normalize_verdict(index.verdict()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );

    let second = index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(2)],
            to: module_id(2),
        },
    );
    explicit.set(OwnerId(2), module_id(2));
    assert_eq!(
        normalize_verdict(index.verdict()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );

    index.undo(&owner_graph, second);
    explicit.set(OwnerId(2), module_id(0));
    assert_eq!(
        normalize_verdict(index.verdict()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );

    index.undo(&owner_graph, first);
    explicit.set(OwnerId(1), module_id(0));
    assert_eq!(
        normalize_verdict(index.verdict()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );
    for owner in 0..owner_graph.num_nodes() {
        assert_eq!(
            index.partition().of(OwnerId(owner)),
            baseline.of(OwnerId(owner))
        );
    }
}

#[test]
fn verdict_touching_matches_full_verdict_filtered_to_module() {
    let source = "const a = b + 1; const b = a + 1; const c = 1;";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);
    index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(2)],
            to: module_id(2),
        },
    );

    let full = index.verdict();
    assert_eq!(
        normalize_verdict(index.verdict_touching(module_id(1))),
        normalize_verdict(filter_verdict_touching(&full, module_id(1))),
    );
    assert_eq!(
        normalize_verdict(index.verdict_touching(module_id(2))),
        normalize_verdict(filter_verdict_touching(&full, module_id(2))),
        "unrelated module should not inherit the a/b SCC",
    );
}

#[test]
fn incremental_index_reports_cross_rebinds_without_scc_edges() {
    let source = "let a = 0; function b() { a = 1; }";
    let owner_graph = parse_and_build(source);
    let baseline = Partition::new(&owner_graph, module_id(0));
    let mut explicit = baseline.clone();
    let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

    index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    explicit.set(OwnerId(1), module_id(1));

    let verdict = index.verdict();
    assert_eq!(
        normalize_verdict(verdict.clone()),
        normalize_verdict(check_realizability(&owner_graph, &explicit)),
    );
    assert!(
        verdict.unrealizable_sccs.is_empty(),
        "rebinds are direct violations, not SCC edges: {verdict:#?}",
    );
    assert_eq!(verdict.cross_rebinds.len(), 1);
    assert_eq!(
        normalize_verdict(index.verdict_touching(module_id(1))),
        normalize_verdict(verdict),
    );
}

type NormalizedVerdict = (
    BTreeSet<(Vec<ModuleId>, Vec<usize>)>,
    BTreeSet<(ModuleId, ModuleId, usize)>,
);

fn normalize_verdict(verdict: RealizabilityVerdict) -> NormalizedVerdict {
    let sccs = verdict
        .unrealizable_sccs
        .into_iter()
        .map(|scc| {
            let modules: Vec<ModuleId> = scc.modules.into_iter().collect();
            let edges: Vec<usize> = scc
                .constraining_owner_edges
                .into_iter()
                .map(|edge| edge.0)
                .collect();
            (modules, edges)
        })
        .collect();
    let rebinds = verdict
        .cross_rebinds
        .into_iter()
        .map(|rebind| (rebind.from, rebind.to, rebind.owner_edge.0))
        .collect();
    (sccs, rebinds)
}

fn filter_verdict_touching(
    verdict: &RealizabilityVerdict,
    module: ModuleId,
) -> RealizabilityVerdict {
    RealizabilityVerdict {
        unrealizable_sccs: verdict
            .unrealizable_sccs
            .iter()
            .filter(|scc| scc.modules.contains(&module))
            .cloned()
            .collect(),
        cross_rebinds: verdict
            .cross_rebinds
            .iter()
            .filter(|rebind| rebind.from == module || rebind.to == module)
            .cloned()
            .collect(),
    }
}

/// Reach inside the `RealizabilityIndex` to assert that the
/// `IncrementalQuotient`'s cached base simulator (when populated)
/// matches a from-scratch `EsmEvaluationSimulator::build` against
/// the live `i_graph` + `constraining_buckets`. Lives in the
/// realizability.rs `mod tests` so it can name the private types
/// (`IncrementalQuotient`, `EsmEvaluationSimulator`).
fn assert_cached_simulator_matches_rebuild(index: &RealizabilityIndex, label: &str, phase: &str) {
    let quotient = &index.quotient;
    // Materialize the same inputs `EsmEvaluationSimulator::build`
    // would have walked from scratch, bypassing the cache so a
    // bug in the cached-input path can't mask a divergence here.
    let mut i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
    for (from, to) in quotient.i_graph.edge_pairs() {
        i_successors.entry(from).or_default().insert(to);
    }
    let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> =
        quotient.constraining_buckets.keys().copied().collect();
    let rebuilt =
        EsmEvaluationSimulator::build(&i_successors, &constraining_pairs, quotient.residual);
    // Force the cache to populate (verdict() takes the base path).
    let cached = quotient.base_simulator().clone();
    assert_eq!(
        cached, rebuilt,
        "{label}: cached base simulator diverges from rebuild ({phase})",
    );

    // Property: the cached `(i_successors, constraining_pairs)`
    // inputs must match the from-scratch walk too. This pins the
    // overlay path's clone-and-patch correctness — overlay queries
    // mutate these cached snapshots, and a mismatched base would
    // taint every overlay query.
    let (cached_inputs_succs, cached_inputs_pairs) = quotient.effective_simulator_inputs(None);
    let mut fresh_succs: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
    for (from, to) in quotient.i_graph.edge_pairs() {
        fresh_succs.entry(from).or_default().insert(to);
    }
    let fresh_pairs: BTreeSet<(ModuleId, ModuleId)> =
        quotient.constraining_buckets.keys().copied().collect();
    assert_eq!(
        cached_inputs_succs, fresh_succs,
        "{label}: cached base i_successors diverges from rebuild ({phase})",
    );
    assert_eq!(
        cached_inputs_pairs, fresh_pairs,
        "{label}: cached base constraining pairs diverges from rebuild ({phase})",
    );
}

/// Property test pinning the incremental simulator cache to its
/// from-scratch correctness reference. For each fixture, applies
/// an arbitrary sequence of `MoveOwners` deltas through the
/// `RealizabilityIndex`, asserting after every push and every
/// undo that the `IncrementalQuotient`'s cached
/// `EsmEvaluationSimulator` byte-equals what
/// `EsmEvaluationSimulator::build(...)` would produce against the
/// current `i_graph` / `constraining_buckets`. Also asserts the
/// cached `(i_successors, constraining_pairs)` snapshots match.
///
/// Initially RED before the cache is wired to invalidate on edge
/// mutations; GREEN once `add_current_edge` /
/// `remove_current_edge` / `rollback_graphs` all drop the cache.
#[test]
fn incremental_simulator_matches_rebuild_after_each_delta() {
    struct Fixture {
        label: &'static str,
        source: &'static str,
        deltas: Vec<(Vec<usize>, usize)>,
    }
    let fixtures = vec![
        // Two-cycle plus a lazy bystander.
        Fixture {
            label: "two_eager_plus_lazy",
            source: "const a = b + 1; const b = a + 1; function c() { return a; }",
            deltas: vec![(vec![1], 1), (vec![2], 2), (vec![1, 2], 3)],
        },
        // Asymmetric I-cycle with a residual mediator.
        Fixture {
            label: "asymmetric_with_mediator",
            source: "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; \
                     function lazy_reader() { return cross_value; } \
                     function mediator_helper() { return dep_value + lazy_reader(); } \
                     const mediator_init = mediator_helper(); console.log(mediator_init);",
            deltas: vec![(vec![0, 2], 1), (vec![1], 2), (vec![3, 4], 3)],
        },
        // Cross-destination rebind — exercises the rebind-only
        // overlay code path (no simulator change).
        Fixture {
            label: "rebind_then_unmove",
            source: "let a = 0; function b() { a = 1; }",
            deltas: vec![(vec![1], 1), (vec![1], 0)],
        },
        // Single-module fixture (no cross-module edges → simulator
        // input set stays empty across all deltas).
        Fixture {
            label: "single_module",
            source: "const a = 1; const b = a + 1; const c = a * b;",
            deltas: vec![(vec![1], 1), (vec![2], 1), (vec![1, 2], 0)],
        },
    ];
    for fixture in fixtures {
        let owner_graph = parse_and_build(fixture.source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);
        assert_cached_simulator_matches_rebuild(&index, fixture.label, "initial");
        let mut handles: Vec<DeltaHandle> = Vec::new();
        for (owner_indices, dest) in &fixture.deltas {
            let owners: Vec<OwnerId> = owner_indices.iter().copied().map(OwnerId).collect();
            let handle = index.push(
                &owner_graph,
                PartitionDelta::MoveOwners {
                    owners,
                    to: module_id(*dest),
                },
            );
            handles.push(handle);
            assert_cached_simulator_matches_rebuild(&index, fixture.label, "after-push");
            // verdict() pulls through the cached simulator; assert
            // it stays consistent with `check_realizability`.
            let projected = index.partition().clone();
            assert_eq!(
                normalize_verdict(index.verdict()),
                normalize_verdict(check_realizability(&owner_graph, &projected)),
                "{}: verdict diverged from check_realizability after push",
                fixture.label,
            );
        }
        while let Some(handle) = handles.pop() {
            index.undo(&owner_graph, handle);
            assert_cached_simulator_matches_rebuild(&index, fixture.label, "after-undo");
            let projected = index.partition().clone();
            assert_eq!(
                normalize_verdict(index.verdict()),
                normalize_verdict(check_realizability(&owner_graph, &projected)),
                "{}: verdict diverged from check_realizability after undo",
                fixture.label,
            );
        }
    }
}

// ---------------------------------------------------------------------
// Gate-ladder tests (plans/incremental_gate_unification.md §3; PR 3
// of §8): the tier-laddered boolean must equal the evidence-producing
// overlay verdict on every move query, with each fixture pinning the
// tier expected to decide it.
// ---------------------------------------------------------------------

/// Assert the ladder, the boolean wrapper, and the overlay verdict
/// agree for one move query; return the decision for tier pinning.
fn assert_ladder_matches_verdict(
    index: &RealizabilityIndex,
    owner_graph: &OwnerGraph,
    owners: &[OwnerId],
    to: ModuleId,
) -> LadderDecision {
    let decision = index.ladder_decision_after_moving_owners_touching(owner_graph, owners, to);
    let verdict = index.verdict_after_moving_owners_touching(owner_graph, owners, to);
    assert_eq!(
        decision.accepts(),
        verdict.is_realizable(),
        "ladder {decision:?} diverges from the overlay verdict for move \
         {owners:?} → {to:?}: {verdict:#?}",
    );
    assert_eq!(
        decision.accepts(),
        index.would_remain_realizable_after_moving_owners_touching(owner_graph, owners, to),
    );
    decision
}

#[test]
fn ladder_tier0_delta_free_move_accepts_on_clean_state() {
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    // Owner 1 already lives in module 0 — the move is delta-free.
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(0));
    assert_eq!(decision, LadderDecision::DeltaFreeAccept);
}

#[test]
fn ladder_tier0_delta_free_move_rejects_on_dirty_pre_state() {
    // Mutual constraining cycle committed between modules 1 and 2; a
    // delta-free move touching module 1 must reject (post == pre, and
    // the pre-state touching verdict is dirty).
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(1), module_id(2));
    let index = RealizabilityIndex::from_partition(&owner_graph, partition);
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(0)], module_id(1));
    assert_eq!(decision, LadderDecision::DeltaFreeReject);
}

#[test]
fn ladder_tier1_rejects_constraining_cycle_move() {
    // Moving `b` out of residual closes the mutual eager 2-cycle —
    // a Pass-1 reject the constraining condensation decides.
    let source = "const a = b + 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::ConstrainingCycleReject);
}

#[test]
fn ladder_tier1_rejects_cross_rebind_move() {
    // Moving the writer out of residual turns the intra-module rebind
    // into a clause-2 cross-module rebinding write.
    let source = "let a = 0; function b() { a = 1; }";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::CrossRebindReject);
}

#[test]
fn ladder_tier2_accepts_acyclic_cross_module_move() {
    // The move adds one constraining edge and closes nothing — the
    // I-condensation proves Pass 2 vacuous without a simulator build.
    let source = "const a = 1; const b = a + 1;";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::NoMultiModuleISccAccept);
}

#[test]
fn ladder_tier2_accepts_pure_lazy_cycle_move() {
    // The move closes a pure-lazy I-cycle: multi-module I-SCC with no
    // constraining pair inside — Lemma 2 says it never TDZs.
    let source = "function a() { return b(); } function b() { return a(); }";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::NoConstrainingPairAccept);
}

#[test]
fn ladder_tier3_accepts_lemma_two_rescued_move() {
    // The `lemma_two_rescues_asymmetric_cycle...` shape reached via a
    // speculative move: dep_value + lazy_reader sit in mod 1; moving
    // cross_value to mod 2 closes the asymmetric I-SCC {1, 2} with a
    // constraining pair, so the ladder must run the simulator — which
    // rescues (Lemma 2).
    let source = "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; function lazy_reader() { return cross_value; } console.log(dep_value, cross_value, lazy_reader());";
    let owner_graph = parse_and_build(source);
    let mut partition = Partition::new(&owner_graph, module_id(0));
    partition.set(OwnerId(0), module_id(1));
    partition.set(OwnerId(2), module_id(1));
    let index = RealizabilityIndex::from_partition(&owner_graph, partition);
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(2));
    assert_eq!(decision, LadderDecision::SimulatorAccept);
}

#[test]
fn ladder_tier3_rejects_tdz_move() {
    // Asymmetric I-SCC with the constraining edge pointing INTO
    // residual (the `constraining_edge_into_residual_inside_scc`
    // shape, reached via a move): residual is the DFS root and
    // evaluates last, so the moved statement's eager read of `seed`
    // TDZs. Pass 1 is clean (one constraining direction) and the
    // I-SCC carries a constraining pair, so only tier 3 can decide.
    let source = "const seed = 1; const x = seed + 1; function readX() { return x; }";
    let owner_graph = parse_and_build(source);
    let index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let decision = assert_ladder_matches_verdict(&index, &owner_graph, &[OwnerId(1)], module_id(1));
    assert_eq!(decision, LadderDecision::SimulatorReject);
}

/// Condensation-order maintenance: the ladder stays equal to the
/// overlay verdict across committed pushes (incremental edge
/// insert/remove), `commit`, and `undo` (invalidate + lazy rebuild,
/// plan §4's journal interaction).
#[test]
fn ladder_matches_verdict_across_push_commit_undo() {
    let source = "const a = b + 1; const b = a + 1; function c() { return a; }";
    let owner_graph = parse_and_build(source);
    let mut index = RealizabilityIndex::from_partition(
        &owner_graph,
        Partition::new(&owner_graph, module_id(0)),
    );
    let sweep = |index: &RealizabilityIndex| {
        for owner in 0..owner_graph.num_nodes() {
            for module in 0..4 {
                assert_ladder_matches_verdict(
                    index,
                    &owner_graph,
                    &[OwnerId(owner)],
                    module_id(module),
                );
            }
        }
    };
    sweep(&index);
    index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        },
    );
    sweep(&index);
    index.commit();
    sweep(&index);
    let speculative = index.push(
        &owner_graph,
        PartitionDelta::MoveOwners {
            owners: vec![OwnerId(2)],
            to: module_id(2),
        },
    );
    sweep(&index);
    index.undo(&owner_graph, speculative);
    sweep(&index);
}
