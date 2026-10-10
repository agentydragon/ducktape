//! Fresh residual claims from current module proposals.

use debundle_e2e_support::GraphFixture;
use serde_json::json;
use std::fs;

fn provider_fixture() -> (GraphFixture, serde_json::Value) {
    let fixture = GraphFixture::new(
        "const provider = { value: 4 };\nconsole.log(provider.value);",
        &[],
    );
    let report = fixture.json(&["modules", "propose", "--size-cap-lines", "1"]);
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["landable_today"] == true
                && row["anonymous_statement_owner_ids"]
                    .as_array()
                    .is_none_or(Vec::is_empty)
                && row["binding_ids"]
                    .as_array()
                    .is_some_and(|bindings| bindings.iter().any(|binding| binding == "provider"))
        })
        .expect("a graph-ready residual provider proposal");
    let request = json!({
        "proposal_id": proposal["proposed_module_id"],
        "owner_ids": proposal["owner_ids"],
        "module": "services/provider",
        "readable_names": {"provider": "Provider"}
    });
    (fixture, request)
}

#[test]
fn applies_fresh_proposal_with_a_structural_selector() {
    let (fixture, request) = provider_fixture();
    let temp = tempfile::tempdir().unwrap();
    let batch = temp.path().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let args = [
        "modules",
        "apply",
        "--batch",
        batch.to_str().unwrap(),
        "--size-cap-lines",
        "1",
    ];
    fixture.assert_success(&[&args[..], &["--dry-run"]].concat());
    assert!(!fixture.modules.join("services/provider.yaml").exists());
    fixture.assert_success(&args);
    let yaml = fs::read_to_string(fixture.modules.join("services/provider.yaml")).unwrap();
    assert!(yaml.contains("source_matches:"), "{yaml}");
    assert!(!yaml.contains("provider"), "{yaml}");
    fixture.assert_runs("4\n");
}

#[test]
fn rejects_stale_selection_without_writing_any_claim() {
    let (fixture, mut request) = provider_fixture();
    request["owner_ids"] = json!(["stale-owner"]);
    let temp = tempfile::tempdir().unwrap();
    let batch = temp.path().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    fixture.assert_rejected_unchanged(
        &[
            "modules",
            "apply",
            "--batch",
            batch.to_str().unwrap(),
            "--size-cap-lines",
            "1",
        ],
        &["owner IDs changed"],
    );
}

#[test]
fn rejects_source_that_no_longer_has_the_reviewed_binding() {
    let (fixture, request) = provider_fixture();
    let temp = tempfile::tempdir().unwrap();
    let batch = temp.path().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    fs::write(fixture.source_path(), "const other = { value: 4 };\n").unwrap();
    fixture.assert_rejected_unchanged(
        &[
            "modules",
            "apply",
            "--batch",
            batch.to_str().unwrap(),
            "--size-cap-lines",
            "1",
        ],
        &["needs exactly one source declaration"],
    );
}

#[test]
fn assign_points_fresh_residual_claims_to_apply() {
    let (fixture, _) = provider_fixture();
    fixture.assert_rejected_unchanged(
        &["bindings", "assign", "provider:services/provider"],
        &["modules apply"],
    );
}
