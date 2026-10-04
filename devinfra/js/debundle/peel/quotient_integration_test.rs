//! Integration tests for the `quotient` kernel and the `propose`
//! renderer-over-quotient. Fixtures seed quotients through the
//! test-only constructors in `quotient::testing`.

use analysis::{DepKind, OwnerGraphReport};
use report_fixtures::{
    active_owner, atomic_edge, atomic_unit_for, claims, graph_of, no_claims, owner_edge,
    residual_owner, singleton_graph,
};

use crate::propose::{ModuleProposal, propose};
use crate::quotient::testing::{
    PartitionGroup, greedy_merge_to_convergence_full_scan, module_group,
};
use crate::quotient::{
    ClassId, CycleClassSet, CycleEvidence, OwnerIdx, QuotientGraph, SeedContractionRejected,
    SpecModuleGroup, build_seed_quotient, greedy_merge_to_convergence,
};

fn spec_module(module_id: &str, owners: &[&str]) -> SpecModuleGroup {
    SpecModuleGroup {
        module_id: module_id.into(),
        owner_ids: owners.iter().map(|id| (*id).into()).collect(),
    }
}

// ---------- Tests. ----------

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
    let a1 = residual_owner("owner:a1", 1, &["BindingA1"], 5);
    let a2 = residual_owner("owner:a2", 2, &["BindingA2"], 5);
    let b1 = residual_owner("owner:b1", 3, &["BindingB1"], 5);
    let b2 = residual_owner("owner:b2", 4, &["BindingB2"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:a1", "owner:b1", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:b2", "owner:a2", DepKind::EagerUse, true),
    ];
    let report = singleton_graph(vec![a1.clone(), a2.clone(), b1.clone(), b2.clone()], edges);
    let spec = vec![
        spec_module("mod_alpha", &["owner:a1", "owner:a2"]),
        spec_module("mod_beta", &["owner:b1", "owner:b2"]),
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
        &("owner:b1".to_string(), "owner:b2".to_string()),
        "rejection should point at the b1<->b2 contraction",
    );
    assert!(!cycle.is_empty(), "cycle evidence must be non-empty");
    // The cycle evidence must mention both alpha-class owners and
    // the b1-class — the cycle the proposed contraction would join.
    let evidence_owners: Vec<&str> = cycle
        .cycles
        .iter()
        .flat_map(|c| c.owner_ids.iter().map(String::as_str))
        .collect();
    assert!(
        evidence_owners.contains(&"owner:a1") && evidence_owners.contains(&"owner:a2"),
        "cycle evidence should include alpha owners: {evidence_owners:?}",
    );
    assert!(
        evidence_owners.contains(&"owner:b1") || evidence_owners.contains(&"owner:b2"),
        "cycle evidence should include at least one beta owner: {evidence_owners:?}",
    );

    // mod_alpha did apply — a1 and a2 share a class.
    let a1_idx = q.owner_idx_of("owner:a1").unwrap();
    let a2_idx = q.owner_idx_of("owner:a2").unwrap();
    assert_eq!(q.class_of(a1_idx), q.class_of(a2_idx));
    // mod_beta did NOT apply — b1 and b2 are still in distinct
    // classes (the kernel never silently merged them).
    let b1_idx = q.owner_idx_of("owner:b1").unwrap();
    let b2_idx = q.owner_idx_of("owner:b2").unwrap();
    assert_ne!(q.class_of(b1_idx), q.class_of(b2_idx));
}

// ---------- Gate-ladder pinning tests. ----------
//
// These pin the module-level gate predicate that `check_merge_boolean`
// routes through.

/// A 3-owner atomic unit whose members form a constraining cycle
/// `a → b → c → a` exists precisely because its members MUST
/// co-locate, and the module-level predicate accepts the contractions
/// (all three owners project to the residual module, so every merge
/// is a delta-free no-op). A class-level cycle check would reject
/// them — the class graph is cyclic from construction — and the unit
/// could not seed.
#[test]
fn seed_co_locates_constraining_cycle_atomic_unit() {
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:a", "owner:b", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:b", "owner:c", DepKind::EagerUse, true),
        owner_edge("edge:2", "owner:c", "owner:a", DepKind::EagerUse, true),
    ];
    let unit = atomic_unit_for("atomic:0", &[&a, &b, &c]);
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        edges,
        vec![unit],
        vec![],
    );
    let (q, rejected) =
        build_seed_quotient(&report, &report.atomic_graph.nodes, &[], 10_000).unwrap();
    assert!(
        rejected.is_empty(),
        "atomic-unit contractions internal to the residual module are \
         delta-free no-ops under the module-level predicate and must \
         not be rejected: {rejected:?}",
    );
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let b_idx = q.owner_idx_of("owner:b").unwrap();
    let c_idx = q.owner_idx_of("owner:c").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
    assert_eq!(q.class_of(b_idx), q.class_of(c_idx));
}

/// Entry's implicit imports already close the runtime cycle for x -> r.
/// Merging an unrelated helper into x does not repair that dependency and
/// must remain rejected, with evidence naming the eager reader.
#[test]
fn merge_cannot_hide_an_existing_entry_dependency() {
    let x = active_owner("owner:x", 1, &["BindingX"], 10, "ui/x");
    let r = residual_owner("owner:r", 2, &["BindingR"], 5);
    let h = residual_owner("owner:h", 3, &["BindingH"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:x", "owner:r", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:r", "owner:h", DepKind::LazyUse, false),
    ];
    let report = singleton_graph(vec![x.clone(), r.clone(), h.clone()], edges);
    let groups = vec![module_group(vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition(&report, 10_000, &groups).unwrap();
    let cx = group_ids[0];
    let ch = q.class_of(q.owner_idx_of("owner:h").unwrap());

    // The source graph is acyclic, but entry's implicit import closes the
    // runtime cycle before the merge. The merge still leaves x -> r in place.
    assert!(!q.realizability_verdict().is_realizable());
    assert!(
        !q.merge_preserves_invariants(cx, ch),
        "merging owner:h into ui/x closes the asymmetric I-cycle \
         {{ui/x, residual}} with a TDZ'ing constraining pair; the \
         boolean gate must reject",
    );
    let evidence = q
        .would_be_cycles_after_contract(cx, ch)
        .expect("diagnostic gate must surface the Pass-2 rejection");
    let evidence_owners: Vec<&str> = evidence
        .cycles
        .iter()
        .flat_map(|cycle| cycle.owner_ids.iter().map(String::as_str))
        .collect();
    assert!(
        evidence_owners.contains(&"owner:x"),
        "evidence must mention the eager reader: {evidence_owners:?}",
    );
    assert!(
        q.contract(cx, ch).is_err(),
        "contract must refuse to commit the TDZ-closing merge",
    );
}

#[test]
fn seed_rejection_diagnostic_is_canonical() {
    // Same fixture run twice; rejection diagnostic byte-equal across
    // runs. Determinism check.
    let make_report = || {
        let a1 = residual_owner("owner:a1", 1, &["BindingA1"], 5);
        let a2 = residual_owner("owner:a2", 2, &["BindingA2"], 5);
        let b1 = residual_owner("owner:b1", 3, &["BindingB1"], 5);
        let b2 = residual_owner("owner:b2", 4, &["BindingB2"], 5);
        let edges = vec![
            owner_edge("edge:0", "owner:a1", "owner:b1", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:b2", "owner:a2", DepKind::EagerUse, true),
        ];
        singleton_graph(vec![a1.clone(), a2.clone(), b1.clone(), b2.clone()], edges)
    };
    let spec = vec![
        spec_module("mod_alpha", &["owner:a1", "owner:a2"]),
        spec_module("mod_beta", &["owner:b1", "owner:b2"]),
    ];

    let report_a = make_report();
    let (_q1, rejected_a) =
        build_seed_quotient(&report_a, &report_a.atomic_graph.nodes, &spec, 10_000).unwrap();
    let report_b = make_report();
    let (_q2, rejected_b) =
        build_seed_quotient(&report_b, &report_b.atomic_graph.nodes, &spec, 10_000).unwrap();

    let json_a = serde_json::to_string_pretty(&rejected_a).unwrap();
    let json_b = serde_json::to_string_pretty(&rejected_b).unwrap();
    assert_eq!(
        json_a, json_b,
        "rejection diagnostic must be byte-identical across runs",
    );
}

#[test]
fn post_seed_reports_each_unrealizable_scc_in_owner_id_order() {
    // Two independent mutual-eager module pairs, (c, d) before (a, b)
    // in the report. Every spec module is one owner, so no seed
    // contraction is refused and only the post-seed gate sees the
    // cycles. The report orders them by owner ids, not by class ids.
    let owners = [
        active_owner("owner:c", 1, &["BindingC"], 5, "mod_c"),
        active_owner("owner:d", 2, &["BindingD"], 5, "mod_d"),
        active_owner("owner:a", 3, &["BindingA"], 5, "mod_a"),
        active_owner("owner:b", 4, &["BindingB"], 5, "mod_b"),
    ];
    let edges = vec![
        owner_edge("edge:0", "owner:c", "owner:d", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:d", "owner:c", DepKind::EagerUse, true),
        owner_edge("edge:2", "owner:a", "owner:b", DepKind::EagerUse, true),
        owner_edge("edge:3", "owner:b", "owner:a", DepKind::EagerUse, true),
    ];
    let units = owners
        .iter()
        .enumerate()
        .map(|(i, owner)| atomic_unit_for(&format!("atomic:{i}"), &[owner]))
        .collect();
    let spec: Vec<SpecModuleGroup> = owners
        .iter()
        .map(|owner| SpecModuleGroup {
            module_id: owner.destination.as_str().to_string(),
            owner_ids: vec![owner.id.clone()],
        })
        .collect();
    let report = graph_of(owners.to_vec(), edges, units, vec![]);

    let (_q, rejected) =
        build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000).unwrap();

    let scc = |owner_ids: [&str; 2], classes: [usize; 2]| {
        let owner_ids = owner_ids.map(String::from).to_vec();
        SeedContractionRejected::PostSeedUnrealizableScc {
            owner_ids: owner_ids.clone(),
            cycle: CycleEvidence {
                cycles: vec![CycleClassSet {
                    classes: classes.map(ClassId).to_vec(),
                    owner_ids,
                }],
            },
        }
    };
    assert_eq!(
        rejected,
        vec![
            scc(["owner:a", "owner:b"], [2, 3]),
            scc(["owner:c", "owner:d"], [0, 1]),
        ],
    );
}

#[test]
fn contract_never_un_contracts() {
    // API surface check: after a contraction, the involved owners
    // remain in the same class no matter what subsequent operations
    // are performed. There is no public `split` / `un_contract` /
    // `set_class` on QuotientGraph; the only mutation is
    // `contract`, which is monotone (coarsens `~`).
    //
    // We verify this empirically by:
    //   1. Building a fresh quotient.
    //   2. Contracting (c(a), c(b)).
    //   3. Performing every other contraction the kernel allows and
    //      asserting that c(a) == c(b) after each.
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    let d = residual_owner("owner:d", 4, &["BindingD"], 5);
    let report = singleton_graph(vec![a.clone(), b.clone(), c.clone(), d.clone()], vec![]);
    let mut q = QuotientGraph::from_report(&report, 10_000).unwrap();
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let b_idx = q.owner_idx_of("owner:b").unwrap();
    let c_idx = q.owner_idx_of("owner:c").unwrap();
    let d_idx = q.owner_idx_of("owner:d").unwrap();

    let ca = q.class_of(a_idx);
    let cb = q.class_of(b_idx);
    q.contract(ca, cb).expect("contract(a, b)");
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));

    // After contracting (c, d), a and b still share a class.
    let cc = q.class_of(c_idx);
    let cd = q.class_of(d_idx);
    q.contract(cc, cd).expect("contract(c, d)");
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));

    // After contracting (a-class, c-class), all four share a
    // class — a and b are still together.
    let cab = q.class_of(a_idx);
    let ccd = q.class_of(c_idx);
    q.contract(cab, ccd).expect("contract(ab, cd)");
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
    assert_eq!(q.class_of(a_idx), q.class_of(c_idx));
    assert_eq!(q.class_of(a_idx), q.class_of(d_idx));
}

#[test]
fn factorize_golden_output_unchanged() {
    // Golden test: propose's output stays byte-identical for the
    // same representative inputs. The renderer-over-quotient path
    // must keep these outputs stable unless the proposal contract
    // intentionally changes.
    //
    // Each fixture exercises a representative shape:
    //   - `residual_singletons`: two unrelated residual owners,
    //     no edges.
    //   - `closed_residual_unit`: two residual units coupled by
    //     a constraining edge.
    //   - `extend_active_via_anon`: an anonymous statement whose
    //     unique constraining edge points at an active module.
    //
    // Snapshots live at `devinfra/js/debundle/peel/golden/`. To
    // regenerate (only after a deliberate, justified change), set
    // `UPDATE_GOLDENS=1` when running the test.
    let f1 = propose(&golden_residual_singletons(), &no_claims(), 10_000).unwrap();
    let f2 = propose(&golden_closed_residual_unit(), &no_claims(), 10_000).unwrap();
    let claims_active = claims(&[("BindingA", "ui/x")]);
    let f3 = propose(&golden_extend_active_via_anon(), &claims_active, 10_000).unwrap();

    let json1 = serde_json::to_string_pretty(&f1).unwrap();
    let json2 = serde_json::to_string_pretty(&f2).unwrap();
    let json3 = serde_json::to_string_pretty(&f3).unwrap();

    // Strip a single trailing newline from each golden file before
    // comparing — JSON formatters and pre-commit hooks routinely
    // add one, while `serde_json::to_string_pretty` doesn't. The
    // semantic content is what we're locking down, not whether
    // pre-commit thinks the file ends in a newline.
    let golden1 = include_str!("golden/residual_singletons.json").trim_end_matches('\n');
    let golden2 = include_str!("golden/closed_residual_unit.json").trim_end_matches('\n');
    let golden3 = include_str!("golden/extend_active_via_anon.json").trim_end_matches('\n');

    assert_eq!(
        json1, golden1,
        "residual_singletons fixture diverged from golden baseline",
    );
    assert_eq!(
        json2, golden2,
        "closed_residual_unit fixture diverged from golden baseline",
    );
    assert_eq!(
        json3, golden3,
        "extend_active_via_anon fixture diverged from golden baseline",
    );
}

fn golden_residual_singletons() -> OwnerGraphReport {
    let a = residual_owner("owner:a", 1, &["BindingA"], 10);
    let b = residual_owner("owner:b", 2, &["BindingB"], 10);
    singleton_graph(vec![a.clone(), b.clone()], vec![])
}

fn golden_closed_residual_unit() -> OwnerGraphReport {
    let a = residual_owner("owner:a", 1, &["BindingA"], 10);
    let b = residual_owner("owner:b", 2, &["BindingB"], 10);
    graph_of(
        vec![a.clone(), b.clone()],
        vec![owner_edge(
            "edge:0",
            "owner:a",
            "owner:b",
            DepKind::EagerUse,
            true,
        )],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
        ],
        vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
    )
}

fn golden_extend_active_via_anon() -> OwnerGraphReport {
    // BindingA is in an active module ui/x. An anonymous statement
    // (no declared bindings) has one constraining edge into a.
    // propose should promote it to extend:ui/x.
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let anon = residual_owner("owner:anon", 2, &[], 5);
    graph_of(
        vec![a.clone(), anon.clone()],
        vec![owner_edge(
            "edge:0",
            "owner:anon",
            "owner:a",
            DepKind::EagerUse,
            true,
        )],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&anon]),
        ],
        vec![atomic_edge("atomic_edge:0", "atomic:1", "atomic:0")],
    )
}

// ---------- Greedy merge to convergence tests. ----------
//
// The greedy operates over a quotient whose initial partition the
// caller has chosen — typically the seed quotient (atomic units +
// spec modules pre-contracted) augmented with whatever classes the
// renderer has marked as pre-existing active modules. The kernel
// distinguishes "pre-existing module" classes from "residual orphan"
// classes via the `is_pre_existing_module` bit on each class; the
// mergeability rules govern whether greedy may extend a module with an
// orphan, merge existing modules, or stop.
//
// All fixtures below use `from_report_with_partition`, the
// constructor that takes per-group metadata (lines + the
// pre-existing-module bit). Owners not in any group remain singletons
// with their per-owner residual flag derived from the report.

#[test]
fn greedy_absorbs_tiny_named_helper_into_unique_consumer() {
    // Pre-existing module M = {owner:a (BindingA)}. Residual
    // owner:helper (BindingHelper) is read only by owner:a via an
    // EagerUse edge. Its unique consumer is the only module
    // neighbor, so greedy should absorb it.
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let helper = residual_owner("owner:helper", 2, &["BindingHelper"], 5);
    let report = graph_of(
        vec![a.clone(), helper.clone()],
        vec![owner_edge(
            "edge:0",
            "owner:a",
            "owner:helper",
            DepKind::EagerUse,
            true,
        )],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&helper]),
        ],
        vec![atomic_edge("atomic_edge:0", "atomic:0", "atomic:1")],
    );

    let groups = vec![module_group(vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition(&report, 10_000, &groups).unwrap();
    let contractions = greedy_merge_to_convergence(&mut q);

    assert_eq!(contractions.len(), 1, "got: {contractions:?}");
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let helper_idx = q.owner_idx_of("owner:helper").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(helper_idx));
    assert_eq!(q.class_of(a_idx), group_ids[0]);
}

#[test]
fn greedy_terminates_at_convergence() {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let h1 = residual_owner("owner:h1", 2, &["BindingH1"], 5);
    let h2 = residual_owner("owner:h2", 3, &["BindingH2"], 5);
    let h3 = residual_owner("owner:h3", 4, &["BindingH3"], 5);
    // Start realizable: residual helpers depend on the emitted module,
    // not vice versa. Each contraction preserves that entry-last invariant.
    let report = singleton_graph(
        vec![a.clone(), h1.clone(), h2.clone(), h3.clone()],
        vec![
            owner_edge("edge:0", "owner:h1", "owner:a", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:h2", "owner:a", DepKind::EagerUse, true),
            owner_edge("edge:2", "owner:h3", "owner:a", DepKind::EagerUse, true),
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

#[test]
fn greedy_never_splits_existing_spec_module() {
    let a1 = active_owner("owner:a1", 1, &["BindingA1"], 10, "ui/x");
    let a2 = active_owner("owner:a2", 2, &["BindingA2"], 10, "ui/x");
    let h = residual_owner("owner:h", 3, &["BindingH"], 5);
    let report = singleton_graph(
        vec![a1.clone(), a2.clone(), h.clone()],
        vec![owner_edge(
            "edge:0",
            "owner:a1",
            "owner:h",
            DepKind::EagerUse,
            true,
        )],
    );

    let groups = vec![module_group(vec![0, 1])];
    let (mut q, _) = QuotientGraph::from_report_with_partition(&report, 10_000, &groups).unwrap();
    let _ = greedy_merge_to_convergence(&mut q);

    let a1_idx = q.owner_idx_of("owner:a1").unwrap();
    let a2_idx = q.owner_idx_of("owner:a2").unwrap();
    assert_eq!(
        q.class_of(a1_idx),
        q.class_of(a2_idx),
        "spec-module owners must stay co-located",
    );
}

// ---------- Full mergeability + merge output shape. ----------
//
// The gate allows two pre-existing-module classes to merge (with or
// without absorbing residual orphans). The renderer carries
// `merge_into: Option<Vec<String>>` on proposals whose operands
// include ≥2 pre-existing-module groups.

#[test]
fn greedy_merges_three_clusters_under_cap() {
    // Three pre-existing-module clusters of 20 + 20 + 10 lines, all
    // mutually coupled by EagerUse edges. Cap = 150 → all three fit.
    // Assert greedy merges all three into one class.
    let a = active_owner("owner:a", 1, &["BindingA"], 20, "ui/a");
    let b = active_owner("owner:b", 2, &["BindingB"], 20, "ui/b");
    let c = active_owner("owner:c", 3, &["BindingC"], 10, "ui/c");
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        vec![
            owner_edge("edge:ab", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:bc", "owner:b", "owner:c", DepKind::EagerUse, true),
            owner_edge("edge:ac", "owner:a", "owner:c", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:a", &[&a]),
            atomic_unit_for("atomic:b", &[&b]),
            atomic_unit_for("atomic:c", &[&c]),
        ],
        vec![],
    );
    let groups = vec![
        module_group(vec![0]),
        module_group(vec![1]),
        module_group(vec![2]),
    ];
    let (mut q, _) = QuotientGraph::from_report_with_partition(&report, 150, &groups).unwrap();
    let contractions = greedy_merge_to_convergence(&mut q);
    assert_eq!(
        contractions.len(),
        2,
        "three clusters under cap should collapse via 2 contractions: {contractions:?}",
    );
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let b_idx = q.owner_idx_of("owner:b").unwrap();
    let c_idx = q.owner_idx_of("owner:c").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
    assert_eq!(q.class_of(b_idx), q.class_of(c_idx));
}

#[test]
fn greedy_stops_at_cap() {
    // Same fixture as `greedy_merges_three_clusters_under_cap`, cap
    // = 40 lines. After the first merge the surviving class is 40
    // lines; the third cluster's 10-line addition would tip it to
    // 50, exceeding the cap. Assert exactly one contraction occurred.
    let a = active_owner("owner:a", 1, &["BindingA"], 20, "ui/a");
    let b = active_owner("owner:b", 2, &["BindingB"], 20, "ui/b");
    let c = active_owner("owner:c", 3, &["BindingC"], 10, "ui/c");
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        vec![
            owner_edge("edge:ab", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:bc", "owner:b", "owner:c", DepKind::EagerUse, true),
            owner_edge("edge:ac", "owner:a", "owner:c", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:a", &[&a]),
            atomic_unit_for("atomic:b", &[&b]),
            atomic_unit_for("atomic:c", &[&c]),
        ],
        vec![],
    );
    let groups = vec![
        module_group(vec![0]),
        module_group(vec![1]),
        module_group(vec![2]),
    ];
    let (mut q, _) = QuotientGraph::from_report_with_partition(&report, 40, &groups).unwrap();
    let contractions = greedy_merge_to_convergence(&mut q);
    assert_eq!(
        contractions.len(),
        1,
        "cap=40 must allow exactly one merge: {contractions:?}",
    );
    // Determinism: candidates are (a, b)=20+20=40, (a, c)=20+10=30
    // (cycle-creating; rejected), (b, c)=20+10=30. After cycle
    // filtering, (b, c) wins on result-size (30 < 40); (a, b) is
    // refused on second-pass cap because its 40-line survivor +
    // 10-line orphan would exceed 40 anyway, but it's picked
    // strictly after (b, c) by the tiebreak.
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let b_idx = q.owner_idx_of("owner:b").unwrap();
    let c_idx = q.owner_idx_of("owner:c").unwrap();
    assert_eq!(
        q.class_of(b_idx),
        q.class_of(c_idx),
        "owner:b and owner:c should be merged (smallest combined size wins tiebreak)",
    );
    assert_ne!(
        q.class_of(a_idx),
        q.class_of(b_idx),
        "owner:a stays separate (a+bc would total 50, over cap 40)",
    );
}

#[test]
fn greedy_resolves_realizability_cycle_by_merging() {
    // Asymmetric I-cycle: mod_a → mod_b (EagerUse constraining) and
    // mod_b → mod_a (LazyUse non-constraining). The constraining
    // edge alone doesn't cycle, but the symmetric coupling makes
    // them a strong merge candidate; after merge the post-merge
    // quotient is realizable (intra-class self-loop).
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/a");
    let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/b");
    let report = graph_of(
        vec![a.clone(), b.clone()],
        vec![
            owner_edge("edge:ab", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:ba", "owner:b", "owner:a", DepKind::LazyUse, false),
        ],
        vec![
            atomic_unit_for("atomic:a", &[&a]),
            atomic_unit_for("atomic:b", &[&b]),
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
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let b_idx = q.owner_idx_of("owner:b").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
    // Post-merge quotient: no remaining cross-class cycles.
    let verdict = q.realizability_verdict();
    assert!(
        verdict.is_realizable(),
        "post-merge quotient should be realizable: {verdict:?}",
    );
}

#[test]
fn merge_absorbs_residual_owner_with_only_intra_deps() {
    // mod_a + mod_b mutually coupled; residual `helper` reads from
    // both. Assert the merge proposal's operands include
    // `owner:helper`.
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/a");
    let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/b");
    let helper = residual_owner("owner:helper", 3, &["BindingHelper"], 5);
    let report = graph_of(
        vec![a.clone(), b.clone(), helper.clone()],
        vec![
            owner_edge("edge:ab", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:ba", "owner:b", "owner:a", DepKind::EagerUse, true),
            owner_edge(
                "edge:helper_a",
                "owner:helper",
                "owner:a",
                DepKind::EagerUse,
                true,
            ),
            owner_edge(
                "edge:helper_b",
                "owner:helper",
                "owner:b",
                DepKind::EagerUse,
                true,
            ),
        ],
        vec![
            atomic_unit_for("atomic:a", &[&a]),
            atomic_unit_for("atomic:b", &[&b]),
            atomic_unit_for("atomic:helper", &[&helper]),
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
            .extension_owner_ids
            .contains(&"owner:helper".to_string()),
        "merge proposal should list owner:helper as an extension owner: {merge:?}",
    );
}

// ---------- Atomic-DAG reachability seeding. ----------
//
// The third gated contraction pass in `build_seed_quotient` groups
// residual owners by atomic-DAG reachability. Well-formed input stays
// stable (locked down by `factorize_golden_output_unchanged` above).
// Input whose atomic-DAG reachability closure would form a cycle gets
// a `SeedContractionRejected::AtomicReachability` diagnostic
// pinpointing the rejected pair.

#[test]
fn pass3_diagnostic_walk_never_commits_a_merge() {
    // Invariant guard for the pass-3 diagnostic walk in
    // `build_seed_quotient`. After the fixed-point contraction loop
    // exits (zero successful contractions), the diagnostic walk
    // re-classifies each still-unmerged atomic-DAG edge to record
    // *why* it could not contract. That walk must be read-only: it
    // must never commit a merge. A stray successful contraction there
    // would join two classes the fixed-point loop deliberately left
    // apart, silently corrupting the partition that the post-seed
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
    let foo = active_owner("owner:foo", 1, &["Foo"], 5, "mod_alpha");
    let bar = residual_owner("owner:bar", 2, &["Bar"], 5);
    let helper = residual_owner("owner:helper", 3, &["Helper"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:bar", "owner:foo", DepKind::EagerUse, true),
        owner_edge(
            "edge:1",
            "owner:foo",
            "owner:helper",
            DepKind::EagerUse,
            true,
        ),
        owner_edge(
            "edge:2",
            "owner:helper",
            "owner:bar",
            DepKind::EagerUse,
            true,
        ),
    ];
    let report = graph_of(
        vec![foo.clone(), bar.clone(), helper.clone()],
        edges,
        vec![
            atomic_unit_for("atomic:foo", &[&foo]),
            atomic_unit_for("atomic:bar", &[&bar]),
            atomic_unit_for("atomic:helper", &[&helper]),
        ],
        vec![
            atomic_edge("atomic_edge:a", "atomic:bar", "atomic:foo"),
            atomic_edge("atomic_edge:b", "atomic:foo", "atomic:helper"),
            atomic_edge("atomic_edge:c", "atomic:helper", "atomic:bar"),
        ],
    );
    let spec = vec![spec_module("mod_alpha", &["owner:foo"])];
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
        let (src_owner, tgt_owner) = rejected_pair;
        let src_idx = q
            .owner_idx_of(src_owner)
            .unwrap_or_else(|| panic!("rejected source owner {src_owner} must be in graph"));
        let tgt_idx = q
            .owner_idx_of(tgt_owner)
            .unwrap_or_else(|| panic!("rejected target owner {tgt_owner} must be in graph"));
        assert_ne!(
            q.class_of(src_idx),
            q.class_of(tgt_idx),
            "diagnostic walk must NOT have committed the rejected merge of \
             {src_owner} and {tgt_owner}; they must remain in distinct classes",
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
// run on the projected partition. A planner that reimplemented
// Tarjan over only constraining edges in the JSON report would miss
// asymmetric I-cycles the materializer catches via its
// `EsmEvaluationSimulator` pass. The tests below pin the agreement:
// every planner verdict must match the materializer verdict on the
// same input.

/// Build an `OwnerGraph` + `Partition` from a report + spec module
/// list. The partition assigns each owner to the module derived from
/// its `destination.id`; residual destinations land on the residual
/// `ModuleId`. Used by the planner-vs-materializer cross-check tests.
fn owner_graph_and_partition_from_spec(
    report: &analysis::OwnerGraphReport,
    spec: &[SpecModuleGroup],
) -> (analysis::OwnerGraph, analysis::Partition) {
    use std::collections::HashMap;
    let owner_graph = analysis::OwnerGraph::from_report(report).unwrap();
    let owner_index: HashMap<&str, usize> = report
        .nodes
        .iter()
        .enumerate()
        .map(|(i, node)| (node.id.as_str(), i))
        .collect();
    // Module-id assignment: residual goes to ModuleId(0). Every
    // distinct spec module gets its own ModuleId starting at 1.
    let residual = analysis::ModuleId::logical(0);
    let mut spec_module_ids: HashMap<&str, analysis::ModuleId> = HashMap::new();
    let mut next_idx = 1usize;
    for module in spec {
        spec_module_ids
            .entry(module.module_id.as_str())
            .or_insert_with(|| {
                let m = analysis::ModuleId::logical(next_idx);
                next_idx += 1;
                m
            });
    }
    // Owner→ModuleId: by default residual; spec modules override.
    let mut of: Vec<analysis::ModuleId> = vec![residual; owner_graph.num_nodes()];
    for module in spec {
        let mid = spec_module_ids[module.module_id.as_str()];
        for owner_id in &module.owner_ids {
            if let Some(&o) = owner_index.get(owner_id.as_str()) {
                of[o] = mid;
            }
        }
    }
    let partition = analysis::Partition::from_assignments(of, residual);
    (owner_graph, partition)
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
        report: analysis::OwnerGraphReport,
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
        let a = active_owner("owner:a", 1, &["BindingA"], 5, "mod_solo");
        let b = active_owner("owner:b", 2, &["BindingB"], 5, "mod_solo");
        cases.push(Case {
            label: "single_module_intra_edges",
            report: singleton_graph(
                vec![a.clone(), b.clone()],
                vec![owner_edge(
                    "edge:0",
                    "owner:a",
                    "owner:b",
                    analysis::DepKind::EagerUse,
                    true,
                )],
            ),
            spec: vec![spec_module("mod_solo", &["owner:a", "owner:b"])],
        });
    }

    // Case 3: asymmetric I-cycle through a mediator (Lemma 2 cannot
    // rescue). Mirrors the materializer's
    // `mediator_reaches_asymmetric_cycle_test` shape — three
    // modules, residual reaches the SCC only via a non-residual
    // mediator. The SCC's constraining edge TDZs at runtime.
    {
        let entry = residual_owner("owner:entry", 0, &[], 1);
        let dep_value = active_owner("owner:dep_value", 1, &["BindingDepValue"], 5, "mod_dep");
        let lazy_reader =
            active_owner("owner:lazy_reader", 2, &["BindingLazyReader"], 5, "mod_dep");
        let cross_value = active_owner(
            "owner:cross_value",
            3,
            &["BindingCrossValue"],
            5,
            "mod_dependent",
        );
        let mediator_helper = active_owner(
            "owner:mediator_helper",
            4,
            &["BindingMediatorHelper"],
            5,
            "mod_mediator",
        );
        let mediator_init = active_owner(
            "owner:mediator_init",
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
                    owner_edge(
                        "edge:entry_mediator",
                        "owner:entry",
                        "owner:mediator_init",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    owner_edge(
                        "edge:mediator_dep",
                        "owner:mediator_helper",
                        "owner:dep_value",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                    owner_edge(
                        "edge:mediator_intra",
                        "owner:mediator_init",
                        "owner:mediator_helper",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    owner_edge(
                        "edge:dependent_dep",
                        "owner:cross_value",
                        "owner:dep_value",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    owner_edge(
                        "edge:dep_back",
                        "owner:lazy_reader",
                        "owner:cross_value",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                ],
            ),
            spec: vec![
                spec_module("mod_dep", &["owner:dep_value", "owner:lazy_reader"]),
                spec_module("mod_dependent", &["owner:cross_value"]),
                spec_module(
                    "mod_mediator",
                    &["owner:mediator_helper", "owner:mediator_init"],
                ),
            ],
        });
    }

    // Case 4: mutual constraining cycle.
    {
        let a1 = residual_owner("owner:a1", 1, &["BindingA1"], 5);
        let b1 = residual_owner("owner:b1", 2, &["BindingB1"], 5);
        cases.push(Case {
            label: "mutual_constraining_cycle",
            report: singleton_graph(
                vec![a1.clone(), b1.clone()],
                vec![
                    owner_edge(
                        "edge:fwd",
                        "owner:a1",
                        "owner:b1",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    owner_edge(
                        "edge:back",
                        "owner:b1",
                        "owner:a1",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                ],
            ),
            // Note: residual destinations — no spec modules. The
            // planner's seed pass merges atomic units only; since
            // each atomic is a singleton, no contractions happen and
            // no rejection fires. The materializer also sees the
            // SCC purely within residual (one module) and doesn't
            // flag it (intra-module). Both should agree: realizable.
            spec: vec![],
        });
    }

    // Case 5: lazy-only cross-module edges.
    {
        let alpha = active_owner("owner:alpha", 1, &["BindingAlpha"], 5, "mod_alpha");
        let beta = active_owner("owner:beta", 2, &["BindingBeta"], 5, "mod_beta");
        cases.push(Case {
            label: "lazy_only_cross_module",
            report: singleton_graph(
                vec![alpha.clone(), beta.clone()],
                vec![
                    owner_edge(
                        "edge:0",
                        "owner:alpha",
                        "owner:beta",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                    owner_edge(
                        "edge:1",
                        "owner:beta",
                        "owner:alpha",
                        analysis::DepKind::LazyUse,
                        false,
                    ),
                ],
            ),
            spec: vec![
                spec_module("mod_alpha", &["owner:alpha"]),
                spec_module("mod_beta", &["owner:beta"]),
            ],
        });
    }

    let mut materializer_unrealizable_labels = Vec::new();
    for case in &cases {
        // Materializer-side.
        let (owner_graph, partition) =
            owner_graph_and_partition_from_spec(&case.report, &case.spec);
        let verdict = gate::check_realizability(&owner_graph, &partition);
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
    report: &OwnerGraphReport,
    groups: &[PartitionGroup],
    cap_lines: usize,
) -> (QuotientGraph, QuotientGraph) {
    let (q_a, _) = QuotientGraph::from_report_with_partition(report, cap_lines, groups).unwrap();
    let (q_b, _) = QuotientGraph::from_report_with_partition(report, cap_lines, groups).unwrap();
    (q_a, q_b)
}

/// Fixture 1: chain. One pre-existing module a; orphans b → c → d → e
/// chain backward into a. Greedy should absorb them sequentially.
fn fixture_chain() -> (OwnerGraphReport, Vec<PartitionGroup>) {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    let d = residual_owner("owner:d", 4, &["BindingD"], 5);
    let e = residual_owner("owner:e", 5, &["BindingE"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:b", "owner:a", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:c", "owner:b", DepKind::EagerUse, true),
        owner_edge("edge:2", "owner:d", "owner:c", DepKind::EagerUse, true),
        owner_edge("edge:3", "owner:e", "owner:d", DepKind::EagerUse, true),
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
fn fixture_star() -> (OwnerGraphReport, Vec<PartitionGroup>) {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    let d = residual_owner("owner:d", 4, &["BindingD"], 5);
    let e = residual_owner("owner:e", 5, &["BindingE"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:b", "owner:a", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:c", "owner:a", DepKind::EagerUse, true),
        owner_edge("edge:2", "owner:d", "owner:a", DepKind::EagerUse, true),
        owner_edge("edge:3", "owner:e", "owner:a", DepKind::EagerUse, true),
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
fn fixture_mutual_eager() -> (OwnerGraphReport, Vec<PartitionGroup>) {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
    let h1 = residual_owner("owner:h1", 3, &["BindingH1"], 5);
    let h2 = residual_owner("owner:h2", 4, &["BindingH2"], 5);
    let edges = vec![
        // h1 reads a (constraining); h2 reads b (constraining).
        owner_edge("edge:0", "owner:h1", "owner:a", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:h2", "owner:b", DepKind::EagerUse, true),
        // Cross-module non-constraining lazy edges (a ↔ b) — present
        // for coupling, not for constraining adjacency.
        owner_edge("edge:2", "owner:a", "owner:b", DepKind::LazyUse, false),
        owner_edge("edge:3", "owner:b", "owner:a", DepKind::LazyUse, false),
    ];
    let report = singleton_graph(vec![a.clone(), b.clone(), h1.clone(), h2.clone()], edges);
    let groups = vec![module_group(vec![0]), module_group(vec![1])];
    (report, groups)
}

/// Fixture 4: asymmetric cycle that the greedy must resolve by
/// contracting one pair, dissolving the other side of the cycle.
/// Two modules a, b with constraining edges a → b and b → a (via
/// helpers); greedy should pick one merge.
fn fixture_asymmetric_cycle() -> (OwnerGraphReport, Vec<PartitionGroup>) {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
    let h = residual_owner("owner:h", 3, &["BindingH"], 5);
    let edges = vec![
        // a → h → b is the forward path; b → a directly closes the
        // cycle. h is an orphan with a unique extension target (a).
        owner_edge("edge:0", "owner:a", "owner:h", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:h", "owner:b", DepKind::EagerUse, true),
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
fn fixture_fully_connected_small() -> (OwnerGraphReport, Vec<PartitionGroup>) {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
    let c = active_owner("owner:c", 3, &["BindingC"], 10, "ui/z");
    let oa = residual_owner("owner:oa", 4, &["BindingOA"], 5);
    let ob = residual_owner("owner:ob", 5, &["BindingOB"], 5);
    let oc = residual_owner("owner:oc", 6, &["BindingOC"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:oa", "owner:a", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:ob", "owner:b", DepKind::EagerUse, true),
        owner_edge("edge:2", "owner:oc", "owner:c", DepKind::EagerUse, true),
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

type Fixture = (OwnerGraphReport, Vec<PartitionGroup>);
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
fn gate_bypassing_partition_cycle_surfaces_and_recovers() {
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
    let a = active_owner("owner:a", 1, &["BindingA"], 5, "ui/a");
    let b = active_owner("owner:b", 2, &["BindingB"], 5, "ui/b");
    let c = active_owner("owner:c", 3, &["BindingC"], 5, "ui/c");
    let report = singleton_graph(
        vec![a.clone(), b.clone(), c.clone()],
        vec![
            owner_edge("edge:0", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:b", "owner:c", DepKind::EagerUse, true),
        ],
    );
    let (mut q, group_classes) = QuotientGraph::from_report_with_partition(
        &report,
        10_000,
        &[PartitionGroup {
            owner_idxs: vec![OwnerIdx(0), OwnerIdx(2)],
            is_pre_existing_module: false,
        }],
    )
    .unwrap();
    let merged = group_classes[0];
    let b_class = q.class_of(OwnerIdx(1));
    assert_eq!(q.class_of(OwnerIdx(0)), merged);
    assert_eq!(q.class_of(OwnerIdx(2)), merged);

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
    assert_eq!(q.class_of(OwnerIdx(1)), survivor);
    assert!(
        q.realizability_verdict().is_realizable(),
        "dissolving the cycle must restore realizability",
    );
}
