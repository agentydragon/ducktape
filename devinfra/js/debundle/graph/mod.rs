//! Dependency-graph crate, above `analysis`: the fine-grained per-statement
//! dependency graph built from chunk facts, its module-level quotient, the
//! canonical ESM linker/source-import orderings, atomic groups, assignment
//! assembly and the graph wire reports.
//!
//! Modules:
//! - [`edge`] — edge types ([`EdgeRole`], [`EdgeReason`],
//!   [`DependencyEdge`], [`DependencyEdgeId`]).
//! - [`dependency_graph`] — the [`DependencyGraph`] IR and [`SourceUnit`] rows;
//!   their dense IDs are `source_identity::SourceUnitId`. It also provides JSON
//!   recovery ([`DependencyGraphInput`], [`GraphReportIdError`]).
//! - [`build`] — dependency-graph construction ([`build_dependency_graph_with`],
//!   at-init call promotion, the S-chain).
//! - [`quotient`] — module-level edge projection ([`EndpointView`],
//!   [`assignment_endpoints`], [`project_endpoints`]).
//! - [`linker_order`] — the canonical ESM I-graph
//!   ([`ChunkConstrainingEdgeSet`], [`chunk_constraining_module_edges`])
//!   and its linker / source-import orderings.
//! - [`atomic_groups`], [`assignment_assembly`], [`module_assignment`],
//!   [`placement_projection`] — co-location groups and the spec assignment.
//! - [`report`] — serde wire reports of the graph, quotient and atomic groups.

pub mod assignment_assembly;
pub mod atomic_groups;
mod build;
mod commutation_order;
mod dependency_graph;
mod edge;
mod input;
mod linker_order;
pub mod module_assignment;
pub mod placement_projection;
mod quotient;
pub mod report;

#[cfg(test)]
mod analysis_tests;
#[cfg(test)]
mod test_support;
#[cfg(test)]
mod tests;

pub use build::{
    CommutingStatementsDiagnostic, DependencyGraphBuildError, DuplicateTopLevelDeclaration,
    GraphOptions, KnownConflict, PremiseVerdict, build_dependency_graph_with,
};
pub use commutation_order::{CommutationOrder, PreservedOrder};
pub use dependency_graph::{
    AppliedCommutingPremise, DependencyGraph, GraphReportIdError, SourceUnit, SourceUnitClass,
};
pub use edge::{
    BindingTimelineOrder, CellConflict, CellEffectClass, CellOverlapReport, DependencyEdge,
    DependencyEdgeId, EdgeReason, EdgeRole, HeapBarrierCause, SequencedCause,
};
pub use input::DependencyGraphInput;
pub use linker_order::{
    ChunkConstrainingEdgeSet, chunk_constraining_module_edges, chunk_linker_order_from_pairs,
    chunk_source_import_order_from_adjacency, position_lookup,
};
pub use quotient::{EndpointView, assignment_endpoints, project_endpoints};
