//! Integration tests for the `quotient` kernel and the `propose`
//! renderer-over-quotient. Fixtures seed quotients through the
//! test-only constructors in `quotient::testing`.

use std::collections::BTreeMap;

use analysis::{DepKind, DependencyGraphReport, ModuleId};
use report_fixtures::{
    active_source_unit, atomic_edge, atomic_group_for, claims, dependency_edge, graph_of,
    promoted_dependency_edge, residual_source_unit, singleton_graph,
};

use crate::propose::{ModuleProposal, propose};
use crate::quotient::testing::{
    PartitionGroup, greedy_merge_to_convergence_full_scan, module_group,
};
use crate::quotient::{
    ClassId, CycleClassSet, CycleEvidence, QuotientGraph, SeedContractionRejected, SourceUnitIndex,
    SpecModuleGroup, build_seed_quotient, greedy_merge_to_convergence,
};

fn spec_module(module_id: &str, source_units: &[&str]) -> SpecModuleGroup {
    SpecModuleGroup {
        module_id: module_id.into(),
        source_unit_ids: source_units.iter().map(|id| (*id).into()).collect(),
    }
}

// ---------- Tests. ----------

/// Moving a residual owner into an active module changes import order for the
/// separate `ui/m5`–`ui/m6` I-SCC, exposing a TDZ there.
#[test]
fn original_home_merge_rejects_remote_tdz() {
    let report = singleton_graph(
        vec![
            active_source_unit("unit:0", 1, &["B0"], 5, "ui/m5"),
            active_source_unit("unit:1", 2, &["B1"], 5, "ui/m5"),
            residual_source_unit("unit:2", 3, &["B2"], 5),
            residual_source_unit("unit:3", 4, &["B3"], 5),
            active_source_unit("unit:4", 5, &["B4"], 5, "ui/m6"),
            active_source_unit("unit:5", 6, &["B5"], 5, "ui/m4"),
            residual_source_unit("unit:6", 7, &["B6"], 5),
            active_source_unit("unit:7", 8, &["B7"], 5, "ui/m0"),
        ],
        vec![
            dependency_edge("edge:0", "unit:0", "unit:1", DepKind::Sequenced, true),
            dependency_edge("edge:1", "unit:0", "unit:4", DepKind::LazyUse, false),
            dependency_edge("edge:2", "unit:1", "unit:0", DepKind::LazyUse, false),
            dependency_edge("edge:3", "unit:1", "unit:4", DepKind::LazyUse, false),
            promoted_dependency_edge(
                "edge:4",
                "unit:1",
                "unit:5",
                DepKind::EagerUse,
                true,
                "unit:4",
            ),
            dependency_edge("edge:5", "unit:2", "unit:3", DepKind::LazyUse, false),
            dependency_edge("edge:6", "unit:3", "unit:1", DepKind::EagerUse, true),
            dependency_edge("edge:7", "unit:3", "unit:2", DepKind::LazyUse, false),
            dependency_edge("edge:8", "unit:4", "unit:1", DepKind::EagerUse, true),
            dependency_edge("edge:9", "unit:6", "unit:1", DepKind::LazyUse, false),
            dependency_edge("edge:10", "unit:6", "unit:5", DepKind::Sequenced, true),
            dependency_edge("edge:11", "unit:7", "unit:0", DepKind::EagerUse, true),
        ],
    );
    let spec = [
        spec_module("ui/m0", &["unit:7"]),
        spec_module("ui/m4", &["unit:5"]),
        spec_module("ui/m5", &["unit:0", "unit:1"]),
        spec_module("ui/m6", &["unit:4"]),
    ];
    let (mut q, _) =
        build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000).unwrap();
    assert!(q.realizability_verdict().is_realizable());
    let left = q.class_of(q.source_unit_index_of("unit:3").unwrap());
    let right = q.class_of(q.source_unit_index_of("unit:7").unwrap());
    assert!(q.merge_preserves_invariants(left, right));
    let mut post = q.realizability_assignment().clone();
    let target = post.of(analysis::SourceUnitId(7));
    post.set(analysis::SourceUnitId(3), target);
    let post_verdict = gate::check_realizability(q.dependency_graph(), &post);
    let index = gate::RealizabilityIndex::from_assignment(
        q.dependency_graph(),
        q.realizability_assignment().clone(),
    );
    assert_eq!(
        index.verdict_after_moving_source_units(
            q.dependency_graph(),
            &[analysis::SourceUnitId(3)],
            target
        ),
        post_verdict,
    );
    assert_eq!(post_verdict.unrealizable_sccs.len(), 1);
    assert!(
        !post_verdict.unrealizable_sccs[0]
            .core
            .modules
            .contains(&target)
    );
    assert!(!q.contract_if_preserving(left, right));
    assert!(q.realizability_verdict().is_realizable());
}

#[test]
fn group_contraction_commits_one_final_entry_assignment_and_honors_cap() {
    let report = singleton_graph(
        vec![
            active_source_unit("unit:a", 1, &["A"], 5, "home"),
            active_source_unit("unit:b", 2, &["B"], 5, "home"),
            residual_source_unit("unit:entry", 3, &["Entry"], 5),
            active_source_unit("unit:outside", 4, &["Outside"], 5, "outside"),
        ],
        vec![],
    );
    let assignments = [ModuleId(1), ModuleId(2), ModuleId(0), ModuleId(3)];
    let mut q = QuotientGraph::from_report_with_realizable_assignment(
        &report,
        9,
        &assignments,
        ModuleId(0),
    )
    .unwrap();
    assert!(!q.contract_if_preserving(ClassId(0), ClassId(0)));
    assert!(!q.contract_if_preserving(ClassId(0), ClassId(1)));
    assert_eq!(q.class_of(SourceUnitIndex(1)), ClassId(1));

    let mut q = QuotientGraph::from_report_with_realizable_assignment(
        &report,
        14,
        &assignments,
        ModuleId(0),
    )
    .unwrap();
    let classes = [ClassId(0), ClassId(1), ClassId(2)];
    assert!(!q.contract_group_if_preserving(&classes));
    assert_eq!(
        q.realizability_assignment().of(analysis::SourceUnitId(0)),
        assignments[0]
    );
    assert_eq!(q.class_of(SourceUnitIndex(0)), ClassId(0));

    let mut q = QuotientGraph::from_report_with_realizable_assignment(
        &report,
        15,
        &assignments,
        ModuleId(0),
    )
    .unwrap();
    assert!(q.contract_group_if_preserving(&classes));
    for source_unit in 0..3 {
        assert_eq!(q.class_of(SourceUnitIndex(source_unit)), ClassId(0));
        assert_eq!(
            q.realizability_assignment()
                .of(analysis::SourceUnitId(source_unit)),
            ModuleId(0)
        );
    }
    assert_eq!(q.class_of(SourceUnitIndex(3)), ClassId(3));
    assert_eq!(
        q.realizability_assignment().of(analysis::SourceUnitId(3)),
        assignments[3]
    );
    assert!(q.realizability_verdict().is_realizable());
    assert!(!q.contract_if_preserving(ClassId(0), ClassId(1)));
    assert!(!q.contract_group_if_preserving(&classes));
}

#[test]
fn seed_skips_unrealizable_spec_module_contraction_and_reports() {
    // Fixture: spec declares two modules mod_alpha and mod_beta.
    // mod_alpha contains owners {a1, a2}; mod_beta contains {b1, b2}.
    // The constraining edges form an asymmetric cycle between the
    // two modules:
    //   a1 -> b1 (EagerUse, constraining)
    //   b2 -> a2 (EagerUse, constraining)
    // After contracting mod_alpha (a1, a2 share a class) the
    // post-contract quotient has a constraining edge a1-class -> b1
    // and b2 -> a2-class. When the kernel then tries to contract
    // mod_beta (b1 and b2), b1 and b2 would land in one class, and
    // then [a-class, b-class] form a mutual constraining cycle. The
    // gate must reject that contraction.
    let a1 = residual_source_unit("unit:a1", 1, &["BindingA1"], 5);
    let a2 = residual_source_unit("unit:a2", 2, &["BindingA2"], 5);
    let b1 = residual_source_unit("unit:b1", 3, &["BindingB1"], 5);
    let b2 = residual_source_unit("unit:b2", 4, &["BindingB2"], 5);
    let edges = vec![
        dependency_edge("edge:0", "unit:a1", "unit:b1", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:b2", "unit:a2", DepKind::EagerUse, true),
    ];
    let report = singleton_graph(vec![a1.clone(), a2.clone(), b1.clone(), b2.clone()], edges);
    let spec = vec![
        spec_module("mod_alpha", &["unit:a1", "unit:a2"]),
        spec_module("mod_beta", &["unit:b1", "unit:b2"]),
    ];
    let (q, rejected) =
        build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000).unwrap();

    // Exactly one of the two contractions must be rejected. The
    // canonical order is mod_alpha first (lex), so mod_alpha
    // applies cleanly and mod_beta gets rejected.
    let spec_rejections: Vec<&SeedContractionRejected> = rejected
        .iter()
        .filter(|r| matches!(r, SeedContractionRejected::SpecModule { .. }))
        .collect();
    assert_eq!(
        spec_rejections.len(),
        1,
        "exactly one spec-module rejection expected, got {rejected:?}",
    );
    let SeedContractionRejected::SpecModule {
        module_id,
        rejected_pair,
        cycle,
        ..
    } = spec_rejections[0]
    else {
        panic!("expected SpecModule variant");
    };
    assert_eq!(module_id, "mod_beta");
    assert_eq!(
        rejected_pair,
        &("unit:b1".to_string(), "unit:b2".to_string()),
        "rejection should point at the b1<->b2 contraction",
    );
    assert!(!cycle.is_empty(), "cycle evidence must be non-empty");
    // The cycle evidence must mention both alpha-class owners and
    // the b1-class — the cycle the proposed contraction would join.
    let evidence_source_units: Vec<&str> = cycle
        .cycles
        .iter()
        .flat_map(|c| c.source_unit_ids.iter().map(String::as_str))
        .collect();
    assert!(
        evidence_source_units.contains(&"unit:a1") && evidence_source_units.contains(&"unit:a2"),
        "cycle evidence should include alpha owners: {evidence_source_units:?}",
    );
    assert!(
        evidence_source_units.contains(&"unit:b1") || evidence_source_units.contains(&"unit:b2"),
        "cycle evidence should include at least one beta unit: {evidence_source_units:?}",
    );

    // mod_alpha did apply — a1 and a2 share a class.
    let a1_idx = q.source_unit_index_of("unit:a1").unwrap();
    let a2_idx = q.source_unit_index_of("unit:a2").unwrap();
    assert_eq!(q.class_of(a1_idx), q.class_of(a2_idx));
    // mod_beta did NOT apply — b1 and b2 are still in distinct
    // classes (the kernel never silently merged them).
    let b1_idx = q.source_unit_index_of("unit:b1").unwrap();
    let b2_idx = q.source_unit_index_of("unit:b2").unwrap();
    assert_ne!(q.class_of(b1_idx), q.class_of(b2_idx));
}

// ---------- Gate-ladder pinning tests. ----------
//
// These pin the module-level gate predicate that `check_merge_boolean`
// routes through.

/// A 3-owner atomic group whose members form a constraining cycle
/// `a → b → c → a` exists precisely because its members MUST
/// co-locate, and the module-level predicate accepts the contractions
/// (all three owners project to the residual module, so every merge
/// is a delta-free no-op). A class-level cycle check would reject
/// them — the class graph is cyclic from construction — and the group
/// could not seed.
#[test]
fn seed_co_locates_constraining_cycle_atomic_group() {
    let a = residual_source_unit("unit:a", 1, &["BindingA"], 5);
    let b = residual_source_unit("unit:b", 2, &["BindingB"], 5);
    let c = residual_source_unit("unit:c", 3, &["BindingC"], 5);
    let edges = vec![
        dependency_edge("edge:0", "unit:a", "unit:b", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:b", "unit:c", DepKind::EagerUse, true),
        dependency_edge("edge:2", "unit:c", "unit:a", DepKind::EagerUse, true),
    ];
    let atomic_group = atomic_group_for("atomic:0", &[&a, &b, &c]);
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        edges,
        vec![atomic_group],
        vec![],
    );
    let (q, rejected) =
        build_seed_quotient(&report, &report.atomic_graph.nodes, &[], 10_000).unwrap();
    assert!(
        rejected.is_empty(),
        "atomic-group contractions internal to the residual module are \
         delta-free no-ops under the module-level predicate and must \
         not be rejected: {rejected:?}",
    );
    let a_idx = q.source_unit_index_of("unit:a").unwrap();
    let b_idx = q.source_unit_index_of("unit:b").unwrap();
    let c_idx = q.source_unit_index_of("unit:c").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
    assert_eq!(q.class_of(b_idx), q.class_of(c_idx));
}

/// Entry's implicit imports already close the runtime cycle for x -> r.
/// Merging an unrelated helper into x does not repair that dependency and
/// must remain rejected, with evidence naming the eager reader.
#[test]
fn merge_cannot_hide_an_existing_entry_dependency() {
    let x = active_source_unit("unit:x", 1, &["BindingX"], 10, "ui/x");
    let r = residual_source_unit("unit:r", 2, &["BindingR"], 5);
    let h = residual_source_unit("unit:h", 3, &["BindingH"], 5);
    let edges = vec![
        dependency_edge("edge:0", "unit:x", "unit:r", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:r", "unit:h", DepKind::LazyUse, false),
    ];
    let report = singleton_graph(vec![x.clone(), r.clone(), h.clone()], edges);
    let groups = vec![module_group(vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition(&report, 10_000, &groups).unwrap();
    let cx = group_ids[0];
    let ch = q.class_of(q.source_unit_index_of("unit:h").unwrap());

    // The source graph is acyclic, but entry's implicit import closes the
    // runtime cycle before the merge. The merge still leaves x -> r in place.
    assert!(!q.realizability_verdict().is_realizable());
    assert!(
        !q.merge_preserves_invariants(cx, ch),
        "merging unit:h into ui/x closes the asymmetric I-cycle \
         {{ui/x, residual}} with a TDZ'ing constraining pair; the \
         boolean gate must reject",
    );
    let evidence = q
        .would_be_cycles_after_contract(cx, ch)
        .expect("diagnostic gate must surface the Pass-2 rejection");
    let evidence_source_units: Vec<&str> = evidence
        .cycles
        .iter()
        .flat_map(|cycle| cycle.source_unit_ids.iter().map(String::as_str))
        .collect();
    assert!(
        evidence_source_units.contains(&"unit:x"),
        "evidence must mention the eager reader: {evidence_source_units:?}",
    );
    assert!(
        q.contract(cx, ch).is_err(),
        "contract must refuse to commit the TDZ-closing merge",
    );
}

#[test]
fn post_seed_reports_each_unrealizable_scc_in_source_unit_id_order() {
    // Two independent mutual-eager module pairs, (c, d) before (a, b)
    // in the report. Every spec module is one owner, so no seed
    // contraction is refused and only the post-seed gate sees the
    // cycles. The report orders them by source-unit IDs, not by class ids.
    let source_units = [
        active_source_unit("unit:c", 1, &["BindingC"], 5, "mod_c"),
        active_source_unit("unit:d", 2, &["BindingD"], 5, "mod_d"),
        active_source_unit("unit:a", 3, &["BindingA"], 5, "mod_a"),
        active_source_unit("unit:b", 4, &["BindingB"], 5, "mod_b"),
    ];
    let edges = vec![
        dependency_edge("edge:0", "unit:c", "unit:d", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:d", "unit:c", DepKind::EagerUse, true),
        dependency_edge("edge:2", "unit:a", "unit:b", DepKind::EagerUse, true),
        dependency_edge("edge:3", "unit:b", "unit:a", DepKind::EagerUse, true),
    ];
    let atomic_groups = source_units
        .iter()
        .enumerate()
        .map(|(i, source_unit)| atomic_group_for(&format!("atomic:{i}"), &[source_unit]))
        .collect();
    let spec: Vec<SpecModuleGroup> = source_units
        .iter()
        .map(|source_unit| SpecModuleGroup {
            module_id: source_unit.destination.as_str().to_string(),
            source_unit_ids: vec![source_unit.id.clone()],
        })
        .collect();
    let report = graph_of(source_units.to_vec(), edges, atomic_groups, vec![]);

    let (_q, rejected) =
        build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000).unwrap();

    let scc = |source_unit_ids: [&str; 2], classes: [usize; 2]| {
        let source_unit_ids = source_unit_ids.map(String::from).to_vec();
        SeedContractionRejected::PostSeedUnrealizableScc {
            source_unit_ids: source_unit_ids.clone(),
            cycle: CycleEvidence {
                cycles: vec![CycleClassSet {
                    classes: classes.map(ClassId).to_vec(),
                    source_unit_ids,
                }],
            },
        }
    };
    assert_eq!(
        rejected,
        vec![
            scc(["unit:a", "unit:b"], [2, 3]),
            scc(["unit:c", "unit:d"], [0, 1]),
        ],
    );
}

// ---------- Greedy merge to convergence tests. ----------
//
// The greedy operates over a quotient whose initial assignment the
// caller has chosen — typically the seed quotient (atomic groups +
// spec modules pre-contracted) augmented with whatever classes the
// renderer has marked as pre-existing active modules. The kernel
// distinguishes "pre-existing module" classes from "residual orphan"
// classes via the `is_pre_existing_module` bit on each class; the
// mergeability rules govern whether greedy may extend a module with an
// orphan, merge existing modules, or stop.
//
// All fixtures below use `from_report_with_partition`, the
// constructor that takes per-group metadata (lines + the
// pre-existing-module bit). SourceUnits not in any group remain singletons
// with their per-owner residual flag derived from the report.

#[test]
fn greedy_absorbs_tiny_named_helper_into_unique_consumer() {
    // Pre-existing module M = {unit:a (BindingA)}. Residual
    // unit:helper (BindingHelper) is read only by unit:a via an
    // EagerUse edge. Its unique consumer is the only module
    // neighbor, so greedy should absorb it.
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x");
    let helper = residual_source_unit("unit:helper", 2, &["BindingHelper"], 5);
    let report = graph_of(
        vec![a.clone(), helper.clone()],
        vec![dependency_edge(
            "edge:0",
            "unit:a",
            "unit:helper",
            DepKind::EagerUse,
            true,
        )],
        vec![
            atomic_group_for("atomic:0", &[&a]),
            atomic_group_for("atomic:1", &[&helper]),
        ],
        vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
    );

    let groups = vec![module_group(vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition(&report, 10_000, &groups).unwrap();
    let contractions = greedy_merge_to_convergence(&mut q);

    assert_eq!(contractions.len(), 1, "got: {contractions:?}");
    let a_idx = q.source_unit_index_of("unit:a").unwrap();
    let helper_idx = q.source_unit_index_of("unit:helper").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(helper_idx));
    assert_eq!(q.class_of(a_idx), group_ids[0]);
}

#[test]
fn greedy_terminates_at_convergence() {
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x");
    let h1 = residual_source_unit("unit:h1", 2, &["BindingH1"], 5);
    let h2 = residual_source_unit("unit:h2", 3, &["BindingH2"], 5);
    let h3 = residual_source_unit("unit:h3", 4, &["BindingH3"], 5);
    // Start realizable: residual helpers depend on the emitted module,
    // not vice versa. Each contraction preserves that entry-last invariant.
    let report = singleton_graph(
        vec![a.clone(), h1.clone(), h2.clone(), h3.clone()],
        vec![
            dependency_edge("edge:0", "unit:h1", "unit:a", DepKind::EagerUse, true),
            dependency_edge("edge:1", "unit:h2", "unit:a", DepKind::EagerUse, true),
            dependency_edge("edge:2", "unit:h3", "unit:a", DepKind::EagerUse, true),
        ],
    );

    let groups = vec![module_group(vec![0])];
    let (mut q, _) = QuotientGraph::from_report_with_partition(&report, 10_000, &groups).unwrap();
    let before = q.iter_classes().count();
    let contractions = greedy_merge_to_convergence(&mut q);
    let after = q.iter_classes().count();
    // Three orphans absorbed; class count decreases by exactly 3.
    assert_eq!(
        before.saturating_sub(after),
        contractions.len(),
        "each contraction reduces class count by 1",
    );
    assert_eq!(contractions.len(), 3, "got: {contractions:?}");

    // Running greedy again is a no-op (converged).
    let again = greedy_merge_to_convergence(&mut q);
    assert!(again.is_empty(), "second pass should be empty: {again:?}");
}

// ---------- Full mergeability + merge output shape. ----------
//
// The gate allows two pre-existing-module classes to merge (with or
// without absorbing residual orphans). The renderer carries
// `merge_into: Option<Vec<String>>` on proposals whose operands
// include ≥2 pre-existing-module groups.

#[test]
fn greedy_merges_three_clusters_until_the_size_cap() {
    // Three pre-existing-module clusters of 20 + 20 + 10 lines, all
    // mutually coupled by EagerUse edges.
    let a = active_source_unit("unit:a", 1, &["BindingA"], 20, "ui/a");
    let b = active_source_unit("unit:b", 2, &["BindingB"], 20, "ui/b");
    let c = active_source_unit("unit:c", 3, &["BindingC"], 10, "ui/c");
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        vec![
            dependency_edge("edge:ab", "unit:a", "unit:b", DepKind::EagerUse, true),
            dependency_edge("edge:bc", "unit:b", "unit:c", DepKind::EagerUse, true),
            dependency_edge("edge:ac", "unit:a", "unit:c", DepKind::EagerUse, true),
        ],
        vec![
            atomic_group_for("atomic:a", &[&a]),
            atomic_group_for("atomic:b", &[&b]),
            atomic_group_for("atomic:c", &[&c]),
        ],
        vec![],
    );
    let groups = vec![
        module_group(vec![0]),
        module_group(vec![1]),
        module_group(vec![2]),
    ];
    for (cap, expected_contractions, expected_classes) in [
        // Everything fits: all three collapse through two contractions.
        (150, 2, vec![vec!["unit:a", "unit:b", "unit:c"]]),
        // Candidates are (a, b)=40 lines, (a, c)=30 (cycle-creating, so
        // rejected) and (b, c)=30. (b, c) wins the tie-break on result
        // size, and folding `a` into the 30-line survivor would total 50,
        // over the cap.
        (40, 1, vec![vec!["unit:a"], vec!["unit:b", "unit:c"]]),
    ] {
        let (mut q, _) = QuotientGraph::from_report_with_partition(&report, cap, &groups).unwrap();
        let contractions = greedy_merge_to_convergence(&mut q);
        assert_eq!(
            contractions.len(),
            expected_contractions,
            "cap {cap}: {contractions:?}",
        );
        let mut classes = BTreeMap::<ClassId, Vec<&str>>::new();
        for source_unit in ["unit:a", "unit:b", "unit:c"] {
            classes
                .entry(q.class_of(q.source_unit_index_of(source_unit).unwrap()))
                .or_default()
                .push(source_unit);
        }
        assert_eq!(
            classes.into_values().collect::<Vec<_>>(),
            expected_classes,
            "cap {cap}",
        );
    }
}

#[test]
fn greedy_resolves_realizability_cycle_by_merging() {
    // Asymmetric I-cycle: mod_a → mod_b (EagerUse constraining) and
    // mod_b → mod_a (LazyUse non-constraining). The constraining
    // edge alone doesn't cycle, but the symmetric coupling makes
    // them a strong merge candidate; after merge the post-merge
    // quotient is realizable (intra-class self-loop).
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/a");
    let b = active_source_unit("unit:b", 2, &["BindingB"], 10, "ui/b");
    let report = graph_of(
        vec![a.clone(), b.clone()],
        vec![
            dependency_edge("edge:ab", "unit:a", "unit:b", DepKind::EagerUse, true),
            dependency_edge("edge:ba", "unit:b", "unit:a", DepKind::LazyUse, false),
        ],
        vec![
            atomic_group_for("atomic:a", &[&a]),
            atomic_group_for("atomic:b", &[&b]),
        ],
        vec![],
    );
    let groups = vec![module_group(vec![0]), module_group(vec![1])];
    let (mut q, _) = QuotientGraph::from_report_with_partition(&report, 10_000, &groups).unwrap();
    let contractions = greedy_merge_to_convergence(&mut q);
    assert_eq!(
        contractions.len(),
        1,
        "asymmetric coupling between two modules should merge: {contractions:?}",
    );
    let a_idx = q.source_unit_index_of("unit:a").unwrap();
    let b_idx = q.source_unit_index_of("unit:b").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
    // Post-merge quotient: no remaining cross-class cycles.
    let verdict = q.realizability_verdict();
    assert!(
        verdict.is_realizable(),
        "post-merge quotient should be realizable: {verdict:?}",
    );
}

#[test]
fn merge_absorbs_residual_source_unit_with_only_intra_deps() {
    // mod_a + mod_b mutually coupled; residual `helper` reads from
    // both. Assert the merge proposal's operands include
    // `unit:helper`.
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/a");
    let b = active_source_unit("unit:b", 2, &["BindingB"], 10, "ui/b");
    let helper = residual_source_unit("unit:helper", 3, &["BindingHelper"], 5);
    let report = graph_of(
        vec![a.clone(), b.clone(), helper.clone()],
        vec![
            dependency_edge("edge:ab", "unit:a", "unit:b", DepKind::EagerUse, true),
            dependency_edge("edge:ba", "unit:b", "unit:a", DepKind::EagerUse, true),
            dependency_edge(
                "edge:helper_a",
                "unit:helper",
                "unit:a",
                DepKind::EagerUse,
                true,
            ),
            dependency_edge(
                "edge:helper_b",
                "unit:helper",
                "unit:b",
                DepKind::EagerUse,
                true,
            ),
        ],
        vec![
            atomic_group_for("atomic:a", &[&a]),
            atomic_group_for("atomic:b", &[&b]),
            atomic_group_for("atomic:helper", &[&helper]),
        ],
        vec![],
    );
    let result = propose(
        &report,
        &claims(&[("BindingA", "ui/a"), ("BindingB", "ui/b")]),
        10_000,
    )
    .unwrap();
    let merge_proposals: Vec<&ModuleProposal> = result
        .proposals
        .iter()
        .filter(|p| p.merge_into.is_some())
        .collect();
    assert_eq!(
        merge_proposals.len(),
        1,
        "exactly one merge proposal expected, got proposals: {:?}",
        result.proposals,
    );
    let merge = merge_proposals[0];
    assert_eq!(
        merge.merge_into.clone().unwrap(),
        vec!["ui/a".to_string(), "ui/b".to_string()],
    );
    // The merge proposal carries the absorbed residual helper as
    // an extension owner — `merge + extend` semantics.
    assert!(
        merge
            .extension_source_unit_ids
            .contains(&"unit:helper".to_string()),
        "merge proposal should list unit:helper as an extension unit: {merge:?}",
    );
}

// ---------- Atomic-DAG reachability seeding. ----------
//
// The third gated contraction pass in `build_seed_quotient` groups
// residual owners by atomic-DAG reachability. Input whose atomic-DAG
// reachability closure would form a cycle gets a
// `SeedContractionRejected::AtomicReachability` diagnostic pinpointing
// the rejected pair.

#[test]
fn pass3_diagnostic_walk_never_commits_a_merge() {
    // Invariant guard for the pass-3 diagnostic walk in
    // `build_seed_quotient`. After the fixed-point contraction loop
    // exits (zero successful contractions), the diagnostic walk
    // re-classifies each still-unmerged atomic-DAG edge to record
    // *why* it could not contract. That walk must be read-only: it
    // must never commit a merge. A stray successful contraction there
    // would join two classes the fixed-point loop deliberately left
    // apart, silently corrupting the assignment that the post-seed
    // realizability gate (and the returned `q`) then reads — and that
    // merge would be neither counted nor looped.
    //
    // We pin the externally-observable consequence: for every
    // `AtomicReachability`-rejected pair, the two pivot owners remain
    // in DISTINCT classes in the returned quotient. If the diagnostic
    // walk had committed the rejected merge (a stray `contract` in the
    // diagnostics phase), the pair's owners would share a class —
    // exactly the corruption the read-only predicates
    // (`check_merge_preconditions` / `would_be_cycles_after_contract`)
    // prevent.
    //
    // Fixture: residual Bar / Helper and active Foo (in spec module
    // mod_alpha) form a constraining 3-cycle (Bar reads Foo, Foo reads
    // Helper, Helper reads Bar); pass-3's atomic-DAG-reachability
    // contraction rejects the cycle-closing edges, driving the
    // diagnostic walk. A constraining cycle among residual owners alone
    // yields no rejection: the contractions collapse it into one
    // residual class and same-class edges are skipped silently.
    let foo = active_source_unit("unit:foo", 1, &["Foo"], 5, "mod_alpha");
    let bar = residual_source_unit("unit:bar", 2, &["Bar"], 5);
    let helper = residual_source_unit("unit:helper", 3, &["Helper"], 5);
    let edges = vec![
        dependency_edge("edge:0", "unit:bar", "unit:foo", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:foo", "unit:helper", DepKind::EagerUse, true),
        dependency_edge("edge:2", "unit:helper", "unit:bar", DepKind::EagerUse, true),
    ];
    let report = graph_of(
        vec![foo.clone(), bar.clone(), helper.clone()],
        edges,
        vec![
            atomic_group_for("atomic:foo", &[&foo]),
            atomic_group_for("atomic:bar", &[&bar]),
            atomic_group_for("atomic:helper", &[&helper]),
        ],
        vec![
            atomic_edge("atomic_edge:a", "atomic:bar", "atomic:foo"),
            atomic_edge("atomic_edge:b", "atomic:foo", "atomic:helper"),
            atomic_edge("atomic_edge:c", "atomic:helper", "atomic:bar"),
        ],
    );
    let spec = vec![spec_module("mod_alpha", &["unit:foo"])];
    let (q, rejected) =
        build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000).unwrap();

    let reachability_rejections: Vec<&SeedContractionRejected> = rejected
        .iter()
        .filter(|r| matches!(r, SeedContractionRejected::AtomicReachability { .. }))
        .collect();
    assert!(
        !reachability_rejections.is_empty(),
        "fixture must drive the pass-3 diagnostic walk via at least one \
         AtomicReachability rejection, got: {rejected:?}",
    );

    // The load-bearing assertion: no rejected pair was actually
    // merged by the diagnostic walk. Each pivot pair stays in a
    // distinct class.
    for r in &reachability_rejections {
        let SeedContractionRejected::AtomicReachability {
            rejected_pair,
            cycle,
            ..
        } = r
        else {
            unreachable!("filtered to AtomicReachability above");
        };
        assert!(
            !cycle.is_empty(),
            "AtomicReachability rejection must carry cycle evidence: {r:?}"
        );
        let (src_source_unit, tgt_source_unit) = rejected_pair;
        let src_idx = q
            .source_unit_index_of(src_source_unit)
            .unwrap_or_else(|| panic!("rejected source owner {src_source_unit} must be in graph"));
        let tgt_idx = q
            .source_unit_index_of(tgt_source_unit)
            .unwrap_or_else(|| panic!("rejected target owner {tgt_source_unit} must be in graph"));
        assert_ne!(
            q.class_of(src_idx),
            q.class_of(tgt_idx),
            "diagnostic walk must NOT have committed the rejected merge of \
             {src_source_unit} and {tgt_source_unit}; they must remain in distinct classes",
        );
    }

    // `propose` surfaces the seed rejections, cycle evidence included.
    let proposed = propose(&report, &claims(&[("Foo", "mod_alpha")]), 10_000).unwrap();
    assert_eq!(proposed.seed_rejections, rejected);
}

// ---------- Planner gate ≡ materializer gate. ----------
//
// The peel planner's seed-quotient cycle gate must produce the same
// realizability verdict as the materializer's `check_realizability`
// run on the projected assignment. A planner that reimplemented
// Tarjan over only constraining edges in the JSON report would miss
// asymmetric I-cycles the materializer catches via its
// `EsmEvaluationSimulator` pass. The tests below pin the agreement:
// every planner verdict must match the materializer verdict on the
// same input.

/// Build a `DependencyGraph` + `ModuleAssignment` from a report + spec module
/// list. The assignment assigns each owner to the module derived from
/// its `destination.id`; residual destinations land on the residual
/// `ModuleId`. Used by the planner-vs-materializer cross-check tests.
fn dependency_graph_and_assignment_from_spec(
    report: &analysis::DependencyGraphReport,
    spec: &[SpecModuleGroup],
) -> (analysis::DependencyGraph, analysis::ModuleAssignment) {
    use std::collections::HashMap;
    let dependency_graph = analysis::DependencyGraph::from_report(report).unwrap();
    let source_unit_index: HashMap<&str, usize> = report
        .nodes
        .iter()
        .enumerate()
        .map(|(i, node)| (node.id.as_str(), i))
        .collect();
    // Module-id assignment: residual goes to ModuleId(0). Every
    // distinct spec module gets its own ModuleId starting at 1.
    let residual = analysis::ModuleId(0);
    let mut spec_module_ids: HashMap<&str, analysis::ModuleId> = HashMap::new();
    let mut next_idx = 1usize;
    for module in spec {
        spec_module_ids
            .entry(module.module_id.as_str())
            .or_insert_with(|| {
                let m = analysis::ModuleId(next_idx);
                next_idx += 1;
                m
            });
    }
    // SourceUnit→ModuleId: by default residual; spec modules override.
    let mut of: Vec<analysis::ModuleId> = vec![residual; dependency_graph.num_nodes()];
    for module in spec {
        let mid = spec_module_ids[module.module_id.as_str()];
        for source_unit_id in &module.source_unit_ids {
            if let Some(&o) = source_unit_index.get(source_unit_id.as_str()) {
                of[o] = mid;
            }
        }
    }
    let assignment = analysis::ModuleAssignment::from_assignments(of, residual);
    (dependency_graph, assignment)
}

#[test]
fn planner_and_materializer_agree_on_corpus() {
    // Corpus property test: across a mix of well-formed and
    // unrealizable fixture chunks, the planner's seed-quotient
    // verdict and the materializer's `check_realizability` verdict
    // agree on realizability (boolean). The fixtures cover:
    //   - empty / no edges (trivially realizable);
    //   - a single-module fixture with intra-module edges only;
    //   - a single asymmetric I-cycle (unrealizable);
    //   - a mutual constraining cycle (unrealizable);
    //   - a pair of modules with only lazy edges between them
    //     (realizable).

    struct Case {
        label: &'static str,
        report: analysis::DependencyGraphReport,
        spec: Vec<SpecModuleGroup>,
    }

    let mut cases: Vec<Case> = Vec::new();

    // Case 1: empty graph.
    cases.push(Case {
        label: "empty",
        report: singleton_graph(vec![], vec![]),
        spec: vec![],
    });

    // Case 2: single module, no cross-module edges.
    {
        let a = active_source_unit("unit:a", 1, &["BindingA"], 5, "mod_solo");
        let b = active_source_unit("unit:b", 2, &["BindingB"], 5, "mod_solo");
        cases.push(Case {
            label: "single_module_intra_edges",
            report: singleton_graph(
                vec![a.clone(), b.clone()],
                vec![dependency_edge(
                    "edge:0",
                    "unit:a",
                    "unit:b",
                    analysis::DepKind::EagerUse,
                    true,
                )],
            ),
            spec: vec![spec_module("mod_solo", &["unit:a", "unit:b"])],
        });
    }

    // Case 3: asymmetric I-cycle through a mediator (Lemma 2 cannot
    // rescue). Mirrors the materializer's
    // `mediator_reaches_asymmetric_cycle_test` shape — three
    // modules, residual reaches the SCC only via a non-residual
    // mediator. The SCC's constraining edge TDZs at runtime.
    {
        let entry = residual_source_unit("unit:entry", 0, &[], 1);
        let dep_value = active_source_unit("unit:dep_value", 1, &["BindingDepValue"], 5, "mod_dep");
        let lazy_reader =
            active_source_unit("unit:lazy_reader", 2, &["BindingLazyReader"], 5, "mod_dep");
        let cross_value = active_source_unit(
            "unit:cross_value",
            3,
            &["BindingCrossValue"],
            5,
            "mod_dependent",
        );
        let mediator_helper = active_source_unit(
            "unit:mediator_helper",
            4,
            &["BindingMediatorHelper"],
            5,
            "mod_mediator",
        );
        let mediator_init = active_source_unit(
            "unit:mediator_init",
            5,
            &["BindingMediatorInit"],
            5,
            "mod_mediator",
        );
        cases.push(Case {
            label: "asymmetric_i_cycle_via_mediator",
            report: singleton_graph(
                vec![
                    entry.clone(),
                    dep_value.clone(),
                    lazy_reader.clone(),
                    cross_value.clone(),
                    mediator_helper.clone(),
                    mediator_init.clone(),
                ],
                vec![
                    dependency_edge(
                        "edge:entry_mediator",
                        "unit:entry",
                        "unit:mediator_init",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    dependency_edge(
                        "edge:mediator_dep",
                        "unit:mediator_helper",
                        "unit:dep_value",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                    dependency_edge(
                        "edge:mediator_intra",
                        "unit:mediator_init",
                        "unit:mediator_helper",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    dependency_edge(
                        "edge:dependent_dep",
                        "unit:cross_value",
                        "unit:dep_value",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    dependency_edge(
                        "edge:dep_back",
                        "unit:lazy_reader",
                        "unit:cross_value",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                ],
            ),
            spec: vec![
                spec_module("mod_dep", &["unit:dep_value", "unit:lazy_reader"]),
                spec_module("mod_dependent", &["unit:cross_value"]),
                spec_module(
                    "mod_mediator",
                    &["unit:mediator_helper", "unit:mediator_init"],
                ),
            ],
        });
    }

    // Case 4: mutual constraining cycle.
    {
        let a1 = residual_source_unit("unit:a1", 1, &["BindingA1"], 5);
        let b1 = residual_source_unit("unit:b1", 2, &["BindingB1"], 5);
        cases.push(Case {
            label: "mutual_constraining_cycle",
            report: singleton_graph(
                vec![a1.clone(), b1.clone()],
                vec![
                    dependency_edge(
                        "edge:fwd",
                        "unit:a1",
                        "unit:b1",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    dependency_edge(
                        "edge:back",
                        "unit:b1",
                        "unit:a1",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                ],
            ),
            // Note: residual destinations — no spec modules. The
            // planner's seed pass merges atomic groups only; since
            // each atomic is a singleton, no contractions happen and
            // no rejection fires. The materializer also sees the
            // SCC purely within residual (one module) and doesn't
            // flag it (intra-module). Both should agree: realizable.
            spec: vec![],
        });
    }

    // Case 5: lazy-only cross-module edges.
    {
        let alpha = active_source_unit("unit:alpha", 1, &["BindingAlpha"], 5, "mod_alpha");
        let beta = active_source_unit("unit:beta", 2, &["BindingBeta"], 5, "mod_beta");
        cases.push(Case {
            label: "lazy_only_cross_module",
            report: singleton_graph(
                vec![alpha.clone(), beta.clone()],
                vec![
                    dependency_edge(
                        "edge:0",
                        "unit:alpha",
                        "unit:beta",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                    dependency_edge(
                        "edge:1",
                        "unit:beta",
                        "unit:alpha",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                ],
            ),
            spec: vec![
                spec_module("mod_alpha", &["unit:alpha"]),
                spec_module("mod_beta", &["unit:beta"]),
            ],
        });
    }

    let mut materializer_unrealizable_labels = Vec::new();
    for case in &cases {
        // Materializer-side.
        let (dependency_graph, assignment) =
            dependency_graph_and_assignment_from_spec(&case.report, &case.spec);
        let verdict = gate::check_realizability(&dependency_graph, &assignment);
        let materializer_unrealizable = !verdict.is_realizable();

        // Planner-side.
        let (_q, rejected) = build_seed_quotient(
            &case.report,
            &case.report.atomic_graph.nodes,
            &case.spec,
            10_000,
        )
        .unwrap();
        let planner_has_rejection = !rejected.is_empty();

        assert_eq!(
            materializer_unrealizable, planner_has_rejection,
            "[{}] planner and materializer disagree:\n\
             materializer unrealizable = {materializer_unrealizable}\n\
             planner rejected = {planner_has_rejection}\n\
             materializer verdict: {verdict:?}\n\
             planner rejections: {rejected:?}",
            case.label,
        );
        if materializer_unrealizable {
            materializer_unrealizable_labels.push(case.label);
        }
    }
    // Without this, the agreement above passes vacuously when every case
    // is realizable.
    assert!(
        materializer_unrealizable_labels.contains(&"asymmetric_i_cycle_via_mediator"),
        "the mediator asymmetric I-cycle must be unrealizable per the materializer: \
         {materializer_unrealizable_labels:?}",
    );
}

// ---------------------------------------------------------------------
// Lazy-PQ vs. full-scan byte-equality corpus.
//
// See `devinfra/js/debundle/docs/peel_proposer.md`. This is the
// load-bearing correctness gate for the PQ-driven greedy. Every
// fixture below builds a quotient and runs both drivers from identical
// starting states; the contraction sequences must be byte-equal.
// ---------------------------------------------------------------------

/// Build a fixture quotient from a report + spec module groups. Two
/// independent quotients are constructed (one for each driver) so
/// the comparison is over isolated graphs.
fn build_fixture(
    report: &DependencyGraphReport,
    groups: &[PartitionGroup],
    cap_lines: usize,
) -> (QuotientGraph, QuotientGraph) {
    let (q_a, _) = QuotientGraph::from_report_with_partition(report, cap_lines, groups).unwrap();
    let (q_b, _) = QuotientGraph::from_report_with_partition(report, cap_lines, groups).unwrap();
    (q_a, q_b)
}

/// Fixture 1: chain. One pre-existing module a; orphans b → c → d → e
/// chain backward into a. Greedy should absorb them sequentially.
fn fixture_chain() -> (DependencyGraphReport, Vec<PartitionGroup>) {
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x");
    let b = residual_source_unit("unit:b", 2, &["BindingB"], 5);
    let c = residual_source_unit("unit:c", 3, &["BindingC"], 5);
    let d = residual_source_unit("unit:d", 4, &["BindingD"], 5);
    let e = residual_source_unit("unit:e", 5, &["BindingE"], 5);
    let edges = vec![
        dependency_edge("edge:0", "unit:b", "unit:a", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:c", "unit:b", DepKind::EagerUse, true),
        dependency_edge("edge:2", "unit:d", "unit:c", DepKind::EagerUse, true),
        dependency_edge("edge:3", "unit:e", "unit:d", DepKind::EagerUse, true),
    ];
    let report = singleton_graph(
        vec![a.clone(), b.clone(), c.clone(), d.clone(), e.clone()],
        edges,
    );
    let groups = vec![module_group(vec![0])];
    (report, groups)
}

/// Fixture 2: star topology. One pre-existing module a; orphans b,
/// c, d, e each connect ONLY to a (no inter-orphan edges). Greedy
/// absorbs each in some deterministic order.
fn fixture_star() -> (DependencyGraphReport, Vec<PartitionGroup>) {
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x");
    let b = residual_source_unit("unit:b", 2, &["BindingB"], 5);
    let c = residual_source_unit("unit:c", 3, &["BindingC"], 5);
    let d = residual_source_unit("unit:d", 4, &["BindingD"], 5);
    let e = residual_source_unit("unit:e", 5, &["BindingE"], 5);
    let edges = vec![
        dependency_edge("edge:0", "unit:b", "unit:a", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:c", "unit:a", DepKind::EagerUse, true),
        dependency_edge("edge:2", "unit:d", "unit:a", DepKind::EagerUse, true),
        dependency_edge("edge:3", "unit:e", "unit:a", DepKind::EagerUse, true),
    ];
    let report = singleton_graph(
        vec![a.clone(), b.clone(), c.clone(), d.clone(), e.clone()],
        edges,
    );
    let groups = vec![module_group(vec![0])];
    (report, groups)
}

/// Fixture 3: mutual-eager edges between two pre-existing modules
/// (no constraining cycle — eager reads in one direction only, even
/// though both directions exist on the I-graph). The greedy must
/// either merge the two modules or leave them alone deterministically.
fn fixture_mutual_eager() -> (DependencyGraphReport, Vec<PartitionGroup>) {
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x");
    let b = active_source_unit("unit:b", 2, &["BindingB"], 10, "ui/y");
    let h1 = residual_source_unit("unit:h1", 3, &["BindingH1"], 5);
    let h2 = residual_source_unit("unit:h2", 4, &["BindingH2"], 5);
    let edges = vec![
        // h1 reads a (constraining); h2 reads b (constraining).
        dependency_edge("edge:0", "unit:h1", "unit:a", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:h2", "unit:b", DepKind::EagerUse, true),
        // Cross-module non-constraining lazy edges (a ↔ b) — present
        // for coupling, not for constraining adjacency.
        dependency_edge("edge:2", "unit:a", "unit:b", DepKind::LazyUse, false),
        dependency_edge("edge:3", "unit:b", "unit:a", DepKind::LazyUse, false),
    ];
    let report = singleton_graph(vec![a.clone(), b.clone(), h1.clone(), h2.clone()], edges);
    let groups = vec![module_group(vec![0]), module_group(vec![1])];
    (report, groups)
}

/// Fixture 4: asymmetric cycle that the greedy must resolve by
/// contracting one pair, dissolving the other side of the cycle.
/// Two modules a, b with constraining edges a → b and b → a (via
/// helpers); greedy should pick one merge.
fn fixture_asymmetric_cycle() -> (DependencyGraphReport, Vec<PartitionGroup>) {
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x");
    let b = active_source_unit("unit:b", 2, &["BindingB"], 10, "ui/y");
    let h = residual_source_unit("unit:h", 3, &["BindingH"], 5);
    let edges = vec![
        // a → h → b is the forward path; b → a directly closes the
        // cycle. h is an orphan with a unique extension target (a).
        dependency_edge("edge:0", "unit:a", "unit:h", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:h", "unit:b", DepKind::EagerUse, true),
    ];
    let report = singleton_graph(vec![a.clone(), b.clone(), h.clone()], edges);
    let groups = vec![module_group(vec![0]), module_group(vec![1])];
    (report, groups)
}

/// Fixture 5: fully-connected small classes. Three pre-existing
/// modules a, b, c; each pair has a constraining edge in one
/// direction (forming a 3-cycle). Plus three orphans, each uniquely
/// pointing to one of the modules. Tests coupling-drift handling:
/// once one orphan is absorbed, its absorber's coupling vs. the
/// other modules may shift.
fn fixture_fully_connected_small() -> (DependencyGraphReport, Vec<PartitionGroup>) {
    let a = active_source_unit("unit:a", 1, &["BindingA"], 10, "ui/x");
    let b = active_source_unit("unit:b", 2, &["BindingB"], 10, "ui/y");
    let c = active_source_unit("unit:c", 3, &["BindingC"], 10, "ui/z");
    let oa = residual_source_unit("unit:oa", 4, &["BindingOA"], 5);
    let ob = residual_source_unit("unit:ob", 5, &["BindingOB"], 5);
    let oc = residual_source_unit("unit:oc", 6, &["BindingOC"], 5);
    let edges = vec![
        dependency_edge("edge:0", "unit:oa", "unit:a", DepKind::EagerUse, true),
        dependency_edge("edge:1", "unit:ob", "unit:b", DepKind::EagerUse, true),
        dependency_edge("edge:2", "unit:oc", "unit:c", DepKind::EagerUse, true),
    ];
    let report = singleton_graph(
        vec![
            a.clone(),
            b.clone(),
            c.clone(),
            oa.clone(),
            ob.clone(),
            oc.clone(),
        ],
        edges,
    );
    let groups = vec![
        module_group(vec![0]),
        module_group(vec![1]),
        module_group(vec![2]),
    ];
    (report, groups)
}

type Fixture = (DependencyGraphReport, Vec<PartitionGroup>);
type FixtureBuilder = fn() -> Fixture;

#[test]
fn lazy_pq_greedy_matches_full_scan_greedy_on_corpus() {
    let fixtures: Vec<(&str, FixtureBuilder)> = vec![
        ("chain", fixture_chain),
        ("star", fixture_star),
        ("mutual_eager", fixture_mutual_eager),
        ("asymmetric_cycle", fixture_asymmetric_cycle),
        ("fully_connected_small", fixture_fully_connected_small),
    ];
    for (name, builder) in fixtures {
        let (report, groups) = builder();
        let (mut q_full, mut q_lazy) = build_fixture(&report, &groups, 10_000);
        let full_scan_steps = greedy_merge_to_convergence_full_scan(&mut q_full);
        let lazy_pq_steps = greedy_merge_to_convergence(&mut q_lazy);
        assert_eq!(
            lazy_pq_steps, full_scan_steps,
            "[{name}] lazy-PQ greedy diverged from full-scan greedy"
        );
    }
}

#[test]
fn gate_bypassing_assignment_cycle_surfaces_and_recovers() {
    // `from_report_with_partition` bypasses the contraction gate: a
    // group that closes a module-graph cycle is legal input. Shape:
    // a → b → c (owner constraining edges), group {a, c}. Contracting
    // a and c yields the module cycle {a,c} → b → {a,c}. The kernel
    // must surface the cycle as evidence and keep gating correctly on
    // the unrealizable state (the ladder's CondensationOrder handles
    // cyclic condensations natively — no degraded mode). Distinct
    // active destinations keep each class on its own ModuleId so the
    // cycle stays visible to the realizability projection (an
    // all-residual fixture would collapse into one module and hide
    // it).
    let a = active_source_unit("unit:a", 1, &["BindingA"], 5, "ui/a");
    let b = active_source_unit("unit:b", 2, &["BindingB"], 5, "ui/b");
    let c = active_source_unit("unit:c", 3, &["BindingC"], 5, "ui/c");
    let report = singleton_graph(
        vec![a.clone(), b.clone(), c.clone()],
        vec![
            dependency_edge("edge:0", "unit:a", "unit:b", DepKind::EagerUse, true),
            dependency_edge("edge:1", "unit:b", "unit:c", DepKind::EagerUse, true),
        ],
    );
    let (mut q, group_classes) = QuotientGraph::from_report_with_partition(
        &report,
        10_000,
        &[PartitionGroup {
            source_unit_idxs: vec![SourceUnitIndex(0), SourceUnitIndex(2)],
            is_pre_existing_module: false,
        }],
    )
    .unwrap();
    let merged = group_classes[0];
    let b_class = q.class_of(SourceUnitIndex(1));
    assert_eq!(q.class_of(SourceUnitIndex(0)), merged);
    assert_eq!(q.class_of(SourceUnitIndex(2)), merged);

    // The cycle is visible to the kernel's verdict.
    assert!(
        !q.realizability_verdict().is_realizable(),
        "the bypassed contraction's class cycle must be unrealizable",
    );

    // The gate still answers on the unrealizable state: merging the
    // cycle classes together dissolves the cycle into one class, so
    // the contraction is permitted and the kernel returns to a
    // realizable, cycle-free state.
    let survivor = q.contract(merged, b_class).expect("cycle-dissolving merge");
    assert_eq!(q.class_of(SourceUnitIndex(1)), survivor);
    assert!(
        q.realizability_verdict().is_realizable(),
        "dissolving the cycle must restore realizability",
    );
}
