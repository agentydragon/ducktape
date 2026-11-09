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

/// The xfail form of a positive: a split whose Node parity the caller has
/// already asserted, but whose root binding the effect verdict cannot certify
/// yet. Today's refusal must be the typed `unsupported_operation` loss
/// `operation`; once the kernel covers `capability`, this fails so the test
/// is converted into a positive.
pub fn assert_effect_coverage_pending(
    fixture: &Fixture,
    binding: &str,
    operation: &str,
    capability: &str,
) {
    let execution = binding_execution_report(fixture, binding);
    let authority = &execution["binding_effect_authority"]["authority"];
    assert_ne!(
        authority, "operation_receipt",
        "capability landed ({capability}): convert this xfail into a positive: {execution:#}"
    );
    let losses = execution["precision_losses"]
        .as_array()
        .expect("precision losses");
    assert!(
        losses.iter().any(|loss| {
            loss["reason"]["code"] == "unsupported_operation"
                && loss["reason"]["operation"] == operation
        }),
        "{capability}: today's refusal must be the typed {operation} loss: {execution:#}"
    );
}

pub fn examples(report: &Value) -> &[Value] {
    report["examples"]
        .as_array()
        .expect("effect conflict examples")
}
