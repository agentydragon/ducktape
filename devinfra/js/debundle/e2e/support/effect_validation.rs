//! Effect translation validation fixtures and receipt assertions.

use crate::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use serde_json::Value;

pub fn native_constructor_input(
    source: &str,
    declaration: &str,
    binding: &str,
    operation: NativeOperationKind,
) -> NativeOperationInputContract {
    NativeOperationInputContract {
        selector: SourceUnitSelector::SourceMatch(source_match_selector(
            declaration.trim(),
            &[binding],
        )),
        operation,
        audited_source_sha256: source_sha256(source),
        note: Some("Neutral fixture audits one direct empty native constructor.".to_string()),
    }
}

pub fn binding_execution_report(fixture: &Fixture, binding: &str) -> Value {
    let graph = fixture.dependency_graph();
    let unit = source_unit_for_binding(&graph, binding);
    let node = graph.nodes.iter().find(|node| node.id == unit).unwrap();
    serde_json::to_value(
        node.synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .expect("serialize execution report")
}

pub fn assert_inline_operation_receipt(
    fixture: &Fixture,
    binding: &str,
    expected_calls: usize,
    case: &str,
) {
    let execution = binding_execution_report(fixture, binding);
    assert_eq!(execution["complete"], true, "{case}: {execution:#}");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(
        authority["authority"], "operation_receipt",
        "{case}: {execution:#}"
    );
    let receipt = &authority["receipt"];
    assert_eq!(receipt["binding"], binding, "{case}: {receipt:#}");
    assert_eq!(
        receipt["calls"].as_array().unwrap().len(),
        expected_calls,
        "{case}: {receipt:#}"
    );
}

pub fn examples(report: &Value) -> &[Value] {
    report["examples"]
        .as_array()
        .expect("effect conflict examples")
}
