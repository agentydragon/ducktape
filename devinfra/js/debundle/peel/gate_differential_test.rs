//! Differential harness for the gate-ladder unification
//! (`plans/incremental_gate_unification.md` §7.1).
//!
//! Compares the kernel's hot boolean merge gate
//! (`QuotientGraph::merge_preserves_invariants`) against the plan-§2
//! **reference predicate**: `gate(c1, c2)` accepts iff
//! `check_realizability_touching(owner_graph, post_partition, M)` is
//! realizable, where `M` is the post-merge module and
//! `post_partition` is built by an independent reference projection
//! (NOT the kernel's own `project_partition`).
//!
//! Since the §8 PR 4 cutover, `check_merge_boolean` routes through
//! the index's tier ladder, so the harness asserts **strict
//! equality** on every precondition-passing query — the pre-cutover
//! known-divergence catalog is gone. Every comparison also asserts
//! per-tier skip soundness (§7.1) against the ladder's decision so a
//! ladder bug localizes to its tier. The deterministic fixtures pin
//! the three semantic fixes the cutover landed (the atomic-unit /
//! residual-pile over-rejection, Pass-2 blindness, module-granularity
//! Pass 1) plus the clause-2 cross-rebind caveat.
//!
//! Skeleton caveat (completed by Track F1 in later PRs): generation
//! uses a deterministic xorshift sweep over small synthetic reports
//! rather than proptest (no proptest dep in the crate universe yet).

use std::collections::{BTreeMap, BTreeSet};

use analysis::ids::{LogicalModuleIndex, ModuleId};
use analysis::partition::Partition;
use analysis::{DepKind, OwnerGraphNodeReport, OwnerGraphReport};
use gate::{LadderDecision, RealizabilityVerdict, SccRejection, check_realizability_touching};
use peel::quotient::{
    ClassId, OwnerIdx, PartitionGroup, QuotientGraph, SpecModuleGroup, build_seed_quotient,
    greedy_step,
};
use report_fixtures::{active_owner, atomic_unit_for, graph_of, owner_edge, residual_owner};

const CAP_LINES: usize = 10_000;

fn module_id(index: usize) -> ModuleId {
    ModuleId(LogicalModuleIndex(index))
}

// ---------------------------------------------------------------------
// Independent reference projection.
//
// Rebuilds the module-level partition from the kernel's *public*
// class-membership surface plus the report's own residual flags —
// per the Track F note, deliberately not the kernel's
// `project_partition`. Projection rules (docs/design.md / plan §2):
//   * a class maps to the residual module `logical(0)` iff it is the
//     marked residual catch-all, or it is not anchored to a
//     pre-existing module and every member owner is residual-destined
//     per the report;
//   * every other class gets a distinct module id, assigned in
//     ascending `ClassId` order starting at 1.
// ---------------------------------------------------------------------

/// Owner ids the report marks residual-destined (the gate-residual
/// pile), derived from the report's module table only.
fn residual_owner_ids(report: &OwnerGraphReport) -> BTreeSet<String> {
    report
        .nodes
        .iter()
        .filter(|node| report.is_residual(&node.destination))
        .map(|node| node.id.clone())
        .collect()
}

fn class_maps_to_residual(
    q: &QuotientGraph,
    residual_ids: &BTreeSet<String>,
    class: ClassId,
) -> bool {
    q.class_is_residual(class)
        || (!q.class_is_pre_existing_module(class)
            && q.class_members(class)
                .all(|owner| residual_ids.contains(q.owner_id(owner))))
}

/// Pre-state class → module map plus the next free module index.
fn reference_class_modules(
    q: &QuotientGraph,
    residual_ids: &BTreeSet<String>,
) -> (BTreeMap<ClassId, ModuleId>, usize) {
    let mut map = BTreeMap::new();
    let mut next = 1usize;
    for class in q.iter_classes() {
        let module = if class_maps_to_residual(q, residual_ids, class) {
            module_id(0)
        } else {
            let module = module_id(next);
            next += 1;
            module
        };
        map.insert(class, module);
    }
    (map, next)
}

/// The module the merged `(c1, c2)` class maps to under the reference
/// projection, in the pre-state's module-id space (stable ids let the
/// pre- and post-state verdicts talk about the same `M`).
fn reference_post_module(
    q: &QuotientGraph,
    residual_ids: &BTreeSet<String>,
    pre_modules: &BTreeMap<ClassId, ModuleId>,
    next_fresh: usize,
    c1: ClassId,
    c2: ClassId,
) -> ModuleId {
    let (winner, loser) = (c1.min(c2), c1.max(c2));
    if q.class_is_residual(winner) || q.class_is_residual(loser) {
        return module_id(0);
    }
    let pre_existing =
        q.class_is_pre_existing_module(winner) || q.class_is_pre_existing_module(loser);
    if !pre_existing
        && q.class_members(winner)
            .chain(q.class_members(loser))
            .all(|owner| residual_ids.contains(q.owner_id(owner)))
    {
        return module_id(0);
    }
    // Mixed or pre-existing: reuse the winner's non-residual slot,
    // else the loser's, else mint a fresh id (the gate-residual
    // promotion transition).
    [pre_modules[&winner], pre_modules[&loser]]
        .into_iter()
        .find(|module| *module != module_id(0))
        .unwrap_or(module_id(next_fresh))
}

/// Owner-indexed partition from a class → module map, with the
/// optional speculative `(winner ∪ loser) → M` overlay applied.
fn reference_partition(
    q: &QuotientGraph,
    class_modules: &BTreeMap<ClassId, ModuleId>,
    overlay: Option<(ClassId, ClassId, ModuleId)>,
) -> Partition {
    let residual = module_id(0);
    let mut of = vec![residual; q.owner_graph_for_tests().num_nodes()];
    for class in q.iter_classes() {
        let module = match overlay {
            Some((c1, c2, post)) if class == c1 || class == c2 => post,
            _ => class_modules[&class],
        };
        for owner in q.class_members(class) {
            of[owner.0] = module;
        }
    }
    Partition::from_assignments(of, residual)
}

// ---------------------------------------------------------------------
// Query comparison.
// ---------------------------------------------------------------------

/// Public replica of the kernel's non-cycle merge preconditions
/// (same class / emptiness / residual stickiness / line cap). The
/// reference predicate covers only the realizability clause, so the
/// harness compares only when these pass.
fn preconditions_pass(q: &QuotientGraph, c1: ClassId, c2: ClassId) -> bool {
    c1 != c2
        && q.class_members(c1).next().is_some()
        && q.class_members(c2).next().is_some()
        && q.class_is_residual(c1) == q.class_is_residual(c2)
        && q.class_lines(c1).saturating_add(q.class_lines(c2)) <= CAP_LINES
}

/// The tier ladder must equal the reference predicate on EVERY query,
/// and each deciding tier must be certified by the reference shape its
/// skip-condition theorem names (plan §7.1 tier-skip soundness), so a
/// ladder bug localizes to its tier.
fn assert_ladder_matches_reference(
    c1: ClassId,
    c2: ClassId,
    ladder: LadderDecision,
    reference: &RealizabilityVerdict,
) {
    assert_eq!(
        ladder.accepts(),
        reference.is_realizable(),
        "({c1:?}, {c2:?}): ladder {ladder:?} diverges from the reference \
         predicate: {reference:#?}",
    );
    let mutual_cycle = reference
        .unrealizable_sccs
        .iter()
        .any(|scc| scc.rejection == SccRejection::MutualConstrainingCycle);
    let tdz = reference
        .unrealizable_sccs
        .iter()
        .any(|scc| scc.rejection == SccRejection::EsmEvaluationTdz);
    match ladder {
        LadderDecision::ConstrainingCycleReject => assert!(
            mutual_cycle,
            "({c1:?}, {c2:?}): tier-1 reject must be certified by a \
             MutualConstrainingCycle touching M: {reference:#?}",
        ),
        LadderDecision::CrossRebindReject => assert!(
            !reference.cross_rebinds.is_empty(),
            "({c1:?}, {c2:?}): tier-1 rebind reject must be certified by a \
             clause-2 cross-rebind touching M: {reference:#?}",
        ),
        LadderDecision::SimulatorReject => assert!(
            tdz,
            "({c1:?}, {c2:?}): tier-3 reject must be certified by an \
             EsmEvaluationTdz diagnosis touching M: {reference:#?}",
        ),
        LadderDecision::NoMultiModuleISccAccept | LadderDecision::NoConstrainingPairAccept => {
            assert!(
                !tdz,
                "({c1:?}, {c2:?}): tier-2 vacuity claims Pass 2 produced \
                 nothing touching M: {reference:#?}",
            )
        }
        LadderDecision::DeltaFreeAccept
        | LadderDecision::DeltaFreeReject
        | LadderDecision::SimulatorAccept => {}
    }
}

/// Run one gate-vs-reference comparison: asserts the boolean gate and
/// the ladder both equal the reference predicate, then returns the
/// ladder's decision (`accepts()` is the gate decision, asserted
/// equal). Returns `None` when the non-cycle preconditions fail (the
/// gate's `false` would not be a predicate decision).
fn compare_gate_to_reference(
    report: &OwnerGraphReport,
    q: &mut QuotientGraph,
    c1: ClassId,
    c2: ClassId,
) -> Option<LadderDecision> {
    if !preconditions_pass(q, c1, c2) {
        return None;
    }
    let residual_ids = residual_owner_ids(report);
    let (pre_modules, next_fresh) = reference_class_modules(q, &residual_ids);
    let post_module = reference_post_module(q, &residual_ids, &pre_modules, next_fresh, c1, c2);
    let post_partition = reference_partition(q, &pre_modules, Some((c1, c2, post_module)));
    let owner_graph = q.owner_graph_for_tests();
    let reference = check_realizability_touching(owner_graph, &post_partition, post_module);
    let gate_accepts = q.merge_preserves_invariants(c1, c2);
    let ladder = q.ladder_decision_for_merge(c1, c2);
    assert_ladder_matches_reference(c1, c2, ladder, &reference);
    assert_eq!(
        gate_accepts,
        reference.is_realizable(),
        "({c1:?}, {c2:?}): boolean gate diverges from the reference \
         predicate (ladder {ladder:?}): {reference:#?}",
    );
    Some(ladder)
}

// ---------------------------------------------------------------------
// Deterministic fixtures pinning the semantic fixes the §8 PR 4
// cutover landed. Pre-cutover, each was a cataloged divergence of the
// class-level gate; post-cutover the gate equals the reference and
// each fixture pins the decision's direction.
// ---------------------------------------------------------------------

fn make_module_group(module_id: &str, owner_idxs: Vec<usize>) -> PartitionGroup {
    PartitionGroup {
        owner_idxs: owner_idxs.into_iter().map(OwnerIdx).collect(),
        is_pre_existing_module: true,
        label: Some(module_id.to_string()),
    }
}

/// Sanity anchor: on a clean acyclic shape the gate and the reference
/// agree on every precondition-passing pair (both directions —
/// accepts and rejects).
#[test]
fn gate_matches_reference_on_clean_chain() {
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
    let (mut q, _) =
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups).unwrap();
    let live: Vec<ClassId> = q.iter_classes().collect();
    for i in 0..live.len() {
        for j in (i + 1)..live.len() {
            // Equality with the reference is asserted inside.
            compare_gate_to_reference(&report, &mut q, live[i], live[j])
                .expect("chain classes pass preconditions");
        }
    }
}

/// Plan §2's atomic-unit anomaly, fixed: merging two members of a
/// residual-pile constraining 3-cycle is a delta-free no-op for the
/// module-level predicate and must be accepted. The deleted
/// class-level gate over-rejected it.
#[test]
fn gate_accepts_residual_pile_cycle_merge() {
    let a = residual_owner("owner:a", 1, &["BindingA"], 5);
    let b = residual_owner("owner:b", 2, &["BindingB"], 5);
    let c = residual_owner("owner:c", 3, &["BindingC"], 5);
    let report = graph_of(
        vec![a.clone(), b.clone(), c.clone()],
        vec![
            owner_edge("edge:0", "owner:a", "owner:b", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:b", "owner:c", DepKind::EagerUse, true),
            owner_edge("edge:2", "owner:c", "owner:a", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&b]),
            atomic_unit_for("atomic:2", &[&c]),
        ],
        vec![],
    );
    let mut q = QuotientGraph::from_report(&report, CAP_LINES).unwrap();
    let ca = q.class_of(q.owner_idx_of("owner:a").unwrap());
    let cb = q.class_of(q.owner_idx_of("owner:b").unwrap());
    let ladder = compare_gate_to_reference(&report, &mut q, ca, cb).unwrap();
    assert_eq!(
        ladder,
        LadderDecision::DeltaFreeAccept,
        "residual-pile merge is a delta-free no-op for the module-level \
         gate and must be accepted at tier 0",
    );
}

/// Plan §1 item 1, fixed: a merge that closes an asymmetric I-SCC
/// whose constraining pair TDZs is rejected at the merge (tier 3,
/// `EsmEvaluationTdz`). The deleted hot gate was Pass-2-blind here.
#[test]
fn gate_rejects_pass2_tdz_merge() {
    let x = active_owner("owner:x", 1, &["BindingX"], 10, "ui/x");
    let r = residual_owner("owner:r", 2, &["BindingR"], 5);
    let h = residual_owner("owner:h", 3, &["BindingH"], 5);
    let report = graph_of(
        vec![x.clone(), r.clone(), h.clone()],
        vec![
            owner_edge("edge:0", "owner:x", "owner:r", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:r", "owner:h", DepKind::LazyUse, false),
        ],
        vec![
            atomic_unit_for("atomic:0", &[&x]),
            atomic_unit_for("atomic:1", &[&r]),
            atomic_unit_for("atomic:2", &[&h]),
        ],
        vec![],
    );
    let groups = vec![make_module_group("ui/x", vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups).unwrap();
    let cx = group_ids[0];
    let ch = q.class_of(q.owner_idx_of("owner:h").unwrap());
    let ladder = compare_gate_to_reference(&report, &mut q, cx, ch).unwrap();
    assert_eq!(
        ladder,
        LadderDecision::SimulatorReject,
        "TDZ-closing merge must be rejected at the merge by tier 3",
    );
}

/// Plan §1 item 2, fixed: the class graph is finer than the module
/// projection. `a → r1` and `r2 → b` involve two distinct residual
/// classes, so promoting `b` into `ui/a` closes the module-level
/// mutual cycle `M ↔ R` without any class-level cycle — invisible to
/// the deleted class-granularity gate, rejected by tier 1.
#[test]
fn gate_rejects_module_granularity_pass1_cycle() {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/a");
    let r1 = residual_owner("owner:r1", 2, &["BindingR1"], 5);
    let r2 = residual_owner("owner:r2", 3, &["BindingR2"], 5);
    let b = residual_owner("owner:b", 4, &["BindingB"], 5);
    let report = graph_of(
        vec![a.clone(), r1.clone(), r2.clone(), b.clone()],
        vec![
            owner_edge("edge:0", "owner:a", "owner:r1", DepKind::EagerUse, true),
            owner_edge("edge:1", "owner:r2", "owner:b", DepKind::EagerUse, true),
        ],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&r1]),
            atomic_unit_for("atomic:2", &[&r2]),
            atomic_unit_for("atomic:3", &[&b]),
        ],
        vec![],
    );
    let groups = vec![make_module_group("ui/a", vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups).unwrap();
    let ca = group_ids[0];
    let cb = q.class_of(q.owner_idx_of("owner:b").unwrap());
    let ladder = compare_gate_to_reference(&report, &mut q, ca, cb).unwrap();
    assert_eq!(
        ladder,
        LadderDecision::ConstrainingCycleReject,
        "module-granularity Pass-1 cycle must be rejected by tier 1",
    );
}

/// Clause-2 caveat to plan §3, fixed: a rebind between two
/// residual-pile owners is intra-module pre-merge; promoting the
/// writer's class into `ui/a` makes it a cross-module rebind, which
/// tier 1 rejects. The deleted hot gate never checked rebinds.
#[test]
fn gate_rejects_promotion_created_cross_rebind() {
    let a = active_owner("owner:a", 1, &["BindingA"], 10, "ui/a");
    let h = residual_owner("owner:h", 2, &["BindingH"], 5);
    let r = residual_owner("owner:r", 3, &["BindingR"], 5);
    let report = graph_of(
        vec![a.clone(), h.clone(), r.clone()],
        vec![owner_edge(
            "edge:0",
            "owner:h",
            "owner:r",
            DepKind::EagerRebind,
            true,
        )],
        vec![
            atomic_unit_for("atomic:0", &[&a]),
            atomic_unit_for("atomic:1", &[&h]),
            atomic_unit_for("atomic:2", &[&r]),
        ],
        vec![],
    );
    let groups = vec![make_module_group("ui/a", vec![0])];
    let (mut q, group_ids) =
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups).unwrap();
    let ca = group_ids[0];
    let ch = q.class_of(q.owner_idx_of("owner:h").unwrap());
    let ladder = compare_gate_to_reference(&report, &mut q, ca, ch).unwrap();
    assert_eq!(
        ladder,
        LadderDecision::CrossRebindReject,
        "promotion-created cross-module rebind must be rejected by \
         tier 1's clause-2 check",
    );
}

// ---------------------------------------------------------------------
// Randomized sweep over small synthetic reports.
// ---------------------------------------------------------------------

/// Deterministic xorshift64 — placeholder for the Track F1 proptest
/// generator; no external dep, fully reproducible.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }

    fn chance(&mut self, percent: u64) -> bool {
        self.next() % 100 < percent
    }
}

/// Random small report: mixed residual/active owners, mixed
/// `DepKind`s (including lazy back-edges and rebinds), singleton
/// atomic units plus the occasional multi-member unit, and spec
/// module groups derived from the active destinations.
fn random_report(rng: &mut Rng) -> (OwnerGraphReport, Vec<SpecModuleGroup>) {
    let owner_count = 4 + rng.below(5);
    let nodes: Vec<OwnerGraphNodeReport> = (0..owner_count)
        .map(|i| {
            let id = format!("owner:{i}");
            let binding = format!("B{i}");
            if rng.chance(55) {
                residual_owner(&id, i + 1, &[&binding], 5)
            } else {
                let module = rng.below(3);
                active_owner(&id, i + 1, &[&binding], 5, &format!("ui/m{module}"))
            }
        })
        .collect();

    let mut edges = Vec::new();
    for source in 0..owner_count {
        for target in 0..owner_count {
            if source == target || !rng.chance(22) {
                continue;
            }
            let (kind, constrains) = match rng.below(8) {
                0..=3 => (DepKind::EagerUse, true),
                4..=5 => (DepKind::LazyUse, false),
                6 => (DepKind::Sequenced, true),
                _ => (DepKind::EagerRebind, true),
            };
            edges.push(owner_edge(
                &format!("edge:{}", edges.len()),
                &nodes[source].id,
                &nodes[target].id,
                kind,
                constrains,
            ));
        }
    }

    let mut units: Vec<_> = nodes
        .iter()
        .enumerate()
        .map(|(i, node)| atomic_unit_for(&format!("atomic:{i}"), &[node]))
        .collect();
    if owner_count >= 2 && rng.chance(40) {
        let a = rng.below(owner_count);
        let b = rng.below(owner_count);
        if a != b {
            units.push(atomic_unit_for(
                &format!("atomic:{}", units.len()),
                &[&nodes[a], &nodes[b]],
            ));
        }
    }

    // Spec module groups: active owners grouped by destination path.
    let mut by_module: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for node in &nodes {
        let path = node.destination.as_str().to_string();
        if path != "residual" {
            by_module.entry(path).or_default().push(node.id.clone());
        }
    }
    let spec: Vec<SpecModuleGroup> = by_module
        .into_iter()
        .map(|(module_id, owner_ids)| SpecModuleGroup {
            module_id,
            owner_ids,
        })
        .collect();

    let report = graph_of(nodes, edges, units, vec![]);
    (report, spec)
}

/// Sweep: seed each random report through the REAL seed entry point
/// (`build_seed_quotient`), then alternate full pairwise gate-vs-
/// reference comparison rounds with committed mutations (the real
/// greedy driver, plus directly-contracted gate-accepted pairs).
/// `compare_gate_to_reference` asserts strict gate == reference ==
/// ladder equality plus tier-skip soundness on every query.
#[test]
fn randomized_gate_equals_reference() {
    // Keyed by `LadderDecision` debug name — the sweep's tier-hit
    // distribution — plus "preconditions_failed".
    let mut tally: BTreeMap<String, usize> = BTreeMap::new();
    let mut compared = 0usize;
    for seed in 1..=60u64 {
        let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15));
        let (report, spec) = random_report(&mut rng);
        let (mut q, _rejected) =
            build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, CAP_LINES).unwrap();
        for _round in 0..6 {
            let live: Vec<ClassId> = q.iter_classes().collect();
            let mut accepted_pair: Option<(ClassId, ClassId)> = None;
            for i in 0..live.len() {
                for j in (i + 1)..live.len() {
                    let Some(ladder) = compare_gate_to_reference(&report, &mut q, live[i], live[j])
                    else {
                        *tally.entry("preconditions_failed".to_string()).or_default() += 1;
                        continue;
                    };
                    compared += 1;
                    *tally.entry(format!("{ladder:?}")).or_default() += 1;
                    if ladder.accepts() && accepted_pair.is_none() {
                        accepted_pair = Some((live[i], live[j]));
                    }
                }
            }
            // Commit a mutation through a real entry point so later
            // rounds compare against evolved committed state.
            let mutated = if rng.chance(50) {
                greedy_step(&mut q).is_some()
            } else if let Some((c1, c2)) = accepted_pair {
                q.contract(c1, c2).is_ok()
            } else {
                false
            };
            if !mutated {
                break;
            }
        }
    }
    assert!(
        compared >= 100,
        "sweep must exercise a meaningful number of in-domain \
         queries; tally: {tally:?}",
    );
    eprintln!("gate-vs-reference sweep tier tally ({compared} compared): {tally:?}");
}
