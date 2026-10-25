//! Reviewed owner-to-module batches preserve existing selector metadata.

use debundle_e2e_support::{GraphFixture, TreeFixture, debundler_path, run_tree_fixture};
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;
use std::process::Command;

fn owner_for_binding(fixture: &GraphFixture, binding: &str) -> String {
    let graph: serde_json::Value =
        serde_json::from_slice(&fs::read(&fixture.graph).unwrap()).unwrap();
    graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|declared| {
                    declared
                        .iter()
                        .any(|declared| declared["binding"].as_str() == Some(binding))
                })
        })
        .unwrap_or_else(|| panic!("no graph owner declares {binding:?}"))["id"]
        .as_str()
        .unwrap()
        .to_string()
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
    let alpha = owner_for_binding(&fixture, "alpha");
    let beta = owner_for_binding(&fixture, "beta");
    let gamma = owner_for_binding(&fixture, "gamma");
    let graph: serde_json::Value =
        serde_json::from_slice(&fs::read(&fixture.graph).unwrap()).unwrap();
    let log = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| node["declared_bindings"].as_array().unwrap().is_empty())
        .expect("anonymous log owner")["id"]
        .as_str()
        .unwrap()
        .to_string();
    let batch = write_batch(json!([
        {"owner_id": alpha, "module": "values/alpha"},
        {"owner_id": beta, "module": "values/pair"},
        {"owner_id": gamma, "module": "values/gamma"},
        {"owner_id": log, "module": "effects/log"},
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
fn rejects_atom_split_without_changing_any_module_yaml() {
    let fixture = GraphFixture::atomic_pair();
    let batch = write_batch(json!([{
        "owner_id": owner_for_binding(&fixture, "alpha"),
        "module": "split/alpha",
    }]));
    fixture.assert_rejected_unchanged(
        &[
            "modules",
            "refine",
            "--batch",
            batch.path().join("refine.json").to_str().unwrap(),
        ],
        &["splits one or more atomic units"],
    );
}

#[test]
fn refines_only_the_selected_chunk_under_tree_config() {
    let run = run_tree_fixture(
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
    let graph = run.report_root.join("main/owner_graph.json");
    let batch = run.root().join("refine.json");
    let graph_report: serde_json::Value =
        serde_json::from_slice(&fs::read(&graph).unwrap()).unwrap();
    let owner = graph_report["nodes"]
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
        serde_json::to_vec(&json!([{"owner_id": owner, "module": "chunks/Main/existingCASE"}]))
            .unwrap(),
    )
    .unwrap();
    let output = Command::new(debundler_path())
        .args(["modules", "refine", "--batch"])
        .arg(&batch)
        .args(["--modules"])
        .arg(&modules)
        .args(["--graph"])
        .arg(&graph)
        .args(["--source-root"])
        .arg(run.root().join("snapshot"))
        .args(["--tree-config"])
        .arg(run.root().join("spec_config.yaml"))
        .output()
        .unwrap();
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
fn rejects_unclaimed_scaffolding_and_duplicate_owner_rows() {
    let residual = GraphFixture::new("const alpha = 1;\nconsole.log(alpha);\n", &[]);
    let unclaimed_batch = write_batch(json!([{
        "owner_id": owner_for_binding(&residual, "alpha"),
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
    let graph: serde_json::Value =
        serde_json::from_slice(&fs::read(&exported.graph).unwrap()).unwrap();
    let export_owner = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| node["statement_kind"] == "export")
        .expect("export scaffold owner")["id"]
        .as_str()
        .unwrap();
    let export_batch = write_batch(json!([{
        "owner_id": export_owner,
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
    let alpha = owner_for_binding(&claimed, "alpha");
    let duplicate_batch = write_batch(json!([
        {"owner_id": alpha, "module": "state/alpha"},
        {"owner_id": alpha, "module": "state/other"},
    ]));
    claimed.assert_rejected_unchanged(
        &[
            "modules",
            "refine",
            "--batch",
            duplicate_batch.path().join("refine.json").to_str().unwrap(),
        ],
        &["owner assignment batch repeats"],
    );
}

#[test]
fn atomic_decomposition_can_preserve_local_selector_anchors() {
    let fixture = GraphFixture::new(
        r#"var getDescriptor = Object.getOwnPropertyDescriptor;
var defineProperty = Object.defineProperty;
var decorate = (decorators, target, key) => {
  const descriptor = getDescriptor(target, key);
  for (const decorator of decorators) decorator(target, key, descriptor);
  defineProperty(target, key, descriptor);
};
const tag = (target, key, descriptor) => {
  const original = descriptor.value;
  descriptor.value = function () { return original.call(this) + "!"; };
};
class C { greet() { return "hi"; } }
const marker = 7;
decorate([tag], C.prototype, "greet");
console.log(new C().greet());
"#,
        &[
            (
                "model.yaml",
                "members: [{name: DecoratedClass, selector: {binding: {name: C}}}]\n",
            ),
            (
                "runtime/decorators.yaml",
                "members:\n  - name: applyDecorators\n    selector: {makes_decorate_call: {class: DecoratedClass}}\n  - name: defineProp\n    selector: {intrinsic_alias: {property: defineProperty, referenced_by: applyDecorators}}\n  - name: getPropDesc\n    selector: {intrinsic_alias: {property: getOwnPropertyDescriptor, referenced_by: applyDecorators}}\n  - name: marker\n    selector: {binding: {name: marker}}\n",
            ),
        ],
    );

    fixture.assert_runs("hi!\n");
    let model_yaml = fs::read(fixture.modules.join("model.yaml")).unwrap();
    let decorators_yaml = fs::read(fixture.modules.join("runtime/decorators.yaml")).unwrap();

    let atomic = fixture.json(&["modules", "decompose", "--atomic"]);
    assert_eq!(atomic["candidate_is_realizable"], true);
    assert_eq!(atomic.get("preserve_selector_anchors"), None);
    let mut atomic_paths = BTreeMap::new();
    let mut atomic_rows = Vec::new();
    for module in atomic["modules"].as_array().unwrap() {
        for fragment in module["fragments"].as_array().unwrap() {
            let suggested_module = fragment["suggested_module_path"].as_str().unwrap();
            for owner_id in fragment["owner_ids"].as_array().unwrap() {
                let owner_id = owner_id.as_str().unwrap();
                atomic_paths.insert(owner_id.to_string(), suggested_module.to_string());
                atomic_rows.push(json!({
                    "owner_id": owner_id,
                    "module": suggested_module,
                }));
            }
        }
    }
    assert_ne!(
        atomic_paths[&owner_for_binding(&fixture, "decorate")],
        atomic_paths[&owner_for_binding(&fixture, "getDescriptor")],
        "graph-only atomic decomposition should expose the local anchor split"
    );
    let invalid_batch = write_batch(serde_json::Value::Array(atomic_rows));
    fixture.assert_rejected_unchanged(
        &[
            "modules",
            "refine",
            "--batch",
            invalid_batch.path().join("refine.json").to_str().unwrap(),
        ],
        &[
            "selector anchor",
            "does not name a lowered member in module",
        ],
    );

    let source_path = fixture.source_path();
    let source_root = source_path.parent().unwrap().to_str().unwrap();
    let cohesive = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--preserve-selector-anchors",
        "--source-root",
        source_root,
    ]);
    assert_eq!(cohesive["candidate_is_realizable"], true);
    assert_eq!(cohesive["preserve_selector_anchors"], true);
    assert_eq!(
        cohesive["selector_anchor_relations"]
            .as_array()
            .unwrap()
            .len(),
        2
    );

    let mut cohesive_paths = BTreeMap::new();
    let mut cohesive_rows = Vec::new();
    for module in cohesive["modules"].as_array().unwrap() {
        for fragment in module["fragments"].as_array().unwrap() {
            let suggested_module = fragment["suggested_module_path"].as_str().unwrap();
            for owner_id in fragment["owner_ids"].as_array().unwrap() {
                let owner_id = owner_id.as_str().unwrap();
                cohesive_paths.insert(owner_id.to_string(), suggested_module.to_string());
                cohesive_rows.push(json!({
                    "owner_id": owner_id,
                    "module": suggested_module,
                }));
            }
        }
    }
    let decorate_path = &cohesive_paths[&owner_for_binding(&fixture, "decorate")];
    assert_eq!(
        decorate_path,
        &cohesive_paths[&owner_for_binding(&fixture, "getDescriptor")]
    );
    assert_eq!(
        decorate_path,
        &cohesive_paths[&owner_for_binding(&fixture, "defineProperty")]
    );
    assert_ne!(
        decorate_path,
        &cohesive_paths[&owner_for_binding(&fixture, "marker")],
        "unrelated owners should remain independently decomposable"
    );
    assert_eq!(
        fs::read(fixture.modules.join("model.yaml")).unwrap(),
        model_yaml,
        "decompose must not edit the spec"
    );
    assert_eq!(
        fs::read(fixture.modules.join("runtime/decorators.yaml")).unwrap(),
        decorators_yaml,
        "decompose must not edit the spec"
    );

    let batch = write_batch(serde_json::Value::Array(cohesive_rows));
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().join("refine.json").to_str().unwrap(),
    ]);
    fixture.assert_runs("hi!\n");
}
