//! Effect translation validation fixtures and receipt assertions.

use crate::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;
use graph::report::SourceUnitReport;
use serde_json::Value;

/// Serialize a graph node's execution evidence for assertions on report JSON.
pub fn execution_report(node: &SourceUnitReport) -> Value {
    serde_json::to_value(
        node.synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .expect("serialize execution report")
}

pub fn native_constructor_input(
    source: &str,
    declaration: &str,
    binding: &str,
    operation: NativeOperationKind,
) -> NativeOperationInputContract {
    native_operation_input(
        source,
        declared_source_unit(declaration.trim(), binding),
        operation,
        "Neutral fixture audits one direct empty native constructor.",
    )
}

pub fn binding_execution_report(fixture: &Fixture, binding: &str) -> Value {
    let graph = fixture.dependency_graph();
    execution_report(node_declaring(&graph, binding))
}

pub fn assert_inline_operation_receipt(
    fixture: &Fixture,
    binding: &str,
    expected_calls: usize,
    case: &str,
) -> Value {
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
    execution
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
) -> Value {
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
    execution
}

pub fn examples(report: &Value) -> &[Value] {
    report["examples"]
        .as_array()
        .expect("effect conflict examples")
}

/// The per-chunk `candidate_relocation_assessment.json` of an accepted run.
pub fn candidate_relocation_assessment(fixture: &Fixture) -> Value {
    read_json(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("candidate_relocation_assessment.json"),
    )
}

/// `assert_effect_verdict_accepts`, with the inversions accepted as
/// statement-order commutation.
pub fn assert_commuting_relocation(fixture: &Fixture, case: &str) -> Value {
    let report = assert_effect_verdict_accepts(fixture, case);
    assert_commutation_accepted(&report, case);
    report
}

/// The effect report accepts its inversions as statement-order commutation.
pub fn assert_commutation_accepted(report: &Value, case: &str) {
    assert_eq!(
        report["decision"]["status"], "accepted",
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{case}: {report:#}"
    );
}

/// A relocation the legacy gate accepted and the effect verdict proved: the
/// candidate swaps at least one pair and the assessment accepts within its
/// checked scope.
pub fn assert_effect_verdict_accepts(fixture: &Fixture, case: &str) -> Value {
    let (assessment, report) = assert_accepted_relocation(fixture, case);
    assert_eq!(
        assessment["status"], "accepted_within_checked_scope",
        "{case}: {assessment:#}"
    );
    report
}

/// A valid split (Node parity holds, the legacy gate accepts a relocation)
/// the effect verdict cannot prove yet: the assessment must carry `pending`,
/// the typed status of today's refusal. Once the verdict accepts, this fails
/// so the caller converts the case to `assert_effect_verdict_accepts`.
pub fn assert_effect_verdict_pending(fixture: &Fixture, pending: &str, case: &str) -> Value {
    let (assessment, report) = assert_accepted_relocation(fixture, case);
    assert_ne!(
        assessment["status"], "accepted_within_checked_scope",
        "{case}: capability landed, convert to a positive: {assessment:#}"
    );
    assert_eq!(assessment["status"], pending, "{case}: {assessment:#}");
    report
}

fn assert_accepted_relocation(fixture: &Fixture, case: &str) -> (Value, Value) {
    let assessment = candidate_relocation_assessment(fixture);
    assert_eq!(
        assessment["legacy_gate_accepts"], true,
        "{case}: {assessment:#}"
    );
    let report = fixture.effect_translation_validation();
    assert!(
        report["swapped_pairs"]
            .as_u64()
            .is_some_and(|pairs| pairs > 0),
        "{case}: the candidate relocates nothing: {report:#}"
    );
    (assessment, report)
}
