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
    let dry_run = fixture.json(&[&args[..], &["--dry-run"]].concat());
    assert_eq!(dry_run["selector_reviews"][0]["kind"], "source_match");
    assert_eq!(dry_run["selector_reviews"][0]["origin"], "synthesized");
    assert_eq!(
        dry_run["selector_reviews"][0]["binding_ids"],
        json!(["provider"])
    );
    assert!(dry_run["selector_reviews"][0]["claim"]["match"].is_string());
    assert!(
        dry_run["selector_reviews"][0]["review_warnings"]
            .as_array()
            .is_some_and(|warnings| !warnings.is_empty())
    );
    let text = fixture.command(&[&args[..], &["--dry-run", "--format", "text"]].concat());
    assert!(text.status.success(), "{text:?}");
    let text = String::from_utf8_lossy(&text.stdout);
    assert!(text.contains("source_match"), "{text}");
    assert!(text.contains("review_warnings"), "{text}");
    assert!(!fixture.modules.join("services/provider.yaml").exists());
    fixture.assert_success(&args);
    let yaml = fs::read_to_string(fixture.modules.join("services/provider.yaml")).unwrap();
    assert!(yaml.contains("source_matches:"), "{yaml}");
    assert!(!yaml.contains("provider"), "{yaml}");
    fixture.assert_runs("4\n");
}

#[test]
fn synthesizes_multideclarator_template_with_trailing_spaces() {
    // The spaces before the newline are part of the template literal value.
    let source = format!(
        "var a = () => `foo{}\nbar`, b = 1;\nconsole.log(a());\n",
        "  "
    );
    let fixture = GraphFixture::new(&source, &[]);
    let report = fixture.json(&["modules", "propose", "--size-cap-lines", "2"]);
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["binding_ids"]
                .as_array()
                .is_some_and(|bindings| bindings.iter().any(|binding| binding == "a"))
        })
        .expect("a proposal for the template-valued binding");
    let request = json!({
        "proposal_id": proposal["proposed_module_id"],
        "owner_ids": proposal["owner_ids"],
        "module": "foo/bar",
        "readable_names": {"a": "foo"}
    });
    let temp = tempfile::tempdir().unwrap();
    let batch = temp.path().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let args = [
        "modules",
        "apply",
        "--batch",
        batch.to_str().unwrap(),
        "--size-cap-lines",
        "2",
    ];
    let preview = fixture.command(&[&args[..], &["--dry-run", "--format", "json"]].concat());
    assert!(
        preview.status.success(),
        "{}",
        String::from_utf8_lossy(&preview.stderr)
    );
    let preview: serde_json::Value = serde_json::from_slice(&preview.stdout).unwrap();
    assert_eq!(preview["selector_reviews"][0]["origin"], "synthesized");
    assert!(
        preview["selector_reviews"][0]["claim"]["match"]
            .as_str()
            .unwrap()
            .contains("foo  \n")
    );
    fixture.assert_success(&args);
    fixture.assert_runs("foo  \nbar\n");
}

#[test]
fn applies_reviewed_source_match_instead_of_synthesized_one() {
    let (fixture, mut request) = provider_fixture();
    request["selectors"] = json!({
        "provider": {
            "kind": "source_match",
            "selector": {
                "match": "const Target = { value: 4 };",
                "bindings": [{"local": "Target", "name": "Provider"}]
            }
        }
    });
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
    let preview = fixture.json(&[&args[..], &["--dry-run"]].concat());
    assert_eq!(preview["selector_reviews"][0]["origin"], "provided");
    assert_eq!(
        preview["selector_reviews"][0]["claim"]["match"],
        "const Target = { value: 4 };"
    );
    fixture.assert_success(&args);
    let yaml = fs::read_to_string(fixture.modules.join("services/provider.yaml")).unwrap();
    assert!(yaml.contains("const Target = { value: 4 };"), "{yaml}");
    fixture.assert_runs("4\n");
}

#[test]
fn rejects_reviewed_selector_that_claims_a_different_binding() {
    let fixture = GraphFixture::new(
        "const provider = { value: 4 };\nconst other = { value: 5 };\nconsole.log(provider.value);",
        &[],
    );
    let report = fixture.json(&["modules", "propose", "--size-cap-lines", "1"]);
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["binding_ids"]
                .as_array()
                .is_some_and(|bindings| bindings.iter().any(|binding| binding == "provider"))
        })
        .expect("a provider proposal");
    let request = json!({
        "proposal_id": proposal["proposed_module_id"],
        "owner_ids": proposal["owner_ids"],
        "module": "services/provider",
        "readable_names": {"provider": "Provider"},
        "selectors": {"provider": {"kind": "source_match", "selector": {
            "match": "const Target = { value: 5 };",
            "bindings": [{"local": "Target", "name": "Provider"}]
        }}}
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
        &["resolved binding", "expected \"provider\""],
    );
}

#[test]
fn applies_reviewed_relational_selector() {
    let fixture = GraphFixture::new(
        "const anchor = { value: 4 };\nconst alias = anchor;\nconsole.log(alias.value);\nexport { anchor, alias };",
        &[(
            "services/anchor.yaml",
            "members: [{name: Anchor, selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let report = fixture.json(&["modules", "propose", "--size-cap-lines", "1"]);
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["binding_ids"]
                .as_array()
                .is_some_and(|bindings| bindings.iter().any(|binding| binding == "alias"))
        })
        .expect("an alias proposal");
    let request = json!({
        "proposal_id": proposal["proposed_module_id"],
        "owner_ids": proposal["owner_ids"],
        "module": "services/alias",
        "readable_names": {"alias": "Alias"},
        "selectors": {"alias": {"kind": "relational", "selector": {
            "cross_ref": {"aliases": "Anchor"}
        }}}
    });
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
    let preview = fixture.json(&[&args[..], &["--dry-run"]].concat());
    assert_eq!(preview["selector_reviews"][0]["kind"], "relational");
    fixture.assert_success(&args);
    let yaml = fs::read_to_string(fixture.modules.join("services/alias.yaml")).unwrap();
    assert!(yaml.contains("cross_ref:"), "{yaml}");
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
