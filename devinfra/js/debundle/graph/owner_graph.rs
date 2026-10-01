use std::collections::{BTreeSet, HashMap};
use std::fmt;

use serde::{Deserialize, Serialize};
use swc_ecma_ast::Id;

use crate::purity::Purity;
use crate::{SourceLocation, StatementKind, StatementOrdinal};

use super::edge::{EdgeReason, EdgeRole, OwnerEdge, OwnerEdgeId};

/// Stable-in-run identity of an owner graph vertex. V1 owner
/// vertices are post-comma-list `StatementFacts` rows, so the id
/// is the row's source-order ordinal.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct OwnerId(pub usize);

/// Fine-grained graph before logical modules are formed. Nodes are
/// top-level owners/statements; edges are owner-level reads and
/// source-order side-effect constraints. The module dependency graph
/// is the quotient of this graph by a [`Partition`].
///
/// Storage is **flat-edges + CSR adjacency**, the canonical compiler-IR
/// shape: one [`OwnerEdge`] per reason, indexed by [`OwnerEdgeId`]
/// (= position in `edges`), with per-node `out_edges` / `in_edges`
/// adjacency lists for O(deg) traversal. The previous representation
/// kept two parallel views — a `petgraph::DiGraphMap` for random
/// access by `(from, to)` and a separate `Vec<OwnerEdge>` for
/// stable indices into edges; this collapses them.
///
/// [`Partition`]: crate::partition::Partition
#[derive(Debug, Clone, Default)]
pub struct OwnerGraph {
    pub(crate) nodes: Vec<OwnerNode>,
    pub(crate) edges: Vec<OwnerEdge>,
    /// CSR adjacency by source owner. `out_edges[owner.0]` is a list
    /// of `OwnerEdgeId` indices into `edges`.
    pub(crate) out_edges: Vec<Vec<OwnerEdgeId>>,
    /// CSR adjacency by target owner.
    pub(crate) in_edges: Vec<Vec<OwnerEdgeId>>,
}

#[derive(Debug, Clone)]
pub struct OwnerNode {
    pub id: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<Id>,
    pub kind: StatementKind,
    pub purity: Purity,
}

impl OwnerGraph {
    /// Iterate `&OwnerEdge` in `OwnerEdgeId` order. Each row is one
    /// reason — multiple reasons between the same `(from, to)` pair
    /// appear as separate entries.
    pub fn iter_edges(&self) -> impl Iterator<Item = &OwnerEdge> + '_ {
        self.edges.iter()
    }

    pub fn node(&self, id: OwnerId) -> Option<&OwnerNode> {
        self.nodes.get(id.0).filter(|node| node.id == id)
    }

    pub fn iter_nodes(&self) -> impl Iterator<Item = &OwnerNode> {
        self.nodes.iter()
    }

    /// Total owner-node count. Callers that need to size a per-owner
    /// vector (e.g. partition slots, unit assignments) should use this
    /// instead of reaching into a private field.
    pub fn num_nodes(&self) -> usize {
        self.nodes.len()
    }

    /// Total owner-edge count.
    pub fn num_edges(&self) -> usize {
        self.edges.len()
    }

    /// Owner-edge row by `OwnerEdgeId`. The CSR adjacency the graph
    /// exposes (`out_edges_of` / `in_edges_of`) returns ids; callers dereference those ids back to rows
    /// through this accessor instead of indexing the private edge
    /// table directly.
    pub fn edge(&self, id: OwnerEdgeId) -> &OwnerEdge {
        &self.edges[id.0]
    }

    /// Edges originating at `owner`.
    pub fn out_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.out_edges
            .get(owner.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Edges terminating at `owner`.
    pub fn in_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.in_edges.get(owner.0).map(Vec::as_slice).unwrap_or(&[])
    }
}

impl OwnerGraph {
    /// Reconstruct a typed `OwnerGraph` from a JSON-deserialized
    /// `crate::OwnerGraphReport`. Used by the peel planner CLI so the
    /// realizability gate consults the same IR shape the materializer
    /// gate does, instead of re-deriving cycle detection over the
    /// JSON-flattened edge list.
    ///
    /// The result is "gate-grade": it carries edge endpoints,
    /// `DepKind`, the residual marker, and the per-edge `binding` and
    /// per-node `kind` / `purity` the JSON wire shape mirrors. Every
    /// node's [`OwnerNode::declared`] is empty — the report has no
    /// hygienic `Id`s — so the graph cannot feed
    /// [`crate::factor_assembly::assemble_partition`].
    ///
    /// `OwnerEdgeId`s in the reconstructed graph are assigned in the
    /// order edges appear in `report.edges`; they don't necessarily
    /// match the original `OwnerEdgeId`s that produced the report.
    /// The gate only uses them as opaque identifiers in its evidence
    /// listing.
    ///
    /// Errors with [`UnresolvedOwnerEdgeEndpoint`] when an edge
    /// references an owner id missing from the report's node table —
    /// a malformed or version-skewed `owner_graph.json`. Silently
    /// dropping such edges would hand the planner-side gate a weaker
    /// graph than the one the report described.
    pub fn from_report(
        report: &crate::OwnerGraphReport,
    ) -> Result<Self, UnresolvedOwnerEdgeEndpoint> {
        let by_id: HashMap<String, OwnerId> = report
            .nodes
            .iter()
            .enumerate()
            .map(|(i, n)| (n.id.clone(), OwnerId(i)))
            .collect();

        let nodes: Vec<OwnerNode> = report
            .nodes
            .iter()
            .enumerate()
            .map(|(i, n)| OwnerNode {
                id: OwnerId(i),
                statement_ordinal: n.statement_ordinal,
                source_location: n.source_location.clone(),
                declared: BTreeSet::new(),
                kind: n.statement_kind,
                purity: n.purity.clone(),
            })
            .collect();

        let mut edges: Vec<OwnerEdge> = Vec::with_capacity(report.edges.len());
        for edge in &report.edges {
            let resolve = |endpoint: &String| {
                by_id
                    .get(endpoint)
                    .copied()
                    .ok_or_else(|| UnresolvedOwnerEdgeEndpoint {
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
            let id = OwnerEdgeId(edges.len());
            edges.push(OwnerEdge {
                id,
                from,
                to,
                reason,
            });
        }

        Ok(Self::from_parts(nodes, edges))
    }

    /// Assemble the graph from `nodes` and `edges` (`edges[i].id` is
    /// `OwnerEdgeId(i)`), deriving the CSR adjacency in one pass.
    pub(super) fn from_parts(nodes: Vec<OwnerNode>, edges: Vec<OwnerEdge>) -> Self {
        let mut out_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
        let mut in_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
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

/// An `owner_graph.json` edge references an owner id absent from the
/// report's node table. See [`OwnerGraph::from_report`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedOwnerEdgeEndpoint {
    pub edge_id: String,
    /// The owner id (e.g. `owner:42`) that didn't resolve.
    pub endpoint: String,
}

impl fmt::Display for UnresolvedOwnerEdgeEndpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "owner graph edge {} references owner {} which is not in the report's node table \
             (malformed or version-skewed owner_graph.json)",
            self.edge_id, self.endpoint,
        )
    }
}

impl std::error::Error for UnresolvedOwnerEdgeEndpoint {}
