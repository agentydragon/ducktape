//! Loaded report metadata paired with the canonical dependency-graph topology.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use source_identity::SourceUnitId;

use super::{DependencyEdgeId, DependencyGraph, UnresolvedDependencyEdgeEndpoint};
use crate::DependencyGraphReport;

/// Immutable graph input shared by inspection, planning and edit validation.
///
/// The report preserves wire IDs, labels, saved destinations and diagnostic facts.
/// Topology algorithms borrow the same typed graph, including every edge role and
/// commuting premise, instead of reconstructing it for each candidate. This is a
/// report-backed graph: it does not contain the hygienic bindings and analysis
/// facts needed to assemble an assignment from the compiler's AST.
#[derive(Debug)]
pub struct DependencyGraphInput {
    report: DependencyGraphReport,
    graph: Arc<DependencyGraph>,
    source_units_by_report_id: HashMap<String, SourceUnitId>,
}

impl DependencyGraphInput {
    pub fn read(path: &Path) -> Result<Self> {
        let report = report_io::read_json_gzip(path)
            .with_context(|| format!("reading full dependency graph {}; export it with debundle run --graphs or the dependency_graphs Bazel output group", path.display()))?;
        Self::from_report(report)
            .with_context(|| format!("reconstructing dependency graph {}", path.display()))
    }

    pub fn from_report(
        report: DependencyGraphReport,
    ) -> Result<Self, UnresolvedDependencyEdgeEndpoint> {
        let graph = Arc::new(DependencyGraph::from_report(&report)?);
        let source_units_by_report_id = report
            .nodes
            .iter()
            .enumerate()
            .map(|(index, node)| (node.id.clone(), SourceUnitId(index)))
            .collect();
        Ok(Self {
            report,
            graph,
            source_units_by_report_id,
        })
    }

    pub fn report(&self) -> &DependencyGraphReport {
        &self.report
    }

    pub fn graph(&self) -> &DependencyGraph {
        &self.graph
    }

    /// Quotient working states share the immutable topology while retaining
    /// their own mutable assignments and incremental indexes.
    pub fn shared_graph(&self) -> Arc<DependencyGraph> {
        Arc::clone(&self.graph)
    }

    /// Typed IDs are assigned in report row order when the input is prepared.
    pub fn source_unit_report(&self, id: SourceUnitId) -> &crate::SourceUnitReport {
        &self.report.nodes[id.0]
    }

    pub fn edge_report(&self, id: DependencyEdgeId) -> &crate::DependencyEdgeReport {
        &self.report.edges[id.0]
    }

    pub fn source_unit_id(&self, report_id: &str) -> Option<SourceUnitId> {
        self.source_units_by_report_id.get(report_id).copied()
    }
}
