use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use petgraph::algo::greedy_feedback_arc_set;
use petgraph::graph::DiGraph;
use serde::{Deserialize, Serialize};

use crate::realizability::{RealizabilityVerdict, SccRejection, check_realizability};
use analysis::factor_assembly::AtomicUnitConflict;
use analysis::graph::{EndpointView, OwnerEdgeId, partition_endpoints};
use analysis::partition::Partition;
use spec::ModulePath;
use swc_atoms::Atom;

use analysis::{DepKind, ModuleId, OwnerGraph, StatementOrdinal};

/// Result of validating a module dep graph.
#[derive(Debug, Clone, Serialize)]
pub struct FactorizationReport {
    pub cycles: Vec<CycleReport>,
    /// Atomic factor units the spec splits across destination
    /// modules — unrealizable by construction. Populated from
    /// `ChunkFactorization::assembly_conflicts`; the materializer rejects any
    /// spec with a non-empty list before emitting code.
    pub atomic_unit_conflicts: Vec<AtomicUnitConflict>,
    /// Topological linearization of `I ∪ S` rooted at the entry,
    /// dependency-first. Empty when the dep graph has cycles
    /// (validation rejects). Captured here so debug tooling can
    /// see the linker's evaluation order without re-running
    /// materialization. See docs/design.md "Lemma 2".
    pub linker_order: Vec<ModulePath>,
    /// Clause-2 violations (cross-destination rebinding writes) from
    /// the realizability verdict, rendered for diagnostics. A
    /// rebinding write and the binding it reassigns belong to one
    /// atomic factor unit, so a spec splitting them surfaces as an
    /// `atomic_unit_conflicts` entry first — on the accept path
    /// (`cycles` and `atomic_unit_conflicts` both empty) this list
    /// must be empty too, and the materializer bails if that
    /// invariant ever breaks (`validate_and_emit_reports`).
    pub cross_rebinds: Vec<String>,
}

/// Validator's rendered projection of one unrealizable SCC. The shared
/// "modules in the SCC + edges in the SCC" core is
/// [`analysis::reports::SccCore`]; the in-memory primitive that carries
/// it is [`crate::realizability::SccDiagnosis`]. This shape stringifies
/// the module names and decorates the diagnosis with the `cut` /
/// `lazy_closure` rows the bail-message renderer consumes.
#[derive(Debug, Clone, Serialize)]
pub struct CycleReport {
    pub modules: Vec<ModulePath>,
    /// Spec-author-actionable cut: realizability-constraining
    /// (`at-init` or `side-effect`) reasons whose removal (by
    /// co-locating each binding pair into one module) lifts the
    /// SCC's realizability violation. Derived from the verdict's
    /// owner-edge provenance per [`SccRejection`] kind:
    ///
    /// - [`SccRejection::MutualConstrainingCycle`]: a near-minimum
    ///   feedback arc set over the SCC's constraining edges,
    ///   computed by [`compute_realizability_cut`].
    /// - [`SccRejection::EsmEvaluationTdz`]: exactly the
    ///   constraining edges whose simulated ESM post-order check
    ///   failed — the at-init reads that TDZ at runtime.
    ///
    /// The cut never includes `lazy` reasons — lazy edges don't
    /// constrain ESM evaluation order, so removing one cannot help
    /// fix a cycle. Non-empty for every blocking SCC.
    pub cut: Vec<CycleEdge>,
    /// Populated only for [`SccRejection::EsmEvaluationTdz`]
    /// rejections: the lazy cross-module read edges between SCC
    /// members. For this rejection class the constraining subgraph
    /// alone is acyclic — these are the back-edges that close the
    /// I-cycle the ESM evaluation simulator walked, listed so the
    /// bail summary can show how the cycle forms even though no
    /// lazy edge is ever part of the cut.
    pub lazy_closure: Vec<CycleEdge>,
}

/// Trimmed wire shape for `cycles.json` (one entry per blocking SCC).
///
/// The full list of constraining cross-module edges inside the SCC
/// is recomputable from `owner_graph.json` + this entry's `modules`
/// set, so the wire entry carries only the modules and the cut (a
/// 1335-module SCC's full evidence is multi-MB; the trimmed entry is
/// ~100 KB). Consumers that need per-edge evidence re-derive it via
/// `debundle gate describe <id>` (see `devinfra/js/debundle/docs/cli.md`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlockingSccEntry {
    /// Position of the entry in `cycles.json`. Stable per build —
    /// the CLI `gate describe`/`cut` commands resolve `<id>` against
    /// this index.
    pub id: usize,
    /// Every module in the unrealizable SCC, by canonical
    /// [`ModulePath`] — the same value as the owner-graph module
    /// table's `path`, so consumers join `cycles.json` against
    /// `owner_graph.json` by resolving destination keys through the
    /// table (see `docs/wire_format.md` §"one canonical module
    /// identity").
    pub modules: Vec<ModulePath>,
    /// The actionable subset for spec authors: removing any of these
    /// edges (by co-locating the binding pair into one module) works
    /// toward breaking the SCC. See [`CycleReport::cut`] for the
    /// per-rejection-kind derivation.
    pub cut: Vec<CycleEdge>,
}

impl BlockingSccEntry {
    /// Project a rich [`CycleReport`] onto the trimmed wire shape.
    /// `id` is the entry's index in the `cycles.json` array.
    pub fn from_cycle_report(id: usize, cycle: &CycleReport) -> Self {
        Self {
            id,
            modules: cycle.modules.clone(),
            cut: cycle.cut.clone(),
        }
    }

    /// Build the on-disk `cycles.json` payload (`Vec<BlockingSccEntry>`)
    /// from the materializer's in-memory cycle reports.
    pub fn from_cycle_reports(cycles: &[CycleReport]) -> Vec<Self> {
        cycles
            .iter()
            .enumerate()
            .map(|(idx, cycle)| Self::from_cycle_report(idx, cycle))
            .collect()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CycleEdge {
    /// Source module, by canonical [`ModulePath`].
    pub from: ModulePath,
    /// Target module, by canonical [`ModulePath`].
    pub to: ModulePath,
    pub statement_ordinal: StatementOrdinal,
    /// Target binding being read (declared in `to`'s module). `None`
    /// for sequenced edges (no symbol).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<Atom>,
    /// Source-side binding (the first declared binding of the owner
    /// that issued the read). `None` when the source owner is an
    /// anonymous statement with no declared bindings — diagnostics
    /// fall back to `<anon stmt #{statement_ordinal}>` in that case.
    /// Populated by [`validate_factorization`] from the owner graph,
    /// so cycle reports name *both* ends of each cut edge by binding,
    /// not only the module pair.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from_binding: Option<Atom>,
    /// Edge kind. Lets
    /// downstream consumers (cycle-evidence visualizers, spec
    /// authors triaging which edges to break) tell at a glance
    /// which reasons are actually realizability-constraining
    /// (`eager_use` and `sequenced`) vs.
    /// inert-but-graph-present (`lazy_use`).
    pub kind: DepKind,
}

/// Render the per-cycle summary used in the materializer's bail
/// message. Each cycle is a spec-induced module-quotient SCC carrying
/// realizability-constraining (R/S) edges; the renderer blames
/// **binding pairs**, not just module pairs, so spec authors can act
/// directly: "move `X` (mod_A) and `Y` (mod_B) into one module."
///
/// Full per-cycle cut still goes to
/// `reports/tree/<chunk_id>/cycles.json`. This summary keeps the
/// inline bail message terse: one header line per SCC plus the top-K
/// binding-pair blame rows.
///
/// Each binding-pair row groups cut entries by
/// `(from_binding_label, from_module, to_module, to_binding_label, kind)`
/// and counts the underlying R/S reasons. Anonymous source/target
/// bindings render as `<anon stmt #ord>` so every row has a stable
/// human-readable label even when the source statement declares no
/// binding (top-level `console.log`, side-effecting expressions).
///
/// For ESM-evaluation-simulator (Pass-2 / TDZ) rejections, the cut
/// rows are followed by the lazy back-edges that close the I-cycle
/// (`CycleReport::lazy_closure`), in the same binding-pair format.
///
/// The text retains the words "unrealizable" and "cycle" so existing
/// rejection-keyword tests (`expect_rejection` in the e2e harness)
/// keep working — the format change is additive: more actionable
/// blame, same trigger keywords.
pub fn render_cycle_summary(cycles: &[CycleReport]) -> String {
    let mut out = String::new();
    const TOP_K: usize = 10;
    for (i, cycle) in cycles.iter().enumerate() {
        out.push_str(&format!(
            "Cycle #{i}: {} module(s) in unrealizable SCC, {} R/S edge(s) across {} (from-module, to-module) pair(s).\n",
            cycle.modules.len(),
            cycle.cut.len(),
            cut_pairs_count(&cycle.cut),
        ));

        // Group cut edges by binding-pair blame key. Each group is
        // one (reader, target) atom split the spec author can fix.
        let mut groups: HashMap<BindingPairKey<'_>, BindingPairAgg> = HashMap::new();
        for edge in &cycle.cut {
            let key = BindingPairKey::of(edge);
            let agg = groups.entry(key).or_insert_with(|| BindingPairAgg {
                kind: edge.kind,
                count: 0,
            });
            agg.count += 1;
        }
        let mut ranked: Vec<(BindingPairKey<'_>, BindingPairAgg)> = groups.into_iter().collect();
        ranked.sort_by(|a, b| b.1.count.cmp(&a.1.count).then(a.0.cmp(&b.0)));

        // Every blocking SCC carries a non-empty cut by construction:
        // Pass-1 runs FAS over a strongly connected constraining
        // subgraph; Pass-2 reports only SCCs with a non-empty
        // simulator-violated TDZ set.
        debug_assert!(
            !ranked.is_empty(),
            "blocking SCC #{i} rendered with an empty cut: {cycle:?}",
        );

        out.push_str(
            "  Binding pairs forcing the cycle (count: source binding (module) → kind → target binding (module)):\n",
        );
        for (key, agg) in ranked.iter().take(TOP_K) {
            out.push_str(&format!(
                "    {n:>4}x  {from_b} ({from_m})  --{kind}-->  {to_b} ({to_m})\n",
                n = agg.count,
                from_b = key.from_label,
                from_m = key.from,
                kind = dep_kind_short(agg.kind),
                to_b = key.to_label,
                to_m = key.to,
            ));
        }
        if ranked.len() > TOP_K {
            out.push_str(&format!(
                "    … +{} more binding pair(s)\n",
                ranked.len() - TOP_K,
            ));
        }

        if !cycle.lazy_closure.is_empty() {
            out.push_str(
                "  Constraining subgraph alone is acyclic; the I-cycle closes through lazy read(s) below, and the ESM evaluation simulator proved the at-init pair(s) above evaluate under TDZ (docs/design.md \"Lemma 2\"):\n",
            );
            let mut lazy_groups: HashMap<BindingPairKey<'_>, usize> = HashMap::new();
            for edge in &cycle.lazy_closure {
                *lazy_groups.entry(BindingPairKey::of(edge)).or_insert(0) += 1;
            }
            let mut lazy_ranked: Vec<(BindingPairKey<'_>, usize)> =
                lazy_groups.into_iter().collect();
            lazy_ranked.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
            for (key, count) in lazy_ranked.iter().take(TOP_K) {
                out.push_str(&format!(
                    "    {n:>4}x  {from_b} ({from_m})  --lazy-->  {to_b} ({to_m})\n",
                    n = count,
                    from_b = key.from_label,
                    from_m = key.from,
                    to_b = key.to_label,
                    to_m = key.to,
                ));
            }
            if lazy_ranked.len() > TOP_K {
                out.push_str(&format!(
                    "    … +{} more lazy binding pair(s)\n",
                    lazy_ranked.len() - TOP_K,
                ));
            }
        }

        out.push_str(
            "  Fix: co-locate each (source, target) binding pair above into one logical module, or break the SCC's back-edges in the spec.\n",
        );
    }
    out
}

#[derive(Debug, Clone, Eq, PartialEq, Hash, Ord, PartialOrd)]
struct BindingPairKey<'a> {
    from_label: std::borrow::Cow<'a, str>,
    from: &'a str,
    to: &'a str,
    to_label: std::borrow::Cow<'a, str>,
}

impl<'a> BindingPairKey<'a> {
    fn of(edge: &'a CycleEdge) -> Self {
        let from_label = match &edge.from_binding {
            Some(atom) => std::borrow::Cow::Borrowed(atom.as_ref()),
            None => std::borrow::Cow::Owned(format!("<anon stmt #{}>", edge.statement_ordinal.0)),
        };
        let to_label = match &edge.binding {
            Some(atom) => std::borrow::Cow::Borrowed(atom.as_ref()),
            None => std::borrow::Cow::Borrowed("<side-effect>"),
        };
        BindingPairKey {
            from_label,
            from: edge.from.as_str(),
            to: edge.to.as_str(),
            to_label,
        }
    }
}

#[derive(Debug)]
struct BindingPairAgg {
    kind: DepKind,
    count: usize,
}

fn dep_kind_short(kind: DepKind) -> &'static str {
    match kind {
        DepKind::EagerUse => "at-init",
        DepKind::LazyUse => "lazy",
        DepKind::EagerRebind => "at-init rebind",
        DepKind::LazyRebind => "lazy rebind",
        DepKind::DeferredRebind => "deferred rebind",
        DepKind::Sequenced => "side-effect",
        DepKind::LocalEffect => "local-effect",
    }
}

fn cut_pairs_count(cut: &[CycleEdge]) -> usize {
    let mut seen: HashSet<(&str, &str)> = HashSet::new();
    for edge in cut {
        seen.insert((edge.from.as_str(), edge.to.as_str()));
    }
    seen.len()
}

/// Project the realizability verdict onto the validator's rendered
/// report: one [`CycleReport`] per [`crate::realizability::SccDiagnosis`].
///
/// The verdict is the single source of truth for *which* SCCs block
/// materialization (docs/design.md "Realizability primitive" —
/// "Invariant: no bespoke parallel walks"); this function only
/// decorates each diagnosis's owner-edge provenance into the
/// stringified, binding-pair-labelled rows spec authors read. In
/// particular, an asymmetric I-cycle that Lemma 2's source-import
/// reversal rescues never appears here, even when another SCC in the
/// same chunk is genuinely unrealizable.
pub fn validate_factorization(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> FactorizationReport {
    let verdict = check_realizability(owner_graph, partition);
    let cross_rebinds = render_cross_rebinds(owner_graph, &verdict, module_path);
    if verdict.unrealizable_sccs.is_empty() {
        return FactorizationReport {
            cycles: Vec::new(),
            atomic_unit_conflicts: Vec::new(),
            linker_order: Vec::new(),
            cross_rebinds,
        };
    }
    // statement_ordinal → first declared binding of the owner that
    // owns that statement. Used to label the source side of every
    // CycleEdge so diagnostics blame binding pairs, not just module
    // pairs. An owner may have no declared bindings (anonymous
    // statement); we leave `from_binding = None` in that case and let
    // the renderer fall back to a statement-ordinal placeholder.
    let from_binding_by_ordinal: HashMap<StatementOrdinal, Atom> = owner_graph
        .iter_nodes()
        .filter_map(|node| {
            node.declared
                .iter()
                .next()
                .map(|id| (node.statement_ordinal, id.0.clone()))
        })
        .collect();
    let cycles = verdict
        .unrealizable_sccs
        .iter()
        .map(|diagnosis| {
            let (cut, lazy_closure) = match diagnosis.rejection {
                SccRejection::MutualConstrainingCycle => (
                    compute_realizability_cut(
                        owner_graph,
                        partition,
                        &diagnosis.core.constraining_owner_edges,
                        module_path,
                        &from_binding_by_ordinal,
                    ),
                    Vec::new(),
                ),
                // The diagnosis's owner edges ARE the cut: the
                // simulator surfaced exactly the constraining
                // `(from, to)` pairs whose post-order check failed.
                SccRejection::EsmEvaluationTdz => (
                    cycle_edges_for(
                        owner_graph,
                        partition,
                        diagnosis.core.constraining_owner_edges.iter().copied(),
                        module_path,
                        &from_binding_by_ordinal,
                    ),
                    lazy_closure_edges(
                        owner_graph,
                        partition,
                        &diagnosis.core.modules,
                        module_path,
                        &from_binding_by_ordinal,
                    ),
                ),
            };
            CycleReport {
                modules: diagnosis
                    .core
                    .modules
                    .iter()
                    .copied()
                    .map(module_path)
                    .collect(),
                cut,
                lazy_closure,
            }
        })
        .collect();
    FactorizationReport {
        cycles,
        atomic_unit_conflicts: Vec::new(),
        linker_order: Vec::new(),
        cross_rebinds,
    }
}

/// Render the verdict's clause-2 cross-rebind diagnoses as
/// human-readable lines for [`FactorizationReport::cross_rebinds`].
fn render_cross_rebinds(
    owner_graph: &OwnerGraph,
    verdict: &RealizabilityVerdict,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> Vec<String> {
    verdict
        .cross_rebinds
        .iter()
        .map(|rebind| {
            let edge = owner_graph.edge(rebind.owner_edge);
            let binding = match &edge.reason.binding {
                Some(id) => id.0.to_string(),
                None => format!("<anon stmt #{}>", edge.reason.statement_ordinal.0),
            };
            format!(
                "{} --{}--> {} (binding `{}`)",
                module_path(rebind.from),
                dep_kind_short(edge.reason.kind),
                module_path(rebind.to),
                binding,
            )
        })
        .collect()
}

/// Project verdict owner-edge provenance onto rendered [`CycleEdge`]
/// rows, sorted deterministically
/// `(from, to, statement_ordinal, binding, kind)` so test snapshots
/// compare cleanly. Endpoints use the gate view
/// ([`EndpointView::Gate`]) — the same projection the realizability
/// primitive used to produce the edges, so promoted at-init edges
/// keep their cross-module endpoints.
fn cycle_edges_for(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    edge_ids: impl IntoIterator<Item = OwnerEdgeId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<StatementOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let mut out: Vec<CycleEdge> = edge_ids
        .into_iter()
        .map(|edge_id| {
            let edge = owner_graph.edge(edge_id);
            let (from, to) = partition_endpoints(edge, partition, EndpointView::Gate)
                .expect("verdict owner-edge provenance is cross-module in the gate view");
            CycleEdge {
                from: module_path(from),
                to: module_path(to),
                statement_ordinal: edge.reason.statement_ordinal,
                binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
                from_binding: from_binding_by_ordinal
                    .get(&edge.reason.statement_ordinal)
                    .cloned(),
                kind: edge.reason.kind,
            }
        })
        .collect();
    out.sort_by(|a, b| {
        (
            a.from.as_str(),
            a.to.as_str(),
            a.statement_ordinal,
            &a.binding,
            a.kind,
        )
            .cmp(&(
                b.from.as_str(),
                b.to.as_str(),
                b.statement_ordinal,
                &b.binding,
                b.kind,
            ))
    });
    out
}

/// Lazy cross-module read edges between members of `modules`, gate
/// view. For a [`SccRejection::EsmEvaluationTdz`] rejection the
/// constraining subgraph alone is acyclic — these are the back-edges
/// closing the I-cycle the ESM evaluation simulator walked.
fn lazy_closure_edges(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    modules: &BTreeSet<ModuleId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<StatementOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let lazy_edge_ids = owner_graph.iter_edges().filter_map(|edge| {
        if edge.reason.kind != DepKind::LazyUse
            || owner_graph.node(edge.from).is_none()
            || owner_graph.node(edge.to).is_none()
        {
            return None;
        }
        let (from, to) = partition_endpoints(edge, partition, EndpointView::Gate)?;
        (modules.contains(&from) && modules.contains(&to)).then_some(edge.id)
    });
    cycle_edges_for(
        owner_graph,
        partition,
        lazy_edge_ids,
        module_path,
        from_binding_by_ordinal,
    )
}

/// Render a human-readable summary of atomic-unit conflicts for
/// inclusion in the materializer's bail message. One block per
/// conflict listing the unit's members and each member's claim. The
/// `module_path` callback resolves [`ModuleId`]s to the canonical
/// [`ModulePath`]s the spec author recognizes.
pub fn render_atomic_unit_conflict_summary(
    conflicts: &[AtomicUnitConflict],
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> String {
    let mut out = String::new();
    for (idx, c) in conflicts.iter().enumerate() {
        if idx > 0 {
            out.push('\n');
        }
        let mut member_ids: Vec<String> = c
            .members
            .iter()
            .copied()
            .map(analysis::reports::owner_key)
            .collect();
        member_ids.sort();
        let mut conflicting_modules: Vec<String> = c
            .claims
            .iter()
            .map(|claim| module_path(claim.module).to_string())
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        conflicting_modules.sort();
        out.push_str(&format!(
            "  atomic unit {{{}}} — claims across {{{}}}:\n",
            member_ids.join(", "),
            conflicting_modules.join(", "),
        ));
        for claim in &c.claims {
            let names = if claim.binding_names.is_empty() {
                String::new()
            } else {
                format!(
                    " ({})",
                    claim
                        .binding_names
                        .iter()
                        .map(|atom| atom.as_ref())
                        .collect::<Vec<_>>()
                        .join(",")
                )
            };
            out.push_str(&format!(
                "    - {}{} → {}\n",
                analysis::reports::owner_key(claim.owner),
                names,
                module_path(claim.module),
            ));
        }
    }
    out
}

/// Compute a near-minimum cut of realizability-constraining edges
/// whose removal makes a [`SccRejection::MutualConstrainingCycle`]
/// SCC realizable.
///
/// Input is the diagnosis's owner-edge provenance — every
/// constraining cross-module owner edge inside the SCC, in the gate
/// view. The algorithm groups the owner edges by their gate-view
/// module pair, builds a `DiGraph` with one edge per pair (strongly
/// connected by construction: the diagnosis's modules are one SCC of
/// the constraining subgraph), and runs
/// `petgraph::algo::greedy_feedback_arc_set` (Eades-Lin-Smyth, 1993,
/// `O(V + E)`). Every FAS-picked pair contributes its owner edges to
/// the cut.
///
/// Soundness: removing every FAS pair makes the SCC's constraining
/// subgraph acyclic, so the surviving cross-module edges have a
/// valid evaluation order — realizable per the docs/design.md
/// realizability theorem. Heuristic-minimum: petgraph's FAS
/// approximates within a constant factor on dense instances.
fn compute_realizability_cut(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    constraining_owner_edges: &[OwnerEdgeId],
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<StatementOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let mut by_pair: BTreeMap<(ModuleId, ModuleId), Vec<OwnerEdgeId>> = BTreeMap::new();
    for &edge_id in constraining_owner_edges {
        let edge = owner_graph.edge(edge_id);
        let (from, to) = partition_endpoints(edge, partition, EndpointView::Gate)
            .expect("verdict owner-edge provenance is cross-module in the gate view");
        by_pair.entry((from, to)).or_default().push(edge_id);
    }

    // Module-pair graph (index-based node ids required by
    // `greedy_feedback_arc_set`). Edge weight carries the
    // `(from, to)` pair so FAS picks map back to `by_pair`.
    let mut pair_graph: DiGraph<ModuleId, (ModuleId, ModuleId)> = DiGraph::new();
    let mut idx_of: HashMap<ModuleId, _> = HashMap::new();
    for &(from, to) in by_pair.keys() {
        for module in [from, to] {
            idx_of
                .entry(module)
                .or_insert_with(|| pair_graph.add_node(module));
        }
    }
    for &(from, to) in by_pair.keys() {
        pair_graph.add_edge(idx_of[&from], idx_of[&to], (from, to));
    }

    let mut cut_edge_ids: Vec<OwnerEdgeId> = Vec::new();
    for fas_edge in greedy_feedback_arc_set(&pair_graph) {
        cut_edge_ids.extend_from_slice(&by_pair[fas_edge.weight()]);
    }
    cycle_edges_for(
        owner_graph,
        partition,
        cut_edge_ids,
        module_path,
        from_binding_by_ordinal,
    )
}
