//! `debundle bindings assign`/`unassign` candidate assessments over a saved
//! analysis match the emitted run and refuse stale inputs before writes.

use std::fs;

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use serde_json::{Value, json};

#[test]
fn bindings_assign_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { second(); return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: second}}}]\n",
        )],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let result = staged.assign(Some("second:a_earlier"), Some(&staged.artifact), true);
    assert_ok(&result.result, "bindings assign --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    // A full candidate assessment upgrades the graph-claims label.
    assert_eq!(outcome["gate"], "passed", "{outcome:#}");
    assert!(
        outcome.get("needs_full_assessment").is_none(),
        "{outcome:#}"
    );
    assert_eq!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "{assessment:#}"
    );
    assert_eq!(assessment["legacy_gate_accepts"], true, "{assessment:#}");
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "candidate extraction into the earlier ESM module must invert source-unit order"
    );

    // Apply the identical move through the ordinary graph-backed edit path,
    // then compare the assessment with run --analysis over that candidate YAML.
    let applied = staged.assign(Some("second:a_earlier"), Some(&staged.artifact), false);
    assert_ok(&applied.result, "bindings assign --analysis");
    assert!(
        !applied.result.stderr.contains("needs_full_assessment"),
        "{}",
        applied.result.stderr
    );
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(applied["effect_relocation_assessment"], *assessment);
    assert_eq!(applied["gate"], "passed", "{applied:#}");
    assert!(
        applied.get("needs_full_assessment").is_none(),
        "{applied:#}"
    );
    assert!(staged.modules.join("a_earlier.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn destructuring_placement_preview_matches_staged_emission() {
    let source = "const { left, right } = { left: 10, right: 20 };\n\
console.log(left, right);\n";
    let staged = staged_tree(
        &[("main", source)],
        &[(
            "group.yaml",
            "members: [{selector: {binding: {name: left}}}]\n",
        )],
    );
    let result = staged.assign(None, Some(&staged.artifact), true);
    assert_ok(&result.result, "destructuring bindings assign preview");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    assert_eq!(
        assessment["placement"]["projection_violations"],
        json!([]),
        "the shared request-planning pull must produce one finalized destination"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "destructuring grouping must be covered in the finalized preview"
    );
    let candidate = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "10 20\n",
        "",
    );
}

#[test]
fn nested_var_placement_preview_matches_staged_emission() {
    let source = "try { var impl = \"primary\"; } catch (e) { var impl = \"fallback\"; }\n\
console.log(impl);\n";
    let staged = staged_tree(
        &[("main", source)],
        &[(
            "impl.yaml",
            "members: [{selector: {binding: {name: impl}}}]\n",
        )],
    );
    let result = staged.assign(None, Some(&staged.artifact), true);
    assert_ok(&result.result, "nested-var bindings assign preview");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    assert_eq!(
        assessment["placement"]["projection_violations"],
        json!([]),
        "nested-var planning should yield one finalized whole-statement destination"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "nested-var owners and whole-statement placement must be covered"
    );
    let candidate = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "primary\n",
        "",
    );
}

#[test]
fn bindings_assign_assessment_keeps_uncovered_havoc_conservative() {
    let source = "const written = (globalThis.marker = 1);\n\
const read = globalThis.marker;\n\
console.log(written, read);\n";
    let staged = staged_tree(
        &[("main", source)],
        &[(
            "reader.yaml",
            "members: [{selector: {binding: {name: read}}}]\n",
        )],
    );
    let result = staged.assign(None, Some(&staged.artifact), true);
    assert_ok(
        &result.result,
        "bindings assign --dry-run --analysis with no moves",
    );
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    assert_eq!(
        assessment["decision"]["status"], "conflict",
        "{assessment:#}"
    );
    assert_eq!(assessment["decision"]["havoc"], true, "{assessment:#}");
    assert_eq!(
        assessment["decision"]["uncovered_havoc"], true,
        "{assessment:#}"
    );
    assert!(assessment["counts"]["havoc_pairs"].as_u64().unwrap() > 0);
    assert_candidate_assessment_matches_run(assessment, &staged, false, true);
}

#[test]
fn bindings_unassign_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[
            (
                "a_earlier.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "z_later.yaml",
                "members: [{selector: {binding: {name: second}}}]\n",
            ),
        ],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let before = module_snapshot(&staged.modules);
    let ordinary = staged.unassign("first", None, true);
    assert_ok(
        &ordinary.result,
        "ordinary graph-backed bindings unassign dry-run",
    );
    let ordinary_outcome: Value = serde_json::from_str(&ordinary.result.stdout).unwrap();
    assert!(
        ordinary_outcome
            .get("effect_relocation_assessment")
            .is_none()
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let result = staged.unassign("first", Some(&staged.artifact), true);
    assert_ok(&result.result, "bindings unassign --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "unassigning the first function into residual must change the emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.unassign("first", None, false);
    assert_ok(&applied.result, "ordinary graph-backed bindings unassign");
    assert!(!staged.modules.join("a_earlier.yaml").exists());
    assert!(staged.modules.join("z_later.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn bindings_unassign_requested_assessment_refuses_stale_artifacts_before_yaml_writes() {
    let staged = staged_tree(
        &[("main", "function first() { return \"first\"; }\n")],
        &[(
            "a_earlier.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let foreign = staged_tree(
        &[("main", "function other() { return \"other\"; }\n")],
        &[(
            "other.yaml",
            "members: [{selector: {binding: {name: other}}}]\n",
        )],
    );
    let before = module_snapshot(&staged.modules);
    let result = staged.unassign("first", Some(&foreign.artifact), true);
    assert_eq!(
        result.result.status.code(),
        Some(1),
        "{}",
        result.result.stderr
    );
    assert!(
        result.result.stderr.contains("projection")
            || result.result.stderr.contains("analysis")
            || result.result.stderr.contains("source"),
        "expected staged input mismatch:\n{}",
        result.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn bindings_assign_requested_assessment_refuses_stale_inputs_before_yaml_writes() {
    let staged = staged_tree(
        &[("main", "const value = 1;\nconsole.log(value);\n")],
        &[(
            "value.yaml",
            "members: [{selector: {binding: {name: value}}}]\n",
        )],
    );
    let before = module_snapshot(&staged.modules);
    let original_artifact = fs::read(&staged.artifact).unwrap();

    // Engine identity is checked by the production staged-artifact loader.
    let (mut header, body) =
        staged_analysis::analysis_artifact::read_header(&original_artifact).unwrap();
    header.analysis_engine_sha256 = "another-compiled-engine".to_string();
    let encoded_header = rmp_serde::to_vec_named(&header).unwrap();
    let mut wrong_engine = Vec::new();
    wrong_engine.extend_from_slice(&u32::try_from(encoded_header.len()).unwrap().to_le_bytes());
    wrong_engine.extend_from_slice(&encoded_header);
    wrong_engine.extend_from_slice(body);
    fs::write(&staged.artifact, wrong_engine).unwrap();
    let refused = staged.assign(Some("value:target"), Some(&staged.artifact), true);
    assert!(
        !refused.result.status.success(),
        "wrong engine was accepted"
    );
    assert!(
        refused.result.stderr.contains("engine"),
        "{}",
        refused.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
    fs::write(&staged.artifact, &original_artifact).unwrap();

    // The verified graph/source context rejects a changed source before the
    // prospective YAML edit can be committed.
    let original_source = fs::read(&staged.source).unwrap();
    fs::write(&staged.source, "const value = 2;\nconsole.log(value);\n").unwrap();
    let refused = staged.assign(Some("value:target"), Some(&staged.artifact), true);
    assert!(
        !refused.result.status.success(),
        "changed source was accepted"
    );
    assert_eq!(module_snapshot(&staged.modules), before);
    fs::write(&staged.source, original_source).unwrap();

    // A different projection with the same source and engine is rejected by
    // the same production `AnalysisMode::Load` path.
    let root = staged.root().to_path_buf();
    let alternate_list = root.join("js-files-alt.txt");
    fs::write(&alternate_list, "main.js\n").unwrap();
    let alternate_config = root.join("alternate_spec_config.yaml");
    let config_text = fs::read_to_string(&staged.config).unwrap();
    assert!(
        config_text.contains("js_list_path: js-files.txt"),
        "{config_text}"
    );
    fs::write(
        &alternate_config,
        config_text.replace(
            "js_list_path: js-files.txt",
            "js_list_path: js-files-alt.txt",
        ),
    )
    .unwrap();
    let alternate_projection = root.join("alternate.projection");
    let alternate_artifact = root.join("alternate.dba");
    let alternate_out = root.join("alternate-out");
    let project_args = [
        "spec",
        "project-analysis",
        "--tree-config",
        path(&alternate_config),
        "--tree-modules",
        path(&staged.modules),
        "--tree-vendor-marks",
        path(&staged.vendor_marks),
        "--tree-source-root",
        path(&root),
        "--out-root",
        path(&alternate_out),
        "--out",
        path(&alternate_projection),
    ];
    assert_ok(&command(&project_args), "alternate project-analysis");
    let analyze_args = [
        "--projection",
        path(&alternate_projection),
        "--tree-source-root",
        path(&root),
        "--out",
        path(&alternate_artifact),
    ];
    assert_ok(
        &command_with(&analyzer_path(), &analyze_args),
        "alternate analyze",
    );
    let refused = staged.assign(Some("value:target"), Some(&alternate_artifact), true);
    assert!(
        !refused.result.status.success(),
        "different projection was accepted"
    );
    assert!(
        refused.result.stderr.contains("projection"),
        "{}",
        refused.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

fn assert_candidate_has_placement_violation(
    result: &CommandResult,
    expected_violation: &str,
    label: &str,
) {
    assert_ok(result, label);
    let outcome: Value = serde_json::from_str(&result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    assert_eq!(
        assessment["status"], "blocked_by_placement",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["placement"]["status"], "blocked",
        "{assessment:#}"
    );
    assert!(
        assessment["placement"]["projection_violations"]
            .as_array()
            .expect("typed placement violations")
            .iter()
            .any(|violation| violation["kind"] == expected_violation),
        "expected {expected_violation} in {assessment:#}"
    );
}

fn assert_normal_run_refuses_with_projection_violation(
    run: &TreeRun,
    expected_debug_variant: &str,
    label: &str,
) {
    assert!(
        !run.result.status.success(),
        "{label} unexpectedly emitted runnable output"
    );
    assert!(
        run.result
            .stderr
            .contains("placement projection is unmappable"),
        "{label} did not use the common projection refusal:\n{}",
        run.result.stderr
    );
    assert!(
        run.result.stderr.contains(expected_debug_variant),
        "{label} did not report {expected_debug_variant}:\n{}",
        run.result.stderr
    );
    assert!(
        !run.out_root.join("app").exists(),
        "{label} wrote application output after placement refusal"
    );
}

#[test]
fn staged_assessment_types_conflicting_destructuring_claims() {
    const SOURCE: &str = "const source = { a: 1, b: 2 };\n\
const { a, b } = source;\n\
export { a, b };\n";
    let invalid = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[],
            modules: &[
                (
                    "a_left.yaml",
                    "members: [{selector: {binding: {name: source}}}, {selector: {binding: {name: a}}}]\n",
                ),
                (
                    "b_right.yaml",
                    "members: [{selector: {binding: {name: b}}}]\n",
                ),
            ],
        },
        &[],
    );

    let staged = staged_tree(
        &[("main", SOURCE)],
        &[(
            "z_same.yaml",
            "members: [{selector: {binding: {name: source}}}, {selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}]\n",
        )],
    );
    let result = staged.assign(Some("b:zz_right"), Some(&staged.artifact), true);
    assert_candidate_has_placement_violation(
        &result.result,
        "destructuring_destination_split",
        "bindings assign --dry-run --analysis for split destructuring",
    );
    let before = module_snapshot(&staged.modules);
    let write = staged.assign(Some("b:zz_right"), Some(&staged.artifact), false);
    assert_eq!(
        write.result.status.code(),
        Some(1),
        "{}",
        write.result.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
    assert_normal_run_refuses_with_projection_violation(
        &invalid,
        "DestructuringDestinationSplit",
        "split destructuring",
    );
}

#[test]
fn staged_assessment_types_partially_owned_nested_var_statement() {
    const SOURCE: &str = "if (true) { var a = 1; var b = 2; }\n\
console.log(a, b);\n\
export { a, b };\n";
    let invalid = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[],
            modules: &[(
                "z_same.yaml",
                "members: [{selector: {binding: {name: a}}}]\n",
            )],
        },
        &[],
    );

    let staged = staged_tree(
        &[("main", SOURCE)],
        &[(
            "z_same.yaml",
            "members: [{selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}]\n",
        )],
    );
    let result = staged.unassign("b", Some(&staged.artifact), true);
    assert_candidate_has_placement_violation(
        &result.result,
        "nested_var_missing_owner",
        "bindings unassign --dry-run --analysis for partial nested var",
    );
    assert_normal_run_refuses_with_projection_violation(
        &invalid,
        "NestedVarMissingOwner",
        "partially owned nested var",
    );
}

#[test]
fn staged_assessment_types_split_nested_var_bindings() {
    const SOURCE: &str = "if (true) { var a = 1; var b = 2; }\n\
console.log(a, b);\n\
export { a, b };\n";
    let invalid = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[("main", SOURCE)],
            module_roots: &[],
            modules: &[
                (
                    "a_left.yaml",
                    "members: [{selector: {binding: {name: a}}}]\n",
                ),
                (
                    "b_right.yaml",
                    "members: [{selector: {binding: {name: b}}}]\n",
                ),
            ],
        },
        &[],
    );

    let staged = staged_tree(
        &[("main", SOURCE)],
        &[(
            "z_same.yaml",
            "members: [{selector: {binding: {name: a}}}, {selector: {binding: {name: b}}}]\n",
        )],
    );
    let result = staged.assign(Some("b:zz_right"), Some(&staged.artifact), true);
    assert_candidate_has_placement_violation(
        &result.result,
        "nested_var_destination_split",
        "bindings assign --dry-run --analysis for split nested-var bindings",
    );
    assert_normal_run_refuses_with_projection_violation(
        &invalid,
        "NestedVarDestinationSplit",
        "split nested-var bindings",
    );
}
