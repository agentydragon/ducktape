//! Loaded report metadata paired with the canonical dependency-graph topology.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use source_identity::SourceUnitId;

use super::{DependencyEdgeId, DependencyGraph, GraphReportIdError};
use crate::DependencyGraphReport;

/// Immutable graph input shared by inspection, planning and edit validation.
///
/// The report preserves wire IDs, labels, saved destinations and diagnostic facts.
/// Topology algorithms borrow the same typed graph, including every edge role and
/// commuting premise, instead of reconstructing it for each candidate. This is a
/// report-backed graph: newly read inputs lack the hygienic declarations needed
/// to assemble an assignment from the compiler's AST. Restore those declarations
/// with `with_source_declarations` after verifying source provenance. This does
/// not reconstruct semantic analysis facts.
#[derive(Debug)]
pub struct DependencyGraphInput {
    report: DependencyGraphReport,
    graph: Arc<DependencyGraph>,
    source_units_by_report_id: HashMap<String, SourceUnitId>,
}

impl DependencyGraphInput {
    pub fn read(path: &Path) -> Result<Self> {
        let report = report_io::read_json_report(path)
            .with_context(|| format!("reading full dependency graph {}; export it with debundle run --graphs or the dependency_graphs Bazel output group", path.display()))?;
        Self::from_report(report)
            .with_context(|| format!("reconstructing dependency graph {}", path.display()))
    }

    pub fn from_report(report: DependencyGraphReport) -> Result<Self, GraphReportIdError> {
        let (graph, source_units_by_report_id) = DependencyGraph::from_report(&report)?;
        Ok(Self {
            report,
            graph: Arc::new(graph),
            source_units_by_report_id,
        })
    }

    /// Restore hygienic declaration identities from the source census used to
    /// prepare a selected plan. Report rows do not serialize SWC syntax
    /// contexts, so this joins by the stable source-unit ordinal and verifies
    /// the report's declaration spellings and statement kinds before changing
    /// only each graph node's `declared` set. Callers remain responsible for
    /// proving that `facts` came from the exact source bytes associated with
    /// this report; this method validates layout metadata, not source
    /// provenance or semantic facts.
    pub fn with_source_declarations(
        mut self,
        facts: &[analysis::facts::StructuralStatementFacts],
    ) -> Result<Self> {
        let mut facts_by_ordinal = HashMap::with_capacity(facts.len());
        for fact in facts {
            anyhow::ensure!(
                facts_by_ordinal.insert(fact.ordinal, Some(fact)).is_none(),
                "source declaration census repeats source-unit ordinal {}",
                fact.ordinal
            );
        }

        anyhow::ensure!(
            facts_by_ordinal.len() == self.report.nodes.len(),
            "source declaration census has {} unique rows for {} reported source units",
            facts_by_ordinal.len(),
            self.report.nodes.len()
        );

        let mut declarations_by_report_row = Vec::with_capacity(self.report.nodes.len());
        for node in &self.report.nodes {
            let fact = facts_by_ordinal
                .get_mut(&node.statement_ordinal)
                .with_context(|| {
                    format!(
                        "source declaration census is missing report ordinal {}",
                        node.statement_ordinal
                    )
                })?
                .take()
                .with_context(|| {
                    format!(
                        "dependency graph report repeats source-unit ordinal {}",
                        node.statement_ordinal
                    )
                })?;
            anyhow::ensure!(
                fact.kind == node.statement_kind,
                "source declaration census statement kind differs at ordinal {}",
                node.statement_ordinal
            );

            let mut fact_spellings: Vec<_> = fact.declared.iter().map(|id| id.0.as_ref()).collect();
            let mut report_spellings: Vec<_> = node
                .declared_bindings
                .iter()
                .map(|binding| binding.binding.as_ref())
                .collect();
            fact_spellings.sort_unstable();
            report_spellings.sort_unstable();
            anyhow::ensure!(
                fact_spellings == report_spellings,
                "source declaration census binding spellings differ at ordinal {}",
                node.statement_ordinal
            );
            declarations_by_report_row.push(&fact.declared);
        }

        let graph = std::sync::Arc::make_mut(&mut self.graph);
        anyhow::ensure!(
            graph.nodes.len() == declarations_by_report_row.len(),
            "dependency graph topology has {} nodes for {} report rows",
            graph.nodes.len(),
            declarations_by_report_row.len()
        );
        for (node, declared) in graph.nodes.iter_mut().zip(declarations_by_report_row) {
            node.declared.clone_from(declared);
        }
        Ok(self)
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
