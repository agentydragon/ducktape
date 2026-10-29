//! Dependency-graph IR: the fine-grained per-statement dependency graph
//! built from chunk facts, its module-level quotient, and the
//! canonical ESM linker/source-import orderings derived from it.
//!
//! Submodules:
//! - [`edge`] — edge types ([`EdgeRole`], [`EdgeReason`], [`DepKind`],
//!   [`DependencyEdge`], [`DependencyEdgeId`]).
//! - [`dependency_graph`] — the [`DependencyGraph`] IR ([`SourceUnitId`],
//!   [`SourceUnit`]), JSON recovery ([`DependencyGraph::from_report`],
//!   [`UnresolvedDependencyEdgeEndpoint`]).
//! - [`build`] — dependency-graph construction ([`build_dependency_graph_with`],
//!   at-init call promotion, the S-chain).
//! - [`quotient`] — module-level projection ([`ModuleQuotient`],
//!   [`EndpointView`], [`assignment_endpoints`], [`project_endpoints`],
//!   [`build_module_quotient`]).
//! - [`linker_order`] — the canonical ESM I-graph
//!   ([`ChunkConstrainingEdgeSet`], [`chunk_constraining_module_edges`])
//!   and its linker / source-import orderings.

mod build;
mod dependency_graph;
mod edge;
mod linker_order;
mod quotient;

#[cfg(test)]
mod tests;

pub use build::{
    DependencyGraphBuildError, DependencyGraphOptions, DuplicateTopLevelDeclaration,
    build_dependency_graph_with,
};
pub use dependency_graph::{
    AppliedCommutingPremise, DependencyGraph, SourceUnit, SourceUnitClass, SourceUnitId,
    UnresolvedDependencyEdgeEndpoint,
};
pub use edge::{
    BindingTimelineOrder, CellConflict, CellEffectClass, DepKind, DependencyEdge, DependencyEdgeId,
    EdgeReason, EdgeRole, EffectCellReport, HeapBarrierCause, SequencedCause,
};
pub use linker_order::{
    ChunkConstrainingEdgeSet, chunk_constraining_module_edges, chunk_linker_order_from_pairs,
    chunk_source_import_order_from_adjacency, position_lookup,
};
pub use quotient::{
    EndpointView, ModuleQuotient, assignment_endpoints, build_module_quotient, project_endpoints,
};
