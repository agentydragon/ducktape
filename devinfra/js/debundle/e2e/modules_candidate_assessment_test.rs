//! `debundle modules apply`/`merge`/`delete` candidate assessments
//! over saved analyses match emitted runs and refuse stale inputs
//! before writes.

use std::fs;
use std::process::Command;

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

fn residual_apply_request(staged: &TreeStaged, binding: &str, destination: &str) -> Value {
    json!({
        "unit_id": source_unit_for_binding(staged, binding),
        "module": destination,
    })
}

fn write_import_order(staged: &TreeStaged, name: &str, appended: &[&str]) -> PathBuf {
    let report: Value = read_gzip_json(&staged.graph);
    let mut order: Vec<String> = report["import_order"]
        .as_array()
        .unwrap()
        .iter()
        .map(|path| path.as_str().unwrap().to_owned())
        .collect();
    order.extend(appended.iter().map(|path| (*path).to_owned()));
    let path = staged.root().join(name);
    fs::write(&path, serde_json::to_vec(&order).unwrap()).unwrap();
    path
}

fn apply_with_import_order(
    staged: &TreeStaged,
    batch: &Path,
    import_order: &Path,
    analysis: Option<&Path>,
    dry_run: bool,
    complete_context: bool,
    partial_context_without_analysis: bool,
) -> StagedInvocation {
    let outputs = staged.run.workspace().child();
    let output_root = outputs.path().join("out");
    let mut args = staged.apply_args(
        batch,
        analysis,
        dry_run,
        complete_context,
        partial_context_without_analysis,
        &output_root,
    );
    args.extend(["--import-order".to_string(), path(import_order).to_string()]);
    StagedInvocation::tree_command(&args, outputs)
}

/// Assert that the command was refused with exit status 1 and return its stderr.
#[track_caller]
fn assert_refused(invocation: &StagedInvocation) -> &str {
    assert_eq!(
        invocation.result.status.code(),
        Some(1),
        "{}",
        invocation.result.stderr
    );
    &invocation.result.stderr
}

#[test]
fn modules_apply_assessment_matches_the_selected_residual_candidate() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { second(); return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let request = residual_apply_request(&staged, "second", "a_earlier");
    assert_eq!(
        request["unit_id"],
        source_unit_for_binding(&staged, "second")
    );
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let import_order = write_import_order(&staged, "apply-import-order.json", &["a_earlier"]);

    let before = module_snapshot(&staged.modules);
    let result = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&staged.artifact),
        true,
        true,
        false,
    );
    assert_ok(&result.result, "modules apply --dry-run --analysis");
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
        "claiming the later residual in an earlier module must change the actual emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&staged.artifact),
        false,
        true,
        false,
    );
    assert_ok(&applied.result, "modules apply --analysis");
    let applied: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert_eq!(
        applied["effect_relocation_assessment"], *assessment,
        "the written candidate must use the same canonical assessment as its preview"
    );
    assert!(applied.get("effect_assessment_unavailable").is_none());
    assert!(staged.modules.join("a_earlier.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn modules_apply_assessment_accepts_a_closed_no_swap_candidate() {
    let staged = staged_tree(
        &[(
            "main",
            "function only() { return \"only\"; }\nconsole.log(only());\n",
        )],
        &[("empty.yaml", "members: []\n")],
    );
    let request = residual_apply_request(&staged, "only", "new_module");
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let import_order = write_import_order(&staged, "apply-import-order.json", &["new_module"]);

    let result = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&staged.artifact),
        true,
        true,
        false,
    );
    assert_ok(
        &result.result,
        "modules apply --dry-run --analysis with no swap",
    );
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["decision"]["scope"], "no_reordering",
        "{assessment:#}"
    );
    assert_eq!(assessment["swapped_pairs"], 0, "{assessment:#}");
    assert_eq!(
        assessment["status"], "accepted_within_checked_scope",
        "{assessment:#}"
    );
    assert_eq!(
        assessment["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "{assessment:#}"
    );
}

#[test]
fn modules_apply_assessment_refuses_stale_and_partial_inputs_before_writes() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[(
            "z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let request = residual_apply_request(&staged, "second", "a_earlier");
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let import_order = write_import_order(&staged, "apply-import-order.json", &["a_earlier"]);
    let before = module_snapshot(&staged.modules);

    let foreign = staged_tree(&[("main", "function other() { return \"other\"; }\n")], &[]);
    let stale = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&foreign.artifact),
        true,
        true,
        false,
    );
    let stderr = assert_refused(&stale);
    assert!(
        stderr.contains("projection") || stderr.contains("analysis"),
        "expected staged projection refusal:\n{stderr}"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let partial = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&staged.artifact),
        true,
        false,
        false,
    );
    assert_refused(&partial);
    assert_eq!(module_snapshot(&staged.modules), before);

    let not_dry = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&foreign.artifact),
        false,
        true,
        false,
    );
    assert_refused(&not_dry);
    assert_eq!(module_snapshot(&staged.modules), before);

    let scoped = staged_tree_with_module_roots(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             console.log(first(), second());\n",
        )],
        &[("chunks/main", "main")],
        &[(
            "chunks/main/z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let scoped_request = residual_apply_request(&scoped, "second", "chunks/main/a_earlier");
    let scoped_batch = scoped.root().join("apply.json");
    fs::write(
        &scoped_batch,
        serde_json::to_vec(&json!([scoped_request])).unwrap(),
    )
    .unwrap();
    let scoped_import_order =
        write_import_order(&scoped, "scoped-import-order.json", &["a_earlier"]);
    let scoped_before = module_snapshot(&scoped.modules);
    let scoped_preview = apply_with_import_order(
        &scoped,
        &scoped_batch,
        &scoped_import_order,
        None,
        true,
        false,
        false,
    );
    assert_ok(
        &scoped_preview.result,
        "ordinary scoped modules apply --dry-run",
    );
    let scoped_refused = apply_with_import_order(
        &scoped,
        &scoped_batch,
        &scoped_import_order,
        Some(&scoped.artifact),
        true,
        true,
        false,
    );
    let stderr = assert_refused(&scoped_refused);
    assert!(stderr.contains("one full-tree module root"), "{stderr}");
    assert_eq!(module_snapshot(&scoped.modules), scoped_before);
}

#[test]
fn modules_apply_assessment_inputs_are_explicit_but_ambient_defaults_are_ignored() {
    let staged = staged_tree(
        &[("main", "function value() { return \"value\"; }\n")],
        &[("empty.yaml", "members: []\n")],
    );
    let request = residual_apply_request(&staged, "value", "new_module");
    let batch = staged.root().join("apply.json");
    fs::write(&batch, serde_json::to_vec(&json!([request])).unwrap()).unwrap();
    let import_order = write_import_order(&staged, "apply-import-order.json", &["new_module"]);
    let before = module_snapshot(&staged.modules);
    let root = staged.root();
    let out = root.join("ambient-out");

    let mut ambient_args = staged.apply_args(&batch, None, true, false, false, &out);
    ambient_args.extend([
        "--import-order".to_string(),
        path(&import_order).to_string(),
    ]);
    let ambient = process::debundle_output(
        std::process::Command::new(debundler_path())
            .args(ambient_args.iter().map(String::as_str))
            .env("DEBUNDLE_TREE_SOURCE_ROOT", root)
            .env("DEBUNDLE_OUT", &out),
    )
    .unwrap_or_else(|err| panic!("{err}"));
    assert!(
        ambient.status.success(),
        "ambient transform defaults changed ordinary apply behavior:\n{}",
        String::from_utf8_lossy(&ambient.stderr)
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let mut explicit_args = staged.apply_args(&batch, None, true, false, false, &out);
    explicit_args.extend([
        "--import-order".to_string(),
        path(&import_order).to_string(),
        "--tree-source-root".to_string(),
        path(root).to_string(),
        "--out-root".to_string(),
        path(&out).to_string(),
    ]);
    let explicit = process::debundle_output(
        std::process::Command::new(debundler_path())
            .args(explicit_args.iter().map(String::as_str))
            .env("DEBUNDLE_TREE_SOURCE_ROOT", root)
            .env("DEBUNDLE_OUT", &out),
    )
    .unwrap_or_else(|err| panic!("{err}"));
    assert_eq!(explicit.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&explicit.stderr).contains("require --analysis"),
        "expected explicit-context refusal:\n{}",
        String::from_utf8_lossy(&explicit.stderr)
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_merge_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = staged_tree(
        &[(
            "main",
            "function first() { return \"first\"; }\n\
             function second() { return \"second\"; }\n\
             function third() { return \"third\"; }\n\
             console.log(first(), second(), third());\n",
        )],
        &[
            (
                "a_earlier.yaml",
                "members: [{selector: {binding: {name: first}}}]\n",
            ),
            (
                "b_middle.yaml",
                "members: [{selector: {binding: {name: second}}}]\n",
            ),
            (
                "z_later.yaml",
                "members: [{selector: {binding: {name: third}}}]\n",
            ),
        ],
    );
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let before = module_snapshot(&staged.modules);
    let ordinary = staged.merge("z_later", "a_earlier", None, true);
    assert_ok(
        &ordinary.result,
        "ordinary graph-backed modules merge dry-run",
    );
    let ordinary_outcome: Value = serde_json::from_str(&ordinary.result.stdout).unwrap();
    assert!(
        ordinary_outcome
            .get("effect_relocation_assessment")
            .is_none()
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let result = staged.merge("z_later", "a_earlier", Some(&staged.artifact), true);
    assert_ok(&result.result, "modules merge --dry-run --analysis");
    let outcome: Value = serde_json::from_str(&result.result.stdout).unwrap();
    let assessment = &outcome["effect_relocation_assessment"];
    assert_eq!(
        assessment["decision"]["status"], "accepted",
        "{assessment:#}"
    );
    assert_eq!(assessment["swapped_pairs"], 1, "{assessment:#}");
    assert_ne!(
        assessment["emitted_order"], baseline_report["emitted_order"],
        "merging the earliest unit into the last module must change the emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.merge("z_later", "a_earlier", None, false);
    assert_ok(&applied.result, "ordinary graph-backed modules merge");
    assert!(!staged.modules.join("a_earlier.yaml").exists());
    assert!(staged.modules.join("z_later.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second third\n",
        "",
    );
}

#[test]
fn modules_delete_assessment_matches_the_staged_run_for_its_candidate_plan() {
    let staged = two_function_ordering_tree();
    fs::create_dir_all(staged.modules.join("alternate")).unwrap();
    let baseline = staged.run_with_analysis(&staged.artifact, false);
    assert_ok(&baseline.result, "baseline staged run");
    let baseline_report = read_effect_report(&baseline.output_root());
    assert_eq!(baseline_report["swapped_pairs"], 0, "{baseline_report:#}");

    let before = module_snapshot(&staged.modules);
    let ordinary = staged.delete("a_earlier", true, None, true);
    assert_ok(
        &ordinary.result,
        "ordinary graph-backed modules delete dry-run",
    );
    let ordinary_outcome: Value = serde_json::from_str(&ordinary.result.stdout).unwrap();
    assert!(
        ordinary_outcome
            .get("effect_relocation_assessment")
            .is_none()
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let result = staged.delete("alternate/../a_earlier", true, Some(&staged.artifact), true);
    assert_ok(
        &result.result,
        "modules delete --force --dry-run --analysis",
    );
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
        "deleting the earliest module must move its binding to residual and invert emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied = staged.delete("a_earlier", true, None, false);
    assert_ok(&applied.result, "ordinary graph-backed modules delete");
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
fn modules_delete_requested_assessment_refuses_stale_artifacts_before_yaml_writes() {
    let staged = two_function_ordering_tree();
    let foreign = foreign_analysis_tree();
    let before = module_snapshot(&staged.modules);
    let result = staged.delete("a_earlier", true, Some(&foreign.artifact), true);
    let stderr = assert_refused(&result);
    assert!(
        stderr.contains("projection") || stderr.contains("analysis"),
        "{stderr}\nexpected staged projection/artifact mismatch"
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_merge_requested_assessment_refuses_stale_artifacts_before_yaml_writes() {
    let staged = two_function_ordering_tree();
    let foreign = foreign_analysis_tree();
    let before = module_snapshot(&staged.modules);
    let result = staged.merge("z_later", "a_earlier", Some(&foreign.artifact), true);
    let stderr = assert_refused(&result);
    assert!(
        stderr.contains("projection") || stderr.contains("analysis"),
        "{stderr}\nexpected staged projection/artifact mismatch"
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_apply_assessment_matches_staged_run_for_existing_unit_move() {
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

    let batch = staged.root().join("assignments.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "second"),
            "module": "a_earlier",
        }]))
        .unwrap(),
    )
    .unwrap();
    let import_order = write_import_order(&staged, "assignment-import-order.json", &["a_earlier"]);
    let before = module_snapshot(&staged.modules);
    let result = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&staged.artifact),
        true,
        true,
        false,
    );
    assert_ok(
        &result.result,
        "modules apply --assignments --dry-run --analysis",
    );
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
        "moving the later source unit into an earlier module must invert the emitted order"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let applied =
        apply_with_import_order(&staged, &batch, &import_order, None, false, false, false);
    assert_ok(&applied.result, "ordinary graph-backed modules apply");
    let applied_outcome: Value = serde_json::from_str(&applied.result.stdout).unwrap();
    assert!(
        applied_outcome
            .get("effect_relocation_assessment")
            .is_none()
    );
    assert!(staged.modules.join("a_earlier.yaml").is_file());
    assert!(staged.modules.join("z_later.yaml").is_file());
    let candidate = assert_candidate_assessment_matches_run(assessment, &staged, true, false);
    assert_node_output(
        &candidate.output_root().join("app/main/entry.js"),
        "first second\n",
        "",
    );
}

#[test]
fn modules_apply_assessment_refuses_stale_or_partial_context_before_writes() {
    let staged = staged_tree(
        &[("main", "function first() { return \"first\"; }\n")],
        &[(
            "z_later.yaml",
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
    let batch = staged.root().join("assignments.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "first"),
            "module": "a_earlier",
        }]))
        .unwrap(),
    )
    .unwrap();
    let import_order = write_import_order(&staged, "assignment-import-order.json", &["a_earlier"]);
    let before = module_snapshot(&staged.modules);

    let stale = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&foreign.artifact),
        true,
        true,
        false,
    );
    let stderr = assert_refused(&stale);
    assert!(
        stderr.contains("projection") || stderr.contains("analysis") || stderr.contains("source"),
        "expected staged input mismatch:\n{stderr}"
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let partial = apply_with_import_order(&staged, &batch, &import_order, None, true, false, true);
    let stderr = assert_refused(&partial);
    assert!(stderr.contains("require --analysis"), "{stderr}");
    assert_eq!(module_snapshot(&staged.modules), before);

    let env_defaults = {
        let output = process::debundle_output(
            Command::new(debundler_path())
                .args([
                    "modules",
                    "apply",
                    "--assignments",
                    path(&batch),
                    "--modules",
                    path(&staged.modules),
                    "--graph",
                    path(&staged.graph),
                    "--source-root",
                    path(&staged.root().join("snapshot")),
                    "--tree-config",
                    path(&staged.config),
                    "--import-order",
                    path(&import_order),
                    "--dry-run",
                    "--format",
                    "json",
                ])
                .env("DEBUNDLE_TREE_SOURCE_ROOT", staged.root())
                .env("DEBUNDLE_OUT", staged.root().join("ambient-output")),
        )
        .unwrap_or_else(|err| panic!("{err}"));
        CommandResult {
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            status: output.status,
        }
    };
    assert_ok(
        &env_defaults,
        "ordinary modules apply with transform environment defaults",
    );
    assert_eq!(module_snapshot(&staged.modules), before);

    let explicit_auxiliary_matching_environment = {
        let source_root = staged.root().to_path_buf();
        let out_root = staged.root().join("ambient-output");
        let output = process::debundle_output(
            Command::new(debundler_path())
                .args([
                    "modules",
                    "apply",
                    "--assignments",
                    path(&batch),
                    "--modules",
                    path(&staged.modules),
                    "--graph",
                    path(&staged.graph),
                    "--source-root",
                    path(&source_root.join("snapshot")),
                    "--tree-config",
                    path(&staged.config),
                    "--import-order",
                    path(&import_order),
                    "--tree-source-root",
                    path(&source_root),
                    "--out-root",
                    path(&out_root),
                    "--dry-run",
                    "--format",
                    "json",
                ])
                .env("DEBUNDLE_TREE_SOURCE_ROOT", &source_root)
                .env("DEBUNDLE_OUT", &out_root),
        )
        .unwrap_or_else(|err| panic!("{err}"));
        CommandResult {
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            status: output.status,
        }
    };
    assert_eq!(
        explicit_auxiliary_matching_environment.status.code(),
        Some(1),
        "explicit transform inputs must still require --analysis when equal to environment defaults\n{}",
        explicit_auxiliary_matching_environment.stderr
    );
    assert!(
        explicit_auxiliary_matching_environment
            .stderr
            .contains("require --analysis"),
        "expected explicit-context refusal:\n{}",
        explicit_auxiliary_matching_environment.stderr
    );
    assert_eq!(module_snapshot(&staged.modules), before);
}

#[test]
fn modules_apply_assessment_refuses_scoped_tree_before_writes() {
    let staged = staged_tree_with_module_roots(
        &[("main", "function first() { return \"first\"; }\n")],
        &[("chunks/main", "main")],
        &[(
            "chunks/main/z_later.yaml",
            "members: [{selector: {binding: {name: first}}}]\n",
        )],
    );
    let batch = staged.root().join("assignments.json");
    fs::write(
        &batch,
        serde_json::to_vec(&json!([{
            "unit_id": source_unit_for_binding(&staged, "first"),
            "module": "chunks/main/a_earlier",
        }]))
        .unwrap(),
    )
    .unwrap();
    let import_order = write_import_order(&staged, "assignment-import-order.json", &["a_earlier"]);
    let before = module_snapshot(&staged.modules);
    let result = apply_with_import_order(
        &staged,
        &batch,
        &import_order,
        Some(&staged.artifact),
        true,
        true,
        false,
    );
    let stderr = assert_refused(&result);
    assert!(stderr.contains("one full-tree module root"), "{stderr}");
    assert_eq!(module_snapshot(&staged.modules), before);
}
