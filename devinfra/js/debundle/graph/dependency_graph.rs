use source_identity::{SourceUnitId, SourceUnitOrdinal};
use std::collections::{BTreeSet, HashMap};

use swc_ecma_ast::Id;

use analysis::StatementKind;
use analysis::facts::CallbackFactoryBarrierFact;
use analysis_vocabulary::execution_evidence::SourceLocation;
use analysis_vocabulary::inputs::CommutingStatementsRef;
use analysis_vocabulary::purity::Purity;

use super::edge::{DependencyEdge, DependencyEdgeId, EdgeReason, EdgeRole};

/// Fine-grained dependency graph before logical modules are formed. Nodes are
/// source units from split statement facts; edges are reads between source units
/// and source-order side-effect constraints. The module dependency graph is the
/// quotient of this graph by a [`ModuleAssignment`].
///
/// Storage is **flat-edges + CSR adjacency**, the canonical compiler-IR
/// shape: one [`DependencyEdge`] per reason, indexed by [`DependencyEdgeId`]
/// (= position in `edges`), with per-node `out_edges` / `in_edges`
/// adjacency lists for O(deg) traversal.
///
/// [`ModuleAssignment`]: crate::module_assignment::ModuleAssignment
#[derive(Debug, Clone, Default)]
pub struct DependencyGraph {
    pub(crate) nodes: Vec<SourceUnit>,
    pub(crate) edges: Vec<DependencyEdge>,
    /// CSR adjacency by source unit. `out_edges[source_unit.0]` is a list
    /// of `DependencyEdgeId` indices into `edges`.
    pub(crate) out_edges: Vec<Vec<DependencyEdgeId>>,
    /// CSR adjacency by target source unit.
    pub(crate) in_edges: Vec<Vec<DependencyEdgeId>>,
    /// Author `commuting_statements` premises the build applied, in spec
    /// order. Each listed edge carries the matching
    /// [`EdgeReason::commuting_premise`] marker.
    pub(crate) commuting_premises: Vec<AppliedCommutingPremise>,
}

/// One `commuting_statements` entry the graph build accepted: every
/// `Sequenced` edge between `a` and `b` (either direction) is inert, listed
/// in `inerted_edges`. An accepted premise inerts at least one edge.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppliedCommutingPremise {
    pub reference: CommutingStatementsRef,
    pub spec_location: String,
    pub a: SourceUnitId,
    pub b: SourceUnitId,
    pub inerted_edges: Vec<DependencyEdgeId>,
    pub note: Option<String>,
}

#[derive(Debug, Clone)]
pub struct SourceUnit {
    pub id: SourceUnitId,
    pub statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<Id>,
    pub kind: StatementKind,
    pub purity: Purity,
    pub callback_factory_barriers: Vec<CallbackFactoryBarrierFact>,
    pub heap_native_integrity_uses: Vec<analysis_vocabulary::report_schema::NativeIntegrityUse>,
    pub synchronous_execution:
        Option<analysis_vocabulary::report_schema::SynchronousExecutionReport>,
    pub callback_factory_uses: Vec<analysis::facts::CallbackFactoryUseFact>,
    pub returned_record_uses: Vec<analysis::facts::ReturnedRecordUseFact>,
}

/// Declaration-based classification, independent of syntax kind and destination.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum SourceUnitClass {
    BindingDeclaration,
    AnonymousStatement,
}

impl SourceUnitClass {
    pub(crate) fn from_declared_bindings(bindings: impl IntoIterator) -> Self {
        if bindings.into_iter().next().is_some() {
            Self::BindingDeclaration
        } else {
            Self::AnonymousStatement
        }
    }
}

impl SourceUnit {
    pub fn class(&self) -> SourceUnitClass {
        SourceUnitClass::from_declared_bindings(&self.declared)
    }
}

impl DependencyGraph {
    /// Iterate `&DependencyEdge` in `DependencyEdgeId` order. Each row is one
    /// reason — multiple reasons between the same `(from, to)` pair
    /// appear as separate entries.
    pub fn iter_edges(&self) -> impl Iterator<Item = &DependencyEdge> + '_ {
        self.edges.iter()
    }

    pub fn node(&self, id: SourceUnitId) -> Option<&SourceUnit> {
        self.nodes.get(id.0).filter(|node| node.id == id)
    }

    pub fn iter_nodes(&self) -> impl Iterator<Item = &SourceUnit> {
        self.nodes.iter()
    }

    /// Total source-unit node count. Callers that need to size a per-source-unit
    /// vector (e.g. assignment slots) should use this
    /// instead of reaching into a private field.
    pub fn num_nodes(&self) -> usize {
        self.nodes.len()
    }

    /// Total dependency edge count.
    pub fn num_edges(&self) -> usize {
        self.edges.len()
    }

    /// Author `commuting_statements` premises applied to this graph.
    pub fn commuting_premises(&self) -> &[AppliedCommutingPremise] {
        &self.commuting_premises
    }

    /// Dependency-edge row by `DependencyEdgeId`. CSR adjacency returns IDs;
    /// callers dereference them through this accessor instead of indexing the
    /// private edge table directly.
    pub fn edge(&self, id: DependencyEdgeId) -> &DependencyEdge {
        &self.edges[id.0]
    }

    /// Edges originating at the source unit.
    pub fn out_edges_of(&self, source_unit: SourceUnitId) -> &[DependencyEdgeId] {
        self.out_edges
            .get(source_unit.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Edges terminating at the source unit.
    pub fn in_edges_of(&self, source_unit: SourceUnitId) -> &[DependencyEdgeId] {
        self.in_edges
            .get(source_unit.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

impl DependencyGraph {
    /// Reconstruct the dependency graph and return the ID lookup used to
    /// resolve its endpoints. The report-backed input retains this lookup
    /// for subsequent source-unit queries.
    ///
    /// Edges retain their endpoints, kinds, promotion roles and commuting
    /// premises. Rows lack hygienic declaration IDs, so the input must
    /// restore [`SourceUnit::declared`] before assignment assembly.
    ///
    /// `DependencyEdgeId`s in the reconstructed graph are assigned in the
    /// order edges appear in `report.edges`; they don't necessarily
    /// match the original `DependencyEdgeId`s that produced the report.
    /// The gate only uses them as opaque identifiers in its evidence
    /// listing.
    ///
    /// Errors with [`GraphReportIdError`] when an edge
    /// references a source unit id missing from the report's node table —
    /// a malformed or version-skewed `dependency_graph.json.gz`. Silently
    /// dropping such edges would hand the planner-side gate a weaker
    /// graph than the one the report described.
    pub(super) fn from_report(
        report: &crate::DependencyGraphReport,
    ) -> Result<(Self, HashMap<String, SourceUnitId>), GraphReportIdError> {
        let mut by_id = HashMap::with_capacity(report.nodes.len());
        let mut nodes = Vec::with_capacity(report.nodes.len());
        for row in &report.nodes {
            let id = SourceUnitId(nodes.len());
            if by_id.insert(row.id.clone(), id).is_some() {
                return Err(GraphReportIdError::DuplicateNode { id: row.id.clone() });
            }
            nodes.push(SourceUnit {
                id,
                statement_ordinal: row.statement_ordinal,
                source_location: row.source_location.clone(),
                declared: BTreeSet::new(),
                kind: row.statement_kind,
                purity: row.purity.clone(),
                callback_factory_barriers: Vec::new(),
                heap_native_integrity_uses: row.heap_native_integrity_uses.clone(),
                synchronous_execution: None,
                callback_factory_uses: Vec::new(),
                returned_record_uses: Vec::new(),
            });
        }

        let resolve = |endpoint: &str, row_id: &str| {
            by_id
                .get(endpoint)
                .copied()
                .ok_or_else(|| GraphReportIdError::MissingEndpoint {
                    edge_id: row_id.to_owned(),
                    endpoint: endpoint.to_owned(),
                })
        };

        let mut edges: Vec<DependencyEdge> = Vec::with_capacity(report.edges.len());
        for edge in &report.edges {
            let from = resolve(&edge.source, &edge.id)?;
            let to = resolve(&edge.target, &edge.id)?;
            // Round-trip the edge role so the planner-side gate runs
            // the same cross-module-promotion filter as the
            // materializer.
            let role = match &edge.role {
                Some(role) => role.resolve(&by_id),
                None => EdgeRole::Direct,
            };
            // Round-trip the inert marker too: `constrains_init_order` reads
            // it, so dropping it would hand the planner a stricter gate than
            // the materializer ran.
            let reason = EdgeReason::synthetic(edge.edge_kind, edge.statement_ordinal, role)
                .with_sequenced_cause(edge.sequenced_cause.clone())
                .with_commuting_premise(edge.commuting_premise.clone());
            let id = DependencyEdgeId(edges.len());
            edges.push(DependencyEdge {
                id,
                from,
                to,
                reason,
            });
        }
        let edge_by_report_id: HashMap<&str, DependencyEdgeId> =
            if report.commuting_statements.is_empty() {
                HashMap::new()
            } else {
                report
                    .edges
                    .iter()
                    .zip(&edges)
                    .map(|(row, edge)| (row.id.as_str(), edge.id))
                    .collect()
            };
        let mut commuting_premises = Vec::with_capacity(report.commuting_statements.len());
        for premise in &report.commuting_statements {
            commuting_premises.push(AppliedCommutingPremise {
                reference: premise.reference.clone(),
                spec_location: premise.spec_location.clone(),
                a: resolve(&premise.a, &premise.spec_location)?,
                b: resolve(&premise.b, &premise.spec_location)?,
                inerted_edges: premise
                    .inerted_edge_ids
                    .iter()
                    .filter_map(|id| edge_by_report_id.get(id.as_str()).copied())
                    .collect(),
                note: premise.note.clone(),
            });
        }

        Ok((Self::from_parts(nodes, edges, commuting_premises), by_id))
    }

    /// Assemble the graph from `nodes` and `edges` (`edges[i].id` is
    /// `DependencyEdgeId(i)`), deriving the CSR adjacency in one pass.
    pub(super) fn from_parts(
        nodes: Vec<SourceUnit>,
        edges: Vec<DependencyEdge>,
        commuting_premises: Vec<AppliedCommutingPremise>,
    ) -> Self {
        let mut out_edges: Vec<Vec<DependencyEdgeId>> = vec![Vec::new(); nodes.len()];
        let mut in_edges: Vec<Vec<DependencyEdgeId>> = vec![Vec::new(); nodes.len()];
        for edge in &edges {
            if let Some(slot) = out_edges.get_mut(edge.from.0) {
                slot.push(edge.id);
            }
            if let Some(slot) = in_edges.get_mut(edge.to.0) {
                slot.push(edge.id);
            }
        }
        Self {
            nodes,
            edges,
            out_edges,
            in_edges,
            commuting_premises,
        }
    }
}

/// A saved graph cannot map a report ID to one source unit.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum GraphReportIdError {
    #[error("dependency graph repeats source-unit ID {id}")]
    DuplicateNode { id: String },
    #[error(
        "dependency-graph edge {edge_id} references source-unit ID {endpoint} which is not in the report's node table (malformed or version-skewed dependency_graph.json.gz)"
    )]
    MissingEndpoint { edge_id: String, endpoint: String },
}
