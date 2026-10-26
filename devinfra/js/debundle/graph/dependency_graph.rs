use std::collections::{BTreeSet, HashMap};
use std::fmt;

use serde::{Deserialize, Serialize};
use swc_ecma_ast::Id;

use crate::purity::Purity;
use crate::{SourceLocation, StatementKind, StatementOrdinal};

use super::edge::{DependencyEdge, DependencyEdgeId, EdgeReason, EdgeRole};

/// Stable-in-run identity of one dependency-graph source unit. A source unit is
/// one post-comma-list `StatementFacts` row, so it is not necessarily one
/// original AST statement. Original AST statements remain intact for emission.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SourceUnitId(pub usize);

/// Fine-grained dependency graph before logical modules are formed. Nodes are
/// source units from split statement facts; edges are reads between source units
/// and source-order side-effect constraints. The module dependency graph is the
/// quotient of this graph by a [`ModuleAssignment`].
///
/// Storage is **flat-edges + CSR adjacency**, the canonical compiler-IR
/// shape: one [`DependencyEdge`] per reason, indexed by [`DependencyEdgeId`]
/// (= position in `edges`), with per-node `out_edges` / `in_edges`
/// adjacency lists for O(deg) traversal. The previous representation
/// kept two parallel views — a `petgraph::DiGraphMap` for random
/// access by `(from, to)` and a separate `Vec<DependencyEdge>` for
/// stable indices into edges; this collapses them.
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
}

#[derive(Debug, Clone)]
pub struct SourceUnit {
    pub id: SourceUnitId,
    pub statement_ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<Id>,
    pub kind: StatementKind,
    pub purity: Purity,
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
    /// Reconstruct a typed `DependencyGraph` from a JSON-deserialized
    /// `crate::DependencyGraphReport`. Used by the peel planner CLI so the
    /// realizability gate consults the same IR shape the materializer
    /// gate does, instead of re-deriving cycle detection over the
    /// JSON-flattened edge list.
    ///
    /// The result is "gate-grade": it carries edge endpoints,
    /// `DepKind`, the residual marker, and the per-edge `binding` and
    /// per-node `kind` / `purity` the JSON wire shape mirrors. Every
    /// node's [`SourceUnit::declared`] is empty — the report has no
    /// hygienic `Id`s — so the graph cannot feed
    /// [`crate::assignment_assembly::assemble_assignment`].
    ///
    /// `DependencyEdgeId`s in the reconstructed graph are assigned in the
    /// order edges appear in `report.edges`; they don't necessarily
    /// match the original `DependencyEdgeId`s that produced the report.
    /// The gate only uses them as opaque identifiers in its evidence
    /// listing.
    ///
    /// Errors with [`UnresolvedDependencyEdgeEndpoint`] when an edge
    /// references a source unit id missing from the report's node table —
    /// a malformed or version-skewed `dependency_graph.json`. Silently
    /// dropping such edges would hand the planner-side gate a weaker
    /// graph than the one the report described.
    pub fn from_report(
        report: &crate::DependencyGraphReport,
    ) -> Result<Self, UnresolvedDependencyEdgeEndpoint> {
        let by_id: HashMap<String, SourceUnitId> = report
            .nodes
            .iter()
            .enumerate()
            .map(|(i, n)| (n.id.clone(), SourceUnitId(i)))
            .collect();

        let nodes: Vec<SourceUnit> = report
            .nodes
            .iter()
            .enumerate()
            .map(|(i, n)| SourceUnit {
                id: SourceUnitId(i),
                statement_ordinal: n.statement_ordinal,
                source_location: n.source_location.clone(),
                declared: BTreeSet::new(),
                kind: n.statement_kind,
                purity: n.purity.clone(),
            })
            .collect();

        let mut edges: Vec<DependencyEdge> = Vec::with_capacity(report.edges.len());
        for edge in &report.edges {
            let resolve = |endpoint: &String| {
                by_id
                    .get(endpoint)
                    .copied()
                    .ok_or_else(|| UnresolvedDependencyEdgeEndpoint {
                        edge_id: edge.id.clone(),
                        endpoint: endpoint.clone(),
                    })
            };
            let from = resolve(&edge.source)?;
            let to = resolve(&edge.target)?;
            // Round-trip the edge role so the planner-side gate runs
            // the same cross-module-promotion filter as the
            // materializer.
            let role = match &edge.role {
                Some(role) => role.resolve(&by_id),
                None => EdgeRole::Direct,
            };
            let reason = EdgeReason::synthetic(edge.edge_kind, edge.statement_ordinal, role);
            let id = DependencyEdgeId(edges.len());
            edges.push(DependencyEdge {
                id,
                from,
                to,
                reason,
            });
        }

        Ok(Self::from_parts(nodes, edges))
    }

    /// Assemble the graph from `nodes` and `edges` (`edges[i].id` is
    /// `DependencyEdgeId(i)`), deriving the CSR adjacency in one pass.
    pub(super) fn from_parts(nodes: Vec<SourceUnit>, edges: Vec<DependencyEdge>) -> Self {
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
        }
    }
}

/// An `dependency_graph.json` edge references a source unit id absent from the
/// report's node table. See [`DependencyGraph::from_report`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedDependencyEdgeEndpoint {
    pub edge_id: String,
    /// The source unit id (e.g. `unit:42`) that didn't resolve.
    pub endpoint: String,
}

impl fmt::Display for UnresolvedDependencyEdgeEndpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "dependency-graph edge {} references source-unit ID {} which is not in the report's node table \
             (malformed or version-skewed dependency_graph.json)",
            self.edge_id, self.endpoint,
        )
    }
}

impl std::error::Error for UnresolvedDependencyEdgeEndpoint {}
