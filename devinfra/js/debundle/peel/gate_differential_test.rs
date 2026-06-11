//! Differential harness skeleton for the gate-ladder unification
//! (`plans/incremental_gate_unification.md` §7.1; PR 1 of §8).
//!
//! Compares the kernel's hot boolean merge gate
//! (`QuotientGraph::merge_preserves_invariants`) against the plan-§2
//! **reference predicate**: `gate(c1, c2)` should accept iff
//! `check_realizability_touching(owner_graph, post_partition, M)` is
//! realizable, where `M` is the post-merge module and
//! `post_partition` is built by an independent reference projection
//! (NOT the kernel's own `project_partition`).
//!
//! The current gate is a constraining-only class-level walk and is
//! KNOWN to diverge from the reference; every divergence the harness
//! observes must fall into the catalog below ([`DivergenceClass`]),
//! and the deterministic fixtures assert that each cataloged
//! divergence actually occurs today — the "known-divergence catalog
//! encoded as expected failures". When PR 4 routes
//! `check_merge_boolean` through the realizability ladder, those
//! fixture assertions flip to agreement and this harness becomes a
//! strict-equality check (and the catalog is deleted).
//!
//! Skeleton caveats (completed by Track F1 in later PRs):
//! - generation uses a deterministic xorshift sweep over small
//!   synthetic reports rather than proptest (no proptest dep in the
//!   crate universe yet);
//! - per-tier skip-soundness assertions arrive with the ladder
//!   itself (PR 3).

use std::collections::{BTreeMap, BTreeSet};

use analysis::ids::{LogicalModuleIndex, ModuleId};
use analysis::partition::Partition;
use analysis::{DepKind, OwnerGraphNodeReport, OwnerGraphReport};
use gate::{RealizabilityVerdict, SccRejection, check_realizability_touching};
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
// Query comparison + the known-divergence catalog.
// ---------------------------------------------------------------------

/// Catalog of divergences the current (pre-ladder) gate is known to
/// produce against the reference predicate. Anything outside this
/// enum fails the harness. PR 4 empties the catalog.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum DivergenceClass {
    /// Gate rejects, reference accepts: the class-level walk sees a
    /// cycle among classes that project into one module (plan §2's
    /// atomic-unit anomaly and pass-3 class-cycle rejections).
    ClassLevelOverRejection,
    /// Gate accepts, reference rejects with `EsmEvaluationTdz`: the
    /// hot gate is blind to Pass 2 (plan §1 item 1).
    Pass2Blindness,
    /// Gate accepts, reference rejects with a
    /// `MutualConstrainingCycle`: the class graph is finer than the
    /// module projection, so a cycle through two *distinct* residual
    /// classes is invisible at class granularity (plan §1 item 2).
    ModuleGranularityPass1,
    /// Gate accepts, reference rejects with a clause-2 cross-rebind:
    /// the hot gate never checks rebinds, and a gate-residual
    /// promotion can turn an intra-residual rebind into a
    /// cross-module one (a caveat to plan §3's "merges only ever
    /// convert cross-rebinds to intra-module" formality claim).
    CrossRebindBlindness,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum QueryOutcome {
    Agree,
    /// The PRE-state verdict touching `M` is already unrealizable.
    /// The plan-§2 equality claim ("touching-filtered and
    /// full-verdict accept/reject coincide") is scoped to realizable
    /// pre-states, so these queries are outside the harness's
    /// equality domain and are tallied, not compared.
    DirtyPreState,
    Diverged(DivergenceClass),
}

/// Public replica of the kernel's non-cycle merge preconditions
/// (same class / emptiness / residual stickiness / line cap). The
/// reference predicate covers only the cycle clause, so the harness
/// compares only when these pass.
fn preconditions_pass(q: &QuotientGraph, c1: ClassId, c2: ClassId) -> bool {
    c1 != c2
        && q.class_members(c1).next().is_some()
        && q.class_members(c2).next().is_some()
        && q.class_is_residual(c1) == q.class_is_residual(c2)
        && q.class_lines(c1).saturating_add(q.class_lines(c2)) <= CAP_LINES
}

fn classify_divergence(gate_accepts: bool, reference: &RealizabilityVerdict) -> DivergenceClass {
    if !gate_accepts {
        return DivergenceClass::ClassLevelOverRejection;
    }
    if reference
        .unrealizable_sccs
        .iter()
        .any(|scc| scc.rejection == SccRejection::MutualConstrainingCycle)
    {
        return DivergenceClass::ModuleGranularityPass1;
    }
    if reference
        .unrealizable_sccs
        .iter()
        .any(|scc| scc.rejection == SccRejection::EsmEvaluationTdz)
    {
        return DivergenceClass::Pass2Blindness;
    }
    assert!(
        !reference.cross_rebinds.is_empty(),
        "a rejecting reference verdict with no SCCs must carry cross-rebinds: {reference:#?}",
    );
    DivergenceClass::CrossRebindBlindness
}

/// Run one gate-vs-reference comparison. Returns `None` when the
/// non-cycle preconditions fail (the gate's `false` would not be a
/// predicate decision).
fn compare_gate_to_reference(
    report: &OwnerGraphReport,
    q: &mut QuotientGraph,
    c1: ClassId,
    c2: ClassId,
) -> Option<QueryOutcome> {
    if !preconditions_pass(q, c1, c2) {
        return None;
    }
    let residual_ids = residual_owner_ids(report);
    let (pre_modules, next_fresh) = reference_class_modules(q, &residual_ids);
    let post_module = reference_post_module(q, &residual_ids, &pre_modules, next_fresh, c1, c2);
    let pre_partition = reference_partition(q, &pre_modules, None);
    let post_partition = reference_partition(q, &pre_modules, Some((c1, c2, post_module)));
    let owner_graph = q.owner_graph_for_tests();
    let reference = check_realizability_touching(owner_graph, &post_partition, post_module);
    let pre_dirty =
        !check_realizability_touching(owner_graph, &pre_partition, post_module).is_realizable();
    let gate_accepts = q.merge_preserves_invariants(c1, c2);
    Some(if gate_accepts == reference.is_realizable() {
        QueryOutcome::Agree
    } else if pre_dirty {
        QueryOutcome::DirtyPreState
    } else {
        QueryOutcome::Diverged(classify_divergence(gate_accepts, &reference))
    })
}

// ---------------------------------------------------------------------
// Deterministic divergence fixtures — the catalog's expected failures.
// Each asserts the divergence the current gate produces TODAY; when
// PR 4 lands the ladder, these flip to `Agree` and the catalog dies.
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
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups);
    let live: Vec<ClassId> = q.iter_classes().collect();
    for i in 0..live.len() {
        for j in (i + 1)..live.len() {
            let outcome = compare_gate_to_reference(&report, &mut q, live[i], live[j])
                .expect("chain classes pass preconditions");
            assert_eq!(
                outcome,
                QueryOutcome::Agree,
                "({:?}, {:?}) must agree on the clean chain",
                live[i],
                live[j],
            );
        }
    }
}

/// Expected failure (plan §2, atomic-unit anomaly): merging two
/// members of a residual-pile constraining 3-cycle is a delta-free
/// no-op for the reference, but the class-level gate rejects it.
#[test]
fn gate_over_rejects_residual_pile_cycle_merge() {
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
    let mut q = QuotientGraph::from_report(&report, CAP_LINES);
    let ca = q.class_of(q.owner_idx_of("owner:a").unwrap());
    let cb = q.class_of(q.owner_idx_of("owner:b").unwrap());
    let outcome = compare_gate_to_reference(&report, &mut q, ca, cb).unwrap();
    assert_eq!(
        outcome,
        QueryOutcome::Diverged(DivergenceClass::ClassLevelOverRejection),
        "current gate is expected to over-reject this residual-pile \
         merge; if this now AGREES, the ladder landed — un-ignore the \
         §7.3 pinning tests and flip this harness to strict equality",
    );
}

/// Expected failure (plan §1 item 1): a merge that closes an
/// asymmetric I-SCC whose constraining pair TDZs is accepted by the
/// hot gate but rejected by the reference with `EsmEvaluationTdz`.
#[test]
fn gate_accepts_pass2_tdz_merge_reference_rejects() {
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
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups);
    let cx = group_ids[0];
    let ch = q.class_of(q.owner_idx_of("owner:h").unwrap());
    let outcome = compare_gate_to_reference(&report, &mut q, cx, ch).unwrap();
    assert_eq!(
        outcome,
        QueryOutcome::Diverged(DivergenceClass::Pass2Blindness),
        "current gate is expected to be Pass-2-blind here; if this now \
         AGREES, the ladder landed — un-ignore the §7.3 pinning tests \
         and flip this harness to strict equality",
    );
}

/// Expected failure (plan §1 item 2): the class graph is finer than
/// the module projection. `a → r1` and `r2 → b` involve two distinct
/// residual classes, so promoting `b` into `ui/a` closes the
/// module-level mutual cycle `M ↔ R` without any class-level cycle.
#[test]
fn gate_accepts_module_granularity_pass1_cycle_reference_rejects() {
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
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups);
    let ca = group_ids[0];
    let cb = q.class_of(q.owner_idx_of("owner:b").unwrap());
    let outcome = compare_gate_to_reference(&report, &mut q, ca, cb).unwrap();
    assert_eq!(
        outcome,
        QueryOutcome::Diverged(DivergenceClass::ModuleGranularityPass1),
        "current gate is expected to miss the module-granularity \
         Pass-1 cycle; if this now AGREES, the ladder landed — \
         un-ignore the §7.3 pinning tests and flip this harness to \
         strict equality",
    );
}

/// Expected failure (clause-2 caveat to plan §3): a rebind between
/// two residual-pile owners is intra-module pre-merge; promoting the
/// writer's class into `ui/a` makes it a cross-module rebind the hot
/// gate never checks.
#[test]
fn gate_accepts_promotion_created_cross_rebind_reference_rejects() {
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
        QuotientGraph::from_report_with_partition_extended(&report, CAP_LINES, &groups);
    let ca = group_ids[0];
    let ch = q.class_of(q.owner_idx_of("owner:h").unwrap());
    let outcome = compare_gate_to_reference(&report, &mut q, ca, ch).unwrap();
    assert_eq!(
        outcome,
        QueryOutcome::Diverged(DivergenceClass::CrossRebindBlindness),
        "current gate is expected to be blind to promotion-created \
         cross-rebinds; if this now AGREES, the ladder landed — \
         un-ignore the §7.3 pinning tests and flip this harness to \
         strict equality",
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
/// Every divergence must be a cataloged [`DivergenceClass`];
/// `classify_divergence` panics on any rejecting-reference shape
/// outside the catalog.
#[test]
fn randomized_gate_divergences_stay_within_catalog() {
    let mut tally: BTreeMap<&'static str, usize> = BTreeMap::new();
    for seed in 1..=60u64 {
        let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15));
        let (report, spec) = random_report(&mut rng);
        let (mut q, _rejected) =
            build_seed_quotient(&report, &report.atomic_graph.nodes, &spec, CAP_LINES);
        for _round in 0..6 {
            let live: Vec<ClassId> = q.iter_classes().collect();
            let mut accepted_pair: Option<(ClassId, ClassId)> = None;
            for i in 0..live.len() {
                for j in (i + 1)..live.len() {
                    let Some(outcome) =
                        compare_gate_to_reference(&report, &mut q, live[i], live[j])
                    else {
                        *tally.entry("preconditions_failed").or_default() += 1;
                        continue;
                    };
                    let key = match outcome {
                        QueryOutcome::Agree => "agree",
                        QueryOutcome::DirtyPreState => "dirty_pre_state",
                        QueryOutcome::Diverged(DivergenceClass::ClassLevelOverRejection) => {
                            "class_level_over_rejection"
                        }
                        QueryOutcome::Diverged(DivergenceClass::Pass2Blindness) => {
                            "pass2_blindness"
                        }
                        QueryOutcome::Diverged(DivergenceClass::ModuleGranularityPass1) => {
                            "module_granularity_pass1"
                        }
                        QueryOutcome::Diverged(DivergenceClass::CrossRebindBlindness) => {
                            "cross_rebind_blindness"
                        }
                    };
                    *tally.entry(key).or_default() += 1;
                    if matches!(outcome, QueryOutcome::Agree)
                        && accepted_pair.is_none()
                        && q.merge_preserves_invariants(live[i], live[j])
                    {
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
    let agreed = tally.get("agree").copied().unwrap_or(0);
    assert!(
        agreed >= 100,
        "sweep must exercise a meaningful number of in-domain \
         agreeing queries; tally: {tally:?}",
    );
    eprintln!("gate-vs-reference sweep tally: {tally:?}");
}
