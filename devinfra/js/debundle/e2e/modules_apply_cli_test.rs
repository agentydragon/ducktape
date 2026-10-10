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

#[test]
fn applies_host_scoped_accessor_with_a_structural_selector() {
    let fixture = GraphFixture::new(
        "class Cache { constructor(make) { this.make = make; } of(host) { return this.make(host); } }\nfunction context() { return { host: 'foo' }; }\nclass ProviderA { label = 'foo'; }\nvar factoryA = new Cache(() => new ProviderA);\nclass ProviderB { label = 'bar'; }\nvar factoryB = new Cache(() => new ProviderB);\nfunction accessor() { return factoryB.of(context().host); }\nfunction gap() { return null; }\nfunction sibling() { return factoryA.of(context().host); }\nconsole.log(accessor().label);\n",
        &[(
            "existing.yaml",
            "members: [{selector: {binding: {name: Cache}}}, {selector: {binding: {name: context}}}, {selector: {binding: {name: ProviderA}}}, {selector: {binding: {name: factoryA}}}, {selector: {binding: {name: ProviderB}}}, {selector: {binding: {name: factoryB}}}]\n",
        )],
    );
    let report = fixture.json(&["modules", "propose", "--size-cap-lines", "1"]);
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["landable_today"] == true
                && row["binding_ids"]
                    .as_array()
                    .is_some_and(|bindings| bindings.iter().any(|binding| binding == "accessor"))
        })
        .expect("an accessor proposal");
    let request = json!({
        "proposal_id": proposal["proposed_module_id"],
        "owner_ids": proposal["owner_ids"],
        "module": "services/accessor",
        "readable_names": {"accessor": "getProviderForHost"}
    });
    let temp = tempfile::tempdir().unwrap();
    let batch = temp.path().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    fixture.assert_success(&[
        "modules",
        "apply",
        "--batch",
        batch.to_str().unwrap(),
        "--size-cap-lines",
        "1",
    ]);
    let module_file = fixture.modules.join("services/accessor.yaml");
    let yaml = fs::read_to_string(&module_file).unwrap();
    assert!(yaml.contains("source_matches:"), "{yaml}");
    assert!(!yaml.contains("accessor"), "{yaml}");
    assert!(yaml.contains("class ProviderB"), "{yaml}");
    assert!(yaml.contains("var factoryB = ANYTHING"), "{yaml}");
    fixture.assert_runs("bar\n");

    let applied: serde_yaml::Value = serde_yaml::from_str(&yaml).unwrap();
    let applied_match = applied["source_matches"][0]["match"].as_str().unwrap();
    fs::write(
        &module_file,
        "members: [{name: getProviderForHost, selector: {binding: {name: accessor}}}]\n",
    )
    .unwrap();
    let source = fixture.source_path();
    let report = fixture.json(&[
        "spec",
        "synthesize-selectors",
        "--module",
        "services/accessor",
        "--source-file",
        source.to_str().unwrap(),
    ]);
    assert_eq!(report["candidates"][0]["match_source"], applied_match);
}

#[test]
fn explains_when_repeated_accessors_have_no_unique_context() {
    let fixture = GraphFixture::new(
        "function a() { return 1; }\nfunction b() { return 1; }\nfunction c() { return 1; }\nfunction d() { return 1; }\nfunction e() { return 1; }\nfunction f() { return 1; }\nfunction g() { return 1; }\nconsole.log(d());\n",
        &[],
    );
    let report = fixture.json(&["modules", "propose", "--size-cap-lines", "1"]);
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["landable_today"] == true
                && row["binding_ids"]
                    .as_array()
                    .is_some_and(|bindings| bindings.iter().any(|binding| binding == "d"))
        })
        .expect("a repeated accessor proposal");
    let request = json!({
        "proposal_id": proposal["proposed_module_id"],
        "owner_ids": proposal["owner_ids"],
        "module": "services/accessor",
        "readable_names": {"d": "getValue"}
    });
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
        &[
            "no uniquely proven structural selector",
            "exact declaration does not prove",
        ],
    );
}
