use std::collections::HashMap;
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
    /// CSR-style "edges referencing this owner as their at-init
    /// callee", indexed by owner index. Empty for owners that no edge
    /// references via [`EdgeRole::PromotedAtInit`]. Lets
    /// `impacted_owner_edges` look up callee-referencing edges in
    /// `O(|edges of that callee|)` instead of scanning the full edge
    /// list per call (a `verdict_with_overlay_touching` per-candidate
    /// hot path on gaffer-scale inputs).
    pub(crate) callee_edges: Vec<Vec<OwnerEdgeId>>,
}

#[derive(Debug, Clone)]
pub struct OwnerNode {
    pub id: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: std::collections::BTreeSet<Id>,
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
    /// exposes (`out_edges_of` / `in_edges_of` / `callee_edges_of`)
    /// returns ids; callers dereference those ids back to rows
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

    /// Edges referencing `owner` as their at-init callee. Mirrors
    /// `out_edges_of`/`in_edges_of` but for the callee-owner index.
    pub fn callee_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.callee_edges
            .get(owner.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

/// Recovery handle that maps the JSON `OwnerGraphReport` owner-id
/// strings to the `OwnerId`s of an `OwnerGraph` built via
/// `OwnerGraph::from_report`. The position of an owner-id in
/// `OwnerGraphReport.nodes` equals the constructed `OwnerId.0`, so the
/// index lookup is just a position scan; this struct keeps the lookup
/// O(1) via an interned `HashMap`.
#[derive(Debug, Clone)]
pub struct OwnerReportIndex {
    pub owner_ids: Vec<String>,
    by_id: HashMap<String, OwnerId>,
}

impl OwnerReportIndex {
    pub fn lookup(&self, id: &str) -> Option<OwnerId> {
        self.by_id.get(id).copied()
    }

    pub fn id_of(&self, owner: OwnerId) -> Option<&str> {
        self.owner_ids.get(owner.0).map(String::as_str)
    }
}

impl OwnerGraph {
    /// Reconstruct a typed `OwnerGraph` from a JSON-deserialized
    /// `crate::OwnerGraphReport`. Used by the peel planner CLI so the
    /// realizability gate consults the same IR shape the materializer
    /// gate does, instead of re-deriving cycle detection over the
    /// JSON-flattened edge list.
    ///
    /// `facts` is the per-statement fact slice from a
    /// `ChunkFactsReport` (see `facts/wire.rs`); when supplied, the
    /// reconstructed nodes' [`OwnerNode::declared`] sets are
    /// populated by joining each node's `statement_ordinal` against
    /// the matching `StatementFactsReport.declared`. Pass `&[]` to
    /// opt out — `declared` stays empty, which is appropriate for
    /// gate-only consumers that never call
    /// [`crate::factor_assembly::assemble_partition`] on the
    /// reconstructed graph.
    ///
    /// The result is "gate-grade": the returned graph carries enough
    /// information for `check_realizability` (edge endpoints,
    /// `DepKind`, residual marker) and — with `facts` supplied — the
    /// per-owner declared-binding set
    /// `factor_assembly::compute_owner_claims` walks. Per-edge
    /// `binding` and per-node `kind` / `purity` mirror the JSON wire
    /// shape; the hygienic `SyntaxContext` carried by `IdReport` is
    /// only meaningful within a single SWC `Globals` scope, so
    /// reconstructed `Id`s round-trip *within one process* but
    /// **must not** be compared against re-parsed AST identifiers
    /// from a different `Globals` (see `stage_one_sidecars.rs` →
    /// "`facts.json` is debug-only").
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
        facts: &[crate::StatementFactsReport],
    ) -> Result<(Self, OwnerReportIndex), UnresolvedOwnerEdgeEndpoint> {
        let owner_ids: Vec<String> = report.nodes.iter().map(|n| n.id.clone()).collect();
        let by_id: HashMap<String, OwnerId> = owner_ids
            .iter()
            .enumerate()
            .map(|(i, id)| (id.clone(), OwnerId(i)))
            .collect();

        // Join key: a statement's ordinal is its identity across both
        // wire shapes — `OwnerGraphNodeReport.statement_ordinal` and
        // `StatementFactsReport.ordinal`. Build a lookup table once
        // so the per-node hydration below is O(1) per node.
        let declared_by_ordinal: HashMap<StatementOrdinal, &Vec<crate::IdReport>> =
            facts.iter().map(|f| (f.ordinal, &f.declared)).collect();

        let nodes: Vec<OwnerNode> = report
            .nodes
            .iter()
            .enumerate()
            .map(|(i, n)| OwnerNode {
                id: OwnerId(i),
                statement_ordinal: n.statement_ordinal,
                source_location: n.source_location.clone(),
                declared: declared_by_ordinal
                    .get(&n.statement_ordinal)
                    .map(|ids| ids.iter().map(crate::IdReport::to_id).collect())
                    .unwrap_or_default(),
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

        let mut out_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
        let mut in_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
        let mut callee_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
        for edge in &edges {
            if let Some(slot) = out_edges.get_mut(edge.from.0) {
                slot.push(edge.id);
            }
            if let Some(slot) = in_edges.get_mut(edge.to.0) {
                slot.push(edge.id);
            }
            if let Some(callee) = edge.reason.role.promoted_callee()
                && let Some(slot) = callee_edges.get_mut(callee.0)
            {
                slot.push(edge.id);
            }
        }

        let graph = OwnerGraph {
            nodes,
            edges,
            out_edges,
            in_edges,
            callee_edges,
        };
        let index = OwnerReportIndex { owner_ids, by_id };
        Ok((graph, index))
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
