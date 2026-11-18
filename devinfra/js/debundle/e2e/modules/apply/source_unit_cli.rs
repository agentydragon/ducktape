//! Reviewed source_unit-to-module batches preserve existing selector metadata.

use super::support::apply_args;
use analysis::DependencyGraphReport;
use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::process;
use debundle_e2e_support::{
    TreeFixture, VariableDeclarationKind, VariableInitializerKind,
    assert_module_variable_declarators, assert_node_output, debundler_path, read_gzip_json,
    run_tree_graph_fixture,
};
use serde_json::json;
use std::fs;
use std::path::Path;
use std::process::Command;

#[test]
fn unseeded_plan_preserves_a_residual_binding_readable_name() {
    assert_unseeded_readable_name(
        "const anchor = 0;\nconst value = 41;\nconsole.log(value + 1);\n",
        "ReadableValue",
        &[(
            VariableDeclarationKind::Const,
            &["ReadableValue"],
            VariableInitializerKind::Number,
        )],
    );
}

#[test]
fn unseeded_group_keeps_readable_names_and_nested_sibling_references() {
    assert_unseeded_readable_name(
        "const anchor = 0;\nconst value = 41, read = () => value;\nconsole.log(read() + 1);\n",
        "ReadableValue",
        &[
            (
                VariableDeclarationKind::Const,
                &["ReadableValue"],
                VariableInitializerKind::Number,
            ),
            (
                VariableDeclarationKind::Const,
                &["read"],
                VariableInitializerKind::Arrow,
            ),
        ],
    );
}

#[test]
fn readable_name_can_equal_a_selector_keyword() {
    assert_unseeded_readable_name(
        "const anchor = 0;\nconst value = 41, read = () => value;\nconsole.log(read() + 1);\n",
        "ANYTHING",
        &[
            (
                VariableDeclarationKind::Const,
                &["ANYTHING"],
                VariableInitializerKind::Number,
            ),
            (
                VariableDeclarationKind::Const,
                &["read"],
                VariableInitializerKind::Arrow,
            ),
        ],
    );
}

fn assert_unseeded_readable_name(
    source: &str,
    readable_name: &str,
    declarations: &[(VariableDeclarationKind, &[&str], VariableInitializerKind)],
) {
    let fixture = GraphFixture::new(
        source,
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let config_path = fixture.root().join("spec_config.yaml");
    let mut config: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&config_path).unwrap()).unwrap();
    let renames = serde_yaml::to_value(json!({"main": {"members": [{
        "name": readable_name, "selector": {"binding": {"name": "value"}},
    }]}}))
    .unwrap();
    config["chunk_renames"] = renames.clone();
    fs::write(&config_path, serde_yaml::to_string(&config).unwrap()).unwrap();
    fixture.refresh_graph();

    let proposal = fixture.json(&[
        "modules",
        "propose",
        "--tree-config",
        config_path.to_str().unwrap(),
    ]);
    assert_eq!(proposal["status"], "found", "{proposal}");
    let plan_path = fixture.root().join("plan.json");
    fs::write(&plan_path, serde_json::to_vec(&proposal["plan"]).unwrap()).unwrap();
    fixture.assert_success(&[
        "modules",
        "apply",
        "--plan",
        plan_path.to_str().unwrap(),
        "--tree-config",
        config_path.to_str().unwrap(),
    ]);
    let updated: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&config_path).unwrap()).unwrap();
    assert_eq!(updated["chunk_renames"], renames);
    let emitted = fixture.rerun();
    let destination = proposal["plan"]["changes"][0]["module"].as_str().unwrap();
    assert_module_variable_declarators(
        emitted.path(),
        &format!("app/main/{destination}.js"),
        declarations,
    );
    assert_node_output(&emitted.path().join("app/main/entry.js"), "42\n", "");
}

#[test]
fn can_repair_a_rejected_baseline_without_weakening_the_final_check() {
    let fixture = GraphFixture::rejected(
        "const first = 1;\nconst second = first + 1;\nconst third = second + 1;\nconsole.log(third);\n",
        &[
            (
                "endpoints.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: third}}}]\n",
            ),
            (
                "middle.yaml",
                "members: [{selector: {binding: {name: second}}}]\n",
            ),
        ],
    );
    let batch = fixture.root().join("repair.json");
    fs::write(
        &batch,
        serde_json::to_vec(
            &json!([{"unit_id": fixture.source_unit_for_binding("second"), "module": "endpoints"}]),
        )
        .unwrap(),
    )
    .unwrap();
    let args = apply_args(&fixture, &batch);
    let mut dry_run_args = args.clone();
    dry_run_args.push("--dry-run".to_owned());
    fixture.assert_success(&dry_run_args.iter().map(String::as_str).collect::<Vec<_>>());
    assert_apply_success(&fixture, &batch);
    fixture.assert_runs("3\n");
}

fn source_units_for_binding(graph: &DependencyGraphReport, binding: &str) -> Vec<String> {
    graph
        .nodes
        .iter()
        .filter(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding.as_ref() == binding)
        })
        .map(|node| node.id.clone())
        .collect()
}

fn source_unit_for_binding(graph: &DependencyGraphReport, binding: &str) -> String {
    let mut units = source_units_for_binding(graph, binding);
    assert_eq!(
        units.len(),
        1,
        "expected one declaration site for {binding:?}"
    );
    units.pop().unwrap()
}

fn write_batch(value: serde_json::Value) -> tempfile::TempDir {
    let temp = tempfile::tempdir().unwrap();
    fs::write(
        temp.path().join("assignments.json"),
        serde_json::to_vec(&value).unwrap(),
    )
    .unwrap();
    temp
}

fn apply_output(
    fixture: &GraphFixture,
    assignments_path: &Path,
    extra: &[&str],
) -> std::process::Output {
    let mut args = apply_args(fixture, assignments_path);
    args.extend(extra.iter().map(|arg| (*arg).to_owned()));
    fixture.command(&args.iter().map(String::as_str).collect::<Vec<_>>())
}

fn assert_apply_success(fixture: &GraphFixture, assignments_path: &Path) {
    let output = apply_output(fixture, assignments_path, &[]);
    process::assert_success(&output);
}

fn assert_apply_rejected(fixture: &GraphFixture, assignments_path: &Path, diagnostics: &[&str]) {
    let args = apply_args(fixture, assignments_path);
    fixture.assert_rejected_unchanged(
        &args.iter().map(String::as_str).collect::<Vec<_>>(),
        diagnostics,
    );
}

#[test]
fn moves_member_source_match_group_and_anonymous_claim_with_metadata() {
    let fixture = GraphFixture::new(
        "const alpha = 1;\nconst beta = 2, gamma = 3;\nconsole.log(alpha, beta, gamma);\n",
        &[(
            "home.yaml",
            "comment: Keep this module shell\nnote: source provenance\nmembers:\n  - name: Alpha\n    selector: {binding: {name: alpha}}\nsource_matches:\n  - match: 'const patternBeta = 2, patternGamma = 3;'\n    bindings:\n      - {local: patternBeta, name: Beta}\n      - {local: patternGamma, name: Gamma}\n    note: shared declaration selector\nannotations:\n  Alpha: {note: alpha annotation}\n  Beta: {note: beta annotation}\n  Gamma: {note: gamma annotation}\nanonymous_statements:\n  - match: 'console.log(alpha, beta, gamma);'\n    note: log provenance\n    comment: Log all values\n",
        )],
    );
    let graph = fixture.dependency_graph();
    let alpha = source_unit_for_binding(&graph, "alpha");
    let beta = source_unit_for_binding(&graph, "beta");
    let gamma = source_unit_for_binding(&graph, "gamma");
    let log = graph
        .nodes
        .iter()
        .find(|node| node.declared_bindings.is_empty())
        .expect("anonymous log source_unit")
        .id
        .clone();
    let batch = write_batch(json!([
        {"unit_id": alpha, "module": "values/alpha"},
        {"unit_id": beta, "module": "values/pair"},
        {"unit_id": gamma, "module": "values/gamma"},
        {"unit_id": log, "module": "effects/log"},
    ]));
    assert_apply_success(&fixture, &batch.path().join("assignments.json"));

    let home: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("home.yaml")).unwrap()).unwrap();
    assert_eq!(home["comment"], "Keep this module shell");
    assert_eq!(home["note"], "source provenance");
    assert!(home.get("members").is_none());
    assert!(
        home.get("source_matches")
            .is_none_or(|claims| claims.as_sequence().is_some_and(Vec::is_empty)),
        "source-match claims remained in the original module: {home:#?}"
    );
    assert!(home.get("anonymous_statements").is_none());

    let alpha_doc: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("values/alpha.yaml")).unwrap())
            .unwrap();
    assert_eq!(alpha_doc["members"][0]["name"], "Alpha");
    assert_eq!(
        alpha_doc["annotations"]["Alpha"]["note"],
        "alpha annotation"
    );

    let pair_doc: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("values/pair.yaml")).unwrap())
            .unwrap();
    assert_eq!(
        pair_doc["source_matches"][0]["note"],
        "shared declaration selector"
    );
    assert_eq!(
        pair_doc["source_matches"][0]["bindings"][0]["local"],
        "patternBeta"
    );
    assert_eq!(pair_doc["source_matches"][0]["bindings"][0]["name"], "Beta");
    assert_eq!(pair_doc["annotations"]["Beta"]["note"], "beta annotation");
    assert_eq!(
        pair_doc["source_matches"][0]["bindings"]
            .as_sequence()
            .unwrap()
            .len(),
        1
    );

    let gamma_doc: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("values/gamma.yaml")).unwrap())
            .unwrap();
    assert_eq!(
        gamma_doc["source_matches"][0]["note"],
        "shared declaration selector"
    );
    assert_eq!(
        gamma_doc["source_matches"][0]["bindings"][0]["local"],
        "patternGamma"
    );
    assert_eq!(
        gamma_doc["annotations"]["Gamma"]["note"],
        "gamma annotation"
    );

    let log_doc: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("effects/log.yaml")).unwrap())
            .unwrap();
    assert_eq!(log_doc["anonymous_statements"][0]["note"], "log provenance");
    assert_eq!(
        log_doc["anonymous_statements"][0]["comment"],
        "Log all values"
    );
    fixture.assert_runs("1 2 3\n");
}

#[test]
fn rejects_yaml_filenames_as_module_apply_destinations() {
    let fixture = GraphFixture::new(
        "const alpha = 1;\nconsole.log(alpha);\n",
        &[
            (
                "home.yaml",
                "members: [{selector: {binding: {name: alpha}}}]\n",
            ),
            ("existing.yaml", "members: []\n"),
        ],
    );
    let unit_id = source_unit_for_binding(&fixture.dependency_graph(), "alpha");

    for module in ["existing.yaml", "new.yaml", "new.yml"] {
        let batch = write_batch(json!([{
            "unit_id": unit_id.clone(),
            "module": module,
        }]));
        let alternative = module
            .strip_suffix(".yaml")
            .or_else(|| module.strip_suffix(".yml"))
            .unwrap();
        let diagnostic = format!(
            "module destination {module:?} looks like a YAML filename; use extensionless logical module ID {alternative:?}"
        );
        fixture.assert_rejected_unchanged(
            &[
                "modules",
                "apply",
                "--assignments",
                batch.path().join("assignments.json").to_str().unwrap(),
            ],
            &[&diagnostic],
        );
    }
}

#[test]
fn rejects_atom_split_without_changing_any_module_yaml() {
    let fixture = GraphFixture::atomic_pair();
    let batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&fixture.dependency_graph(), "alpha"),
        "module": "split/alpha",
    }]));
    assert_apply_rejected(
        &fixture,
        &batch.path().join("assignments.json"),
        &["atomic_group_conflicts"],
    );
}

#[test]
fn moves_free_claim_from_use_inside_multi_declarator_statement() {
    let fixture = GraphFixture::new(
        "const spare = 0, limit = 8, sanitizer = value => value.length > limit ? value.slice(0, limit) : value, tail = 1;\nconsole.log(sanitizer('hello world'));\n",
        &[(
            "home.yaml",
            "source_matches:\n  - match: 'const normalize = value => value.length > maxLength ? value.slice(0, maxLength) : value;'\n    bindings: [normalize, maxLength]\n",
        )],
    );
    fixture.assert_runs("hello wo\n");

    let batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&fixture.dependency_graph(), "limit"),
        "module": "config/limit",
    }]));
    assert_apply_success(&fixture, &batch.path().join("assignments.json"));
    fixture.assert_runs("hello wo\n");
}

#[test]
fn moves_one_claim_from_gapped_source_match_group() {
    let fixture = GraphFixture::new(
        "function first() { return 1; }\nfunction middle() { return 99; }\nfunction second() { return 2; }\nconsole.log(first() + second());\n",
        &[(
            "home.yaml",
            "source_matches:\n  - match: |\n      function left() { return 1; }\n      STMT_LIST;\n      function right() { return 2; }\n    bindings: [left, right]\n",
        )],
    );
    fixture.assert_runs("3\n");

    let batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&fixture.dependency_graph(), "first"),
        "module": "split/first",
    }]));
    assert_apply_success(&fixture, &batch.path().join("assignments.json"));
    fixture.assert_runs("3\n");
}

#[test]
fn apply_ignores_modules_owned_by_other_chunks_and_preserves_case_sensitive_root() {
    let run = run_tree_graph_fixture(
        &TreeFixture {
            chunks: &[
                (
                    "main",
                    "const alpha = 4;\nconst retained = 8;\nconsole.log(alpha, retained);\n",
                ),
                ("helper", "const helper = 9;\nconsole.log(helper);\n"),
            ],
            module_roots: &[("chunks/Main", "main"), ("chunks/helper", "helper")],
            modules: &[
                (
                    "chunks/Main/HomeCase.yaml",
                    "members: [{selector: {binding: {name: alpha}}}]\n",
                ),
                (
                    "chunks/Main/ExistingCase.yaml",
                    "members: [{name: Retained, selector: {binding: {name: retained}}}]\n",
                ),
                (
                    "chunks/helper/home.yaml",
                    "members: [{selector: {binding: {name: helper}}}]\n",
                ),
            ],
        },
        &[],
    );
    assert!(run.result.status.success(), "{}", run.result.stderr);
    let modules = run.root().join("modules");
    let main_home = modules.join("chunks/Main/HomeCase.yaml");
    let existing_destination = modules.join("chunks/Main/ExistingCase.yaml");
    let helper_home = modules.join("chunks/helper/home.yaml");
    let helper_before = fs::read(&helper_home).unwrap();
    let destination_before = fs::read(&existing_destination).unwrap();
    let graph = run.report_root.join("main/dependency_graph.json.gz");
    let batch = run.root().join("assignments.json");
    let graph_report: serde_json::Value = read_gzip_json(&graph);
    let source_unit = graph_report["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|declared| {
                    declared
                        .iter()
                        .any(|declared| declared["binding"].as_str() == Some("alpha"))
                })
        })
        .unwrap()["id"]
        .as_str()
        .unwrap();
    fs::write(
        &batch,
        serde_json::to_vec(
            &json!([{"unit_id": source_unit, "module": "chunks/Main/existingCASE"}]),
        )
        .unwrap(),
    )
    .unwrap();
    let order = run.root().join("import-order.json");
    // Empty emitted owners remain in the exact universe and retain baseline
    // order even when their last claim moves elsewhere.
    fs::write(
        &order,
        serde_json::to_vec(&json!(["homecase", "existingcase"])).unwrap(),
    )
    .unwrap();
    let output = process::debundle_output(
        Command::new(debundler_path())
            .args(["modules", "apply", "--assignments"])
            .arg(&batch)
            .args(["--modules"])
            .arg(&modules)
            .args(["--graph"])
            .arg(&graph)
            .args(["--source-root"])
            .arg(run.root().join("snapshot"))
            .args(["--tree-config"])
            .arg(run.root().join("spec_config.yaml"))
            .args(["--import-order"])
            .arg(&order),
    )
    .unwrap_or_else(|err| panic!("{err}"));
    process::assert_success(&output);
    assert!(existing_destination.exists());
    assert_ne!(fs::read(&existing_destination).unwrap(), destination_before);
    assert!(!modules.join("chunks/Main/existingcase.yaml").exists());
    assert!(!modules.join("chunks/main/existingCASE.yaml").exists());
    let destination: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&existing_destination).unwrap()).unwrap();
    assert_eq!(destination["members"].as_sequence().unwrap().len(), 2);
    assert!(
        destination["members"]
            .as_sequence()
            .unwrap()
            .iter()
            .any(|member| member["name"] == "Retained")
    );
    assert!(
        main_home.exists(),
        "empty module files remain until explicit delete"
    );
    let empty_home: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&main_home).unwrap()).unwrap();
    assert!(
        empty_home
            .get("members")
            .is_none_or(|rows| rows.as_sequence().is_some_and(Vec::is_empty))
    );
    assert_eq!(fs::read(helper_home).unwrap(), helper_before);
}

#[test]
fn claims_residual_units_and_rejects_scaffolding_and_duplicate_rows() {
    let residual = GraphFixture::new("const alpha = 1;\nconsole.log(alpha);\n", &[]);
    let residual_batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&residual.dependency_graph(), "alpha"),
        "module": "state/alpha",
    }]));
    assert_apply_success(&residual, &residual_batch.path().join("assignments.json"));
    assert!(residual.modules.join("state/alpha.yaml").exists());
    residual.assert_runs("1\n");

    let exported = GraphFixture::new("const alpha = 1;\nexport { alpha };\n", &[]);
    let graph: serde_json::Value = read_gzip_json(&exported.graph);
    let export_source_unit = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| node["statement_kind"] == "export")
        .expect("export scaffold source_unit")["id"]
        .as_str()
        .unwrap();
    let export_batch = write_batch(json!([{
        "unit_id": export_source_unit,
        "module": "state/export",
    }]));
    exported.assert_rejected_unchanged(
        &[
            "modules",
            "apply",
            "--assignments",
            export_batch
                .path()
                .join("assignments.json")
                .to_str()
                .unwrap(),
        ],
        &["import/export scaffolding"],
    );

    let claimed = GraphFixture::new(
        "const alpha = 1;\nconsole.log(alpha);\n",
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: alpha}}}]\n",
        )],
    );
    let alpha = source_unit_for_binding(&claimed.dependency_graph(), "alpha");
    let duplicate_batch = write_batch(json!([
        {"unit_id": alpha, "module": "state/alpha"},
        {"unit_id": alpha, "module": "state/other"},
    ]));
    claimed.assert_rejected_unchanged(
        &[
            "modules",
            "apply",
            "--assignments",
            duplicate_batch
                .path()
                .join("assignments.json")
                .to_str()
                .unwrap(),
        ],
        &["source unit assignment batch repeats"],
    );
}

#[test]
fn applies_reviewed_selector_override_to_new_residual_claim() {
    let fixture = GraphFixture::new("const alpha = 1;\nconsole.log(alpha);\n", &[]);
    let unit_id = source_unit_for_binding(&fixture.dependency_graph(), "alpha");
    let batch = write_batch(json!([{
        "unit_id": unit_id,
        "module": "state/alpha",
    }]));
    let readable_names = fixture.root().join("readable-names.json");
    fs::write(&readable_names, br#"{"alpha":"Alpha"}"#).unwrap();
    let selector_overrides = fixture.root().join("selector-overrides.json");
    fs::write(
        &selector_overrides,
        serde_json::to_vec(&json!({
            (unit_id): {
                "alpha": {
                    "kind": "source_match",
                    "selector": {
                        "match": "const Value = 1;",
                        "bindings": [{"local": "Value", "name": "Alpha"}],
                    }
                }
            }
        }))
        .unwrap(),
    )
    .unwrap();
    let output = apply_output(
        &fixture,
        &batch.path().join("assignments.json"),
        &[
            "--readable-names",
            readable_names.to_str().unwrap(),
            "--selector-overrides",
            selector_overrides.to_str().unwrap(),
        ],
    );
    process::assert_success(&output);
    let destination: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(fixture.modules.join("state/alpha.yaml")).unwrap())
            .unwrap();
    assert_eq!(
        destination["source_matches"][0]["match"],
        "const Value = 1;"
    );
    assert_eq!(
        destination["source_matches"][0]["bindings"][0]["name"],
        "Alpha"
    );
    fixture.assert_runs("1\n");
}

#[test]
fn source_match_claim_with_raw_sibling_references_survives_split() {
    let fixture = GraphFixture::new(
        "const prefix = 0, a = () => b(), b = () => c(), c = () => \"ready\", tail = 1;\nconsole.log(a());\n",
        &[(
            "home.yaml",
            r#"source_matches:
  - match: |
      const DECLARATORS_BEFORE = null,
        Factory = () => b(),
        Loader = () => c(),
        Value = () => "ready",
        DECLARATORS_BETWEEN = null,
        Tail = 1;
    bindings:
      - Factory
      - Loader
      - Value
"#,
        )],
    );
    fixture.assert_runs("ready\n");
    let batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&fixture.dependency_graph(), "b"),
        "module": "parts/loader",
    }]));
    assert_apply_success(&fixture, &batch.path().join("assignments.json"));
    fixture.assert_runs("ready\n");
}

#[test]
fn source_match_claim_with_declared_sibling_references_survives_split() {
    let fixture = GraphFixture::new(
        "const prefix = 0, a = () => b(), b = () => c(), c = () => \"ready\", tail = 1;\nconsole.log(a());\n",
        &[(
            "home.yaml",
            r#"source_matches:
  - match: |
      const DECLARATORS_BEFORE = null,
        Factory = () => Loader(),
        Loader = () => Value(),
        Value = () => "ready",
        DECLARATORS_BETWEEN = null,
        Tail = 1;
    bindings:
      - Factory
      - Loader
      - Value
"#,
        )],
    );
    fixture.assert_runs("ready\n");

    let batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&fixture.dependency_graph(), "b"),
        "module": "parts/loader",
    }]));
    assert_apply_success(&fixture, &batch.path().join("assignments.json"));
    fixture.assert_runs("ready\n");
}

#[test]
fn applying_a_local_binding_preserves_an_import_claim() {
    let fixture = GraphFixture::with_dependency(
        "import { value as a } from './dependency.js';\nconst b = 2;\nconsole.log(a, b);\n",
        "export const value = 1;\n",
        &[
            (
                "imports.yaml",
                "members: [{selector: {binding: {name: a, kind: import_specifier}}}]\n",
            ),
            (
                "values.yaml",
                "members: [{selector: {binding: {name: b}}}]\n",
            ),
        ],
    );
    let import_path = fixture.modules.join("imports.yaml");
    let import_claim = fs::read(&import_path).unwrap();
    let batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&fixture.dependency_graph(), "b"),
        "module": "moved/value",
    }]));
    assert_apply_success(&fixture, &batch.path().join("assignments.json"));
    assert_eq!(fs::read(import_path).unwrap(), import_claim);
    let empty_values = fixture.modules.join("values.yaml");
    assert!(
        empty_values.exists(),
        "apply leaves empty shells for explicit cleanup"
    );
    fixture.assert_runs("1 2\n");
}

#[test]
fn both_edit_commands_move_all_sites_of_one_var_binding() {
    for command in ["assign", "apply"] {
        let fixture = GraphFixture::new(
            "var a = 1;\nvar a = a + 1;\nconsole.log(a);\n",
            &[(
                "values.yaml",
                "members: [{selector: {binding: {name: a}}}]\n",
            )],
        );
        let declarations = source_units_for_binding(&fixture.dependency_graph(), "a");
        assert_eq!(declarations.len(), 2);
        if command == "assign" {
            let order = fixture.root().join("binding-import-order.json");
            fs::write(&order, serde_json::to_vec(&json!(["moved/value"])).unwrap()).unwrap();
            fixture.assert_success(&[
                "bindings",
                "assign",
                "a:moved/value",
                "--import-order",
                order.to_str().unwrap(),
            ]);
        } else {
            let batch = write_batch(json!(
                declarations
                    .into_iter()
                    .map(|id| { json!({"unit_id": id, "module": "moved/value"}) })
                    .collect::<Vec<_>>()
            ));
            assert_apply_success(&fixture, &batch.path().join("assignments.json"));
        }
        assert_eq!(
            fixture.modules.join("values.yaml").exists(),
            command == "apply",
            "only modules apply preserves empty shells for explicit cleanup"
        );
        let destination: serde_yaml::Value =
            serde_yaml::from_slice(&fs::read(fixture.modules.join("moved/value.yaml")).unwrap())
                .unwrap();
        let members = destination["members"].as_sequence().unwrap();
        assert_eq!(members.len(), 1);
        assert_eq!(members[0]["selector"]["binding"]["name"], "a");
        fixture.assert_runs("2\n");
    }
}
