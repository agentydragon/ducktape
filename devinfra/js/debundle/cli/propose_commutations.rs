//! `debundle spec propose-commutations` — draft `commuting_statements`
//! entries for the `sequenced` edges inside a rejection's blocking SCCs
//! (SPEC.md § Commuting statements, § Realizability cut explanations).
//!
//! The command reads `cycles.json` and `dependency_graph.json`, classifies
//! each constraining `sequenced` edge between an SCC's modules with the
//! verdict the graph build retained on it
//! (`edges[].premise_verdict`, decided by `graph/build.rs::premise_verdict`,
//! the same value the gate applies), synthesizes a proven selector for each
//! endpoint, and emits a YAML draft plus a report. It writes only the draft
//! (`--out`); applying an entry is the author's step, after which the gate's
//! own contradiction and unused checks validate it.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use ::gate::{BlockingSccEntry, CycleEdge};
use analysis::graph::{EndpointView, assignment_endpoints};
use analysis::{
    DepKind, DependencyEdgeId, DependencyEdgeReport, DependencyGraph, DependencyGraphReport,
    EntryDestination, KnownConflict, ModuleAssignment, ModuleDestinationKind, ModuleId, ModuleKey,
    PremiseVerdict, SequencedCause, SourceLocation, SourceUnitReport,
};
use anyhow::{Context, Result, bail};
use clap::Args as ClapArgs;
use graph_provenance::VerifiedGraphSourceContext;
use peel::OutputFormat;
use petgraph::algo::is_cyclic_directed;
use petgraph::graphmap::DiGraphMap;
use selector_codemod::{
    SourceUnitSelectorRequest, SourceUnitSelectorSynthesis, synthesize_source_unit_selectors,
};
use serde::Serialize;
use source_inspection::PreparedSource;
use spec::{CommutingStatementsContract, ModulePath, ResolvedMemberSelector, SourceUnitSelector};
use spec_modules::{collect_module_files, module_path_from_file};
use swc_atoms::Atom;

use crate::{GraphCompatibilityScope, InspectionRoots, emit_report};

/// Args for `debundle spec propose-commutations`.
#[derive(Debug, ClapArgs)]
pub(super) struct ProposeCommutationsArgs {
    /// `dependency_graph.json` of the rejected chunk.
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub graph: PathBuf,

    /// Override the default `cycles.json` location (the sibling of `--graph`).
    #[arg(long = "cycles")]
    pub cycles: Option<PathBuf>,

    /// Selector lookup root containing the graph's verified input file.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: PathBuf,

    /// The chunk's module YAML tree. With it, an endpoint that is a logical
    /// member is written as `resolved_member` under its authored module
    /// path; without it every endpoint gets a synthesized `source_match`.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules: Option<PathBuf>,

    /// Write the YAML draft to this file (the report still goes to stdout).
    #[arg(long = "out")]
    pub out_path: Option<PathBuf>,

    /// Output format. Default `text` on tty, `json` on pipe.
    #[arg(long, value_enum)]
    pub format: Option<OutputFormat>,
}

impl ProposeCommutationsArgs {
    pub(super) fn resolved_cycles_path(&self) -> PathBuf {
        self.cycles
            .clone()
            .unwrap_or_else(|| crate::gate::sibling_cycles_path(&self.graph))
    }

    pub(super) fn verify_graph(
        &self,
        roots: &InspectionRoots,
    ) -> Result<VerifiedGraphSourceContext> {
        let scope = match &self.modules {
            Some(_) => GraphCompatibilityScope::AuthoredModuleTree { tree_config: None },
            None => GraphCompatibilityScope::ExactSourceUnit,
        };
        crate::verify_graph_compatibility_with_scope(
            &self.graph,
            self.modules.as_deref(),
            Some(self.source_root.as_path()),
            roots.spec_root.as_deref(),
            roots.graph_source_root.as_deref(),
            &roots.graph_package_roots,
            scope,
        )
    }
}

#[derive(Debug, Serialize)]
pub struct ProposeCommutationsReport {
    pub chunk: String,
    pub graph: PathBuf,
    pub cycles: PathBuf,
    /// Ranked best first; `rank` is the 1-based position.
    pub proposals: Vec<CommutationProposal>,
    /// Cut edges a premise can never inert: at-init reads (`eager_use`) and
    /// the other non-`sequenced` kinds. The remedy is co-location.
    pub needs_co_location: Vec<CoLocationNeed>,
    /// Pairs the gate would reject and pairs without a unique selector; every
    /// constraining `sequenced` edge inside a blocking SCC lands either here
    /// or in `proposals`.
    pub rejected: Vec<RejectedCandidate>,
    /// The `chunk_analysis_options` YAML fragment with every proposal, in
    /// rank order, `note` left for the author.
    pub draft_yaml: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub draft_written: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CommutationProposal {
    pub rank: usize,
    pub scc_id: usize,
    pub scc_modules: Vec<ModulePath>,
    /// Indices into the SCC's `cut` this pair's edges cover; empty when the
    /// gate's feedback arc set cut the SCC on another arc.
    pub cut_edges: Vec<usize>,
    pub rank_reason: RankReason,
    /// Inerting this pair alone leaves the SCC's constraining subgraph
    /// acyclic (gate view, every parallel unit edge counted).
    pub single_pair_cut: bool,
    /// Constraining unit edges still joining the cut edge's module pair
    /// after this pair is inerted.
    pub remaining_module_pair_edges: usize,
    pub entry: CommutingStatementsContract,
    pub a: ProposedEndpoint,
    pub b: ProposedEndpoint,
    pub inerted_edges: Vec<InertedEdge>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RankReason {
    /// This pair alone breaks the SCC.
    SinglePairCut,
    /// The SCC stays cyclic; ranked by the parallel edges left on its
    /// module pair.
    PartialCut,
}

#[derive(Debug, Serialize)]
pub struct ProposedEndpoint {
    pub source_unit: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<Atom>,
    pub module: ModulePath,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    /// First source line of the source unit (its own declarator in a
    /// comma-list declaration), truncated.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub excerpt: Option<String>,
    pub selector_origin: SelectorOrigin,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SelectorOrigin {
    /// A logical member of the `--modules` tree, named by module and export.
    ResolvedMember,
    /// A `source_match` synthesized and proven alone with the shared resolve.
    SynthesizedSourceMatch,
}

#[derive(Debug, Serialize)]
pub struct InertedEdge {
    pub edge: String,
    pub from: String,
    pub to: String,
    pub explanation: SequencedCause,
    pub explanation_text: String,
}

#[derive(Debug, Serialize)]
pub struct CoLocationNeed {
    pub scc_id: usize,
    pub cut_index: usize,
    pub edge: CycleEdge,
}

#[derive(Debug, Serialize)]
pub struct RejectedCandidate {
    pub scc_id: usize,
    /// Indices into the SCC's `cut` this pair's edges cover; empty for a
    /// pair whose edges the feedback arc set did not pick.
    pub cut_edges: Vec<usize>,
    /// Source units of the candidate pair; absent when a cut edge matched
    /// no graph edge.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub a: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub b: Option<String>,
    #[serde(flatten)]
    pub reason: RejectionReason,
}

#[derive(Debug, Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum RejectionReason {
    /// The gate would reject the pair as contradicting a source fact.
    KnownConflict { conflicts: Vec<ConflictingEdge> },
    /// Every edge between the pair is strict-chain adjacency of two
    /// summarized effects; the gate would reject the pair as unused.
    UnusedSummarized,
    /// The graph predates retained verdicts; regenerate it.
    VerdictNotRecorded { edge: String },
    /// No selector proves `endpoint` alone; `detail` is the nearest miss.
    NoUniqueSelector { endpoint: String, detail: String },
    /// `cycles.json` and `dependency_graph.json` disagree.
    CutEdgeNotInGraph,
}

#[derive(Debug, Serialize)]
pub struct ConflictingEdge {
    pub edge: String,
    pub from: String,
    pub to: String,
    pub conflict: KnownConflict,
}

pub(super) fn run(
    args: ProposeCommutationsArgs,
    context: &VerifiedGraphSourceContext,
) -> Result<()> {
    let cycles_path = args.resolved_cycles_path();
    let sccs = crate::gate::load_blocking_sccs(&cycles_path)?;
    let report = crate::load_dependency_graph_report(&args.graph)?;
    let authored_modules = match &args.modules {
        Some(root) => Some(authored_module_paths(root)?),
        None => None,
    };
    let source = PreparedSource::from_source(
        context.input_file(),
        context.source_path(),
        context.source().to_owned(),
    )?;
    let mut analysis = Analysis::new(&report, &source, authored_modules.as_ref())?;
    let (needs_co_location, mut rejected) = analysis.collect_candidates(&sccs)?;
    let mut proposals = analysis.synthesize_selectors(context, &mut rejected)?;
    rank(&mut proposals);
    let draft_yaml = render_draft(&report.chunk_id, &proposals)?;
    let draft_written = match &args.out_path {
        Some(path) => {
            std::fs::write(path, &draft_yaml)
                .with_context(|| format!("writing draft {}", path.display()))?;
            Some(path.clone())
        }
        None => None,
    };
    let output = ProposeCommutationsReport {
        chunk: report.chunk_id.clone(),
        graph: args.graph.clone(),
        cycles: cycles_path,
        proposals,
        needs_co_location,
        rejected,
        draft_yaml,
        draft_written,
    };
    emit_report(
        args.format,
        &output,
        render_text,
        "writing propose-commutations output",
    )
}

/// Canonical module path → authored spelling, for every YAML module of the
/// tree. `resolved_member.module` must use the authored spelling (the
/// resolver keys member sites by it), which the graph's canonical,
/// case-folded paths cannot supply.
fn authored_module_paths(modules_root: &Path) -> Result<BTreeMap<ModulePath, String>> {
    let mut out = BTreeMap::new();
    for file in collect_module_files(modules_root)? {
        let authored = module_path_from_file(&file, modules_root);
        let canonical = ModulePath::parse(&authored, "")
            .with_context(|| format!("module YAML {} path", file.display()))?;
        out.insert(canonical, authored);
    }
    Ok(out)
}

struct ConstrainingEdge {
    id: DependencyEdgeId,
    module_pair: (ModuleId, ModuleId),
}

/// A candidate pair before its endpoints have selectors.
struct Candidate {
    scc_id: usize,
    scc_modules: Vec<ModulePath>,
    cut_edges: Vec<usize>,
    /// The module pair its first edge constrains, for the parallel-edge count.
    module_pair: (ModuleId, ModuleId),
    a: usize,
    b: usize,
    inerted: Vec<DependencyEdgeId>,
}

/// How an endpoint will be written.
enum EndpointPlan {
    ResolvedMember(ResolvedMemberSelector),
    Synthesize(SourceUnitSelectorRequest),
}

struct Analysis<'a> {
    report: &'a DependencyGraphReport,
    source: &'a PreparedSource,
    authored_modules: Option<&'a BTreeMap<ModulePath, String>>,
    graph: DependencyGraph,
    assignment: ModuleAssignment,
    node_index: HashMap<&'a str, usize>,
    /// Raw parsed body index of every source unit (`None` when the ordinal
    /// is outside the parsed source).
    body_indices: Vec<Option<usize>>,
    candidates: Vec<Candidate>,
    /// Per SCC id: its constraining subgraph, for [`cut_effect`].
    scc_constraining: BTreeMap<usize, BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>>>,
}

impl<'a> Analysis<'a> {
    fn new(
        report: &'a DependencyGraphReport,
        source: &'a PreparedSource,
        authored_modules: Option<&'a BTreeMap<ModulePath, String>>,
    ) -> Result<Self> {
        let graph =
            DependencyGraph::from_report(report).context("reconstructing dependency graph")?;
        let module_ids: HashMap<&ModuleKey, ModuleId> = report
            .quotient
            .nodes
            .iter()
            .enumerate()
            .map(|(index, entry)| (&entry.key, ModuleId(index)))
            .collect();
        let entry = report
            .quotient
            .nodes
            .iter()
            .position(|entry| entry.kind == ModuleDestinationKind::Entry)
            .context("module table has no entry destination")?;
        let of = report
            .nodes
            .iter()
            .map(|node| {
                module_ids.get(&node.destination).copied().with_context(|| {
                    format!(
                        "source unit {} has destination {} with no module-table entry",
                        node.id, node.destination
                    )
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let assignment = ModuleAssignment::from_assignments(
            of,
            EntryDestination::from_module_id(ModuleId(entry)),
        );
        let node_index = report
            .nodes
            .iter()
            .enumerate()
            .map(|(index, node)| (node.id.as_str(), index))
            .collect();
        let ordinals: Vec<_> = report
            .nodes
            .iter()
            .map(|node| node.statement_ordinal)
            .collect();
        let body_indices = source
            .body_indices_for_source_unit_ordinals(&ordinals)
            .into_iter()
            .map(|index| index.map(|index| index.0))
            .collect();
        Ok(Self {
            report,
            source,
            authored_modules,
            graph,
            assignment,
            node_index,
            body_indices,
            candidates: Vec::new(),
            scc_constraining: BTreeMap::new(),
        })
    }

    fn module_path(&self, key: &ModuleKey) -> Result<&'a ModulePath> {
        self.report
            .module(key)
            .map(|entry| &entry.path)
            .with_context(|| format!("module key {key} has no module-table entry"))
    }

    fn node(&self, index: usize) -> &'a SourceUnitReport {
        &self.report.nodes[index]
    }

    /// Constraining unit edges between members of `modules`, by module pair in
    /// the gate view, every parallel edge counted (unlike the gate's
    /// per-module-pair sequenced dedup, which would hide a parallel
    /// sequenced edge the pair does not inert).
    fn scc_constraining_edges(
        &self,
        modules: &BTreeSet<ModuleId>,
    ) -> BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> {
        let mut out: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> = BTreeMap::new();
        for edge in self.graph.iter_edges() {
            if !edge.reason.constrains_init_order() {
                continue;
            }
            let Some((from, to)) = assignment_endpoints(edge, &self.assignment, EndpointView::Gate)
            else {
                continue;
            };
            if modules.contains(&from) && modules.contains(&to) {
                out.entry((from, to)).or_default().push(edge.id);
            }
        }
        out
    }

    /// Classify every constraining `sequenced` edge inside every SCC: a pair
    /// the gate would accept becomes a candidate (selectors come later). The
    /// SCC's `cut` is the gate's feedback arc set, chosen without regard to
    /// what a premise can inert, so the pair that breaks the SCC may sit on
    /// another arc; the cut only marks which candidates it covers and which
    /// at-init edges need co-location instead.
    fn collect_candidates(
        &mut self,
        sccs: &[BlockingSccEntry],
    ) -> Result<(Vec<CoLocationNeed>, Vec<RejectedCandidate>)> {
        let mut needs_co_location = Vec::new();
        let mut rejected = Vec::new();
        for scc in sccs {
            let scc_module_ids = scc
                .modules
                .iter()
                .map(|path| {
                    self.report
                        .quotient
                        .nodes
                        .iter()
                        .position(|entry| &entry.path == path)
                        .map(ModuleId)
                        .with_context(|| format!("SCC module {path} is not in the module table"))
                })
                .collect::<Result<BTreeSet<_>>>()?;
            let constraining = self.scc_constraining_edges(&scc_module_ids);
            let mut cut_indices_by_edge: BTreeMap<DependencyEdgeId, Vec<usize>> = BTreeMap::new();
            for (cut_index, cut) in scc.cut.iter().enumerate() {
                if cut.kind != DepKind::Sequenced {
                    needs_co_location.push(CoLocationNeed {
                        scc_id: scc.id,
                        cut_index,
                        edge: cut.clone(),
                    });
                    continue;
                }
                let matching = self.matching_graph_edges(cut)?;
                if matching.is_empty() {
                    rejected.push(RejectedCandidate {
                        scc_id: scc.id,
                        cut_edges: vec![cut_index],
                        a: None,
                        b: None,
                        reason: RejectionReason::CutEdgeNotInGraph,
                    });
                    continue;
                }
                for edge_index in matching {
                    cut_indices_by_edge
                        .entry(DependencyEdgeId(edge_index))
                        .or_default()
                        .push(cut_index);
                }
            }
            // Unordered source-unit pair -> its sequenced edges, each with the
            // module pair it constrains.
            let mut by_pair: BTreeMap<(usize, usize), Vec<ConstrainingEdge>> = BTreeMap::new();
            for (&module_pair, edges) in &constraining {
                for &edge_id in edges {
                    let edge = &self.report.edges[edge_id.0];
                    if edge.edge_kind != DepKind::Sequenced {
                        continue;
                    }
                    let a = self.node_index[edge.source.as_str()];
                    let b = self.node_index[edge.target.as_str()];
                    by_pair
                        .entry((a.min(b), a.max(b)))
                        .or_default()
                        .push(ConstrainingEdge {
                            id: edge_id,
                            module_pair,
                        });
                }
            }
            for ((a, b), edges) in by_pair {
                let mut cut_edges: Vec<usize> = edges
                    .iter()
                    .flat_map(|edge| {
                        cut_indices_by_edge
                            .get(&edge.id)
                            .into_iter()
                            .flatten()
                            .copied()
                    })
                    .collect();
                cut_edges.sort_unstable();
                cut_edges.dedup();
                match self.classify_pair(a, b) {
                    PairVerdict::Inertable(inerted) => self.candidates.push(Candidate {
                        scc_id: scc.id,
                        scc_modules: scc.modules.clone(),
                        cut_edges,
                        module_pair: edges[0].module_pair,
                        a,
                        b,
                        inerted,
                    }),
                    PairVerdict::Rejected(reason) => rejected.push(RejectedCandidate {
                        scc_id: scc.id,
                        cut_edges,
                        a: Some(self.node(a).id.clone()),
                        b: Some(self.node(b).id.clone()),
                        reason,
                    }),
                }
            }
            self.scc_constraining.insert(scc.id, constraining);
        }
        Ok((needs_co_location, rejected))
    }

    /// Graph edges the cut edge stands for: `sequenced`, same statement
    /// ordinal, endpoints in the cut's module pair.
    fn matching_graph_edges(&self, cut: &CycleEdge) -> Result<Vec<usize>> {
        let mut out = Vec::new();
        for (index, edge) in self.report.edges.iter().enumerate() {
            if edge.edge_kind != DepKind::Sequenced
                || edge.statement_ordinal != cut.statement_ordinal
                || !edge.constrains_init_order
            {
                continue;
            }
            let (Some(&source), Some(&target)) = (
                self.node_index.get(edge.source.as_str()),
                self.node_index.get(edge.target.as_str()),
            ) else {
                bail!(
                    "edge {} references a source unit missing from the graph",
                    edge.id
                );
            };
            if self.module_path(&self.node(source).destination)? == &cut.from
                && self.module_path(&self.node(target).destination)? == &cut.to
            {
                out.push(index);
            }
        }
        Ok(out)
    }

    /// What the gate would do with a premise over `(a, b)`, read from the
    /// verdict retained on every edge between them.
    fn classify_pair(&self, a: usize, b: usize) -> PairVerdict {
        let a_id = self.node(a).id.as_str();
        let b_id = self.node(b).id.as_str();
        let mut conflicts = Vec::new();
        let mut inerted = Vec::new();
        let mut summarized = false;
        for (index, edge) in self.report.edges.iter().enumerate() {
            let joins = (edge.source == a_id && edge.target == b_id)
                || (edge.source == b_id && edge.target == a_id);
            if !joins || !matches!(edge.edge_kind, DepKind::Sequenced | DepKind::CoDeclaration) {
                continue;
            }
            match &edge.premise_verdict {
                None => {
                    return PairVerdict::Rejected(RejectionReason::VerdictNotRecorded {
                        edge: edge.id.clone(),
                    });
                }
                Some(PremiseVerdict::Inertable) => inerted.push(DependencyEdgeId(index)),
                Some(PremiseVerdict::Summarized) => summarized = true,
                Some(PremiseVerdict::Known { conflict }) => conflicts.push(ConflictingEdge {
                    edge: edge.id.clone(),
                    from: edge.source.clone(),
                    to: edge.target.clone(),
                    conflict: conflict.clone(),
                }),
            }
        }
        if !conflicts.is_empty() {
            return PairVerdict::Rejected(RejectionReason::KnownConflict { conflicts });
        }
        if inerted.is_empty() {
            debug_assert!(summarized, "a sequenced cut edge joins the pair");
            return PairVerdict::Rejected(RejectionReason::UnusedSummarized);
        }
        PairVerdict::Inertable(inerted)
    }

    fn endpoint_plan(&self, node_index: usize) -> Result<EndpointPlan, String> {
        let node = self.node(node_index);
        if let Some(binding) = node.declared_bindings.first() {
            let member = self.authored_modules.and_then(|authored| {
                let entry = self.report.module(&node.destination)?;
                (entry.kind == ModuleDestinationKind::Specified)
                    .then(|| authored.get(&entry.path))
                    .flatten()
            });
            return Ok(match member {
                Some(module) => EndpointPlan::ResolvedMember(ResolvedMemberSelector {
                    module: module.clone(),
                    name: binding.export_name.to_string(),
                }),
                None => EndpointPlan::Synthesize(SourceUnitSelectorRequest::DeclaredBinding {
                    binding: binding.binding.to_string(),
                }),
            });
        }
        let body_index = self.body_indices[node_index].ok_or_else(|| {
            format!(
                "source unit {} ordinal {} is outside the parsed source; regenerate the dependency graph",
                node.id, node.statement_ordinal.0
            )
        })?;
        Ok(EndpointPlan::Synthesize(
            SourceUnitSelectorRequest::AnonymousStatement { body_index },
        ))
    }

    fn endpoint_report(
        &self,
        node_index: usize,
        selector_origin: SelectorOrigin,
    ) -> Result<ProposedEndpoint> {
        let node = self.node(node_index);
        let excerpt = match self.body_indices[node_index] {
            Some(index) => Some(excerpt_of(
                self.source
                    .source_unit_original_source(index, node.statement_ordinal)?,
            )),
            None => None,
        };
        Ok(ProposedEndpoint {
            source_unit: node.id.clone(),
            binding: node.declared_bindings.first().map(|b| b.binding.clone()),
            module: self.module_path(&node.destination)?.clone(),
            source_location: node.source_location.clone(),
            excerpt,
            selector_origin,
        })
    }

    /// Give every candidate its two selectors, demoting a candidate whose
    /// endpoint has no uniquely proven selector to `rejected`.
    fn synthesize_selectors(
        &mut self,
        context: &VerifiedGraphSourceContext,
        rejected: &mut Vec<RejectedCandidate>,
    ) -> Result<Vec<CommutationProposal>> {
        let mut proposals = Vec::new();
        let mut plans: BTreeMap<usize, Result<EndpointPlan, String>> = BTreeMap::new();
        for candidate in &self.candidates {
            for node_index in [candidate.a, candidate.b] {
                plans
                    .entry(node_index)
                    .or_insert_with(|| self.endpoint_plan(node_index));
            }
        }
        let requests: Vec<(usize, SourceUnitSelectorRequest)> = plans
            .iter()
            .filter_map(|(node_index, plan)| match plan {
                Ok(EndpointPlan::Synthesize(request)) => Some((*node_index, request.clone())),
                _ => None,
            })
            .collect();
        let synthesized = synthesize_source_unit_selectors(
            &context.source_path().display().to_string(),
            context.source().to_owned(),
            &requests
                .iter()
                .map(|(_, request)| request.clone())
                .collect::<Vec<_>>(),
        )?;
        let mut selectors: BTreeMap<usize, Result<(SourceUnitSelector, SelectorOrigin), String>> =
            BTreeMap::new();
        for ((node_index, _), synthesis) in requests.iter().zip(synthesized) {
            let outcome = match synthesis {
                SourceUnitSelectorSynthesis::Synthesized(selector)
                    if self.body_indices[*node_index] == Some(selector.body_index) =>
                {
                    Ok((
                        SourceUnitSelector::SourceMatch(selector.claim),
                        SelectorOrigin::SynthesizedSourceMatch,
                    ))
                }
                SourceUnitSelectorSynthesis::Synthesized(selector) => Err(format!(
                    "the synthesized selector proves body index {} but the source unit is body index {:?}",
                    selector.body_index, self.body_indices[*node_index]
                )),
                SourceUnitSelectorSynthesis::Skipped { reason } => Err(reason),
            };
            selectors.insert(*node_index, outcome);
        }
        for (node_index, plan) in plans {
            match plan {
                Ok(EndpointPlan::ResolvedMember(member)) => {
                    selectors.insert(
                        node_index,
                        Ok((
                            SourceUnitSelector::ResolvedMember(member),
                            SelectorOrigin::ResolvedMember,
                        )),
                    );
                }
                Ok(EndpointPlan::Synthesize(_)) => {}
                Err(reason) => {
                    selectors.insert(node_index, Err(reason));
                }
            }
        }

        let candidates = std::mem::take(&mut self.candidates);
        for candidate in candidates {
            let endpoint =
                |node_index: usize| -> Result<(SourceUnitSelector, ProposedEndpoint), String> {
                    let (selector, origin) = selectors[&node_index].clone()?;
                    let report = self
                        .endpoint_report(node_index, origin)
                        .map_err(|error| format!("{error:#}"))?;
                    Ok((selector, report))
                };
            let (a, b) = match (endpoint(candidate.a), endpoint(candidate.b)) {
                (Ok(a), Ok(b)) => (a, b),
                (Err(detail), _) | (_, Err(detail)) => {
                    let failed = if selectors[&candidate.a].is_err() {
                        candidate.a
                    } else {
                        candidate.b
                    };
                    rejected.push(RejectedCandidate {
                        scc_id: candidate.scc_id,
                        cut_edges: candidate.cut_edges,
                        a: Some(self.node(candidate.a).id.clone()),
                        b: Some(self.node(candidate.b).id.clone()),
                        reason: RejectionReason::NoUniqueSelector {
                            endpoint: self.node(failed).id.clone(),
                            detail,
                        },
                    });
                    continue;
                }
            };
            let (single_pair_cut, remaining_module_pair_edges) =
                cut_effect(&self.scc_constraining[&candidate.scc_id], &candidate);
            let inerted_edges = candidate
                .inerted
                .iter()
                .map(|id| {
                    let edge: &DependencyEdgeReport = &self.report.edges[id.0];
                    let cause = edge.sequenced_cause.clone().with_context(|| {
                        format!("inertable edge {} has no retained cause", edge.id)
                    })?;
                    Ok(InertedEdge {
                        edge: edge.id.clone(),
                        from: edge.source.clone(),
                        to: edge.target.clone(),
                        explanation_text: cause.to_string(),
                        explanation: cause,
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            proposals.push(CommutationProposal {
                rank: 0,
                scc_id: candidate.scc_id,
                scc_modules: candidate.scc_modules,
                cut_edges: candidate.cut_edges,
                rank_reason: if single_pair_cut {
                    RankReason::SinglePairCut
                } else {
                    RankReason::PartialCut
                },
                single_pair_cut,
                remaining_module_pair_edges,
                entry: CommutingStatementsContract {
                    a: a.0,
                    b: b.0,
                    note: None,
                },
                a: a.1,
                b: b.1,
                inerted_edges,
            });
        }
        Ok(proposals)
    }
}

enum PairVerdict {
    Inertable(Vec<DependencyEdgeId>),
    Rejected(RejectionReason),
}

/// Whether inerting `candidate` alone leaves the SCC's constraining subgraph
/// acyclic, and the constraining edges left on its module pair.
fn cut_effect(
    constraining: &BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>>,
    candidate: &Candidate,
) -> (bool, usize) {
    let inerted: BTreeSet<DependencyEdgeId> = candidate.inerted.iter().copied().collect();
    let mut remaining: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    let mut module_pair_edges = 0;
    for (&(from, to), edges) in constraining {
        let left = edges.iter().filter(|id| !inerted.contains(id)).count();
        if (from, to) == candidate.module_pair {
            module_pair_edges = left;
        }
        if left > 0 {
            remaining.add_edge(from, to, ());
        }
    }
    (!is_cyclic_directed(&remaining), module_pair_edges)
}

/// Best first: a pair that alone breaks its SCC, then one the gate's cut
/// already names, then fewer parallel edges left on its module pair, then
/// report order.
fn rank(proposals: &mut [CommutationProposal]) {
    proposals.sort_by_key(|proposal| {
        (
            !proposal.single_pair_cut,
            proposal.cut_edges.is_empty(),
            proposal.remaining_module_pair_edges,
            proposal.scc_id,
            proposal.a.source_unit.clone(),
        )
    });
    for (index, proposal) in proposals.iter_mut().enumerate() {
        proposal.rank = index + 1;
    }
}

const EXCERPT_LIMIT: usize = 120;

fn excerpt_of(original_source: &str) -> String {
    let first_line = original_source.lines().next().unwrap_or_default().trim();
    match first_line.char_indices().nth(EXCERPT_LIMIT) {
        Some((cut, _)) => format!("{}…", &first_line[..cut]),
        None => first_line.to_owned(),
    }
}

fn location_text(endpoint: &ProposedEndpoint) -> String {
    let location = endpoint.source_location.as_ref().map_or_else(
        || "<location unavailable>".to_owned(),
        |location| {
            format!(
                "{}:{}:{}",
                location.source_path,
                location.start_line,
                location
                    .start_column
                    .map_or_else(|| "?".to_owned(), |column| column.to_string())
            )
        },
    );
    let binding = endpoint
        .binding
        .as_ref()
        .map_or_else(|| "anonymous statement".to_owned(), |b| format!("`{b}`"));
    format!(
        "{} {binding} in {} at {location}{}",
        endpoint.source_unit,
        endpoint.module,
        endpoint
            .excerpt
            .as_deref()
            .map(|excerpt| format!(" — {excerpt}"))
            .unwrap_or_default(),
    )
}

/// The `chunk_analysis_options` fragment: one `commuting_statements` entry
/// per proposal under the graph's chunk, each preceded by comments carrying
/// the report's explanation so the draft can be reviewed on its own.
fn render_draft(chunk: &str, proposals: &[CommutationProposal]) -> Result<String> {
    const ITEM_INDENT: &str = "      ";
    let chunk_key = serde_yaml::to_string(chunk)?;
    let mut out = format!(
        "chunk_analysis_options:\n  {}:\n    commuting_statements:\n",
        chunk_key.trim_end()
    );
    if proposals.is_empty() {
        out.push_str(&format!("{ITEM_INDENT}[]\n"));
        return Ok(out);
    }
    for proposal in proposals {
        let mut comments = vec![format!(
            "rank {} ({}): breaks SCC {} ({})",
            proposal.rank,
            match proposal.rank_reason {
                RankReason::SinglePairCut => "single-pair cut",
                RankReason::PartialCut => "partial cut",
            },
            proposal.scc_id,
            proposal
                .scc_modules
                .iter()
                .map(ModulePath::as_str)
                .collect::<Vec<_>>()
                .join(", "),
        )];
        for edge in &proposal.inerted_edges {
            comments.push(format!(
                "inerts {} {} -> {}: {}",
                edge.edge, edge.from, edge.to, edge.explanation_text
            ));
        }
        comments.push(format!("a: {}", location_text(&proposal.a)));
        comments.push(format!("b: {}", location_text(&proposal.b)));
        for comment in comments {
            for line in comment.lines() {
                out.push_str(&format!("{ITEM_INDENT}# {line}\n"));
            }
        }
        let mut entry = serde_yaml::to_value(&proposal.entry)?;
        entry
            .as_mapping_mut()
            .context("a commuting_statements entry serializes as a mapping")?
            .insert(
                serde_yaml::Value::String("note".to_owned()),
                serde_yaml::Value::Null,
            );
        let rendered = serde_yaml::to_string(&vec![entry])?;
        for line in rendered.lines() {
            out.push_str(ITEM_INDENT);
            out.push_str(line);
            out.push('\n');
        }
    }
    Ok(out)
}

fn render_text(report: &ProposeCommutationsReport, out: &mut String) {
    out.push_str(&format!(
        "chunk {}: {} proposal(s), {} cut edge(s) need co-location, {} candidate(s) rejected\n",
        report.chunk,
        report.proposals.len(),
        report.needs_co_location.len(),
        report.rejected.len(),
    ));
    for proposal in &report.proposals {
        out.push_str(&format!(
            "  #{} SCC {} [{}] remaining module-pair edges: {}\n    a: {}\n    b: {}\n",
            proposal.rank,
            proposal.scc_id,
            match proposal.rank_reason {
                RankReason::SinglePairCut => "single-pair cut",
                RankReason::PartialCut => "partial cut",
            },
            proposal.remaining_module_pair_edges,
            location_text(&proposal.a),
            location_text(&proposal.b),
        ));
        for edge in &proposal.inerted_edges {
            out.push_str(&format!(
                "    inerts {} {} -> {}: {}\n",
                edge.edge, edge.from, edge.to, edge.explanation_text
            ));
        }
    }
    for need in &report.needs_co_location {
        out.push_str(&format!(
            "  needs co-location: SCC {} cut[{}] {} --{}--> {}\n",
            need.scc_id,
            need.cut_index,
            need.edge.from,
            need.edge.kind.diagnostic_label(),
            need.edge.to,
        ));
    }
    for rejected in &report.rejected {
        out.push_str(&format!(
            "  rejected: SCC {} cut{:?} {} / {}: {}\n",
            rejected.scc_id,
            rejected.cut_edges,
            rejected.a.as_deref().unwrap_or("?"),
            rejected.b.as_deref().unwrap_or("?"),
            rejection_text(&rejected.reason),
        ));
    }
    if let Some(path) = &report.draft_written {
        out.push_str(&format!("draft written to {}\n", path.display()));
    }
    out.push_str("---\n");
    out.push_str(&report.draft_yaml);
}

fn rejection_text(reason: &RejectionReason) -> String {
    match reason {
        RejectionReason::KnownConflict { conflicts } => format!(
            "contradicts a source fact: {}",
            conflicts
                .iter()
                .map(|c| format!("{} -> {}: {}", c.from, c.to, c.conflict))
                .collect::<Vec<_>>()
                .join("; ")
        ),
        RejectionReason::UnusedSummarized => {
            "both endpoints are summarized; the premise would inert nothing".to_owned()
        }
        RejectionReason::VerdictNotRecorded { edge } => {
            format!("{edge} has no retained premise verdict; regenerate the graph")
        }
        RejectionReason::NoUniqueSelector { endpoint, detail } => {
            format!("no unique selector for {endpoint}: {detail}")
        }
        RejectionReason::CutEdgeNotInGraph => {
            "the cut edge matches no graph edge; regenerate both reports".to_owned()
        }
    }
}
