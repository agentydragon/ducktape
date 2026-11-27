//! Classifies, ranks and proves the candidate commutations of the blocking SCCs.

mod candidates;
mod outcomes;
mod proposals;

use crate::propose_commutations::report::{
    BlockingReason, EndpointSelectorFailure, RejectionReason, SelectorOrigin,
};
use analysis::{EntryDestination, ModuleId};
use analysis_model::DepKind;
use anyhow::{Context, Result};
use graph::CommutationOrder;
use graph::{
    DependencyEdgeId, DependencyGraph, DependencyGraphInput, DependencyGraphReport,
    ModuleAssignment, ModuleDestinationKind, ModuleKey, SourceUnitReport,
};
use petgraph::algo::is_cyclic_directed;
use petgraph::graphmap::DiGraphMap;
use petgraph::visit::EdgeFiltered;
use selector_codemod::source_unit::SourceUnitSelectorRequest;
use source_identity::SourceUnitId;
use source_identity::SourceUnitOrdinal;
use source_inspection::PreparedSource;
use spec::input_contracts::SourceUnitSelector;
use spec::{ModulePath, ResolvedMemberSelector};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

struct ConstrainingEdge {
    id: DependencyEdgeId,
    module_pair: (ModuleId, ModuleId),
}

/// A candidate pair before its endpoints have selectors.
struct Candidate {
    scc_id: usize,
    cut_edges: Vec<usize>,
    /// The module pair its first edge constrains, for the parallel-edge count.
    module_pair: (ModuleId, ModuleId),
    a: usize,
    b: usize,
    inerted: Vec<DependencyEdgeId>,
}

/// A candidate placed in the SCC where it ranks best.
pub(super) struct RankedCandidate {
    candidate: Candidate,
    also_in_sccs: Vec<usize>,
    single_pair_cut: bool,
    remaining_module_pair_edges: usize,
}

impl RankedCandidate {
    fn rank_key(&self) -> impl Ord + use<> {
        (
            !self.single_pair_cut,
            self.remaining_module_pair_edges != 0,
            std::cmp::Reverse(self.candidate.inerted.len()),
            self.candidate.cut_edges.is_empty(),
            self.remaining_module_pair_edges,
            self.candidate.scc_id,
            self.candidate.a,
            self.candidate.b,
        )
    }
}

/// An endpoint's selector and how it was made, or why it has none.
type EndpointSelector = Result<(SourceUnitSelector, SelectorOrigin), EndpointSelectorFailure>;

/// How an endpoint will be written.
enum EndpointPlan {
    ResolvedMember(ResolvedMemberSelector),
    Synthesize(SourceUnitSelectorRequest),
}

pub(super) struct Analysis<'a> {
    report: &'a DependencyGraphReport,
    source: &'a PreparedSource,
    authored_modules: Option<&'a BTreeMap<ModulePath, String>>,
    graph: &'a DependencyGraph,
    assignment: ModuleAssignment,
    temporal_order: CommutationOrder,
    existing_pairs: Vec<(SourceUnitId, SourceUnitId)>,
    /// Module path of every source unit.
    node_modules: Vec<&'a ModulePath>,
    /// `sequenced` and `co_declaration` edges by unordered source-unit pair:
    /// the edges a premise over the pair is classified on.
    premise_edges: HashMap<(usize, usize), Vec<DependencyEdgeId>>,
    /// Constraining `sequenced` edges by statement ordinal, for matching
    /// `cut` rows to graph edges.
    sequenced_by_ordinal: HashMap<SourceUnitOrdinal, Vec<DependencyEdgeId>>,
    /// Raw parsed body index of every source unit (`None` when the ordinal
    /// is outside the parsed source).
    body_indices: Vec<Option<usize>>,
    candidates: Vec<Candidate>,
    sccs: BTreeMap<usize, SccState>,
}

/// One blocking SCC as the analysis sees it.
struct SccState {
    modules: Vec<ModulePath>,
    /// The SCC's constraining subgraph, by module pair in the gate view,
    /// every parallel edge counted (unlike the gate's per-module-pair
    /// sequenced dedup, which would hide a parallel sequenced edge a pair
    /// does not inert).
    constraining: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>>,
    /// The module pair of every edge in `constraining`.
    edge_module_pairs: HashMap<DependencyEdgeId, (ModuleId, ModuleId)>,
    /// The module graph of `constraining`.
    graph: DiGraphMap<ModuleId, ()>,
    /// Whether `graph` is cyclic with nothing inerted.
    cyclic: bool,
    /// Why each rejected source-unit pair's edges stay constraining.
    rejected_pairs: HashMap<(usize, usize), BlockingReason>,
    /// Edges the SCC's proposals inert together.
    inerted: HashSet<DependencyEdgeId>,
    /// Endpoints of `inerted`'s pairs whose selectors were assumed.
    unproven_endpoints: HashSet<usize>,
}

impl<'a> Analysis<'a> {
    pub(super) fn new(
        input: &'a DependencyGraphInput,
        source: &'a PreparedSource,
        authored_modules: Option<&'a BTreeMap<ModulePath, String>>,
    ) -> Result<Self> {
        let report = input.report();
        let graph = input.graph();
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
        let node_modules = of
            .iter()
            .map(|module| &report.quotient.nodes[module.0].path)
            .collect();
        let assignment = ModuleAssignment::from_assignments(
            of,
            EntryDestination::from_module_id(ModuleId(entry)),
        );
        let mut premise_edges: HashMap<(usize, usize), Vec<DependencyEdgeId>> = HashMap::new();
        let mut sequenced_by_ordinal: HashMap<SourceUnitOrdinal, Vec<DependencyEdgeId>> =
            HashMap::new();
        for edge in graph.iter_edges() {
            let (source, target) = (edge.from.0, edge.to.0);
            let id = edge.id;
            if matches!(
                edge.reason.kind(),
                DepKind::Sequenced | DepKind::CoDeclaration
            ) {
                premise_edges
                    .entry((source.min(target), source.max(target)))
                    .or_default()
                    .push(id);
            }
            if edge.reason.kind() == DepKind::Sequenced && edge.reason.constrains_init_order() {
                sequenced_by_ordinal
                    .entry(edge.reason.statement_ordinal())
                    .or_default()
                    .push(id);
            }
        }
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
        let existing = graph.commuting_premises();
        let existing_pairs = existing
            .iter()
            .map(|premise| (premise.a, premise.b))
            .collect();
        let temporal_order = CommutationOrder::from_edges(
            graph.num_nodes(),
            graph
                .iter_edges()
                .map(|edge| (edge, input.edge_report(edge.id).premise_verdict.as_ref())),
            existing,
        );
        Ok(Self {
            report,
            source,
            authored_modules,
            graph,
            assignment,
            temporal_order,
            existing_pairs,
            node_modules,
            premise_edges,
            sequenced_by_ordinal,
            body_indices,
            candidates: Vec::new(),
            sccs: BTreeMap::new(),
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

    fn scc_mut(&mut self, scc_id: usize) -> &mut SccState {
        self.sccs
            .get_mut(&scc_id)
            .expect("every candidate's SCC was collected")
    }
}

enum PairVerdict {
    Inertable(Vec<DependencyEdgeId>),
    Rejected(RejectionReason),
}

/// Whether inerting `candidate` alone leaves the SCC's constraining subgraph
/// acyclic, and the constraining edges left on its module pair. Only a module
/// pair the candidate empties changes the module graph, so the SCC's cycle
/// check is redone, without those arcs, only for a candidate that empties one.
fn cut_effect(scc: &SccState, candidate: &Candidate) -> (bool, usize) {
    let mut inerted_by_pair: BTreeMap<(ModuleId, ModuleId), usize> = BTreeMap::new();
    for pair in candidate
        .inerted
        .iter()
        .filter_map(|id| scc.edge_module_pairs.get(id))
    {
        *inerted_by_pair.entry(*pair).or_default() += 1;
    }
    let left = |pair: &(ModuleId, ModuleId)| {
        scc.constraining.get(pair).map_or(0, Vec::len)
            - inerted_by_pair.get(pair).copied().unwrap_or_default()
    };
    let emptied: BTreeSet<(ModuleId, ModuleId)> = inerted_by_pair
        .keys()
        .copied()
        .filter(|pair| left(pair) == 0)
        .collect();
    let cyclic = if emptied.is_empty() {
        scc.cyclic
    } else {
        is_cyclic_directed(&EdgeFiltered::from_fn(&scc.graph, |(from, to, _)| {
            !emptied.contains(&(from, to))
        }))
    };
    (!cyclic, left(&candidate.module_pair))
}

fn module_graph<'p>(
    pairs: impl Iterator<Item = &'p (ModuleId, ModuleId)>,
) -> DiGraphMap<ModuleId, ()> {
    let mut graph = DiGraphMap::new();
    for &(from, to) in pairs {
        graph.add_edge(from, to, ());
    }
    graph
}
