//! Owner graph IR: the fine-grained per-statement dependency graph
//! built from chunk facts, its module-level quotient, and the
//! canonical ESM linker/source-import orderings derived from it.
//!
//! Submodules:
//! - [`edge`] — edge types ([`EdgeRole`], [`EdgeReason`], [`DepKind`],
//!   [`OwnerEdge`], [`OwnerEdgeId`], [`EdgeMetadata`]).
//! - [`owner_graph`] — the [`OwnerGraph`] IR ([`OwnerId`],
//!   [`OwnerNode`]), JSON recovery ([`OwnerGraph::from_report`],
//!   [`OwnerReportIndex`], [`UnresolvedOwnerEdgeEndpoint`]).
//! - [`build`] — owner-graph construction ([`build_owner_graph`],
//!   [`build_owner_graph_with`], at-init call promotion, the S-chain).
//! - [`quotient`] — module-level projection ([`ModuleQuotient`],
//!   [`EndpointView`], [`partition_endpoints`],
//!   [`build_module_quotient`]).
//! - [`linker_order`] — the canonical ESM I-graph
//!   ([`ChunkConstrainingEdgeSet`], [`chunk_constraining_module_edges`])
//!   and its linker / source-import orderings.

mod build;
mod edge;
mod linker_order;
mod owner_graph;
mod quotient;

#[cfg(test)]
mod tests;

pub use build::{
    DuplicateTopLevelDeclaration, OwnerGraphOptions, build_owner_graph, build_owner_graph_with,
};
pub use edge::{DepKind, EdgeMetadata, EdgeReason, EdgeRole, OwnerEdge, OwnerEdgeId};
pub use linker_order::{
    ChunkConstrainingEdgeSet, chunk_constraining_module_edges, chunk_linker_order,
    chunk_linker_order_from_pairs, chunk_source_import_order,
    chunk_source_import_order_from_adjacency, position_lookup,
};
pub use owner_graph::{
    OwnerGraph, OwnerId, OwnerNode, OwnerReportIndex, UnresolvedOwnerEdgeEndpoint,
};
pub use quotient::{EndpointView, ModuleQuotient, build_module_quotient, partition_endpoints};
