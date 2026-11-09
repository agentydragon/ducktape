//! Resolved-member layout, note and premise edits against a saved analysis
//! artifact: which keep its identity and which refuse it.

use std::fs;

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use serde_json::{Value, json};

#[test]
fn resolved_member_layout_edits_keep_analysis_identity() {
    let (staged, _fixture) = staged(native_member_fixture());
    let original_artifact = fs::read(&staged.artifact).unwrap();
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let modules = spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .unwrap();
    let mut module = modules.remove("native").unwrap();
    module["members"][0]["name"] = "chosen".into();
    modules.insert("renamed".into(), module);
    let input = &mut spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0];
    input["resolved_member"]["module"] = "renamed".into();
    input["resolved_member"]["name"] = "chosen".into();
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("edited.projection");
    assert_ok(&staged.project(&edited), "project renamed native member");
    assert!(
        fs::read(&staged.projection).unwrap() == fs::read(&edited).unwrap(),
        "renaming the module and public member changed the analysis projection"
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&reused.result, "reuse native member analysis");
    let graph: Value = read_gzip_json(
        &reused
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let evidence = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"]["native_operation_input_uses"]
                .as_array()
                .into_iter()
                .flatten()
        })
        .map(|input| &input["evidence"])
        .next()
        .expect("the native premise must be consumed");
    assert_eq!(
        evidence["selector"]["resolved_member"],
        json!({"module": "renamed", "name": "chosen"})
    );
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct renamed native member run");
    assert_same_invocation_trees(&reused, &direct);
    fs::copy(edited, &staged.projection).unwrap();
    assert_ok(&staged.analyze(), "reanalyze renamed native member");
    assert!(
        original_artifact == fs::read(&staged.artifact).unwrap(),
        "layout edit changed analysis artifact bytes"
    );
}

#[test]
fn native_source_match_notes_keep_analysis_identity() {
    let mut opts = native_member_fixture();
    opts.native_operation_inputs[0].1[0].selector = serde_yaml::from_str(
        "source_match:\n  match: 'var alias = Object.defineProperty;'\n  bindings: [alias]\n  note: Original selector rationale.\n",
    ).unwrap();
    let (staged, _fixture) = staged(opts);
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["source_match"]["note"] =
        "Revised selector rationale.".into();
    write_yaml_file(&staged.spec, &spec);
    let edited = staged.root().join("edited.projection");
    assert_ok(
        &staged.project(&edited),
        "project revised selector rationale",
    );
    assert!(
        fs::read(&staged.projection).unwrap() == fs::read(edited).unwrap(),
        "selector rationale changed analysis identity"
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&reused.result, "reuse analysis after selector note edit");
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct run after selector note edit");
    assert_same_invocation_trees(&reused, &direct);
}

#[test]
fn analyze_refuses_provider_source_changed_after_site_projection() {
    let fixture = write_validate_fixture_spec(native_member_fixture());
    let root = fixture.spec_path.parent().unwrap().to_path_buf();
    let staged = Staged {
        spec: fixture.spec_path.clone(),
        projection: root.join("analysis.projection"),
        artifact: root.join("analysis.dba"),
    };
    assert_ok(
        &staged.project(&staged.projection),
        "project native source sites",
    );

    let provider_source = root.join("snapshot/static/app.js");
    let mut changed_source = fs::read_to_string(&provider_source).unwrap();
    changed_source.push_str("// changed after source-site projection\n");
    fs::write(&provider_source, changed_source).unwrap();

    let refused = staged.analyze();
    assert_eq!(refused.status.code(), Some(1), "{}", refused.stderr);
    assert!(
        refused
            .stderr
            .contains("analysis source-site provider static/app has stale parser input"),
        "expected source-site provenance refusal:\n{}",
        refused.stderr
    );
    assert!(
        !staged.artifact.exists(),
        "stale source sites wrote an artifact"
    );
}

#[test]
fn resolved_member_retargeting_refuses_saved_analysis() {
    let (staged, _fixture) = staged(native_member_fixture());
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["resolved_member"]
        ["name"] = "other".into();
    write_yaml_file(&staged.spec, &spec);
    let edited = staged.root().join("edited.projection");
    assert_ok(&staged.project(&edited), "project retargeted native member");
    assert_ne!(
        fs::read(&staged.projection).unwrap(),
        fs::read(&edited).unwrap()
    );
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(
        !refused.result.status.success(),
        "retargeted native premise reused old facts"
    );
    assert!(
        refused.result.stderr.contains("another projection"),
        "{}",
        refused.result.stderr
    );
    fs::copy(edited, &staged.projection).unwrap();
    assert_ok(&staged.analyze(), "analyze retargeted native member");
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "run retargeted native member",
    );
}

#[test]
fn resolved_member_premise_edits_refuse_saved_analysis() {
    let (staged, _fixture) = staged(native_member_fixture());
    let original: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    for (field, value) in [
        ("operation", "map_empty".to_owned()),
        ("audited_source_sha256", "0".repeat(64)),
    ] {
        let mut spec = original.clone();
        spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0][field] =
            value.into();
        write_yaml_file(&staged.spec, &spec);
        let refused = staged.run(&["--analysis", path(&staged.artifact)]);
        assert!(
            !refused.result.status.success(),
            "changed {field} reused old facts"
        );
        assert!(
            refused.result.stderr.contains("another projection"),
            "{}",
            refused.result.stderr
        );
    }
}
