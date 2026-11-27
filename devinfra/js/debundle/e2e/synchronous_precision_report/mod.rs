//! Report evidence comes from the same bounded execution used by the gate.

mod capture_precision;
mod construction_reach;
mod execution_bounds;
mod native_prefix;
mod own_data_reads;

use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring, source_unit_for_binding};
use debundle_e2e_support::*;
use graph::report::DependencyGraphReport;
use serde_json::Value;

fn execution(fixture: &Fixture, binding: &str) -> Value {
    let graph = fixture.dependency_graph();
    execution_from_graph(&graph, binding)
}

fn execution_from_graph(graph: &DependencyGraphReport, binding: &str) -> Value {
    let node = node_declaring(graph, binding);
    serde_json::to_value(node).expect("serialize node")["synchronous_execution"].clone()
}

fn assert_reason(execution: &Value, expected: Value) -> &Value {
    assert_eq!(execution["complete"], false, "{execution}");
    execution["precision_losses"]
        .as_array()
        .expect("losses")
        .iter()
        .find(|loss| loss["reason"] == expected)
        .unwrap_or_else(|| panic!("missing {expected} in {execution}"))
}

#[track_caller]
fn assert_init_order_edge(graph: &DependencyGraphReport, from: &str, to: &str) {
    let (source, target) = (
        source_unit_for_binding(graph, from),
        source_unit_for_binding(graph, to),
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == source && edge.target == target && edge.constrains_init_order
        }),
        "no init-order edge from `{from}` to `{to}`"
    );
}

fn global_reach(cause: &str) -> Value {
    serde_json::json!({"kind": "global", "cause": cause})
}
