//! Integration tests for the `peel::quotient` kernel and the
//! `factorize` renderer-over-quotient. Compiled against `:peel`'s
//! public API as a separate crate — the same surface external
//! consumers of the kernel see.
//!
//! Test list (commit 1 + 1b of `plans/peel_proposer_contraction_model.md`):
//!
//! - `seed_pre_contracts_atomic_units`
//! - `seed_pre_contracts_spec_modules`
//! - `seed_skips_unrealizable_spec_module_contraction_and_reports`
//! - `seed_atomic_unit_contractions_never_rejected_on_well_formed_input`
//! - `seed_rejection_diagnostic_is_canonical`
//! - `contract_never_un_contracts`
//! - `factorize_golden_output_unchanged` — load-bearing snapshot
//!   assertion that the renderer-over-quotient produces byte-identical
//!   output to the pre-commit-1 binary.
//! - `partition_constructor_contracts_each_group` — internal
//!   invariant of commit 1b: the partition-based kernel constructor
//!   collapses each input group into one class, regardless of
//!   pre-existing edges between the owners.

use analysis::{
    AtomicUnitEdgeReport, DepKind, OwnerGraphNodeReport, OwnerGraphReport, Purity, SourceLocation,
    StatementKind, StatementOrdinal,
};
use gate::{RealizabilityVerdict, check_realizability};

use peel::factorize::factorize;
use peel::quotient::{
    OwnerIdx, QuotientGraph, SeedContractionRejected, SpecModuleGroup, build_seed_quotient,
    greedy_merge_to_convergence, greedy_merge_to_convergence_full_scan,
};
use report_fixtures::{
    active_owner, atomic_edge, atomic_unit_for, claims, graph_of, module_ref, no_claims,
    owner_edge, residual_owner,
};

// ---------- Tests. ----------

#[test]
fn seed_pre_contracts_atomic_units() {
    // Fixture: a 3-binding atomic unit. After seeding, all three
    // owners must share a class.
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    let unit = atomic_unit_for("atomic:0", &[&a, &b, &c]);
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        vec![],
        vec![unit.clone()],
        vec![],
    );
    let (q, rejected) = build_seed_quotient(&report, &report.atomic_graph.nodes, &[], 10_000);
    assert!(
        rejected.is_empty(),
        "well-formed atomic unit must not produce rejections: {rejected:?}",
    );
    let a_idx = q.owner_idx_of("owner:a").expect("a in graph");
    let b_idx = q.owner_idx_of("owner:b").expect("b in graph");
    let c_idx = q.owner_idx_of("owner:c").expect("c in graph");
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
    assert_eq!(q.class_of(b_idx), q.class_of(c_idx));
}

#[test]
fn seed_pre_contracts_spec_modules() {
    // Fixture: spec module declares two owners. After seeding, they
    // must share a class.
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let report = graph_of(
        vec![a.clone(), b.clone()],
        vec![],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
        ],
        vec![],
    );
    let spec = vec![SpecModuleGroup {
        module_id: "mod_alpha".to_string(),
        owner_ids: vec!["owner:a".to_string(), "owner:b".to_string()],
    }];
    let (q, rejected) = build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000);
    assert!(
        rejected.is_empty(),
        "well-formed spec module must not produce rejections: {rejected:?}",
    );
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let b_idx = q.owner_idx_of("owner:b").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(b_idx));
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
    let a1 = residual_owner("owner:a1", 1, &["BindingA1"], 5);
    let a2 = residual_owner("owner:a2", 2, &["BindingA2"], 5);
    let b1 = residual_owner("owner:b1", 3, &["BindingB1"], 5);
    let b2 = residual_owner("owner:b2", 4, &["BindingB2"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:a1", "owner:b1", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:b2", "owner:a2", DepKind::EagerUse, true),
    ];
    let report = graph_of(
        vec![a1.clone(), a2.clone(), b1.clone(), b2.clone()],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&a1]),
            atomic_unit_for("atomic:1", &[&a2]),
            atomic_unit_for("atomic:2", &[&b1]),
            atomic_unit_for("atomic:3", &[&b2]),
        ],
        vec![],
    );
    let spec = vec![
        SpecModuleGroup {
            module_id: "mod_alpha".to_string(),
            owner_ids: vec!["owner:a1".to_string(), "owner:a2".to_string()],
        },
        SpecModuleGroup {
            module_id: "mod_beta".to_string(),
            owner_ids: vec!["owner:b1".to_string(), "owner:b2".to_string()],
        },
    ];
    let (q, rejected) = build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000);

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

// ---------- Gate-ladder pinning tests (plans/incremental_gate_unification.md §7.3). ----------
//
// These pin the module-level gate predicate decided in the plan's §2,
// which `check_merge_boolean` routes through since the §8 PR 4
// cutover. The third §7.3 case — preservation of
// `seed_skips_unrealizable_spec_module_contraction_and_reports` —
// is the existing test above.

/// §2's atomic-unit anomaly: a 3-owner atomic unit whose members form
/// a constraining cycle `a → b → c → a` exists precisely because its
/// members MUST co-locate, and the module-level predicate accepts the
/// contractions (all three owners project to the residual module, so
/// every merge is a delta-free no-op). The deleted class-level gate
/// rejected them: the class graph was cyclic from construction, the
/// cone-DFS fallback found the pre-existing (transient) path
/// `b → c → a`, and the unit could not seed.
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
    let (q, rejected) = build_seed_quotient(&report, &report.atomic_graph.nodes, &[], 10_000);
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

/// §1's Pass-2 blindness: a merge that closes an asymmetric I-SCC
/// (eager forward, lazy back) where the `EsmEvaluationSimulator`
/// proves TDZ must be rejected AT THE MERGE, with
/// `EsmEvaluationTdz`-backed evidence. The deleted hot gate saw only
/// constraining class edges, accepted, and committed; the only
/// backstop was `build_seed_quotient`'s post-seed
/// `PostSeedUnrealizableScc` report, which does not undo the merge.
///
/// Shape: pre-existing module `ui/x` = {x}; residual-pile owners `r`
/// (stays) and `h` (the merge candidate). `x` eager-reads `r`'s
/// binding (constraining `M → R`); `r` lazily reads `h`'s binding
/// (intra-residual pre-merge, becomes the lazy back-edge `R → M` once
/// `h` is promoted into `ui/x`). The post-merge I-SCC `{M, R}`
/// carries a constraining pair targeting residual — the DFS root
/// evaluates last, so `M`'s eager read of `r`'s binding TDZs.
#[test]
fn merge_closing_asymmetric_i_cycle_is_rejected_at_the_merge() {
    let x = active_owner("owner:x", 1, &["BindingX"], 10, "ui/x");
    let r = residual_owner("owner:r", 2, &["BindingR"], 5);
    let h = residual_owner("owner:h", 3, &["BindingH"], 5);
    let edges = vec![
        owner_edge("edge:0", "owner:x", "owner:r", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:r", "owner:h", DepKind::LazyUse, false),
    ];
    let report = graph_of(
        vec![x.clone(), r.clone(), h.clone()],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&x]),
            atomic_unit_for("atomic:1", &[&r]),
            atomic_unit_for("atomic:2", &[&h]),
        ],
        vec![],
    );
    let groups = vec![make_module_group("ui/x", vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);
    let cx = group_ids[0];
    let ch = q.class_of(q.owner_idx_of("owner:h").unwrap());

    // The pre-merge state is realizable; the merge alone closes the
    // TDZ cycle, so the gate must reject it.
    assert!(q.realizability_verdict().is_realizable());
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
fn seed_atomic_unit_contractions_never_rejected_on_well_formed_input() {
    // Regression guard: across a handful of well-formed fixtures,
    // no atomic-unit contraction is ever rejected. (Spec-module
    // rejections are allowed; we count only the AtomicUnit
    // variants.)
    let fixtures = [
        fixture_singletons(),
        fixture_unit_of_two(),
        fixture_unit_of_three(),
        fixture_two_units_no_edges(),
    ];
    for (label, report) in fixtures {
        let (_q, rejected) = build_seed_quotient(&report, &report.atomic_graph.nodes, &[], 10_000);
        let atomic_rejections: Vec<&SeedContractionRejected> = rejected
            .iter()
            .filter(|r| matches!(r, SeedContractionRejected::AtomicUnit { .. }))
            .collect();
        assert!(
            atomic_rejections.is_empty(),
            "{label}: atomic-unit contractions must never be rejected on well-formed input, got {atomic_rejections:?}",
        );
    }
}

fn fixture_singletons() -> (&'static str, OwnerGraphReport) {
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    (
        "singletons",
        graph_of(
            vec![a.clone(), b.clone()],
            vec![],
            vec![
                atomic_unit_for("atomic:0", &[&a]),
                atomic_unit_for("atomic:1", &[&b]),
            ],
            vec![],
        ),
    )
}

fn fixture_unit_of_two() -> (&'static str, OwnerGraphReport) {
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    (
        "unit_of_two",
        graph_of(
            vec![a.clone(), b.clone()],
            vec![],
            vec![atomic_unit_for("atomic:0", &[&a, &b])],
            vec![],
        ),
    )
}

fn fixture_unit_of_three() -> (&'static str, OwnerGraphReport) {
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    (
        "unit_of_three",
        graph_of(
            vec![a.clone(), b.clone(), c.clone()],
            vec![],
            vec![atomic_unit_for("atomic:0", &[&a, &b, &c])],
            vec![],
        ),
    )
}

fn fixture_two_units_no_edges() -> (&'static str, OwnerGraphReport) {
    let a1 = residual_owner("owner:a1", 1, &["BindingA1"], 5);
    let a2 = residual_owner("owner:a2", 2, &["BindingA2"], 5);
    let b1 = residual_owner("owner:b1", 3, &["BindingB1"], 5);
    let b2 = residual_owner("owner:b2", 4, &["BindingB2"], 5);
    (
        "two_units_no_edges",
        graph_of(
            vec![a1.clone(), a2.clone(), b1.clone(), b2.clone()],
            vec![],
            vec![
                atomic_unit_for("atomic:0", &[&a1, &a2]),
                atomic_unit_for("atomic:1", &[&b1, &b2]),
            ],
            vec![],
        ),
    )
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
        graph_of(
            vec![a1.clone(), a2.clone(), b1.clone(), b2.clone()],
            edges,
            vec![
                atomic_unit_for("atomic:0", &[&a1]),
                atomic_unit_for("atomic:1", &[&a2]),
                atomic_unit_for("atomic:2", &[&b1]),
                atomic_unit_for("atomic:3", &[&b2]),
            ],
            vec![],
        )
    };
    let spec = vec![
        SpecModuleGroup {
            module_id: "mod_alpha".to_string(),
            owner_ids: vec!["owner:a1".to_string(), "owner:a2".to_string()],
        },
        SpecModuleGroup {
            module_id: "mod_beta".to_string(),
            owner_ids: vec!["owner:b1".to_string(), "owner:b2".to_string()],
        },
    ];

    let report_a = make_report();
    let (_q1, rejected_a) =
        build_seed_quotient(&report_a, &report_a.atomic_graph.nodes, &spec, 10_000);
    let report_b = make_report();
    let (_q2, rejected_b) =
        build_seed_quotient(&report_b, &report_b.atomic_graph.nodes, &spec, 10_000);

    let json_a = serde_json::to_string_pretty(&rejected_a).unwrap();
    let json_b = serde_json::to_string_pretty(&rejected_b).unwrap();
    assert_eq!(
        json_a, json_b,
        "rejection diagnostic must be byte-identical across runs",
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
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone(), d.clone()],
        vec![],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&c]),
            atomic_unit_for("atomic:3", &[&d]),
        ],
        vec![],
    );
    let mut q = QuotientGraph::from_report(&report, 10_000);
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
fn partition_constructor_contracts_each_group() {
    // Internal invariant of the renderer-over-quotient refactor
    // (commit 1b): `from_report_with_partition` materializes a
    // quotient whose equivalence classes are exactly the input
    // groups. This is the bridge between today's cell-discovery
    // pass and the kernel that `emit_proposals` reads.
    //
    // - Owners not listed in any group remain singletons.
    // - Each group's owners share a class.
    // - Cross-group owners are in distinct classes.
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    let d = residual_owner("owner:d", 4, &["BindingD"], 5);
    let e = residual_owner("owner:e", 5, &["BindingE"], 5);
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone(), d.clone(), e.clone()],
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
            atomic_unit_for("atomic:2", &[&c]),
            atomic_unit_for("atomic:3", &[&d]),
            atomic_unit_for("atomic:4", &[&e]),
        ],
        vec![],
    );

    // Group 1: {a, b}; group 2: {c, d}; e stays singleton.
    let groups = vec![
        vec![OwnerIdx(0), OwnerIdx(1)],
        vec![OwnerIdx(2), OwnerIdx(3)],
    ];
    let (q, class_ids) = QuotientGraph::from_report_with_partition(&report, 10_000, &groups);
    assert_eq!(class_ids.len(), 2, "one class id per input group");

    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let b_idx = q.owner_idx_of("owner:b").unwrap();
    let c_idx = q.owner_idx_of("owner:c").unwrap();
    let d_idx = q.owner_idx_of("owner:d").unwrap();
    let e_idx = q.owner_idx_of("owner:e").unwrap();

    assert_eq!(q.class_of(a_idx), q.class_of(b_idx), "a/b co-located");
    assert_eq!(q.class_of(c_idx), q.class_of(d_idx), "c/d co-located");
    assert_ne!(
        q.class_of(a_idx),
        q.class_of(c_idx),
        "groups in distinct classes",
    );
    assert_ne!(
        q.class_of(e_idx),
        q.class_of(a_idx),
        "ungrouped owner stays singleton",
    );
    assert_ne!(
        q.class_of(e_idx),
        q.class_of(c_idx),
        "ungrouped owner stays singleton",
    );

    // The returned class ids must point at the actual class of each
    // group's owners (the renderer reads from these).
    assert_eq!(class_ids[0], q.class_of(a_idx));
    assert_eq!(class_ids[1], q.class_of(c_idx));

    // class_lines reflects the sum of members' source line counts.
    // a and b each contribute 5 lines (per residual_owner above).
    assert_eq!(q.class_lines(class_ids[0]), 10);
}

#[test]
fn factorize_golden_output_unchanged() {
    // Golden test for commit 1b: factorize's output is byte-identical
    // to the pre-commit-1 binary's output for the same input. The
    // baselines were captured by running `factorize` on these
    // fixtures at HEAD = 3c75ae9ae (pre-commit-1, post-anon-only
    // extension), then verified to match commit-1's output (which
    // adds the kernel as a pure-side-effect diagnostic, no behavior
    // change). The renderer-over-quotient refactor (this commit)
    // must keep these outputs stable.
    //
    // Each fixture exercises a representative shape:
    //   - `residual_singletons`: two unrelated residual owners,
    //     no edges.
    //   - `closed_residual_unit`: two residual units coupled by
    //     a constraining edge.
    //   - `extend_active_via_anon`: an anonymous statement whose
    //     unique constraining edge points at an active module
    //     (promote_anonymous_only_cell_to_extension path).
    //
    // Snapshots live at `devinfra/js/debundle/peel/golden/`. To
    // regenerate (only after a deliberate, justified change), set
    // `UPDATE_GOLDENS=1` when running the test.
    let f1 = factorize(&golden_residual_singletons(), &no_claims(), 10_000).unwrap();
    let f2 = factorize(&golden_closed_residual_unit(), &no_claims(), 10_000).unwrap();
    let claims_active = claims(&[("BindingA", "ui/x")]);
    let f3 = factorize(&golden_extend_active_via_anon(), &claims_active, 10_000).unwrap();

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
        "residual_singletons fixture diverged from pre-commit-1 baseline",
    );
    assert_eq!(
        json2, golden2,
        "closed_residual_unit fixture diverged from pre-commit-1 baseline",
    );
    assert_eq!(
        json3, golden3,
        "extend_active_via_anon fixture diverged from pre-commit-1 baseline",
    );
}

fn golden_residual_singletons() -> OwnerGraphReport {
    let a = residual_owner("owner:a", 1, &["BindingA"], 10);
    let b = residual_owner("owner:b", 2, &["BindingB"], 10);
    graph_of(
        vec![a.clone(), b.clone()],
        vec![],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
        ],
        vec![],
    )
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
    // factorize should promote it to extend:ui/x.
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

// ---------- Commit 2: greedy merge to convergence tests. ----------
//
// The greedy operates over a quotient whose initial partition the
// caller has chosen — typically the seed quotient (atomic units +
// spec modules pre-contracted) augmented with whatever cells the
// renderer has marked as pre-existing active modules. The kernel
// distinguishes "pre-existing module" classes from "residual orphan"
// classes via the `is_pre_existing_module` bit on each class; the
// commit-2 mergeability restriction lets greedy only contract an
// orphan into a module, never two modules together (that's commit 3).
//
// All fixtures below use `from_report_with_partition_extended`, the
// commit-2 constructor that takes per-group metadata (lines + the
// pre-existing-module bit). Owners not in any group remain singletons
// with their per-owner residual flag derived from the report.

fn make_module_group(module_id: &str, owner_idxs: Vec<usize>) -> peel::quotient::PartitionGroup {
    peel::quotient::PartitionGroup {
        owner_idxs: owner_idxs.into_iter().map(OwnerIdx).collect(),
        is_pre_existing_module: true,
        label: Some(module_id.to_string()),
    }
}

#[test]
fn greedy_extends_existing_module_with_only_consumer() {
    // Pre-existing module M = {owner:a (BindingA)} declared as
    // active. Residual anonymous owner:anon has a single
    // constraining edge into owner:a. After greedy: the two are
    // in one class.
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let anon = residual_owner("owner:anon", 2, &[], 5);
    let report = graph_of(
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
    );

    let groups = vec![make_module_group("ui/x", vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);
    let contractions = greedy_merge_to_convergence(&mut q);

    // Exactly one contraction merging owner:anon's class into the
    // ui/x module class.
    assert_eq!(contractions.len(), 1, "got: {contractions:?}");
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    let anon_idx = q.owner_idx_of("owner:anon").unwrap();
    assert_eq!(q.class_of(a_idx), q.class_of(anon_idx));
    assert_eq!(q.class_of(a_idx), group_ids[0]);
}

#[test]
fn greedy_absorbs_tiny_named_helper_into_unique_consumer() {
    // Pre-existing module M = {owner:a (BindingA)}. Residual
    // owner:helper (BindingHelper) is read only by owner:a via an
    // EagerUse edge. Today's line-605 gate rejects this; greedy
    // should absorb it.
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

    let groups = vec![make_module_group("ui/x", vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);
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
    let report = graph_of(
        vec![a.clone(), h1.clone(), h2.clone(), h3.clone()],
        vec![
            owner_edge("edge:0", "owner:a", "owner:h1", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:a", "owner:h2", DepKind::EagerUse, true),
            owner_edge("edge:2", "owner:a", "owner:h3", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&h1]),
            atomic_unit_for("atomic:2", &[&h2]),
            atomic_unit_for("atomic:3", &[&h3]),
        ],
        vec![],
    );

    let groups = vec![make_module_group("ui/x", vec![0])];
    let (mut q, _) = QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);
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
    let report = graph_of(
        vec![a1.clone(), a2.clone(), h.clone()],
        vec![owner_edge(
            "edge:0",
            "owner:a1",
            "owner:h",
            DepKind::EagerUse,
            true,
        )],
        vec![
            atomic_unit_for("atomic:0", &[&a1]),
            atomic_unit_for("atomic:1", &[&a2]),
            atomic_unit_for("atomic:2", &[&h]),
        ],
        vec![],
    );

    let groups = vec![make_module_group("ui/x", vec![0, 1])];
    let (mut q, _) = QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);
    let _ = greedy_merge_to_convergence(&mut q);

    let a1_idx = q.owner_idx_of("owner:a1").unwrap();
    let a2_idx = q.owner_idx_of("owner:a2").unwrap();
    assert_eq!(
        q.class_of(a1_idx),
        q.class_of(a2_idx),
        "spec-module owners must stay co-located",
    );
}

#[test]
fn greedy_never_merges_into_residual() {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let residual = OwnerGraphNodeReport {
        id: "owner:residual_catchall".to_string(),
        statement_ordinal: StatementOrdinal(2),
        source_location: Some(SourceLocation {
            source_path: "x.js".to_string(),
            start_line: 200,
            end_line: 204,
        }),
        declared_bindings: vec![],
        statement_kind: StatementKind::VarDecl,
        purity: Purity::Pure,
        destination: module_ref("residual"),
    };
    let h = residual_owner("owner:h", 3, &["BindingH"], 5);
    let report = graph_of(
        vec![a.clone(), residual.clone(), h.clone()],
        vec![
            owner_edge(
                "edge:0",
                "owner:residual_catchall",
                "owner:a",
                DepKind::EagerUse,
                true,
            ),
            owner_edge("edge:1", "owner:h", "owner:a", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&residual]),
            atomic_unit_for("atomic:2", &[&h]),
        ],
        vec![],
    );

    let groups = vec![make_module_group("ui/x", vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);
    let residual_idx = q.owner_idx_of("owner:residual_catchall").unwrap();
    let residual_class = q.class_of(residual_idx);
    // Designate the catch-all class as the sticky residual sink; the
    // kernel leaves classes non-residual at seed time (residual-destined
    // owners are otherwise peelable), so this test marks the sink it
    // wants the greedy to refuse to merge into.
    q.mark_class_residual(residual_class);
    let contractions = greedy_merge_to_convergence(&mut q);
    for (c1, c2) in &contractions {
        assert!(
            !(*c1 == residual_class || *c2 == residual_class),
            "no merge should involve residual class {residual_class:?}: {contractions:?}",
        );
    }
    // owner:h should be merged into ui/x; residual stays alone.
    let h_idx = q.owner_idx_of("owner:h").unwrap();
    let a_idx = q.owner_idx_of("owner:a").unwrap();
    assert_eq!(q.class_of(h_idx), q.class_of(a_idx));
    assert_eq!(q.class_of(a_idx), group_ids[0]);
    // The residual catch-all class still has only its original
    // member.
    assert_eq!(q.class_members(residual_class).count(), 1);
}

#[test]
fn incremental_state_matches_rebuild_on_synthetic_specs() {
    // Property test: across a corpus of synthetic fixtures, after
    // each greedy contraction, the cached cycle set on
    // `QuotientGraph` must byte-equal what a from-scratch rebuild
    // would produce on the same partition. Pins the
    // incremental-realizability cache against the from-scratch
    // reference.
    let mut fixtures: Vec<(
        &'static str,
        OwnerGraphReport,
        Vec<peel::quotient::PartitionGroup>,
    )> = Vec::new();
    fixtures.push(("empty", fixture_singletons().1, vec![]));
    {
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let h1 = residual_owner("owner:h1", 2, &["BindingH1"], 5);
        let h2 = residual_owner("owner:h2", 3, &["BindingH2"], 5);
        fixtures.push((
            "single_module_two_orphans",
            graph_of(
                vec![a.clone(), h1.clone(), h2.clone()],
                vec![
                    owner_edge("edge:0", "owner:a", "owner:h1", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:a", "owner:h2", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&h1]),
                    atomic_unit_for("atomic:2", &[&h2]),
                ],
                vec![],
            ),
            vec![make_module_group("ui/x", vec![0])],
        ));
    }
    {
        let a1 = active_owner("owner:a1", 1, &["BindingA1"], 10, "ui/x");
        let a2 = active_owner("owner:a2", 2, &["BindingA2"], 10, "ui/x");
        let h = residual_owner("owner:h", 3, &["BindingH"], 5);
        fixtures.push((
            "module_with_internal_edges",
            graph_of(
                vec![a1.clone(), a2.clone(), h.clone()],
                vec![
                    owner_edge("edge:0", "owner:a1", "owner:a2", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:a1", "owner:h", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a1]),
                    atomic_unit_for("atomic:1", &[&a2]),
                    atomic_unit_for("atomic:2", &[&h]),
                ],
                vec![],
            ),
            vec![make_module_group("ui/x", vec![0, 1])],
        ));
    }
    {
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
        let h_a = residual_owner("owner:h_a", 3, &["BindingHA"], 5);
        let h_b = residual_owner("owner:h_b", 4, &["BindingHB"], 5);
        fixtures.push((
            "two_modules_no_merge",
            graph_of(
                vec![a.clone(), b.clone(), h_a.clone(), h_b.clone()],
                vec![
                    owner_edge("edge:0", "owner:a", "owner:h_a", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:b", "owner:h_b", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&b]),
                    atomic_unit_for("atomic:2", &[&h_a]),
                    atomic_unit_for("atomic:3", &[&h_b]),
                ],
                vec![],
            ),
            vec![
                make_module_group("ui/x", vec![0]),
                make_module_group("ui/y", vec![1]),
            ],
        ));
    }
    {
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
        let h = residual_owner("owner:h", 3, &["BindingH"], 5);
        fixtures.push((
            "diamond_consumers",
            graph_of(
                vec![a.clone(), b.clone(), h.clone()],
                vec![
                    owner_edge("edge:0", "owner:a", "owner:h", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:b", "owner:h", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&b]),
                    atomic_unit_for("atomic:2", &[&h]),
                ],
                vec![],
            ),
            vec![
                make_module_group("ui/x", vec![0]),
                make_module_group("ui/y", vec![1]),
            ],
        ));
    }

    for (label, report, groups) in fixtures {
        let (mut incremental, _) =
            QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);

        // After construction, the cached cycle set must equal a
        // from-scratch rebuild.
        let cached = incremental.cycle_set();
        let rebuilt = QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups)
            .0
            .cycle_set();
        assert_eq!(
            cached, rebuilt,
            "{label}: initial cached cycle set diverges from rebuild",
        );

        // Step the greedy one contraction at a time; after each
        // contraction the cache stays in sync with a rebuild on the
        // same partition.
        loop {
            let one = peel::quotient::greedy_step(&mut incremental);
            let Some(step) = one else { break };
            // Verify the contracted owners are now co-located.
            assert!(
                incremental.class_members(step.surviving).count() >= 2,
                "{label}: post-contract class {:?} should have ≥ 2 members",
                step.surviving,
            );
            // Verify the cached cycle set matches a from-scratch
            // rebuild over the same partition.
            let cached_now = incremental.cycle_set();
            let replay = replay_partition(&report, &groups, &incremental, 10_000);
            let replay_cycles = replay.cycle_set();
            assert_eq!(
                cached_now, replay_cycles,
                "{label}: cached cycle set diverges from rebuild after merge",
            );
        }
    }
}

/// Build a fresh quotient from the same report+groups and re-apply
/// `current`'s class membership.
fn replay_partition(
    report: &OwnerGraphReport,
    initial_groups: &[peel::quotient::PartitionGroup],
    current: &QuotientGraph,
    cap_lines: usize,
) -> QuotientGraph {
    use std::collections::BTreeMap;
    let mut by_class: BTreeMap<peel::quotient::ClassId, Vec<OwnerIdx>> = BTreeMap::new();
    for owner in 0..report.nodes.len() {
        let o = OwnerIdx(owner);
        by_class.entry(current.class_of(o)).or_default().push(o);
    }
    // Carry the is_pre_existing_module bit per current class by
    // looking up whether any of its members came from an initial
    // pre-existing-module group.
    let mut pre_existing_owners: std::collections::BTreeSet<OwnerIdx> =
        std::collections::BTreeSet::new();
    for group in initial_groups {
        if group.is_pre_existing_module {
            pre_existing_owners.extend(group.owner_idxs.iter().copied());
        }
    }
    let groups: Vec<peel::quotient::PartitionGroup> = by_class
        .into_values()
        .map(|owners| peel::quotient::PartitionGroup {
            is_pre_existing_module: owners.iter().any(|o| pre_existing_owners.contains(o)),
            owner_idxs: owners,
            label: None,
        })
        .collect();
    let (q, _) = QuotientGraph::from_report_with_partition_extended(report, cap_lines, &groups);
    q
}

type NormalizedVerdict = (
    std::collections::BTreeSet<(Vec<analysis::ModuleId>, Vec<usize>)>,
    std::collections::BTreeSet<(analysis::ModuleId, analysis::ModuleId, usize)>,
);

/// Normalize a `RealizabilityVerdict` for byte-equal comparison.
/// The verdict's `unrealizable_sccs` carry `BTreeSet<ModuleId>`s,
/// which iterate in deterministic order; we collect SCCs into a
/// `BTreeSet<(Vec<ModuleId>, Vec<usize>)>` so comparison is
/// insensitive to SCC ordering. Similarly for `cross_rebinds`.
fn normalize_verdict(verdict: RealizabilityVerdict) -> NormalizedVerdict {
    let sccs = verdict
        .unrealizable_sccs
        .into_iter()
        .map(|scc| {
            let modules: Vec<analysis::ModuleId> = scc.modules.into_iter().collect();
            let edges: Vec<usize> = scc
                .constraining_owner_edges
                .into_iter()
                .map(|e| e.0)
                .collect();
            (modules, edges)
        })
        .collect();
    let rebinds = verdict
        .cross_rebinds
        .into_iter()
        .map(|r| (r.from, r.to, r.owner_edge.0))
        .collect();
    (sccs, rebinds)
}

#[test]
fn incremental_index_matches_rebuild_on_synthetic_specs() {
    // Property: after each `merge_preserves_invariants` /
    // `would_be_cycles_after_contract` query or `contract` call on
    // the kernel, the kernel's `realizability_verdict()` (read from
    // the persistent `RealizabilityIndex`) byte-equals a from-scratch
    // `check_realizability(&owner_graph, &project_partition(None))`.
    //
    // Pins commit 5's wiring: the index's committed partition stays
    // synchronized with the kernel's class projection across both
    // committed mutations and speculative overlay queries. RED if
    // the index is forked from the kernel; GREEN when the wiring is
    // correct.
    use peel::quotient::{ClassId, PartitionGroup};

    let mut fixtures: Vec<(&'static str, OwnerGraphReport, Vec<PartitionGroup>)> = Vec::new();
    fixtures.push(("empty_singletons", fixture_singletons().1, vec![]));
    {
        // Two singleton spec modules with a shared orphan in between.
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
        let h = residual_owner("owner:h", 3, &["BindingH"], 5);
        fixtures.push((
            "two_modules_shared_orphan",
            graph_of(
                vec![a.clone(), b.clone(), h.clone()],
                vec![
                    owner_edge("edge:0", "owner:a", "owner:h", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:b", "owner:h", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&b]),
                    atomic_unit_for("atomic:2", &[&h]),
                ],
                vec![],
            ),
            vec![
                make_module_group("ui/x", vec![0]),
                make_module_group("ui/y", vec![1]),
            ],
        ));
    }
    {
        // Module with internal eager edges plus one orphan consumer.
        let a1 = active_owner("owner:a1", 1, &["BindingA1"], 10, "ui/x");
        let a2 = active_owner("owner:a2", 2, &["BindingA2"], 10, "ui/x");
        let h = residual_owner("owner:h", 3, &["BindingH"], 5);
        fixtures.push((
            "module_with_internal_edges",
            graph_of(
                vec![a1.clone(), a2.clone(), h.clone()],
                vec![
                    owner_edge("edge:0", "owner:a1", "owner:a2", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:a1", "owner:h", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a1]),
                    atomic_unit_for("atomic:1", &[&a2]),
                    atomic_unit_for("atomic:2", &[&h]),
                ],
                vec![],
            ),
            vec![make_module_group("ui/x", vec![0, 1])],
        ));
    }
    {
        // Three modules, chain of consumption.
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
        let c = active_owner("owner:c", 3, &["BindingC"], 10, "ui/z");
        fixtures.push((
            "three_modules_chain",
            graph_of(
                vec![a.clone(), b.clone(), c.clone()],
                vec![
                    owner_edge("edge:0", "owner:a", "owner:b", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:b", "owner:c", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&b]),
                    atomic_unit_for("atomic:2", &[&c]),
                ],
                vec![],
            ),
            vec![
                make_module_group("ui/x", vec![0]),
                make_module_group("ui/y", vec![1]),
                make_module_group("ui/z", vec![2]),
            ],
        ));
    }
    {
        // Two modules with mutual eager edges — unrealizable from
        // the seed.
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
        fixtures.push((
            "mutual_eager_cycle",
            graph_of(
                vec![a.clone(), b.clone()],
                vec![
                    owner_edge("edge:0", "owner:a", "owner:b", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:b", "owner:a", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&b]),
                ],
                vec![],
            ),
            vec![
                make_module_group("ui/x", vec![0]),
                make_module_group("ui/y", vec![1]),
            ],
        ));
    }
    {
        // Module + residual fan-out with multiple orphans.
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let h1 = residual_owner("owner:h1", 2, &["BindingH1"], 5);
        let h2 = residual_owner("owner:h2", 3, &["BindingH2"], 5);
        let h3 = residual_owner("owner:h3", 4, &["BindingH3"], 5);
        fixtures.push((
            "module_fanout_three_orphans",
            graph_of(
                vec![a.clone(), h1.clone(), h2.clone(), h3.clone()],
                vec![
                    owner_edge("edge:0", "owner:a", "owner:h1", DepKind::EagerUse, true),
                    owner_edge("edge:1", "owner:a", "owner:h2", DepKind::EagerUse, true),
                    owner_edge("edge:2", "owner:a", "owner:h3", DepKind::EagerUse, true),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&h1]),
                    atomic_unit_for("atomic:2", &[&h2]),
                    atomic_unit_for("atomic:3", &[&h3]),
                ],
                vec![],
            ),
            vec![make_module_group("ui/x", vec![0])],
        ));
    }
    assert!(
        fixtures.len() >= 5,
        "property test needs >= 5 fixture chunks",
    );

    for (label, report, groups) in fixtures {
        let (mut q, _) =
            QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);

        // Initial assertion: incremental verdict matches rebuild.
        {
            let incremental = normalize_verdict(q.realizability_verdict());
            let rebuild = normalize_verdict(check_realizability(
                q.owner_graph_for_tests(),
                &q.project_partition_for_tests(),
            ));
            assert_eq!(
                incremental, rebuild,
                "{label}: initial incremental verdict diverges from rebuild",
            );
        }

        // Walk through a sequence of arbitrary operations: alternate
        // `merge_preserves_invariants` queries on every pair of live
        // classes with `contract` of one greedily-picked pair per
        // round, with a final `would_be_cycles_after_contract` query
        // on the residual class pair (if any) for good measure.
        loop {
            // Issue queries against every live (c1, c2) pair without
            // mutating; assert verdict invariance.
            let live: Vec<ClassId> = q.iter_classes().collect();
            for i in 0..live.len() {
                for j in (i + 1)..live.len() {
                    let _ = q.merge_preserves_invariants(live[i], live[j]);
                    let _ = q.would_be_cycles_after_contract(live[i], live[j]);
                    let incremental = normalize_verdict(q.realizability_verdict());
                    let rebuild = normalize_verdict(check_realizability(
                        q.owner_graph_for_tests(),
                        &q.project_partition_for_tests(),
                    ));
                    assert_eq!(
                        incremental, rebuild,
                        "{label}: incremental verdict diverges from rebuild \
                         after query on ({:?}, {:?})",
                        live[i], live[j],
                    );
                }
            }
            // Pick one greedy contract and apply it; reassert.
            let one = peel::quotient::greedy_step(&mut q);
            let Some(step) = one else { break };
            let incremental = normalize_verdict(q.realizability_verdict());
            let rebuild = normalize_verdict(check_realizability(
                q.owner_graph_for_tests(),
                &q.project_partition_for_tests(),
            ));
            assert_eq!(
                incremental, rebuild,
                "{label}: incremental verdict diverges from rebuild \
                 after contract {:?}",
                step.picked,
            );
        }
    }
}

#[test]
fn boolean_merge_gate_matches_diagnostic_cycle_gate() {
    // The greedy hot path only needs a yes/no answer, while
    // `would_be_cycles_after_contract` materializes diagnostic
    // evidence. Keep the verdicts equivalent on precondition-clean
    // merges, including the important case where merging endpoints
    // of an intermediate path would create a new multi-class SCC.
    use peel::quotient::ClassId;

    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/a");
    let h = active_owner("owner:h", 2, &["BindingH"], 10, "ui/h");
    let b = active_owner("owner:b", 3, &["BindingB"], 10, "ui/b");
    let report = graph_of(
        vec![a.clone(), h.clone(), b.clone()],
        vec![
            owner_edge("edge:0", "owner:a", "owner:h", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:h", "owner:b", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&h]),
            atomic_unit_for("atomic:2", &[&b]),
        ],
        vec![],
    );
    let groups = vec![
        make_module_group("ui/a", vec![0]),
        make_module_group("ui/h", vec![1]),
        make_module_group("ui/b", vec![2]),
    ];
    let (q, _) = QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);

    for (left, right, expected_preserves) in [
        (ClassId(0), ClassId(1), true),
        (ClassId(1), ClassId(2), true),
        (ClassId(0), ClassId(2), false),
    ] {
        let diagnostic_preserves = q.would_be_cycles_after_contract(left, right).is_none();
        let boolean_preserves = q.merge_preserves_invariants(left, right);
        assert_eq!(
            diagnostic_preserves, expected_preserves,
            "unexpected diagnostic verdict for ({left:?}, {right:?})",
        );
        assert_eq!(
            boolean_preserves, diagnostic_preserves,
            "boolean hot path diverged from diagnostic verdict for ({left:?}, {right:?})",
        );
    }
}

#[test]
#[ignore = "needs GAFFER_OWNER_GRAPH pointing at a real corpus owner_graph.json; run manually"]
fn greedy_on_gaffer_chunk_completes_under_one_minute() {
    // Benchmark: real owner_graph.json from a recent gaffer cache,
    // pointed at via GAFFER_OWNER_GRAPH. We run the full factorize
    // pipeline (cells + greedy + emit) since the greedy is only
    // meaningful with the cells-derived partition's
    // pre-existing-module markings.
    let path = std::env::var("GAFFER_OWNER_GRAPH").expect("GAFFER_OWNER_GRAPH must be set");
    let body = std::fs::read_to_string(&path).expect("read GAFFER_OWNER_GRAPH");
    let report: OwnerGraphReport = serde_json::from_str(&body).expect("parse owner_graph.json");
    // The factorize CLI loads active claims from a modules-root
    // directory. The benchmark just runs factorize without claims
    // (every owner with destination.id != residual is treated as
    // its own active module, which is what the planner would see
    // before any spec edits).
    let started = std::time::Instant::now();
    let result = factorize(&report, &no_claims(), 10_000).unwrap();
    let elapsed = started.elapsed();
    let extension_proposals: usize = result
        .proposals
        .iter()
        .filter(|p| p.extends_module_id.is_some())
        .count();
    eprintln!(
        "gaffer chunk: {} owners, {} proposals ({} extension), {:?}",
        report.nodes.len(),
        result.proposals.len(),
        extension_proposals,
        elapsed,
    );
    assert!(
        elapsed < std::time::Duration::from_secs(60),
        "factorize on gaffer-scale input must complete in under 60s, took {elapsed:?}",
    );
}

// ---------- Commit 3: full mergeability + merge output shape. ----------
//
// The commit-3 gate allows two pre-existing-module classes to merge
// (with or without absorbing residual orphans). The renderer carries
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
        make_module_group("ui/a", vec![0]),
        make_module_group("ui/b", vec![1]),
        make_module_group("ui/c", vec![2]),
    ];
    let (mut q, _) = QuotientGraph::from_report_with_partition_extended(&report, 150, &groups);
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
        make_module_group("ui/a", vec![0]),
        make_module_group("ui/b", vec![1]),
        make_module_group("ui/c", vec![2]),
    ];
    let (mut q, _) = QuotientGraph::from_report_with_partition_extended(&report, 40, &groups);
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
    let groups = vec![
        make_module_group("ui/a", vec![0]),
        make_module_group("ui/b", vec![1]),
    ];
    let (mut q, _) = QuotientGraph::from_report_with_partition_extended(&report, 10_000, &groups);
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
    assert!(
        q.cycle_set().cycles.is_empty(),
        "post-merge quotient should be realizable: {:?}",
        q.cycle_set(),
    );
}

#[test]
fn merge_two_existing_modules_with_mutual_eager_reads() {
    // Two pre-existing modules with mutual EagerUse cross-edges.
    // Assert the rendered proposal carries `merge_into:
    // Some(["ui/a", "ui/b"])`.
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/a");
    let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/b");
    let report = graph_of(
        vec![a.clone(), b.clone()],
        vec![
            owner_edge("edge:ab", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:ba", "owner:b", "owner:a", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:a", &[&a]),
            atomic_unit_for("atomic:b", &[&b]),
        ],
        vec![],
    );
    let result = factorize(
        &report,
        &claims(&[("BindingA", "ui/a"), ("BindingB", "ui/b")]),
        10_000,
    )
    .unwrap();
    let merge_proposals: Vec<&peel::factorize::FactorizeProposal> = result
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
    let merge_into = merge_proposals[0].merge_into.clone().unwrap();
    assert_eq!(
        merge_into,
        vec!["ui/a".to_string(), "ui/b".to_string()],
        "merge_into should list both module ids in canonical order",
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
    let result = factorize(
        &report,
        &claims(&[("BindingA", "ui/a"), ("BindingB", "ui/b")]),
        10_000,
    )
    .unwrap();
    let merge_proposals: Vec<&peel::factorize::FactorizeProposal> = result
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

// ---------- Commit 4: unify cell discovery into seeding. ----------
//
// The commit-4 refactor deletes `proposal_cells_from_atomic_graph` and
// its `Cell` IR; the equivalent partition is now produced by a third
// gated contraction pass in `build_seed_quotient`. Behavior on
// well-formed input is byte-identical to the commit-1b snapshots
// (locked down by `factorize_golden_output_unchanged` above). Behavior
// on input whose atomic-DAG reachability closure would form a cycle
// is intentionally different: today's cell discovery would have
// silently formed the closure and let the downstream realizability
// gate report a generic cycle; the unified seeding refuses the
// pass-3 contraction and emits a `SeedContractionRejected::AtomicReachability`
// diagnostic pinpointing the rejected pair.

#[test]
fn unification_byte_identical_on_well_formed_inputs() {
    // Companion to `factorize_golden_output_unchanged`. The plan's
    // commit-4 spec calls out that the three commit-1b golden
    // snapshots (residual_singletons, closed_residual_unit,
    // extend_active_via_anon) produce zero rejections under the
    // gated seeding and therefore must stay byte-identical after
    // unification. This test asserts the "zero rejections" half;
    // the byte-identity half is covered by
    // `factorize_golden_output_unchanged`.
    let claims_active = claims(&[("BindingA", "ui/x")]);

    let r1 = factorize(&golden_residual_singletons(), &no_claims(), 10_000).unwrap();
    let r2 = factorize(&golden_closed_residual_unit(), &no_claims(), 10_000).unwrap();
    let r3 = factorize(&golden_extend_active_via_anon(), &claims_active, 10_000).unwrap();

    assert!(
        r1.seed_rejections.is_empty(),
        "residual_singletons fixture must produce zero seed rejections: {:?}",
        r1.seed_rejections,
    );
    assert!(
        r2.seed_rejections.is_empty(),
        "closed_residual_unit fixture must produce zero seed rejections: {:?}",
        r2.seed_rejections,
    );
    assert!(
        r3.seed_rejections.is_empty(),
        "extend_active_via_anon fixture must produce zero seed rejections: {:?}",
        r3.seed_rejections,
    );
}

#[test]
fn unification_rejects_cyclic_atomic_reachability_with_diagnostic() {
    // Fixture: two residual atomic units mod_alpha = {Foo} and
    // mod_beta = {Bar} with constraining edges in both directions
    // (Foo reads Bar; Bar reads Foo). Atomic-DAG edges
    // atomic:alpha → atomic:beta and atomic:beta → atomic:alpha.
    // Today's cell discovery's transitive closure would coalesce
    // {Foo, Bar} into a single residual cell (and the downstream
    // realizability gate would then report the cycle as a generic
    // SCC); the gated seeding's pass 3 contracts these one edge
    // at a time. The first edge's contraction succeeds (singletons
    // → one residual class). The second edge would re-encounter
    // the already-contracted class (same-class) and skip silently
    // — no diagnostic, no cycle. To create a *rejected* contraction
    // diagnostic, we use three units with directional edges:
    //   atomic:alpha (Foo, residual) → atomic:gamma (Helper, residual)
    //   atomic:beta  (Bar, residual) → atomic:gamma (Helper, residual)
    // and additionally
    //   atomic:gamma → atomic:alpha (closing the cycle in atomic
    //   graph through a third residual class).
    // Pass 3 walks edges in id-lex order. After contracting
    // alpha→gamma and beta→gamma, all three are one class. Then
    // gamma→alpha is same-class. To force a *cyclic* rejection
    // we need three classes where the third edge's contraction
    // would create a multi-class SCC that wasn't there before.
    //
    // Simpler fixture: three singleton residual units linked
    // alpha → beta → gamma → alpha as atomic-DAG edges, and the
    // underlying owner-graph constraining edges form a directed
    // 3-cycle. Pass 3 walks edges by id; the first two merges
    // collapse {alpha, beta, gamma} into one residual class, so
    // the third edge is same-class and not rejected. The cyclic-
    // rejection diagnostic only fires when the merge candidate's
    // *post-merge* cycle set includes the merged endpoints — i.e.,
    // when there's a path from `c_target` back to `c_source` that
    // does NOT pass through `c_target` or `c_source`'s eventual
    // partners.
    //
    // Concrete fixture used here: residual owners Foo, Bar, Helper.
    //   - Foo reads Bar (constraining; Foo → Bar)
    //   - Bar reads Helper (constraining; Bar → Helper)
    //   - Helper reads Foo (constraining; Helper → Foo)
    // Each owner is its own atomic unit. Atomic-DAG edges:
    //   atomic:foo → atomic:bar
    //   atomic:bar → atomic:helper
    //   atomic:helper → atomic:foo
    // Pass 3 walks edges in id-lex order (alphabetical on edge id).
    // We name the edges so the first-to-process one creates a
    // singleton-class merge between Bar and Helper (closing two of
    // the three classes), then the second-to-process edge attempts
    // foo↔(bar+helper) — which would create a self-loop on the
    // merged class (not a multi-class SCC) and is therefore
    // accepted. So a 3-cycle of three residual singletons just
    // collapses into one class.
    //
    // The cyclic-rejection diagnostic fires when a *fourth*
    // class — a pre-existing spec module — closes the cycle. So the
    // fixture pins one binding to a pre-existing module, leaving
    // two residuals that would close a cycle through the module:
    //   - Foo lives in spec module mod_alpha.
    //   - Bar, Helper are residual.
    //   - Bar reads Foo (Bar → Foo, constraining).
    //   - Helper reads Bar (Helper → Bar, constraining).
    //   - Foo reads Helper (Foo → Helper, constraining).
    // Atomic-DAG edges:
    //   atomic_edge:a (atomic:bar → atomic:foo)      // Bar reads Foo
    //   atomic_edge:b (atomic:foo → atomic:helper)   // Foo reads Helper
    //   atomic_edge:c (atomic:helper → atomic:bar)   // Helper reads Bar
    // Only `atomic_edge:b` and `atomic_edge:c` have a residual
    // target (Helper / Bar are residual; Foo is active so
    // `atomic_edge:a` is skipped by pass-3's `target has residual`
    // filter).
    // Pass 3 in id-lex order:
    //   atomic_edge:b: contract class(Foo) (= mod_alpha class) with
    //     class(Helper). Singleton Helper → no pre-merge cycle. The
    //     merge would set up Foo+Helper in one class; Bar still
    //     reads Foo (Bar's only edge), Helper still reads Bar (now
    //     Foo+Helper → Bar). New cross-class edges:
    //       Bar → Foo+Helper (constraining, via Bar reads Foo)
    //       Foo+Helper → Bar (constraining, via Helper reads Bar)
    //     That's a 2-class SCC. The gate rejects.
    //   atomic_edge:c: same situation by symmetry — would close a
    //     2-class cycle.
    // The diagnostic must name the rejected edge + pair.
    let foo = active_owner("owner:foo", 1, &["Foo"], 5, "mod_alpha");
    let bar = residual_owner("owner:bar", 2, &["Bar"], 5);
    let helper = residual_owner("owner:helper", 3, &["Helper"], 5);
    let edges = vec![
        // Bar reads Foo
        owner_edge("edge:0", "owner:bar", "owner:foo", DepKind::EagerUse, true),
        // Foo reads Helper
        owner_edge(
            "edge:1",
            "owner:foo",
            "owner:helper",
            DepKind::EagerUse,
            true,
        ),
        // Helper reads Bar
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
            // atomic_edge:a — Bar reads Foo (target active, skipped
            // by pass 3's residual-target filter).
            AtomicUnitEdgeReport {
                id: "atomic_edge:a".to_string(),
                source: "atomic:bar".to_string(),
                target: "atomic:foo".to_string(),
                edge_kinds: vec![DepKind::EagerUse],
                owner_edge_ids: vec!["edge:0".to_string()],
                constrains_init_order: true,
            },
            // atomic_edge:b — Foo reads Helper (target residual).
            AtomicUnitEdgeReport {
                id: "atomic_edge:b".to_string(),
                source: "atomic:foo".to_string(),
                target: "atomic:helper".to_string(),
                edge_kinds: vec![DepKind::EagerUse],
                owner_edge_ids: vec!["edge:1".to_string()],
                constrains_init_order: true,
            },
            // atomic_edge:c — Helper reads Bar (target residual).
            AtomicUnitEdgeReport {
                id: "atomic_edge:c".to_string(),
                source: "atomic:helper".to_string(),
                target: "atomic:bar".to_string(),
                edge_kinds: vec![DepKind::EagerUse],
                owner_edge_ids: vec!["edge:2".to_string()],
                constrains_init_order: true,
            },
        ],
    );
    // Spec module mod_alpha contains Foo. The factorize entry
    // point derives spec_modules from the owner destinations, so
    // the active owner above is already registered as mod_alpha.
    let result = factorize(&report, &claims(&[("Foo", "mod_alpha")]), 10_000).unwrap();

    // (a) No proposal should bundle Foo with Helper or Bar — the
    // cycle prevents merging Foo's class with Helper's class
    // through the pass-3 atomic-DAG-reachability contraction.
    let foo_extension = result
        .proposals
        .iter()
        .find(|p| p.extends_module_id.as_deref() == Some("mod_alpha"));
    if let Some(p) = foo_extension {
        assert!(
            !p.extension_owner_ids.contains(&"owner:helper".to_string())
                && !p.extension_owner_ids.contains(&"owner:bar".to_string()),
            "mod_alpha extension must NOT include Helper or Bar: {p:?}",
        );
    }

    // (b) The seed rejections must include an AtomicReachability
    // entry naming the rejected edge + pair.
    let reachability_rejections: Vec<&SeedContractionRejected> = result
        .seed_rejections
        .iter()
        .filter(|r| matches!(r, SeedContractionRejected::AtomicReachability { .. }))
        .collect();
    assert!(
        !reachability_rejections.is_empty(),
        "expected at least one AtomicReachability rejection, got: {:?}",
        result.seed_rejections,
    );
    // At least one rejection must name a (Foo, Helper) or
    // (Helper, Bar) or (Foo, Bar) pair (the cycle-closing edges).
    let pinpoints_cycle = reachability_rejections.iter().any(|r| {
        if let SeedContractionRejected::AtomicReachability {
            rejected_pair,
            cycle,
            ..
        } = r
        {
            !cycle.is_empty()
                && (rejected_pair.0 == "owner:foo"
                    || rejected_pair.1 == "owner:foo"
                    || rejected_pair.0 == "owner:bar"
                    || rejected_pair.1 == "owner:bar"
                    || rejected_pair.0 == "owner:helper"
                    || rejected_pair.1 == "owner:helper")
        } else {
            false
        }
    });
    assert!(
        pinpoints_cycle,
        "AtomicReachability rejection must pinpoint a cycle-closing pair with cycle evidence: {reachability_rejections:?}",
    );
}

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
    // walk had committed the rejected merge (the pre-fix `contract` in
    // the diagnostics phase's stray `Ok(_)` arm), the pair's owners
    // would share a class — exactly the corruption the read-only
    // predicates (`check_merge_preconditions` /
    // `would_be_cycles_after_contract`) prevent.
    //
    // Fixture mirrors the cycle in
    // `unification_rejects_cyclic_atomic_reachability_with_diagnostic`:
    // residual Bar / Helper and active Foo (in spec module mod_alpha)
    // form a constraining 3-cycle; pass-3's atomic-DAG-reachability
    // contraction rejects the cycle-closing edges, driving the
    // diagnostic walk.
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
    let spec = vec![SpecModuleGroup {
        module_id: "mod_alpha".to_string(),
        owner_ids: vec!["owner:foo".to_string()],
    }];
    let (q, rejected) = build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000);

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
        let SeedContractionRejected::AtomicReachability { rejected_pair, .. } = r else {
            unreachable!("filtered to AtomicReachability above");
        };
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
}

// ---------- Track A unification: planner gate ≡ materializer gate. ----------
//
// The peel planner's seed-quotient cycle gate must produce the same
// realizability verdict as the materializer's `check_realizability`
// run on the projected partition. Before unification (pre-Track-A),
// the planner reimplemented Tarjan over only constraining edges in
// the JSON report, missing asymmetric I-cycles the materializer
// catches via its `EsmEvaluationSimulator` pass. The tests below pin
// the unified behavior: every planner verdict must match the
// materializer verdict on the same input.

/// Build an `OwnerGraph` + `Partition` from a report + spec module
/// list. The partition assigns each owner to the module derived from
/// its `destination.id`; residual destinations land on the residual
/// `ModuleId`. Used by the planner-vs-materializer cross-check tests.
fn owner_graph_and_partition_from_spec(
    report: &analysis::OwnerGraphReport,
    spec: &[peel::quotient::SpecModuleGroup],
) -> (analysis::OwnerGraph, analysis::Partition) {
    use std::collections::HashMap;
    let (owner_graph, index) = analysis::OwnerGraph::from_report(report, &[]);
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
            if let Some(o) = index.lookup(owner_id) {
                of[o.0] = mid;
            }
        }
    }
    let partition = analysis::Partition::from_assignments(of, residual);
    (owner_graph, partition)
}

#[test]
fn planner_seed_rejection_matches_materializer_verdict_on_asymmetric_cycle() {
    // Asymmetric I-cycle through a non-residual mediator —
    // the materializer-side adversarial shape Lemma 2 cannot rescue
    // (mirrors `mediator_reaches_asymmetric_cycle_test`).
    //
    //   entry        -> mediator   EagerUse  (constraining=true)
    //   mediator     -> dep        LazyUse   (non-constraining, opens DFS)
    //   dependent    -> dep        EagerUse  (constraining=true) [fwd]
    //   dep          -> dependent  LazyUse   (non-constraining)  [back]
    //
    // I-graph SCC after seeding spec modules:
    //   {mod_dep, mod_dependent}. Residual reaches the SCC only via
    //   mod_mediator (its `mediator → dep` lazy edge is part of I).
    //
    // Why Lemma 2 fails: mod_mediator's imports are sorted by
    // linker_position (dependency-first), so DFS enters mod_dep
    // first; mod_dep's body lazily references cross_value, then
    // mod_dependent is entered → `cross_value`'s eager read of
    // `dep_value` TDZs while mod_dep is mid-evaluation.
    //
    // Materializer flags the SCC as unrealizable; the buggy
    // planner sees no constraining-only cycle and reports zero
    // rejections.
    let entry = residual_owner("owner:entry", 0, &[], 1);
    let dep_value = active_owner("owner:dep_value", 1, &["BindingDepValue"], 5, "mod_dep");
    let lazy_reader = active_owner("owner:lazy_reader", 2, &["BindingLazyReader"], 5, "mod_dep");
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
    let edges = vec![
        // residual `entry` eagerly reads mediator_init →
        // residual → mod_mediator (constraining).
        owner_edge(
            "edge:entry_mediator",
            "owner:entry",
            "owner:mediator_init",
            analysis::DepKind::EagerUse,
            true,
        ),
        // mediator_helper lazily reads dep_value → mod_mediator →
        // mod_dep (lazy, non-constraining).
        owner_edge(
            "edge:mediator_dep",
            "owner:mediator_helper",
            "owner:dep_value",
            analysis::DepKind::LazyUse,
            false,
        ),
        // mediator_init eagerly calls mediator_helper (intra-module).
        owner_edge(
            "edge:mediator_intra",
            "owner:mediator_init",
            "owner:mediator_helper",
            analysis::DepKind::EagerUse,
            true,
        ),
        // cross_value eagerly reads dep_value → mod_dependent →
        // mod_dep (constraining; forward).
        owner_edge(
            "edge:dependent_dep",
            "owner:cross_value",
            "owner:dep_value",
            analysis::DepKind::EagerUse,
            true,
        ),
        // lazy_reader's body lazily references cross_value →
        // mod_dep → mod_dependent (lazy back-edge; closes I-SCC).
        owner_edge(
            "edge:dep_back",
            "owner:lazy_reader",
            "owner:cross_value",
            analysis::DepKind::LazyUse,
            false,
        ),
    ];
    let report = graph_of(
        vec![
            entry.clone(),
            dep_value.clone(),
            lazy_reader.clone(),
            cross_value.clone(),
            mediator_helper.clone(),
            mediator_init.clone(),
        ],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&entry]),
            atomic_unit_for("atomic:1", &[&dep_value]),
            atomic_unit_for("atomic:2", &[&lazy_reader]),
            atomic_unit_for("atomic:3", &[&cross_value]),
            atomic_unit_for("atomic:4", &[&mediator_helper]),
            atomic_unit_for("atomic:5", &[&mediator_init]),
        ],
        vec![],
    );
    let spec = vec![
        peel::quotient::SpecModuleGroup {
            module_id: "mod_dep".to_string(),
            owner_ids: vec![
                "owner:dep_value".to_string(),
                "owner:lazy_reader".to_string(),
            ],
        },
        peel::quotient::SpecModuleGroup {
            module_id: "mod_dependent".to_string(),
            owner_ids: vec!["owner:cross_value".to_string()],
        },
        peel::quotient::SpecModuleGroup {
            module_id: "mod_mediator".to_string(),
            owner_ids: vec![
                "owner:mediator_helper".to_string(),
                "owner:mediator_init".to_string(),
            ],
        },
    ];

    // Materializer-side verdict.
    let (owner_graph, partition) = owner_graph_and_partition_from_spec(&report, &spec);
    let verdict = gate::check_realizability(&owner_graph, &partition);
    let materializer_unrealizable = !verdict.is_realizable();
    assert!(
        materializer_unrealizable,
        "fixture is supposed to be unrealizable per the materializer: {verdict:?}",
    );

    // Planner-side verdict.
    let (_q, rejected) =
        peel::quotient::build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, 10_000);
    let planner_has_rejection = !rejected.is_empty();

    // The two MUST agree. If the materializer says unrealizable, the
    // planner must surface a seed rejection — both seeing the same
    // asymmetric I-cycle.
    //
    // Note: the planner's surfacing is granular (per spec-module or
    // per atomic-DAG edge). For this fixture both modules are
    // singletons, so no in-module contraction happens; the cycle
    // surfaces only if the planner *also* walks the post-seed
    // partition and reports cycles, OR if the kernel's contract gate
    // refuses some upstream merge. Either is acceptable evidence.
    //
    // Until Track A lands, the planner sees no `LazyUse` back-edge
    // (it filters non-constraining edges) and the verdicts diverge.
    assert_eq!(
        materializer_unrealizable, planner_has_rejection,
        "planner and materializer disagree on asymmetric I-cycle fixture:\n\
         materializer unrealizable = {materializer_unrealizable}, \
         planner rejected = {planner_has_rejection}\n\
         materializer verdict: {verdict:?}\n\
         planner rejections: {rejected:?}",
    );
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
    //     (realizable; planner used to over-reject when treated
    //     differently from materializer).

    struct Case {
        label: &'static str,
        report: analysis::OwnerGraphReport,
        spec: Vec<peel::quotient::SpecModuleGroup>,
    }

    let mut cases: Vec<Case> = Vec::new();

    // Case 1: empty graph.
    cases.push(Case {
        label: "empty",
        report: graph_of(vec![], vec![], vec![], vec![]),
        spec: vec![],
    });

    // Case 2: single module, no cross-module edges.
    {
        let a = active_owner("owner:a", 1, &["BindingA"], 5, "mod_solo");
        let b = active_owner("owner:b", 2, &["BindingB"], 5, "mod_solo");
        cases.push(Case {
            label: "single_module_intra_edges",
            report: graph_of(
                vec![a.clone(), b.clone()],
                vec![owner_edge(
                    "edge:0",
                    "owner:a",
                    "owner:b",
                    analysis::DepKind::EagerUse,
                    true,
                )],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&b]),
                ],
                vec![],
            ),
            spec: vec![peel::quotient::SpecModuleGroup {
                module_id: "mod_solo".to_string(),
                owner_ids: vec!["owner:a".to_string(), "owner:b".to_string()],
            }],
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
            report: graph_of(
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
                vec![
                    atomic_unit_for("atomic:0", &[&entry]),
                    atomic_unit_for("atomic:1", &[&dep_value]),
                    atomic_unit_for("atomic:2", &[&lazy_reader]),
                    atomic_unit_for("atomic:3", &[&cross_value]),
                    atomic_unit_for("atomic:4", &[&mediator_helper]),
                    atomic_unit_for("atomic:5", &[&mediator_init]),
                ],
                vec![],
            ),
            spec: vec![
                peel::quotient::SpecModuleGroup {
                    module_id: "mod_dep".to_string(),
                    owner_ids: vec![
                        "owner:dep_value".to_string(),
                        "owner:lazy_reader".to_string(),
                    ],
                },
                peel::quotient::SpecModuleGroup {
                    module_id: "mod_dependent".to_string(),
                    owner_ids: vec!["owner:cross_value".to_string()],
                },
                peel::quotient::SpecModuleGroup {
                    module_id: "mod_mediator".to_string(),
                    owner_ids: vec![
                        "owner:mediator_helper".to_string(),
                        "owner:mediator_init".to_string(),
                    ],
                },
            ],
        });
    }

    // Case 4: mutual constraining cycle.
    {
        let a1 = residual_owner("owner:a1", 1, &["BindingA1"], 5);
        let b1 = residual_owner("owner:b1", 2, &["BindingB1"], 5);
        cases.push(Case {
            label: "mutual_constraining_cycle",
            report: graph_of(
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
                vec![
                    atomic_unit_for("atomic:0", &[&a1]),
                    atomic_unit_for("atomic:1", &[&b1]),
                ],
                vec![],
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
            report: graph_of(
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
                vec![
                    atomic_unit_for("atomic:0", &[&alpha]),
                    atomic_unit_for("atomic:1", &[&beta]),
                ],
                vec![],
            ),
            spec: vec![
                peel::quotient::SpecModuleGroup {
                    module_id: "mod_alpha".to_string(),
                    owner_ids: vec!["owner:alpha".to_string()],
                },
                peel::quotient::SpecModuleGroup {
                    module_id: "mod_beta".to_string(),
                    owner_ids: vec!["owner:beta".to_string()],
                },
            ],
        });
    }

    for case in &cases {
        // Materializer-side.
        let (owner_graph, partition) =
            owner_graph_and_partition_from_spec(&case.report, &case.spec);
        let verdict = gate::check_realizability(&owner_graph, &partition);
        let materializer_unrealizable = !verdict.is_realizable();

        // Planner-side.
        let (_q, rejected) = peel::quotient::build_seed_quotient(
            &case.report,
            &case.report.atomic_graph.nodes,
            &case.spec,
            10_000,
        );
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
    }
}

#[test]
fn incremental_kernel_query_matches_rebuild_after_each_contract() {
    // Property test (extends `incremental_state_matches_rebuild_on_synthetic_specs`):
    // after every greedy contraction in the unified gate, the
    // incremental kernel's verdict on a candidate merge agrees with
    // the materializer's verdict on a from-scratch projected
    // partition. This pins the unified gate's cross-query state
    // updates against the from-scratch reference.

    let mut fixtures: Vec<(
        &'static str,
        analysis::OwnerGraphReport,
        Vec<peel::quotient::PartitionGroup>,
    )> = Vec::new();
    fixtures.push(("empty_corpus", fixture_singletons().1, vec![]));

    {
        let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
        let h1 = residual_owner("owner:h1", 2, &["BindingH1"], 5);
        let h2 = residual_owner("owner:h2", 3, &["BindingH2"], 5);
        fixtures.push((
            "single_module_two_orphans_unified",
            graph_of(
                vec![a.clone(), h1.clone(), h2.clone()],
                vec![
                    owner_edge(
                        "edge:0",
                        "owner:a",
                        "owner:h1",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                    owner_edge(
                        "edge:1",
                        "owner:a",
                        "owner:h2",
                        analysis::DepKind::EagerUse,
                        true,
                    ),
                ],
                vec![
                    atomic_unit_for("atomic:0", &[&a]),
                    atomic_unit_for("atomic:1", &[&h1]),
                    atomic_unit_for("atomic:2", &[&h2]),
                ],
                vec![],
            ),
            vec![make_module_group("ui/x", vec![0])],
        ));
    }

    for (label, report, groups) in fixtures {
        let (mut incremental, _) =
            peel::quotient::QuotientGraph::from_report_with_partition_extended(
                &report, 10_000, &groups,
            );

        loop {
            let one = peel::quotient::greedy_step(&mut incremental);
            let Some(_) = one else { break };
            // After the contract, the cached cycle set should equal
            // a from-scratch rebuild over the same partition.
            let cached = incremental.cycle_set();
            let replay = replay_partition(&report, &groups, &incremental, 10_000);
            assert_eq!(
                cached,
                replay.cycle_set(),
                "[{label}] cached cycle set diverges from rebuild after merge",
            );
        }
    }
}

// ---------------------------------------------------------------------
// Lazy-PQ vs. full-scan byte-equality corpus.
//
// See `plans/peel_lazy_pq_greedy.md` "Output equivalence to the
// current greedy" — this is the load-bearing correctness gate for
// the PQ-driven greedy. Every fixture below builds a quotient and
// runs both drivers from identical starting states; the contraction
// sequences must be byte-equal.
// ---------------------------------------------------------------------

/// Build a fixture quotient from a report + spec module groups. Two
/// independent quotients are constructed (one for each driver) so
/// the comparison is over isolated graphs.
fn build_fixture(
    report: &OwnerGraphReport,
    groups: &[peel::quotient::PartitionGroup],
    cap_lines: usize,
) -> (QuotientGraph, QuotientGraph) {
    let (q_a, _) = QuotientGraph::from_report_with_partition_extended(report, cap_lines, groups);
    let (q_b, _) = QuotientGraph::from_report_with_partition_extended(report, cap_lines, groups);
    (q_a, q_b)
}

/// Fixture 1: chain. One pre-existing module a; orphans b → c → d → e
/// chain backward into a. Greedy should absorb them sequentially.
fn fixture_chain() -> (OwnerGraphReport, Vec<peel::quotient::PartitionGroup>) {
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
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone(), d.clone(), e.clone()],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&c]),
            atomic_unit_for("atomic:3", &[&d]),
            atomic_unit_for("atomic:4", &[&e]),
        ],
        vec![],
    );
    let groups = vec![make_module_group("ui/x", vec![0])];
    (report, groups)
}

/// Fixture 2: star topology. One pre-existing module a; orphans b,
/// c, d, e each connect ONLY to a (no inter-orphan edges). Greedy
/// absorbs each in some deterministic order.
fn fixture_star() -> (OwnerGraphReport, Vec<peel::quotient::PartitionGroup>) {
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
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone(), d.clone(), e.clone()],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&c]),
            atomic_unit_for("atomic:3", &[&d]),
            atomic_unit_for("atomic:4", &[&e]),
        ],
        vec![],
    );
    let groups = vec![make_module_group("ui/x", vec![0])];
    (report, groups)
}

/// Fixture 3: mutual-eager edges between two pre-existing modules
/// (no constraining cycle — eager reads in one direction only, even
/// though both directions exist on the I-graph). The greedy must
/// either merge the two modules or leave them alone deterministically.
fn fixture_mutual_eager() -> (OwnerGraphReport, Vec<peel::quotient::PartitionGroup>) {
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
    let report = graph_of(
        vec![a.clone(), b.clone(), h1.clone(), h2.clone()],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&h1]),
            atomic_unit_for("atomic:3", &[&h2]),
        ],
        vec![],
    );
    let groups = vec![
        make_module_group("ui/x", vec![0]),
        make_module_group("ui/y", vec![1]),
    ];
    (report, groups)
}

/// Fixture 4: asymmetric cycle that the greedy must resolve by
/// contracting one pair, dissolving the other side of the cycle.
/// Two modules a, b with constraining edges a → b and b → a (via
/// helpers); greedy should pick one merge.
fn fixture_asymmetric_cycle() -> (OwnerGraphReport, Vec<peel::quotient::PartitionGroup>) {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/x");
    let b = active_owner("owner:b", 2, &["BindingB"], 10, "ui/y");
    let h = residual_owner("owner:h", 3, &["BindingH"], 5);
    let edges = vec![
        // a → h → b is the forward path; b → a directly closes the
        // cycle. h is an orphan with a unique extension target (a).
        owner_edge("edge:0", "owner:a", "owner:h", DepKind::EagerUse, true),
        owner_edge("edge:1", "owner:h", "owner:b", DepKind::EagerUse, true),
    ];
    let report = graph_of(
        vec![a.clone(), b.clone(), h.clone()],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&h]),
        ],
        vec![],
    );
    let groups = vec![
        make_module_group("ui/x", vec![0]),
        make_module_group("ui/y", vec![1]),
    ];
    (report, groups)
}

/// Fixture 5: fully-connected small classes. Three pre-existing
/// modules a, b, c; each pair has a constraining edge in one
/// direction (forming a 3-cycle). Plus three orphans, each uniquely
/// pointing to one of the modules. Tests coupling-drift handling:
/// once one orphan is absorbed, its absorber's coupling vs. the
/// other modules may shift.
fn fixture_fully_connected_small() -> (OwnerGraphReport, Vec<peel::quotient::PartitionGroup>) {
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
    let report = graph_of(
        vec![
            a.clone(),
            b.clone(),
            c.clone(),
            oa.clone(),
            ob.clone(),
            oc.clone(),
        ],
        edges,
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&c]),
            atomic_unit_for("atomic:3", &[&oa]),
            atomic_unit_for("atomic:4", &[&ob]),
            atomic_unit_for("atomic:5", &[&oc]),
        ],
        vec![],
    );
    let groups = vec![
        make_module_group("ui/x", vec![0]),
        make_module_group("ui/y", vec![1]),
        make_module_group("ui/z", vec![2]),
    ];
    (report, groups)
}

type Fixture = (OwnerGraphReport, Vec<peel::quotient::PartitionGroup>);
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
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        vec![
            owner_edge("edge:0", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:b", "owner:c", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&c]),
        ],
        vec![],
    );
    let (mut q, group_classes) = QuotientGraph::from_report_with_partition(
        &report,
        10_000,
        &[vec![OwnerIdx(0), OwnerIdx(2)]],
    );
    let merged = group_classes[0];
    let b_class = q.class_of(OwnerIdx(1));
    assert_eq!(q.class_of(OwnerIdx(0)), merged);
    assert_eq!(q.class_of(OwnerIdx(2)), merged);

    // The cycle is visible to the kernel's evidence surface.
    assert!(
        !q.cycle_set().cycles.is_empty(),
        "the bypassed contraction's class cycle must surface in cycle_set()",
    );

    // The gate still answers on the unrealizable state: merging the
    // cycle classes together dissolves the cycle into one class, so
    // the contraction is permitted and the kernel returns to a
    // realizable, cycle-free state.
    let survivor = q.contract(merged, b_class).expect("cycle-dissolving merge");
    assert_eq!(q.class_of(OwnerIdx(1)), survivor);
    assert!(
        q.cycle_set().cycles.is_empty(),
        "dissolving the cycle must clear the evidence",
    );
    // And gated merges keep functioning after recovery.
    assert!(!q.merge_preserves_invariants(survivor, survivor));
}
