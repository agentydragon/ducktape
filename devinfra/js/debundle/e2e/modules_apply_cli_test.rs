//! Fresh residual claims from current module proposals.

use debundle_e2e_support::{
    GraphFixture, TreeFixture, assert_module_exports, assert_node_output, debundler_path,
    run_tree_fixture,
};
use serde_json::json;
use std::fs;
use std::process::Command;

#[test]
fn applies_renamed_multi_declarator_with_cross_initializer_reference() {
    let fixture = GraphFixture::new("var a = 1000, b = 15, c = b + 5;\n", &[]);
    let report = fixture.json(&["modules", "propose"]);
    let names = [
        ("a", "fooDeadline"),
        ("b", "fooDelays"),
        ("c", "fooTotalDelay"),
    ];
    let requests = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|row| {
            let bindings = row["binding_ids"].as_array()?;
            let readable_names = names
                .iter()
                .filter(|(binding, _)| bindings.contains(&json!(binding)))
                .map(|(binding, name)| ((*binding).to_owned(), json!(name)))
                .collect::<serde_json::Map<String, serde_json::Value>>();
            (!readable_names.is_empty()).then(|| {
                json!({
                    "proposal_id": row["proposed_module_id"],
                    "source_unit_ids": row["source_unit_ids"],
                    "module": "foo/delays",
                    "readable_names": readable_names,
                })
            })
        })
        .collect::<Vec<_>>();
    assert!(!requests.is_empty());
    let temp = tempfile::tempdir().unwrap();
    let batch = temp.path().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&requests).unwrap()).unwrap();
    fixture.assert_success(&["modules", "apply", "--batch", batch.to_str().unwrap()]);
    let yaml = fs::read_to_string(fixture.modules.join("foo/delays.yaml")).unwrap();
    assert!(yaml.contains("fooDeadline"), "{yaml}");
    assert!(yaml.contains("fooDelays"), "{yaml}");
    assert!(yaml.contains("fooTotalDelay"), "{yaml}");
    assert!(!yaml.contains("local: a\n"), "{yaml}");
    assert!(!yaml.contains("local: b\n"), "{yaml}");
    assert!(!yaml.contains("local: c\n"), "{yaml}");
    let spec: serde_yaml::Value = serde_yaml::from_str(&yaml).unwrap();
    let selector = spec["source_matches"][0]["match"].as_str().unwrap();
    assert!(selector.contains("fooDeadline"), "{selector}");
    assert!(selector.contains("fooDelays"), "{selector}");
    assert!(selector.contains("fooTotalDelay"), "{selector}");
    assert!(!selector.contains("= b +"), "{selector}");
    fixture.assert_runs("");
}

#[test]
fn apply_ignores_modules_owned_by_other_chunks() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[
                ("main", "const a = { value: 4 };\nconsole.log(a.value);\n"),
                (
                    "helper",
                    "function a() { return 'other'; }\nfunction provider() { return 'helper'; }\nexport { a, provider };\n",
                ),
            ],
            module_roots: &[("chunks/main", "main"), ("chunks/helper", "helper")],
            modules: &[
                ("chunks/main/empty.yaml", "members: []\n"),
                (
                    "chunks/helper/provider.yaml",
                    "members: [{name: Other, selector: {binding: {name: a}}}]\nsource_matches:\n  - match: |\n      function Provider() { return 'helper'; }\n    bindings:\n      - local: Provider\n        name: Provider\n",
                ),
            ],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);
    let modules = run.root().join("modules");
    let graph = run.report_root.join("main/dependency_graph.json");
    let source = run.root().join("snapshot");
    let config = run.root().join("spec_config.yaml");
    let proposed = Command::new(debundler_path())
        .args(["modules", "propose", "--graph"])
        .arg(&graph)
        .arg("--modules")
        .arg(&modules)
        .arg("--tree-config")
        .arg(&config)
        .args(["--size-cap-lines", "1", "--format", "json"])
        .output()
        .unwrap();
    assert!(
        proposed.status.success(),
        "{}",
        String::from_utf8_lossy(&proposed.stderr)
    );
    let report: serde_json::Value = serde_json::from_slice(&proposed.stdout).unwrap();
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["binding_ids"]
                .as_array()
                .is_some_and(|ids| ids.contains(&json!("a")))
        })
        .expect("main binding is proposed");
    let batch = run.root().join("apply.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "proposal_id": proposal["proposed_module_id"],
            "source_unit_ids": proposal["source_unit_ids"],
            "module": "chunks/main/foo",
            "readable_names": {"a": "foo"}
        }]))
        .unwrap(),
    )
    .unwrap();
    let applied = Command::new(debundler_path())
        .args(["modules", "apply", "--batch"])
        .arg(&batch)
        .arg("--modules")
        .arg(&modules)
        .arg("--tree-config")
        .arg(&config)
        .arg("--graph")
        .arg(&graph)
        .arg("--source-root")
        .arg(&source)
        .args(["--size-cap-lines", "1"])
        .arg("--dry-run")
        .output()
        .unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
}

#[test]
fn apply_preserves_case_sensitive_chunk_root() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", "const a = { value: 4 };\nconsole.log(a.value);\n")],
            module_roots: &[("chunks/Main", "main")],
            modules: &[("chunks/Main/empty.yaml", "members: []\n")],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);
    let modules = run.root().join("modules");
    let graph = run.report_root.join("main/dependency_graph.json");
    let config = run.root().join("spec_config.yaml");
    let proposed = Command::new(debundler_path())
        .args(["modules", "propose", "--graph"])
        .arg(&graph)
        .arg("--modules")
        .arg(&modules)
        .arg("--tree-config")
        .arg(&config)
        .args(["--size-cap-lines", "1", "--format", "json"])
        .output()
        .unwrap();
    assert!(
        proposed.status.success(),
        "{}",
        String::from_utf8_lossy(&proposed.stderr)
    );
    let report: serde_json::Value = serde_json::from_slice(&proposed.stdout).unwrap();
    let proposal = report["proposals"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["binding_ids"]
                .as_array()
                .is_some_and(|ids| ids.contains(&json!("a")))
        })
        .expect("binding is proposed");
    let batch = run.root().join("apply.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "proposal_id": proposal["proposed_module_id"],
            "source_unit_ids": proposal["source_unit_ids"],
            "module": "chunks/Main/services/foo",
            "readable_names": {"a": "foo"}
        }]))
        .unwrap(),
    )
    .unwrap();
    let applied = Command::new(debundler_path())
        .args(["modules", "apply", "--batch"])
        .arg(&batch)
        .arg("--modules")
        .arg(&modules)
        .arg("--tree-config")
        .arg(&config)
        .arg("--graph")
        .arg(&graph)
        .arg("--source-root")
        .arg(run.root().join("snapshot"))
        .args(["--size-cap-lines", "1"])
        .output()
        .unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
    assert!(modules.join("chunks/Main/services/foo.yaml").is_file());
    assert!(!modules.join("chunks/main/services/foo.yaml").exists());
    let out_root = run.root().join("rerun");
    let emitted = Command::new(debundler_path())
        .args(["run", "--tree-config"])
        .arg(&config)
        .arg("--tree-modules")
        .arg(&modules)
        .arg("--tree-vendor-marks")
        .arg(run.root().join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(run.root())
        .arg("--out-root")
        .arg(&out_root)
        .output()
        .unwrap();
    assert!(
        emitted.status.success(),
        "{}",
        String::from_utf8_lossy(&emitted.stderr)
    );
    assert_module_exports(&out_root, "app/main/services/foo.js", &["foo"], &[]);
    assert_node_output(&out_root.join("app/main/entry.js"), "4\n", "");
}

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
                && row["anonymous_statement_source_unit_ids"]
                    .as_array()
                    .is_none_or(Vec::is_empty)
                && row["binding_ids"]
                    .as_array()
                    .is_some_and(|bindings| bindings.iter().any(|binding| binding == "provider"))
        })
        .expect("a graph-ready residual provider proposal");
    let request = json!({
        "proposal_id": proposal["proposed_module_id"],
        "source_unit_ids": proposal["source_unit_ids"],
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
        "source_unit_ids": proposal["source_unit_ids"],
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
        "source_unit_ids": proposal["source_unit_ids"],
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
        "source_unit_ids": proposal["source_unit_ids"],
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
    request["source_unit_ids"] = json!(["stale-source_unit"]);
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
        &["source unit IDs changed"],
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
        "source_unit_ids": proposal["source_unit_ids"],
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
        "source_unit_ids": proposal["source_unit_ids"],
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
