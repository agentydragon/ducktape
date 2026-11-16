//! Reviewed source_unit-to-module batches preserve existing selector metadata.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::process;
use debundle_e2e_support::{TreeFixture, debundler_path, read_gzip_json, run_tree_graph_fixture};
use serde_json::json;
use std::fs;
use std::process::Command;

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
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.to_str().unwrap(),
        "--dry-run",
    ]);
    fixture.assert_success(&["modules", "refine", "--batch", batch.to_str().unwrap()]);
    fixture.assert_runs("3\n");
}

fn source_units_for_binding(fixture: &GraphFixture, binding: &str) -> Vec<String> {
    fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .filter(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding.as_ref() == binding)
        })
        .map(|node| node.id)
        .collect()
}

fn source_unit_for_binding(fixture: &GraphFixture, binding: &str) -> String {
    let mut units = source_units_for_binding(fixture, binding);
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
        temp.path().join("refine.json"),
        serde_json::to_vec(&value).unwrap(),
    )
    .unwrap();
    temp
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
    let alpha = source_unit_for_binding(&fixture, "alpha");
    let beta = source_unit_for_binding(&fixture, "beta");
    let gamma = source_unit_for_binding(&fixture, "gamma");
    let graph: serde_json::Value = read_gzip_json(&fixture.graph);
    let log = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| node["declared_bindings"].as_array().unwrap().is_empty())
        .expect("anonymous log source_unit")["id"]
        .as_str()
        .unwrap()
        .to_string();
    let batch = write_batch(json!([
        {"unit_id": alpha, "module": "values/alpha"},
        {"unit_id": beta, "module": "values/pair"},
        {"unit_id": gamma, "module": "values/gamma"},
        {"unit_id": log, "module": "effects/log"},
    ]));
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().join("refine.json").to_str().unwrap(),
    ]);

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
fn rejects_yaml_filenames_as_module_refine_destinations() {
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
    let unit_id = source_unit_for_binding(&fixture, "alpha");

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
                "refine",
                "--batch",
                batch.path().join("refine.json").to_str().unwrap(),
            ],
            &[&diagnostic],
        );
    }
}

#[test]
fn rejects_atom_split_without_changing_any_module_yaml() {
    let fixture = GraphFixture::atomic_pair();
    let batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&fixture, "alpha"),
        "module": "split/alpha",
    }]));
    fixture.assert_rejected_unchanged(
        &[
            "modules",
            "refine",
            "--batch",
            batch.path().join("refine.json").to_str().unwrap(),
        ],
        &["splits one or more atomic groups"],
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
        "unit_id": source_unit_for_binding(&fixture, "limit"),
        "module": "config/limit",
    }]));
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().join("refine.json").to_str().unwrap(),
    ]);
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
        "unit_id": source_unit_for_binding(&fixture, "first"),
        "module": "split/first",
    }]));
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().join("refine.json").to_str().unwrap(),
    ]);
    fixture.assert_runs("3\n");
}

#[test]
fn refines_only_the_selected_chunk_under_tree_config() {
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
    let batch = run.root().join("refine.json");
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
    let output = process::debundle_output(
        Command::new(debundler_path())
            .args(["modules", "refine", "--batch"])
            .arg(&batch)
            .args(["--modules"])
            .arg(&modules)
            .args(["--graph"])
            .arg(&graph)
            .args(["--source-root"])
            .arg(run.root().join("snapshot"))
            .args(["--tree-config"])
            .arg(run.root().join("spec_config.yaml")),
    )
    .unwrap_or_else(|err| panic!("{err}"));
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(existing_destination.exists());
    assert_ne!(fs::read(&existing_destination).unwrap(), destination_before);
    assert!(!modules.join("chunks/Main/existingcase.yaml").exists());
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
    assert!(!main_home.exists());
    assert_eq!(fs::read(helper_home).unwrap(), helper_before);
}

#[test]
fn rejects_unclaimed_scaffolding_and_duplicate_source_unit_rows() {
    let residual = GraphFixture::new("const alpha = 1;\nconsole.log(alpha);\n", &[]);
    let unclaimed_batch = write_batch(json!([{
        "unit_id": source_unit_for_binding(&residual, "alpha"),
        "module": "state/alpha",
    }]));
    residual.assert_rejected_unchanged(
        &[
            "modules",
            "refine",
            "--batch",
            unclaimed_batch.path().join("refine.json").to_str().unwrap(),
        ],
        &["currently unclaimed/residual"],
    );

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
            "refine",
            "--batch",
            export_batch.path().join("refine.json").to_str().unwrap(),
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
    let alpha = source_unit_for_binding(&claimed, "alpha");
    let duplicate_batch = write_batch(json!([
        {"unit_id": alpha, "module": "state/alpha"},
        {"unit_id": alpha, "module": "state/other"},
    ]));
    claimed.assert_rejected_unchanged(
        &[
            "modules",
            "refine",
            "--batch",
            duplicate_batch.path().join("refine.json").to_str().unwrap(),
        ],
        &["source unit assignment batch repeats"],
    );
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
        "unit_id": source_unit_for_binding(&fixture, "b"),
        "module": "parts/loader",
    }]));
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().join("refine.json").to_str().unwrap(),
    ]);
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
        "unit_id": source_unit_for_binding(&fixture, "b"),
        "module": "parts/loader",
    }]));
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().join("refine.json").to_str().unwrap(),
    ]);
    fixture.assert_runs("ready\n");
}

#[test]
fn refining_a_local_binding_preserves_an_import_claim() {
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
        "unit_id": source_unit_for_binding(&fixture, "b"),
        "module": "moved/value",
    }]));
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().join("refine.json").to_str().unwrap(),
    ]);
    assert_eq!(fs::read(import_path).unwrap(), import_claim);
    assert!(!fixture.modules.join("values.yaml").exists());
    fixture.assert_runs("1 2\n");
}

#[test]
fn both_edit_commands_move_all_sites_of_one_var_binding() {
    for command in ["assign", "refine"] {
        let fixture = GraphFixture::new(
            "var a = 1;\nvar a = a + 1;\nconsole.log(a);\n",
            &[(
                "values.yaml",
                "members: [{selector: {binding: {name: a}}}]\n",
            )],
        );
        let declarations = source_units_for_binding(&fixture, "a");
        assert_eq!(declarations.len(), 2);
        if command == "assign" {
            fixture.assert_success(&["bindings", "assign", "a:moved/value"]);
        } else {
            let batch = write_batch(json!(
                declarations
                    .into_iter()
                    .map(|id| { json!({"unit_id": id, "module": "moved/value"}) })
                    .collect::<Vec<_>>()
            ));
            fixture.assert_success(&[
                "modules",
                "refine",
                "--batch",
                batch.path().join("refine.json").to_str().unwrap(),
            ]);
        }
        assert!(!fixture.modules.join("values.yaml").exists());
        let destination: serde_yaml::Value =
            serde_yaml::from_slice(&fs::read(fixture.modules.join("moved/value.yaml")).unwrap())
                .unwrap();
        let members = destination["members"].as_sequence().unwrap();
        assert_eq!(members.len(), 1);
        assert_eq!(members[0]["selector"]["binding"]["name"], "a");
        fixture.assert_runs("2\n");
    }
}
