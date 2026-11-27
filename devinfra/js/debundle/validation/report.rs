//! Assignment-validation report types: blocking SCCs, cycle edges and their
//! explanations.

use super::render::BindingPairKey;
use crate::realizability::{RealizabilityVerdict, SccRejection};
use analysis_model::DepKind;
use analysis_vocabulary::inputs::CommutingStatementsRef;
use graph::assignment_assembly::AtomicGroupConflict;
use graph::report::SequencedSourceUnitCause;
use graph::{DependencyEdge, DependencyGraph, EdgeRole, SequencedCause};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use spec::ModulePath;
use std::collections::BTreeMap;
use std::fmt;
use swc_atoms::Atom;

/// Result of validating a module dep graph.
#[derive(Debug, Clone, Serialize)]
pub struct AssignmentReport {
    pub cycles: Vec<CycleReport>,
    /// Atomic factor groups the spec splits across destination
    /// modules — unrealizable by construction. Populated from
    /// `AssignmentAnalysis::assembly_conflicts`; the materializer rejects any
    /// spec with a non-empty list before emitting code.
    pub atomic_group_conflicts: Vec<AtomicGroupConflict>,
    /// Concrete DFS finish order under the supplied import priority, including
    /// entry. A rejected witness still has an order: `cycles` diagnoses why it
    /// violates initialization constraints. This field never certifies that a
    /// different order exists or that the current assignment is impossible.
    pub linker_order: Vec<ModulePath>,
    /// Clause-2 violations (cross-destination rebinding writes) from
    /// the realizability verdict, rendered for diagnostics. A
    /// rebinding write and the binding it reassigns belong to one
    /// atomic factor group, so a spec splitting them surfaces as an
    /// `atomic_group_conflicts` entry first — on the accept path
    /// (`cycles` and `atomic_group_conflicts` both empty) this list
    /// must be empty too, and the materializer bails if that
    /// invariant ever breaks (`emit_reports_and_resolve_gate` in
    /// `lowering/materialize/mod.rs`).
    pub cross_rebinds: Vec<String>,
}

/// Canonical in-memory validation for one settled assignment. The typed
/// verdict is authoritative; `report` is its rendered diagnostic projection.
/// This value is deliberately not deserializable, so a missing typed verdict
/// can never default to acceptance when reading a report from disk.
#[derive(Debug, Clone)]
pub struct AssignmentValidation {
    pub verdict: RealizabilityVerdict,
    pub report: AssignmentReport,
}

/// Validator's rendered projection of one unrealizable SCC. The shared
/// "modules in the SCC + edges in the SCC" core is
/// [`graph::report::SccCore`]; the in-memory primitive that carries
/// it is [`crate::realizability::SccDiagnosis`]. This shape stringifies
/// the module names and decorates the diagnosis with the `cut` /
/// `lazy_closure` rows the bail-message renderer consumes.
#[derive(Debug, Clone, Serialize)]
pub struct CycleReport {
    pub rejection: BlockingRejection,
    pub modules: Vec<ModulePath>,
    /// One ordered, complete walk through actual Pass-1 constraining edges.
    /// Absent for actual Pass-2 and conditional collapsed diagnoses.
    pub pass_one_witness: Option<PassOneCycleWitness>,
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
/// is recomputable from `dependency_graph.json.gz` + this entry's `modules`
/// set, so the wire entry carries the modules, cut, and one bounded
/// Pass-1 closed walk instead of all SCC edges. Consumers that need
/// every edge re-derive it via
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
    /// `dependency_graph.json.gz` by resolving destination keys through the
    /// table (see `docs/wire_format.md` §"one canonical module
    /// identity").
    pub modules: Vec<ModulePath>,
    /// The actionable subset for spec authors: removing any of these
    /// edges (by co-locating the binding pair into one module) works
    /// toward breaking the SCC. See [`CycleReport::cut`] for the
    /// per-rejection-kind derivation.
    pub cut: Vec<CycleEdge>,
    /// `null` means this rejection has no supported Pass-1 witness.
    pub pass_one_witness: Option<PassOneCycleWitness>,
    /// See [`CycleReport::inert_commuting_edges`].
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inert_commuting_edges: Vec<CycleEdge>,
    /// Which gate pass rejected the SCC. `None` only in reports written
    /// before the field existed.
    #[serde(default)]
    pub rejection: Option<BlockingRejection>,
}

/// Wire projection of [`SccRejection`] for `cycles.json` and the bail
/// summary.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BlockingRejection {
    /// Pass 1: a cycle of constraining edges. The root cause whenever
    /// present.
    MutualConstrainingCycle,
    /// Pass 2 under the linker order: an independent blocker.
    EsmEvaluationTdz,
    /// Reported only alongside Pass-1 cycles, which leave no linker order
    /// for Pass 2. Pass 2 run on the same assignment with each Pass-1 SCC
    /// collapsed into one module: what still blocks once those cycles are
    /// resolved by co-location. Conditional, not a verdict of its own.
    TdzAfterCollapsingPassOne,
}

impl BlockingRejection {
    /// Label appended to a gate-list row, including its leading indentation.
    pub fn list_label(self) -> &'static str {
        match self {
            Self::MutualConstrainingCycle => "  [pass 1: mutual constraining cycle]",
            Self::EsmEvaluationTdz => "  [pass 2: ESM evaluation TDZ]",
            Self::TdzAfterCollapsingPassOne => {
                "  [pass 2, conditional: after collapsing the pass-1 SCCs]"
            }
        }
    }

    /// Suffix appended to a cycle's text summary.
    pub fn cycle_summary_suffix(self) -> &'static str {
        match self {
            Self::MutualConstrainingCycle => " [Pass 1: mutual constraining cycle]",
            Self::EsmEvaluationTdz => " [Pass 2: ESM evaluation TDZ]",
            Self::TdzAfterCollapsingPassOne => {
                " [Pass 2, conditional: after collapsing the Pass-1 SCCs]"
            }
        }
    }

    pub fn of(rejection: SccRejection) -> Self {
        match rejection {
            SccRejection::MutualConstrainingCycle => Self::MutualConstrainingCycle,
            SccRejection::EsmEvaluationTdz => Self::EsmEvaluationTdz,
        }
    }
}

impl BlockingSccEntry {
    /// Project a rich [`CycleReport`] onto the trimmed wire shape.
    /// `id` is the entry's index in the `cycles.json` array.
    pub fn from_cycle_report(id: usize, cycle: &CycleReport) -> Self {
        Self {
            id,
            modules: cycle.modules.clone(),
            cut: cycle.cut.clone(),
            pass_one_witness: cycle.pass_one_witness.clone(),
            inert_commuting_edges: cycle.inert_commuting_edges.clone(),
            rejection: Some(cycle.rejection),
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

/// A complete module closed walk, with one typed dependency cause per step.
/// The text renderer previews only the first few steps; JSON keeps the walk.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PassOneCycleWitness {
    pub complete: bool,
    pub steps: Vec<CycleEdge>,
    pub text_preview_omitted_steps: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CycleEdge {
    /// Source module, by canonical [`ModulePath`].
    pub from: ModulePath,
    /// Target module, by canonical [`ModulePath`].
    pub to: ModulePath,
    /// Source endpoint's actual source-unit ID (`unit:N`).
    pub from_source_unit_id: String,
    /// Target endpoint's actual source-unit ID (`unit:N`).
    pub to_source_unit_id: String,
    /// Edge-reason provenance. This ordinal can differ from either endpoint's
    /// source-unit ordinal, so it must not be used to infer endpoint identity.
    pub statement_ordinal: SourceUnitOrdinal,
    /// Target binding being read (declared in `to`'s module). `None`
    /// for sequenced edges (no symbol).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<Atom>,
    /// Source-side binding (the first declared binding of the source_unit
    /// that issued the read). `None` when the source source_unit is an
    /// anonymous statement with no declared bindings — diagnostics
    /// fall back to `<anon stmt #{statement_ordinal}>` in that case.
    /// Populated by the supplied-priority verifier from the dependency graph,
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
pub(super) fn explain_edge(
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
                        callee_source_unit: graph::report::source_unit_key(callee_source_unit),
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
            target_source_unit: graph::report::impure_source_unit_cause(dependency_graph, edge.to)
                .map(Box::new),
        }),
        _ => None,
    }
}

impl CycleEdge {
    pub fn sequenced_causes(&self) -> impl Iterator<Item = &SequencedSourceUnitCause> {
        let target = match &self.explanation {
            Some(CutEdgeExplanation::Sequenced {
                target_source_unit, ..
            }) => target_source_unit.as_deref(),
            _ => None,
        };
        self.sequenced_source_unit.iter().chain(target)
    }

    /// Render the endpoints, dependency kind and statement ordinal on one line.
    pub fn render_header(&self, out: &mut String) {
        let key = BindingPairKey::of(self);
        out.push_str(&format!(
            "    {from_b} ({from_m})  --{kind}-->  {to_b} ({to_m})  [stmt #{ord}]\n",
            from_b = key.from_label,
            from_m = key.from,
            kind = self.kind.diagnostic_label(),
            to_b = key.to_label,
            to_m = key.to,
            ord = self.statement_ordinal.0,
        ));
    }

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
