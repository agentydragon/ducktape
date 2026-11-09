//! Typed readers and assertions for graph-producing E2E fixtures.

pub mod effect_validation;
mod graph_fixture;
pub mod reflective_provider;
pub use graph_fixture::GraphFixture;

use analysis::reports::schema::SourceUnitReport;
use analysis::{DependencyGraphReport, ProgramGraphReport};
use debundle_e2e_support::{Fixture, RejectedFixture, read_gzip_json};
use serde_json::Value;
use std::path::{Path, PathBuf};

/// Typed graph accessors for fixtures that otherwise only need the CLI harness.
pub trait FixtureGraphExt {
    fn dependency_graph(&self) -> DependencyGraphReport;
    fn program_graph(&self) -> ProgramGraphReport;
}

impl FixtureGraphExt for Fixture {
    fn dependency_graph(&self) -> DependencyGraphReport {
        read_dependency_graph(
            &self
                .report_root
                .join(&self.chunk_id)
                .join("dependency_graph.json.gz"),
        )
    }

    fn program_graph(&self) -> ProgramGraphReport {
        read_program_graph(
            &self
                .report_root
                .join(&self.chunk_id)
                .join("dependency_graph.json.gz"),
        )
    }
}

impl FixtureGraphExt for RejectedFixture {
    fn dependency_graph(&self) -> DependencyGraphReport {
        read_dependency_graph(
            &self
                .report_root
                .join(&self.chunk_id)
                .join("dependency_graph.json.gz"),
        )
    }

    fn program_graph(&self) -> ProgramGraphReport {
        read_program_graph(&self.report_path("dependency_graph.json.gz"))
    }
}

fn read_dependency_graph(path: &Path) -> DependencyGraphReport {
    let report: DependencyGraphReport = read_gzip_json(path);
    assert_no_purity_disagreements(&report);
    report
}

fn assert_no_purity_disagreements(report: &DependencyGraphReport) {
    let disagreements: Vec<_> = report
        .nodes
        .iter()
        .filter(|node| {
            node.synchronous_execution
                .as_ref()
                .is_some_and(|execution| execution.purity_disagreement.is_some())
        })
        .map(|node| (&node.id, &node.source_location))
        .collect();
    assert!(
        disagreements.is_empty(),
        "classifier-pure completion regressions count={} chunk={} sites={:?}",
        disagreements.len(),
        report.chunk_id,
        disagreements
    );
}

/// Check that a rendered cycle-edge row names actual graph-edge endpoints and
/// that their source-unit destinations project to the reported module paths.
pub fn assert_cycle_edge_source_units(graph: &DependencyGraphReport, edge: &Value) {
    let from_id = edge["from_source_unit_id"]
        .as_str()
        .expect("cycle edge source endpoint ID");
    let to_id = edge["to_source_unit_id"]
        .as_str()
        .expect("cycle edge target endpoint ID");
    let from_unit = graph
        .nodes
        .iter()
        .find(|node| node.id == from_id)
        .unwrap_or_else(|| panic!("source endpoint {from_id} is not a graph node: {edge}"));
    let to_unit = graph
        .nodes
        .iter()
        .find(|node| node.id == to_id)
        .unwrap_or_else(|| panic!("target endpoint {to_id} is not a graph node: {edge}"));
    let from_module = graph
        .quotient
        .nodes
        .iter()
        .find(|module| module.key == from_unit.destination)
        .expect("source endpoint destination has a module-table entry");
    let to_module = graph
        .quotient
        .nodes
        .iter()
        .find(|module| module.key == to_unit.destination)
        .expect("target endpoint destination has a module-table entry");
    assert_eq!(edge["from"], from_module.path.as_str(), "{edge}");
    assert_eq!(edge["to"], to_module.path.as_str(), "{edge}");

    assert!(
        graph.edges.iter().any(|candidate| {
            candidate.source == from_id
                && candidate.target == to_id
                && serde_json::to_value(candidate.edge_kind).expect("DepKind serializes")
                    == edge["kind"]
                && serde_json::to_value(candidate.statement_ordinal)
                    .expect("SourceUnitOrdinal serializes")
                    == edge["statement_ordinal"]
        }),
        "no dependency-graph edge matches the reported source/target IDs, kind, and reason ordinal: {edge}"
    );
}

/// The `program_graph.json.gz` that the graph at `graph_path` references.
pub fn program_graph_path(graph_path: &Path) -> PathBuf {
    let graph: DependencyGraphReport = read_gzip_json(graph_path);
    graph_provenance::program_graph_path(graph_path, &graph.program_graph)
}

fn read_program_graph(graph_path: &Path) -> ProgramGraphReport {
    read_gzip_json(&program_graph_path(graph_path))
}

/// The dependency-graph node that declares `binding`, panicking (with the node
/// dump) if none does.
pub fn node_declaring<'a>(graph: &'a DependencyGraphReport, binding: &str) -> &'a SourceUnitReport {
    graph
        .nodes
        .iter()
        .find(|node| node.declared_bindings.iter().any(|b| b.binding == binding))
        .unwrap_or_else(|| {
            panic!(
                "no dependency-graph node declares binding `{binding}`; \
                 nodes: {:#?}",
                graph.nodes,
            )
        })
}

/// SourceUnit-graph node id that declares `binding`.
pub fn source_unit_for_binding<'a>(graph: &'a DependencyGraphReport, binding: &str) -> &'a str {
    node_declaring(graph, binding).id.as_str()
}

/// SourceUnit-graph node id at source-unit `ordinal`.
pub fn source_unit_for_ordinal(graph: &DependencyGraphReport, ordinal: usize) -> &str {
    graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == ordinal)
        .unwrap_or_else(|| panic!("no dependency graph node with ordinal {ordinal}"))
        .id
        .as_str()
}

/// SourceUnit-graph node id whose statement starts on source `line`.
pub fn source_unit_for_line(graph: &DependencyGraphReport, line: usize) -> &str {
    graph
        .nodes
        .iter()
        .find(|node| {
            node.source_location
                .as_ref()
                .is_some_and(|location| location.start_line == line)
        })
        .unwrap_or_else(|| panic!("no dependency graph node starts on line {line}"))
        .id
        .as_str()
}

/// The serialized dependency-graph node that declares `binding`, for tests that
/// assert on the report's JSON shape.
pub fn json_node_declaring<'a>(graph: &'a Value, binding: &str) -> &'a Value {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|candidate| candidate["binding"] == binding)
                })
        })
        .unwrap_or_else(|| panic!("no source node for {binding}: {graph}"))
}

/// Every serialized node's `synchronous_execution.<field>` rows, in node order.
pub fn json_execution_rows(graph: &Value, field: &str) -> Vec<Value> {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"][field]
                .as_array()
                .into_iter()
                .flatten()
                .cloned()
        })
        .collect()
}

/// Whether a serialized node's execution lost precision to an unknown call target.
pub fn json_has_unknown_call_target(node: &Value) -> bool {
    node["synchronous_execution"]["precision_losses"]
        .as_array()
        .is_some_and(|losses| {
            losses
                .iter()
                .any(|loss| loss["reason"]["code"] == "unknown_call_target")
        })
}
