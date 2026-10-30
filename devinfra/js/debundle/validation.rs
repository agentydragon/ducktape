use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fmt;

use petgraph::algo::greedy_feedback_arc_set;
use petgraph::graph::DiGraph;
use serde::{Deserialize, Serialize};

use crate::realizability::{RealizabilityVerdict, SccRejection, check_realizability};
use analysis::assignment_assembly::AtomicGroupConflict;
use analysis::graph::{DependencyEdgeId, EndpointView, assignment_endpoints};
use analysis::module_assignment::ModuleAssignment;
use spec::ModulePath;
use swc_atoms::Atom;

use analysis::analysis_inputs::CommutingStatementsRef;
use analysis::{
    DepKind, DependencyEdge, DependencyGraph, EdgeRole, ModuleId, Purity, SequencedCause,
    SequencedSourceUnitCause,
};

/// Result of validating a module dep graph.
#[derive(Debug, Clone, Serialize)]
pub struct AssignmentReport {
    pub cycles: Vec<CycleReport>,
    /// Atomic factor groups the spec splits across destination
    /// modules — unrealizable by construction. Populated from
    /// `AssignmentAnalysis::assembly_conflicts`; the materializer rejects any
    /// spec with a non-empty list before emitting code.
    pub atomic_group_conflicts: Vec<AtomicGroupConflict>,
    /// Topological linearization of `I ∪ S` rooted at the entry,
    /// dependency-first. Empty when the dep graph has cycles
    /// (validation rejects). Captured here so debug tooling can
    /// see the linker's evaluation order without re-running
    /// materialization. See docs/design.md "Lemma 2".
    pub linker_order: Vec<ModulePath>,
    /// Clause-2 violations (cross-destination rebinding writes) from
    /// the realizability verdict, rendered for diagnostics. A
    /// rebinding write and the binding it reassigns belong to one
    /// atomic factor group, so a spec splitting them surfaces as an
    /// `atomic_group_conflicts` entry first — on the accept path
    /// (`cycles` and `atomic_group_conflicts` both empty) this list
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
    /// source-unit edge provenance per [`SccRejection`] kind:
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
    /// rejections: the nonconstraining cross-module reads (`LazyUse` and
    /// `HoistedUse`) between SCC
    /// members. For this rejection class the constraining subgraph
    /// alone is acyclic — these are the back-edges that close the
    /// I-cycle the ESM evaluation simulator walked, listed so the
    /// bail summary can show how the cycle forms even though no
    /// lazy edge is ever part of the cut.
    pub lazy_closure: Vec<CycleEdge>,
    /// `sequenced` edges between SCC members that an author
    /// `commuting_statements` premise made inert (each row names its
    /// premise). They never constrain and never appear in `cut`; listed so
    /// an author can see what the premise bought while the SCC still
    /// blocks.
    pub inert_commuting_edges: Vec<CycleEdge>,
    /// Spec location of each applied premise, so the bail summary can point
    /// at an inert edge's entry without the dependency graph.
    pub commuting_premise_locations: BTreeMap<CommutingStatementsRef, String>,
}

/// Trimmed wire shape for `cycles.json` (one entry per blocking SCC).
///
/// The full list of constraining cross-module edges inside the SCC
/// is recomputable from `dependency_graph.json` + this entry's `modules`
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
    /// [`ModulePath`] — the same value as the dependency graph module
    /// table's `path`, so consumers join `cycles.json` against
    /// `dependency_graph.json` by resolving destination keys through the
    /// table (see `docs/wire_format.md` §"one canonical module
    /// identity").
    pub modules: Vec<ModulePath>,
    /// The actionable subset for spec authors: removing any of these
    /// edges (by co-locating the binding pair into one module) works
    /// toward breaking the SCC. See [`CycleReport::cut`] for the
    /// per-rejection-kind derivation.
    pub cut: Vec<CycleEdge>,
    /// See [`CycleReport::inert_commuting_edges`].
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inert_commuting_edges: Vec<CycleEdge>,
}

impl BlockingSccEntry {
    /// Project a rich [`CycleReport`] onto the trimmed wire shape.
    /// `id` is the entry's index in the `cycles.json` array.
    pub fn from_cycle_report(id: usize, cycle: &CycleReport) -> Self {
        Self {
            id,
            modules: cycle.modules.clone(),
            cut: cycle.cut.clone(),
            inert_commuting_edges: cycle.inert_commuting_edges.clone(),
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
    pub statement_ordinal: SourceUnitOrdinal,
    /// Target binding being read (declared in `to`'s module). `None`
    /// for sequenced edges (no symbol).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<Atom>,
    /// Source-side binding (the first declared binding of the source_unit
    /// that issued the read). `None` when the source source_unit is an
    /// anonymous statement with no declared bindings — diagnostics
    /// fall back to `<anon stmt #{statement_ordinal}>` in that case.
    /// Populated by [`validate_assignment`] from the dependency graph,
    /// so cycle reports name *both* ends of each cut edge by binding,
    /// not only the module pair.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from_binding: Option<Atom>,
    /// Edge kind. Lets
    /// downstream consumers (cycle-evidence visualizers, spec
    /// authors triaging which edges to break) tell at a glance
    /// which reasons are actually realizability-constraining
    /// (`eager_use` and `sequenced`) vs.
    /// inert-but-graph-present (`lazy_use` and `hoisted_use`).
    pub kind: DepKind,
    /// Impure-initializer evidence of the source side of a `sequenced`
    /// edge (`None` when the source is classified pure).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sequenced_source_unit: Option<SequencedSourceUnitCause>,
    /// Why the gate treats this edge as constraining: the at-init read
    /// position for `eager_use`, or the retained emission cause and the
    /// target side's impure-initializer evidence for `sequenced`. `None`
    /// for the other kinds (their `kind` and `binding` already say why)
    /// and for `sequenced` edges of a graph written before the cause was
    /// retained.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub explanation: Option<CutEdgeExplanation>,
    /// The author `commuting_statements` entry that made this edge inert;
    /// `None` for every constraining edge (and so for every `cut` row).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub commuting_premise: Option<CommutingStatementsRef>,
}

/// Per-kind reasoning behind a constraining [`CycleEdge`].
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CutEdgeExplanation {
    /// `eager_use`: `binding` is read while the source statement
    /// evaluates, from `position`.
    AtInitRead { position: AtInitReadPosition },
    /// `sequenced`: the emission rule that produced the edge and how it
    /// classified both endpoints, plus the target side's impure
    /// initializer (`None` when the target is classified pure, e.g. an
    /// array allocation or a pure content reader).
    Sequenced {
        cause: SequencedCause,
        /// Boxed: the purity evidence dwarfs the other variant.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        target_source_unit: Option<Box<SequencedSourceUnitCause>>,
    },
}

/// Where an at-init read sits relative to the statement that issues it.
/// Derived from the edge's [`EdgeRole`].
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AtInitReadPosition {
    /// The statement's own eager position (`reads.eager`).
    Direct,
    /// Lifted from the first-order body of `callee_source_unit`, a
    /// chunk function the statement calls at init
    /// (`promote_at_init_calls`).
    PromotedAtInit {
        callee_source_unit: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        callee_binding: Option<Atom>,
    },
    /// Reached through the conservative read closure of an at-init call
    /// promotion cannot resolve (member call, IIFE, alias); the role
    /// names the caller itself as callee.
    UnresolvedAtInitCall,
}

impl fmt::Display for CutEdgeExplanation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AtInitRead { position } => match position {
                AtInitReadPosition::Direct => f.write_str("direct at-init read"),
                AtInitReadPosition::PromotedAtInit {
                    callee_source_unit,
                    callee_binding,
                } => {
                    let callee = callee_binding
                        .as_ref()
                        .map_or_else(|| callee_source_unit.clone(), |b| format!("`{b}`"));
                    write!(
                        f,
                        "at-init read promoted from the body of {callee}, called at init"
                    )
                }
                AtInitReadPosition::UnresolvedAtInitCall => f.write_str(
                    "at-init read in the conservative closure of an unresolved at-init call",
                ),
            },
            Self::Sequenced { cause, .. } => fmt::Display::fmt(cause, f),
        }
    }
}

/// Explain why `edge` constrains, from provenance retained on the
/// dependency graph. See [`CycleEdge::explanation`].
fn explain_edge(
    dependency_graph: &DependencyGraph,
    edge: &DependencyEdge,
) -> Option<CutEdgeExplanation> {
    match edge.reason.kind {
        DepKind::EagerUse => {
            let position = match edge.reason.role() {
                EdgeRole::Direct => AtInitReadPosition::Direct,
                EdgeRole::PromotedAtInit { callee_source_unit }
                    if callee_source_unit == edge.from =>
                {
                    AtInitReadPosition::UnresolvedAtInitCall
                }
                EdgeRole::PromotedAtInit { callee_source_unit } => {
                    AtInitReadPosition::PromotedAtInit {
                        callee_source_unit: analysis::reports::source_unit_key(callee_source_unit),
                        callee_binding: dependency_graph
                            .node(callee_source_unit)
                            .and_then(|node| node.declared.iter().next())
                            .map(|id| id.0.clone()),
                    }
                }
            };
            Some(CutEdgeExplanation::AtInitRead { position })
        }
        DepKind::Sequenced => Some(CutEdgeExplanation::Sequenced {
            cause: edge.reason.sequenced_cause()?.clone(),
            target_source_unit: analysis::reports::schema::impure_source_unit_cause(
                dependency_graph,
                edge.to,
            )
            .map(Box::new),
        }),
        _ => None,
    }
}

impl CycleEdge {
    /// Stable diagnostic ordering shared by stored cuts and reconstructed evidence.
    pub fn sort_for_report(edges: &mut [Self]) {
        edges.sort_by(|a, b| {
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
    }
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
        let mut groups: HashMap<BindingPairKey<'_>, Vec<&CycleEdge>> = HashMap::new();
        for edge in &cycle.cut {
            groups
                .entry(BindingPairKey::of(edge))
                .or_default()
                .push(edge);
        }
        let mut ranked: Vec<(BindingPairKey<'_>, Vec<&CycleEdge>)> = groups.into_iter().collect();
        ranked.sort_by(|a, b| b.1.len().cmp(&a.1.len()).then(a.0.cmp(&b.0)));

        // Every blocking SCC carries a non-empty cut by construction:
        // Pass-1 runs FAS over a strongly connected constraining
        // subgraph; Pass-2 reports only SCCs with a non-empty
        // simulator-violated TDZ set.
        debug_assert!(
            !ranked.is_empty(),
            "blocking SCC #{i} rendered with an empty cut: {cycle:?}",
        );

        out.push_str(
            "  Binding pairs forcing the cycle (count: source binding (module) → kind → target binding (module), then why each edge constrains):\n",
        );
        for (key, edges) in ranked.iter().take(TOP_K) {
            out.push_str(&format!(
                "    {n:>4}x  {from_b} ({from_m})  --{kind}-->  {to_b} ({to_m})\n",
                n = edges.len(),
                from_b = key.from_label,
                from_m = key.from,
                kind = edges[0].kind.diagnostic_label(),
                to_b = key.to_label,
                to_m = key.to,
            ));
            render_explanations(edges, &mut out);
        }
        if ranked.len() > TOP_K {
            out.push_str(&format!(
                "    … +{} more binding pair(s)\n",
                ranked.len() - TOP_K,
            ));
        }

        if !cycle.lazy_closure.is_empty() {
            out.push_str(
                "  Constraining subgraph alone is acyclic; the I-cycle closes through nonconstraining imports below, and the ESM evaluation simulator proved the at-init pair(s) above evaluate under TDZ (docs/design.md \"Lemma 2\"):\n",
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

        render_inert_commuting_edges(
            &cycle.inert_commuting_edges,
            &cycle.commuting_premise_locations,
            &mut out,
        );

        out.push_str(
            "  Fix: co-locate each (source, target) binding pair above into one logical module, or break the SCC's back-edges in the spec.\n",
        );
        let causes = sequenced_causes_from_cycle(cycle);
        render_sequenced_source_unit_causes(&causes, &mut out);
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

/// Distinct explanations behind one binding-pair row, indented under
/// it. Shared by the bail summary and `gate describe`'s evidence block.
pub fn render_explanations(edges: &[&CycleEdge], out: &mut String) {
    const WHY_K: usize = 3;
    let distinct: BTreeSet<String> = edges
        .iter()
        .filter_map(|edge| edge.explanation.as_ref())
        .map(ToString::to_string)
        .collect();
    for why in distinct.iter().take(WHY_K) {
        out.push_str(&format!("           - {why}\n"));
    }
    if distinct.len() > WHY_K {
        out.push_str(&format!(
            "           - … +{} more distinct reason(s)\n",
            distinct.len() - WHY_K,
        ));
    }
}

/// The `sequenced` edges between SCC members an author `commuting_statements`
/// premise made inert: a count line, then up to `INERT_K` rows naming both
/// endpoints, the premise (with its spec location where `premise_locations`
/// has it) and the retained cause. Nothing when there is no inert edge.
pub fn render_inert_commuting_edges(
    edges: &[CycleEdge],
    premise_locations: &BTreeMap<CommutingStatementsRef, String>,
    out: &mut String,
) {
    const INERT_K: usize = 3;
    if edges.is_empty() {
        return;
    }
    out.push_str(&format!(
        "  Inert: {} sequenced edge(s) between SCC members made inert by commuting_statements premise(s); never in the cut:\n",
        edges.len(),
    ));
    for edge in edges.iter().take(INERT_K) {
        let key = BindingPairKey::of(edge);
        out.push_str(&format!(
            "    {from_b} ({from_m})  --{kind}-->  {to_b} ({to_m})  [stmt #{ord}]\n",
            from_b = key.from_label,
            from_m = key.from,
            kind = edge.kind.diagnostic_label(),
            to_b = key.to_label,
            to_m = key.to,
            ord = edge.statement_ordinal.0,
        ));
        if let Some(premise) = &edge.commuting_premise {
            let location = premise_locations
                .get(premise)
                .map(|location| format!(" at {location}"))
                .unwrap_or_default();
            out.push_str(&format!("           inert: premise {premise}{location}\n"));
        }
        render_explanations(&[edge], out);
    }
    if edges.len() > INERT_K {
        out.push_str(&format!(
            "    … +{} more inert edge(s)\n",
            edges.len() - INERT_K,
        ));
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
/// decorates each diagnosis's source-unit edge provenance into the
/// stringified, binding-pair-labelled rows spec authors read. In
/// particular, an asymmetric I-cycle that Lemma 2's source-import
/// reversal rescues never appears here, even when another SCC in the
/// same chunk is genuinely unrealizable.
pub fn validate_assignment(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> AssignmentReport {
    let verdict = check_realizability(dependency_graph, assignment);
    let cross_rebinds = render_cross_rebinds(dependency_graph, &verdict, module_path);
    if verdict.unrealizable_sccs.is_empty() {
        return AssignmentReport {
            cycles: Vec::new(),
            atomic_group_conflicts: Vec::new(),
            linker_order: Vec::new(),
            cross_rebinds,
        };
    }
    // statement_ordinal → first declared binding of the source_unit that
    // owns that statement. Used to label the source side of every
    // CycleEdge so diagnostics blame binding pairs, not just module
    // pairs. An source_unit may have no declared bindings (anonymous
    // statement); we leave `from_binding = None` in that case and let
    // the renderer fall back to a statement-ordinal placeholder.
    let from_binding_by_ordinal: HashMap<SourceUnitOrdinal, Atom> = dependency_graph
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
                        dependency_graph,
                        assignment,
                        &diagnosis.core.constraining_dependency_edges,
                        module_path,
                        &from_binding_by_ordinal,
                    ),
                    Vec::new(),
                ),
                // The diagnosis's source-unit edges ARE the cut: the
                // simulator surfaced exactly the constraining
                // `(from, to)` pairs whose post-order check failed.
                SccRejection::EsmEvaluationTdz => (
                    cycle_edges_for(
                        dependency_graph,
                        assignment,
                        diagnosis.core.constraining_dependency_edges.iter().copied(),
                        module_path,
                        &from_binding_by_ordinal,
                    ),
                    nonconstraining_closure_edges(
                        dependency_graph,
                        assignment,
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
                inert_commuting_edges: inert_commuting_edges(
                    dependency_graph,
                    assignment,
                    &diagnosis.core.modules,
                    module_path,
                    &from_binding_by_ordinal,
                ),
                commuting_premise_locations: dependency_graph
                    .commuting_premises()
                    .iter()
                    .map(|premise| (premise.reference.clone(), premise.spec_location.clone()))
                    .collect(),
            }
        })
        .collect();
    AssignmentReport {
        cycles,
        atomic_group_conflicts: Vec::new(),
        linker_order: Vec::new(),
        cross_rebinds,
    }
}

/// Render the verdict's clause-2 cross-rebind diagnoses as
/// human-readable lines for [`AssignmentReport::cross_rebinds`].
fn render_cross_rebinds(
    dependency_graph: &DependencyGraph,
    verdict: &RealizabilityVerdict,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> Vec<String> {
    verdict
        .cross_rebinds
        .iter()
        .map(|rebind| {
            let edge = dependency_graph.edge(rebind.dependency_edge);
            let binding = match &edge.reason.binding {
                Some(id) => id.0.to_string(),
                None => format!("<anon stmt #{}>", edge.reason.statement_ordinal.0),
            };
            format!(
                "{} --{}--> {} (binding `{}`)",
                module_path(rebind.from),
                edge.reason.kind.diagnostic_label(),
                module_path(rebind.to),
                binding,
            )
        })
        .collect()
}

/// Project verdict source-unit edge provenance onto rendered [`CycleEdge`]
/// rows, sorted deterministically
/// `(from, to, statement_ordinal, binding, kind)` so test snapshots
/// compare cleanly. Endpoints use the gate view
/// ([`EndpointView::Gate`]) — the same projection the realizability
/// primitive used to produce the edges, so promoted at-init edges
/// keep their cross-module endpoints.
fn cycle_edges_for(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    edge_ids: impl IntoIterator<Item = DependencyEdgeId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let mut out: Vec<CycleEdge> = edge_ids
        .into_iter()
        .map(|edge_id| {
            let edge = dependency_graph.edge(edge_id);
            let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)
                .expect("verdict source-unit edge provenance is cross-module in the gate view");
            CycleEdge {
                from: module_path(from),
                to: module_path(to),
                statement_ordinal: edge.reason.statement_ordinal,
                binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
                from_binding: from_binding_by_ordinal
                    .get(&edge.reason.statement_ordinal)
                    .cloned(),
                kind: edge.reason.kind,
                sequenced_source_unit: analysis::reports::schema::sequenced_edge_cause(
                    dependency_graph,
                    edge,
                ),
                explanation: explain_edge(dependency_graph, edge),
                commuting_premise: edge.reason.commuting_premise().cloned(),
            }
        })
        .collect();
    CycleEdge::sort_for_report(&mut out);
    out
}

/// Cross-module edges between members of `modules` (gate view) that an
/// author `commuting_statements` premise made inert. See
/// [`CycleReport::inert_commuting_edges`].
fn inert_commuting_edges(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    modules: &BTreeSet<ModuleId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let inert_edge_ids = dependency_graph.iter_edges().filter_map(|edge| {
        edge.reason.commuting_premise()?;
        let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)?;
        (modules.contains(&from) && modules.contains(&to)).then_some(edge.id)
    });
    cycle_edges_for(
        dependency_graph,
        assignment,
        inert_edge_ids,
        module_path,
        from_binding_by_ordinal,
    )
}

/// Nonconstraining cross-module read edges between members of `modules`, gate
/// view. For a [`SccRejection::EsmEvaluationTdz`] rejection the
/// constraining subgraph alone is acyclic — these are the back-edges
/// closing the I-cycle the ESM evaluation simulator walked.
fn nonconstraining_closure_edges(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    modules: &BTreeSet<ModuleId>,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let lazy_edge_ids = dependency_graph.iter_edges().filter_map(|edge| {
        if !matches!(edge.reason.kind, DepKind::LazyUse | DepKind::HoistedUse)
            || dependency_graph.node(edge.from).is_none()
            || dependency_graph.node(edge.to).is_none()
        {
            return None;
        }
        let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)?;
        (modules.contains(&from) && modules.contains(&to)).then_some(edge.id)
    });
    cycle_edges_for(
        dependency_graph,
        assignment,
        lazy_edge_ids,
        module_path,
        from_binding_by_ordinal,
    )
}

/// Render a human-readable summary of atomic-group conflicts for
/// inclusion in the materializer's bail message. One block per
/// conflict listing the group's members and each member's claim. The
/// `module_path` callback resolves [`ModuleId`]s to the canonical
/// [`ModulePath`]s the spec author recognizes.
pub fn render_atomic_group_conflict_summary(
    conflicts: &[AtomicGroupConflict],
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    dependency_graph: &DependencyGraph,
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
            .map(analysis::reports::source_unit_key)
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
            "  atomic group {{{}}} — claims across {{{}}}:\n",
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
                analysis::reports::source_unit_key(claim.source_unit),
                names,
                module_path(claim.module),
            ));
        }
        let causes =
            analysis::reports::schema::sequenced_source_unit_causes(dependency_graph, &c.members);
        render_sequenced_source_unit_causes(&causes, &mut out);
    }
    out
}

fn sequenced_causes_from_cycle(cycle: &CycleReport) -> Vec<SequencedSourceUnitCause> {
    let mut causes = BTreeMap::new();
    for edge in &cycle.cut {
        let target = match &edge.explanation {
            Some(CutEdgeExplanation::Sequenced {
                target_source_unit, ..
            }) => target_source_unit.as_deref(),
            _ => None,
        };
        for cause in edge.sequenced_source_unit.iter().chain(target) {
            causes.insert(cause.source_unit_id.clone(), cause.clone());
        }
    }
    causes.into_values().collect()
}

pub(crate) fn render_sequenced_source_unit_causes(
    causes: &[SequencedSourceUnitCause],
    out: &mut String,
) {
    if causes.is_empty() {
        return;
    }
    out.push_str("  Impure initializers behind side-effect edges:\n");
    for cause in causes {
        let names = if cause.binding_names.is_empty() {
            cause.source_unit_id.clone()
        } else {
            cause
                .binding_names
                .iter()
                .map(Atom::as_ref)
                .collect::<Vec<_>>()
                .join(", ")
        };
        let Purity::NotPure { reasons } = &cause.purity else {
            continue;
        };
        for reason in reasons {
            let location = reason
                .source_location
                .as_ref()
                .or(cause.source_location.as_ref())
                .map(|loc| {
                    format!(
                        "{}:{}:{}",
                        loc.source_path,
                        loc.start_line,
                        loc.start_column
                            .map_or_else(|| "?".to_string(), |col| col.to_string()),
                    )
                })
                .unwrap_or_else(|| "<location unavailable>".to_string());
            let rule = reason.rule.as_str();
            let detail = reason
                .detail
                .as_deref()
                .map(|detail| format!(" ({detail})"))
                .unwrap_or_default();
            // A statement whose synchronous execution is incomplete
            // chains conservatively; say so next to its purity reason.
            let incomplete = match cause.synchronous_execution_complete {
                Some(false) => "; synchronous execution incomplete",
                _ => "",
            };
            out.push_str(&format!(
                "    `{names}` at {location}: {rule}{detail}{incomplete}\n"
            ));
            if let Some(guidance) = &reason.author_guidance {
                out.push_str(&format!("      {guidance}\n"));
            }
        }
    }
}

/// Compute a near-minimum cut of realizability-constraining edges
/// whose removal makes a [`SccRejection::MutualConstrainingCycle`]
/// SCC realizable.
///
/// Input is the diagnosis's source-unit edge provenance — every
/// constraining cross-module source_unit edge inside the SCC, in the gate
/// view. The algorithm groups the source-unit edges by their gate-view
/// module pair, builds a `DiGraph` with one edge per pair (strongly
/// connected by construction: the diagnosis's modules are one SCC of
/// the constraining subgraph), and runs
/// `petgraph::algo::greedy_feedback_arc_set` (Eades-Lin-Smyth, 1993,
/// `O(V + E)`). Every FAS-picked pair contributes its source-unit edges to
/// the cut.
///
/// Soundness: removing every FAS pair makes the SCC's constraining
/// subgraph acyclic, so the surviving cross-module edges have a
/// valid evaluation order — realizable per the docs/design.md
/// realizability theorem. Heuristic-minimum: petgraph's FAS
/// approximates within a constant factor on dense instances.
fn compute_realizability_cut(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    constraining_dependency_edges: &[DependencyEdgeId],
    module_path: &dyn Fn(ModuleId) -> ModulePath,
    from_binding_by_ordinal: &HashMap<SourceUnitOrdinal, Atom>,
) -> Vec<CycleEdge> {
    let mut by_pair: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> = BTreeMap::new();
    for &edge_id in constraining_dependency_edges {
        let edge = dependency_graph.edge(edge_id);
        let (from, to) = assignment_endpoints(edge, assignment, EndpointView::Gate)
            .expect("verdict source-unit edge provenance is cross-module in the gate view");
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

    let mut cut_edge_ids: Vec<DependencyEdgeId> = Vec::new();
    for fas_edge in greedy_feedback_arc_set(&pair_graph) {
        cut_edge_ids.extend_from_slice(&by_pair[fas_edge.weight()]);
    }
    cycle_edges_for(
        dependency_graph,
        assignment,
        cut_edge_ids,
        module_path,
        from_binding_by_ordinal,
    )
}
