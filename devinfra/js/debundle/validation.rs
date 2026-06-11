use std::collections::{HashMap, HashSet};

use petgraph::algo::{condensation, greedy_feedback_arc_set};
use petgraph::graph::DiGraph;
use serde::{Deserialize, Serialize};

use crate::factor_assembly::AtomicUnitConflict;
use crate::graph::build_module_quotient;
use crate::partition::Partition;
use crate::realizability::{RealizabilityVerdict, check_realizability_with_quotient};
use swc_atoms::Atom;

use crate::{DepKind, ModuleId, ModuleQuotient, OwnerGraph, StatementOrdinal};

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
    pub linker_order: Vec<String>,
}

/// Validator's rendered projection of one unrealizable SCC. The
/// in-memory primitive is [`crate::realizability::SccDiagnosis`]
/// (typed `ModuleId`s + `OwnerEdgeId` evidence); this shape adds
/// stringified module names plus the `evidence` and FAS `cut`
/// decorations the bail-message renderer consumes.
#[derive(Debug, Clone, Serialize)]
pub struct CycleReport {
    pub modules: Vec<String>,
    pub evidence: Vec<CycleEdge>,
    /// Spec-author-actionable cut: a near-minimum set of
    /// realizability-constraining (`at-init` or `side-effect`)
    /// reasons whose removal would lift the cycle's realizability
    /// violation. Computed by [`compute_realizability_cut`].
    ///
    /// The cut never includes `lazy` reasons — lazy edges don't
    /// constrain ESM evaluation order, so removing one cannot help
    /// fix a cycle. Each entry corresponds to (and shares its
    /// shape with) a row in `evidence`.
    ///
    /// The algorithm builds a `DiGraph` containing only the
    /// constraining (`R`/`S`) cross-module edges induced by `scc`,
    /// then runs `petgraph::algo::condensation` once to find the
    /// constraining-subgraph SCCs. Each non-trivial SCC is fed to
    /// `petgraph::algo::greedy_feedback_arc_set` (Eades-Lin-Smyth,
    /// 1993, `O(V + E)`); the returned FAS edges name the cut
    /// (one cut entry per constraining reason on each picked edge).
    /// Sound (removing every FAS edge breaks every cycle in the
    /// constraining subgraph) and heuristic-minimum (petgraph's
    /// FAS approximates within a constant factor on dense
    /// instances).
    pub cut: Vec<CycleEdge>,
}

/// Trimmed wire shape for `cycles.json` (one entry per blocking SCC).
///
/// The materializer's in-memory `CycleReport` also carries an
/// `evidence` field — the full list of constraining cross-module
/// edges inside the SCC, keyed by ordinal — but that list is
/// recomputable from `owner_graph.json` + this entry's `modules`
/// set, so it's stripped before serializing to keep the on-disk
/// shape small (a 1335-module SCC's evidence is multi-MB; the
/// trimmed entry is ~100 KB).
///
/// Consumers that need the per-edge evidence re-derive it via
/// `debundle gate describe <id>` (see `devinfra/js/debundle/docs/cli.md`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BlockingSccEntry {
    /// Position of the entry in `cycles.json`. Stable per build —
    /// the CLI `gate describe`/`cut` commands resolve `<id>` against
    /// this index.
    pub id: usize,
    /// Every module in the unrealizable SCC.
    pub modules: Vec<String>,
    /// Near-minimum feedback-arc-set over the SCC's constraining
    /// (`at-init` / `side-effect`) edges. The actionable subset for
    /// spec authors: removing any of these edges (by co-locating
    /// the binding pair into one module) would break the SCC.
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
    pub from: String,
    pub to: String,
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
/// Full per-cycle evidence + cut still goes to
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

        if ranked.is_empty() {
            // Defensive — the SCC is rejected because at least one
            // R/S edge exists, so the cut should be non-empty.
            continue;
        }

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

/// Find SCCs in the dep graph and produce a report listing every
/// non-trivial cycle (size > 1 OR a self-loop). Trivial single-node
/// non-self-loop SCCs are dropped.
///
/// [`crate::realizability::check_realizability`] gates this whole function
/// (early return when its verdict is empty). The historical asymmetry between
/// a "strict" validator and a "relaxed" primitive is gone: the primitive's
/// tightened clause-3 rule rejects both the symmetric-constraining-cycle case
/// (any multi-module SCC in the constraining subgraph) and the
/// residual-in-cycle case (any multi-module SCC in `I` containing
/// residual with a constraining edge whose target is residual), and
/// `lower_chunk` realizes Lemma 2's source-import-order steering for
/// every spec that makes it past the gate. See docs/design.md "Lemma 2:
/// entry-side import ordering" for the order-steering algorithm and
/// the residual-in-cycle carve-out.
///
/// The cycle reports this function emits are advisory evidence for
/// spec authors — when the primitive rejects, this function walks
/// the quotient and prints which modules + edges are involved so
/// the author can pick a colocation or move that breaks the cycle.
pub fn validate_factorization(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    module_name: &dyn Fn(ModuleId) -> String,
) -> FactorizationReport {
    let quotient = build_module_quotient(owner_graph, partition);
    validate_factorization_with_quotient(owner_graph, partition, &quotient, module_name)
}

/// Same as [`validate_factorization`] but takes a pre-built
/// `ModuleQuotient` so callers that already constructed one (notably
/// `ChunkFactorization::build_with`, which caches it as
/// `self.dep_graph`) don't pay for a second
/// `build_module_quotient` + `tarjan_scc` pass on the way through
/// the realizability check.
pub fn validate_factorization_with_quotient(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    quotient: &ModuleQuotient,
    module_name: &dyn Fn(ModuleId) -> String,
) -> FactorizationReport {
    let verdict: RealizabilityVerdict =
        check_realizability_with_quotient(owner_graph, partition, quotient);
    if verdict.unrealizable_sccs.is_empty() {
        return FactorizationReport {
            cycles: Vec::new(),
            atomic_unit_conflicts: Vec::new(),
            linker_order: Vec::new(),
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
    let graph = quotient;
    let mut cycles = Vec::new();
    for scc in verdict.scc_partition() {
        let in_scc: HashSet<ModuleId> = scc.iter().copied().collect();
        let is_cycle = scc.len() > 1 || (scc.len() == 1 && graph.contains_edge(scc[0], scc[0]));
        if !is_cycle {
            continue;
        }
        // Realizability filter (per docs/design.md "The realizability
        // theorem"): an `I ∪ S` SCC is unrealizable iff at least
        // one cross-module edge between its members carries a
        // realizability-constraining reason — an at-init read
        // (`R`) or a side-effect ordering edge (`S`). Lazy reads
        // alone don't constrain it: the ESM linker evaluates the
        // SCC in *some* order, and the lazy reads only fire
        // afterwards (no TDZ, no missed side-effect ordering).
        let scc_constrains_evaluation_order = scc.iter().any(|&from| {
            scc.iter()
                .any(|&to| from != to && graph.has_init_order_constraining_edge(from, to))
        });
        if !scc_constrains_evaluation_order {
            continue;
        }
        let mut evidence = Vec::new();
        for (from, to, weight) in graph.all_edges() {
            if !in_scc.contains(&from) || !in_scc.contains(&to) {
                continue;
            }
            for reason in &weight.reasons {
                evidence.push(CycleEdge {
                    from: module_name(from),
                    to: module_name(to),
                    statement_ordinal: reason.statement_ordinal,
                    binding: reason.binding.as_ref().map(|id| id.0.clone()),
                    from_binding: from_binding_by_ordinal
                        .get(&reason.statement_ordinal)
                        .cloned(),
                    kind: reason.kind,
                });
            }
        }
        let cut = compute_realizability_cut(graph, scc, module_name, &from_binding_by_ordinal);
        cycles.push(CycleReport {
            modules: scc.iter().copied().map(module_name).collect(),
            evidence,
            cut,
        });
    }
    FactorizationReport {
        cycles,
        atomic_unit_conflicts: Vec::new(),
        linker_order: Vec::new(),
    }
}

/// Render a human-readable summary of atomic-unit conflicts for
/// inclusion in the materializer's bail message. One block per
/// conflict listing the unit's members and each member's claim. The
/// `module_name` callback renders [`ModuleId`]s as the strings the
/// spec author recognizes (e.g. `mod_0`, `<residual_entry>`).
pub fn render_atomic_unit_conflict_summary(
    conflicts: &[AtomicUnitConflict],
    module_name: &dyn Fn(ModuleId) -> String,
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
            .map(crate::reports::owner_key)
            .collect();
        member_ids.sort();
        let mut conflicting_modules: Vec<String> = c
            .claims
            .iter()
            .map(|claim| module_name(claim.module))
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
                crate::reports::owner_key(claim.owner),
                names,
                module_name(claim.module),
            ));
        }
    }
    out
}

/// Compute a near-minimum cut of realizability-constraining edges
/// inside `scc` whose removal makes the SCC realizable.
///
/// One-shot algorithm:
/// 1. Build a `DiGraph<ModuleId, ()>` containing only the
///    realizability-constraining (`R`/`S`) cross-module edges
///    between members of `scc`. `LazyUse`-only edges are dropped
///    up front — they don't constrain ESM evaluation order, so no
///    cut entry can be a lazy edge by construction. This replaces
///    the old loop's post-FAS "fallback scan for an R/S edge"
///    hack: FAS now sees only edges that could legitimately be in
///    the cut.
/// 2. Run `petgraph::algo::condensation` once to partition the
///    constraining subgraph into SCCs.
/// 3. For each non-trivial condensation node (a real SCC of the
///    constraining subgraph), induce its subgraph and run
///    `petgraph::algo::greedy_feedback_arc_set` (Eades-Lin-Smyth,
///    1993, `O(V + E)`). Every FAS edge contributes its R/S
///    reasons to the cut.
///
/// Soundness: removing every FAS edge makes the constraining
/// subgraph of `scc` acyclic, so the surviving cross-module edges
/// have a valid evaluation order — realizable per the docs/design.md
/// realizability theorem. Cuts are sorted deterministically
/// `(from, to, statement_ordinal, binding, kind)` so test
/// snapshots compare cleanly.
fn compute_realizability_cut(
    graph: &ModuleQuotient,
    scc: &[ModuleId],
    module_name: &dyn Fn(ModuleId) -> String,
    from_binding_by_ordinal: &HashMap<StatementOrdinal, Atom>,
) -> Vec<CycleEdge> {
    if scc.len() < 2 {
        return Vec::new();
    }
    // Constraining-only induced subgraph on `scc`, as a `DiGraph`
    // (index-based node ids required by `greedy_feedback_arc_set`).
    // Node weight carries the original `ModuleId`; edge weight
    // carries the `(from, to)` pair so we can recover the
    // underlying `EdgeMetadata` from `graph` after FAS.
    let in_scc: HashSet<ModuleId> = scc.iter().copied().collect();
    let mut constraining: DiGraph<ModuleId, (ModuleId, ModuleId)> = DiGraph::new();
    let mut idx_of: HashMap<ModuleId, _> = HashMap::with_capacity(scc.len());
    for &m in scc {
        idx_of.insert(m, constraining.add_node(m));
    }
    for (from, to, weight) in graph.all_edges() {
        if from == to || !in_scc.contains(&from) || !in_scc.contains(&to) {
            continue;
        }
        if !weight.constrains_init_order() {
            continue;
        }
        constraining.add_edge(idx_of[&from], idx_of[&to], (from, to));
    }

    // Condense once to the SCC DAG. `make_acyclic = true` drops
    // self-loops and parallel edges between condensation nodes;
    // we only need the membership lists (each condensation node's
    // `Vec<ModuleId>` weight) to find non-trivial SCCs.
    let condensed = condensation(constraining, true);

    let mut cut: Vec<CycleEdge> = Vec::new();
    for node in condensed.node_indices() {
        let members: &Vec<ModuleId> = &condensed[node];
        if members.len() < 2 {
            continue;
        }
        let in_s: HashSet<ModuleId> = members.iter().copied().collect();

        // Sub-SCC subgraph rebuilt from the original quotient,
        // restricted to constraining edges between members.
        let mut induced: DiGraph<ModuleId, (ModuleId, ModuleId)> = DiGraph::new();
        let mut sub_idx_of: HashMap<ModuleId, _> = HashMap::with_capacity(members.len());
        for &m in members {
            sub_idx_of.insert(m, induced.add_node(m));
        }
        for (from, to, weight) in graph.all_edges() {
            if from == to || !in_s.contains(&from) || !in_s.contains(&to) {
                continue;
            }
            if !weight.constrains_init_order() {
                continue;
            }
            induced.add_edge(sub_idx_of[&from], sub_idx_of[&to], (from, to));
        }

        for fas_edge in greedy_feedback_arc_set(&induced) {
            let (u, v) = *fas_edge.weight();
            let weight = graph
                .edge_weight(u, v)
                .expect("FAS edge endpoints came from the module quotient");
            for reason in &weight.reasons {
                if !reason.constrains_init_order() {
                    continue;
                }
                cut.push(CycleEdge {
                    from: module_name(u),
                    to: module_name(v),
                    statement_ordinal: reason.statement_ordinal,
                    binding: reason.binding.as_ref().map(|id| id.0.clone()),
                    from_binding: from_binding_by_ordinal
                        .get(&reason.statement_ordinal)
                        .cloned(),
                    kind: reason.kind,
                });
            }
        }
    }

    cut.sort_by(|a, b| {
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
    cut
}
