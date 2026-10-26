//! Read-only module decomposition from gated same-module seed fragments.

use debundle_e2e_support::GraphFixture;
use serde_json::{Value, json};
use std::fs;
use std::process::Command;

fn assert_emitted_layout_and_output(
    fixture: &GraphFixture,
    module_paths: &[&str],
    expected_output: &str,
) {
    let source_path = fixture.source_path();
    let root = source_path.parent().unwrap().parent().unwrap();
    let output_root = tempfile::tempdir_in(root).unwrap();
    let output = Command::new(debundle_e2e_support::debundler_path())
        .arg("run")
        .arg("--tree-config")
        .arg(root.join("spec_config.yaml"))
        .arg("--tree-modules")
        .arg(root.join("modules"))
        .arg("--tree-vendor-marks")
        .arg(root.join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(root)
        .arg("--out-root")
        .arg(output_root.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let app = output_root.path().join("app/main");
    for module_path in module_paths {
        assert!(
            app.join(format!("{module_path}.js")).is_file(),
            "missing emitted module {module_path}"
        );
    }
    debundle_e2e_support::assert_node_output(&app.join("entry.js"), expected_output, "");
}

fn apply_decomposition(fixture: &GraphFixture, report: &Value) {
    let assignments =
        report["modules"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|module| {
                module["fragments"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .flat_map(|fragment| {
                        fragment["source_unit_ids"].as_array().unwrap().iter().map(
                            |source_unit_id| {
                                json!({
                                    "source_unit_id": source_unit_id,
                                    "module": fragment["suggested_module_path"],
                                })
                            },
                        )
                    })
            })
            .collect::<Vec<_>>();
    let batch = tempfile::NamedTempFile::new().unwrap();
    fs::write(batch.path(), serde_json::to_vec(&assignments).unwrap()).unwrap();
    fixture.assert_success(&[
        "modules",
        "refine",
        "--batch",
        batch.path().to_str().unwrap(),
    ]);
}

fn decomposition_path<'a>(report: &'a Value, source_unit_id: &Value) -> &'a str {
    report["modules"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|module| module["fragments"].as_array().unwrap())
        .find(|fragment| {
            fragment["source_unit_ids"]
                .as_array()
                .unwrap()
                .contains(source_unit_id)
        })
        .unwrap_or_else(|| {
            panic!("source_unit {source_unit_id} missing from decomposition {report}")
        })["suggested_module_path"]
        .as_str()
        .unwrap()
}

fn source_unit_id_for_binding(fixture: &GraphFixture, binding: &str) -> String {
    fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding.as_str() == binding)
        })
        .unwrap_or_else(|| panic!("missing source_unit for binding {binding:?}"))
        .id
}

fn source_unit_id_for_line(fixture: &GraphFixture, line: usize) -> String {
    fixture
        .dependency_graph()
        .nodes
        .into_iter()
        .find(|node| {
            node.source_location
                .as_ref()
                .is_some_and(|location| location.start_line == line)
        })
        .unwrap_or_else(|| panic!("missing source_unit on source line {line}"))
        .id
}

fn assert_source_unit_module(fixture: &GraphFixture, source_unit_id: &str, expected: &str) {
    assert_eq!(source_unit_module_path(fixture, source_unit_id), expected);
}

fn source_unit_module_path(fixture: &GraphFixture, source_unit_id: &str) -> String {
    let graph = fixture.dependency_graph();
    let source_unit = graph
        .nodes
        .iter()
        .find(|node| node.id == source_unit_id)
        .unwrap();
    graph
        .module(&source_unit.destination)
        .unwrap()
        .path
        .to_string()
}

fn ambiguous_decorator_fixture(with_duplicate_provider: bool) -> GraphFixture {
    let provider = if with_duplicate_provider {
        vec![(
            "other.yaml",
            "members: [{name: applyDecorators, selector: {binding: {name: decorateB}}}]\n",
        )]
    } else {
        vec![("other.yaml", "members: []\n")]
    };
    let mut modules = vec![(
        "home.yaml",
        r#"members:
  - {name: applyDecorators, selector: {binding: {name: decorateA}}}
  - {name: Widget, selector: {binding: {name: Widget}}}
anonymous_statements:
  - {match: 'Array.isArray([]);'}
  - source_match:
      match: 'applyDecorators([], Widget.prototype, "ready");'
"#,
    )];
    modules.extend(provider);
    GraphFixture::new(
        r#"function decorateA(_decorators, target, key) { target[key] = "A"; }
function decorateB(_decorators, target, key) { target[key] = "B"; }
class Widget {}
Array.isArray([]);
decorateA([], Widget.prototype, "ready");
console.log(new Widget().ready);
"#,
        &modules,
    )
}

#[test]
fn decomposition_coheres_anonymous_call_with_ambiguous_local_helper() {
    let fixture = ambiguous_decorator_fixture(true);
    let anonymous_source_unit = source_unit_id_for_line(&fixture, 5);
    assert_source_unit_module(&fixture, &anonymous_source_unit, "home");
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "A\n", "");

    let source_path = fixture.source_path();
    let source_root = source_path.parent().unwrap().to_str().unwrap();
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--preserve-selector-anchors",
        "--source-root",
        source_root,
    ]);
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    let relations = report["selector_anchor_relations"].as_array().unwrap();
    let relation = relations
        .iter()
        .find(|relation| relation["kind"] == "source_match_reference")
        .expect("anonymous source_match reference should retain its local helper origin");
    assert_eq!(
        relation["source"],
        json!({"kind": "anonymous_statement", "index": 1})
    );
    assert_eq!(relation["referenced_by"], "applyDecorators");

    let helper_source_unit = source_unit_id_for_binding(&fixture, "decorateA");
    let duplicate_source_unit = source_unit_id_for_binding(&fixture, "decorateB");
    assert_eq!(relation["source_unit_ids"][0], anonymous_source_unit);
    assert_eq!(relation["anchor_source_unit_ids"][0], helper_source_unit);
    assert_eq!(
        decomposition_path(&report, &json!(anonymous_source_unit)),
        decomposition_path(&report, &json!(helper_source_unit))
    );
    assert_ne!(
        decomposition_path(&report, &json!(helper_source_unit)),
        source_unit_module_path(&fixture, &duplicate_source_unit),
        "the unchanged duplicate provider keeps its original module"
    );

    apply_decomposition(&fixture, &report);
    fixture.assert_runs("A\n");
}

#[test]
fn decomposition_keeps_unique_anonymous_reference_separately_movable() {
    let fixture = ambiguous_decorator_fixture(false);
    let anonymous_source_unit = source_unit_id_for_line(&fixture, 5);
    assert_source_unit_module(&fixture, &anonymous_source_unit, "home");
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "A\n", "");

    let source_path = fixture.source_path();
    let source_root = source_path.parent().unwrap().to_str().unwrap();
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--preserve-selector-anchors",
        "--source-root",
        source_root,
    ]);
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert_eq!(report["selector_anchor_relations"], json!([]));
    let helper_source_unit = source_unit_id_for_binding(&fixture, "decorateA");
    assert_ne!(
        decomposition_path(&report, &json!(anonymous_source_unit)),
        decomposition_path(&report, &json!(helper_source_unit)),
        "a module-neutral unique reference remains free to move"
    );

    apply_decomposition(&fixture, &report);
    fixture.assert_runs("A\n");
}

#[test]
fn rejoining_restores_a_safe_home_and_keeps_an_unrelated_module() {
    let fixture = GraphFixture::new(
        "const alpha = 1;\nconst beta = 2;\nconst gamma = 3;\nconst outside = 4;\nconsole.log(alpha + beta + gamma + outside);\n",
        &[
            (
                "home.yaml",
                "members: [{selector: {binding: {name: alpha}}}, {selector: {binding: {name: beta}}}, {selector: {binding: {name: gamma}}}]\n",
            ),
            (
                "outside.yaml",
                "members: [{selector: {binding: {name: outside}}}]\n",
            ),
        ],
    );
    let home_before = fs::read(fixture.modules.join("home.yaml")).unwrap();
    let outside_before = fs::read(fixture.modules.join("outside.yaml")).unwrap();
    let missing_coarsening = fixture.command(&[
        "modules",
        "decompose",
        "--atomic",
        "--rejoin-original-modules",
    ]);
    assert_eq!(missing_coarsening.status.code(), Some(2));
    assert!(
        String::from_utf8_lossy(&missing_coarsening.stderr).contains("--coarsen-blocking-sccs")
    );
    let missing_rejoining = fixture.command(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-home-pass-limit",
        "1",
    ]);
    assert_eq!(missing_rejoining.status.code(), Some(2));
    assert!(
        String::from_utf8_lossy(&missing_rejoining.stderr).contains("--rejoin-original-modules")
    );
    let args = [
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ];
    let report = fixture.json(&args);
    assert_eq!(
        report,
        fixture.json(&args),
        "rejoining must be deterministic"
    );
    assert_eq!(report["coverage_complete"], true, "{report}");
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert_eq!(report["coarsening"]["stop_reason"], "accepted");
    let rejoining = &report["coarsening"]["original_home_rejoining"];
    assert_eq!(rejoining["stop_reason"], "converged", "{report}");
    assert_eq!(rejoining["attempted_contractions"], 1, "{report}");
    assert_eq!(rejoining["accepted_contractions"], 2, "{report}");
    let mut capped_args = args.to_vec();
    capped_args.extend(["--rejoin-pair-attempt-limit", "0"]);
    let capped = fixture.json(&capped_args);
    assert_eq!(capped["candidate_is_realizable"], true, "{capped}");
    assert_eq!(capped["coverage_complete"], true, "{capped}");
    assert_eq!(
        capped["coarsening"]["original_home_rejoining"]["stop_reason"], "converged",
        "{capped}"
    );
    assert_eq!(
        capped["coarsening"]["original_home_rejoining"]["accepted_contractions"], 2,
        "{capped}"
    );
    assert_eq!(
        capped["coarsening"]["original_home_rejoining"]["budget_limited_home_count"], 0,
        "{capped}"
    );
    assert!(
        rejoining["fully_restored_module_paths"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("home")),
        "{report}"
    );
    let groups = report["coarsening"]["groups"].as_array().unwrap();
    let home = groups
        .iter()
        .find(|group| group["suggested_module_path"] == "home")
        .expect("restored original home");
    assert_eq!(home["origin_module_paths"], serde_json::json!(["home"]));
    assert_eq!(
        home["binding_ids"],
        serde_json::json!(["alpha", "beta", "gamma"])
    );
    let outside = groups
        .iter()
        .find(|group| group["suggested_module_path"] == "outside")
        .expect("unrelated module remains at its path");
    assert_eq!(outside["binding_ids"], serde_json::json!(["outside"]));
    assert_eq!(
        home_before,
        fs::read(fixture.modules.join("home.yaml")).unwrap()
    );
    assert_eq!(
        outside_before,
        fs::read(fixture.modules.join("outside.yaml")).unwrap()
    );
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "10\n", "");
    assert_emitted_layout_and_output(&fixture, &["home", "outside"], "10\n");
}

#[test]
fn zero_pair_budget_still_restores_a_two_fragment_home() {
    let fixture = GraphFixture::new(
        "const left = 2;\nconst right = 3;\nconst outside = 4;\nconsole.log(left + right + outside);\n",
        &[
            (
                "home.yaml",
                "members: [{selector: {binding: {name: left}}}, {selector: {binding: {name: right}}}]\n",
            ),
            (
                "outside.yaml",
                "members: [{selector: {binding: {name: outside}}}]\n",
            ),
        ],
    );
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--rejoin-pair-attempt-limit",
        "0",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["coverage_complete"], true, "{report}");
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    let rejoining = &report["coarsening"]["original_home_rejoining"];
    assert_eq!(rejoining["stop_reason"], "converged", "{report}");
    assert_eq!(rejoining["accepted_contractions"], 1, "{report}");
    assert_eq!(rejoining["budget_limited_home_count"], 0, "{report}");
    assert!(
        rejoining["fully_restored_module_paths"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("home")),
        "{report}"
    );
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "9\n", "");
    assert_emitted_layout_and_output(&fixture, &["home", "outside"], "9\n");
}

#[test]
fn rejoining_keeps_an_unsafe_home_split_and_refine_applies_accepted_groups() {
    let fixture = GraphFixture::rejected(
        "const first = 1;\nconst middle = first + 1;\nconst last = middle + 1;\nconst spare = 4;\nconst extra = 5;\nconsole.log(last + spare + extra);\n",
        &[
            (
                "home.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: last}}}, {selector: {binding: {name: spare}}}, {selector: {binding: {name: extra}}}]\n",
            ),
            (
                "middle.yaml",
                "members: [{selector: {binding: {name: middle}}}]\n",
            ),
        ],
    );
    let middle_before = fs::read(fixture.modules.join("middle.yaml")).unwrap();
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["coverage_complete"], true, "{report}");
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert_eq!(report["cycles"], serde_json::json!([]), "{report}");
    assert_eq!(report["cross_rebinds"], serde_json::json!([]), "{report}");
    let rejoining = &report["coarsening"]["original_home_rejoining"];
    assert_eq!(rejoining["stop_reason"], "converged", "{report}");
    assert_eq!(rejoining["whole_home_pass_count"], 2, "{report}");
    assert!(rejoining.get("whole_home_pass_limit").is_none(), "{report}");
    // One rejected whole-home check, then pair fallback across two safe
    // contractions and a final pass. Do not retry the same rejected batch
    // after each safe pair within the home.
    assert_eq!(rejoining["attempted_contractions"], 7, "{report}");
    assert_eq!(rejoining["accepted_contractions"], 2, "{report}");
    assert!(
        rejoining["unrestored_module_paths"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("home")),
        "{report}"
    );
    let groups = report["coarsening"]["groups"].as_array().unwrap();
    let split: Vec<_> = groups
        .iter()
        .filter(|group| group["origin_module_paths"] == serde_json::json!(["home"]))
        .collect();
    assert_eq!(split.len(), 2, "{report}");

    let zero_passes = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--rejoin-home-pass-limit",
        "0",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(zero_passes["coverage_complete"], true, "{zero_passes}");
    assert_eq!(
        zero_passes["candidate_is_realizable"], true,
        "{zero_passes}"
    );
    let zero_rejoining = &zero_passes["coarsening"]["original_home_rejoining"];
    assert_eq!(
        zero_rejoining["stop_reason"], "whole_home_pass_limit_reached",
        "{zero_passes}"
    );
    assert_eq!(zero_rejoining["whole_home_pass_count"], 0, "{zero_passes}");
    assert_eq!(zero_rejoining["whole_home_pass_limit"], 0, "{zero_passes}");
    assert_eq!(zero_rejoining["attempted_contractions"], 0, "{zero_passes}");
    assert_eq!(zero_rejoining["accepted_contractions"], 0, "{zero_passes}");
    assert!(
        zero_rejoining["unrestored_module_paths"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("home")),
        "{zero_passes}"
    );

    let one_pass = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--rejoin-home-pass-limit",
        "1",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(one_pass["coverage_complete"], true, "{one_pass}");
    assert_eq!(one_pass["candidate_is_realizable"], true, "{one_pass}");
    assert_eq!(one_pass["cycles"], serde_json::json!([]), "{one_pass}");
    assert_eq!(
        one_pass["cross_rebinds"],
        serde_json::json!([]),
        "{one_pass}"
    );
    let one_pass_rejoining = &one_pass["coarsening"]["original_home_rejoining"];
    assert_eq!(
        one_pass_rejoining["stop_reason"], "whole_home_pass_limit_reached",
        "{one_pass}"
    );
    assert_eq!(one_pass_rejoining["whole_home_pass_count"], 1, "{one_pass}");
    assert_eq!(one_pass_rejoining["whole_home_pass_limit"], 1, "{one_pass}");
    assert!(
        one_pass_rejoining["accepted_contractions"]
            .as_u64()
            .unwrap()
            > 0,
        "{one_pass}"
    );
    assert!(
        one_pass_rejoining["unrestored_module_paths"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("home")),
        "{one_pass}"
    );

    let capped = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--rejoin-pair-attempt-limit",
        "0",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(capped["coverage_complete"], true, "{capped}");
    assert_eq!(capped["candidate_is_realizable"], true, "{capped}");
    assert_eq!(capped["cycles"], serde_json::json!([]), "{capped}");
    assert_eq!(capped["cross_rebinds"], serde_json::json!([]), "{capped}");
    let capped_rejoining = &capped["coarsening"]["original_home_rejoining"];
    assert_eq!(
        capped_rejoining["stop_reason"],
        "pair_attempt_limit_reached"
    );
    assert_eq!(capped_rejoining["pair_attempt_limit"], 0);
    assert_eq!(capped_rejoining["budget_limited_home_count"], 1);
    assert_eq!(
        capped_rejoining["budget_limited_home_paths"],
        serde_json::json!(["home"])
    );
    assert_eq!(capped_rejoining["accepted_contractions"], 0);
    let capped_home_groups = capped["coarsening"]["groups"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|group| group["origin_module_paths"] == serde_json::json!(["home"]))
        .count();
    assert!(capped_home_groups > split.len(), "{capped}");
    assert!(
        groups
            .iter()
            .any(|group| group["suggested_module_path"] == "middle"
                && group["binding_ids"] == serde_json::json!(["middle"])),
        "{report}"
    );

    let assignments: Vec<_> = split
        .iter()
        .flat_map(|group| {
            group["source_unit_ids"]
                .as_array()
                .unwrap()
                .iter()
                .map(|source_unit| {
                    serde_json::json!({
                        "source_unit_id": source_unit,
                        "module": group["suggested_module_path"],
                    })
                })
        })
        .collect();
    let batch_dir = tempfile::tempdir().unwrap();
    let batch_path = batch_dir.path().join("batch.json");
    fs::write(&batch_path, serde_json::to_vec(&assignments).unwrap()).unwrap();
    fixture.assert_success(&["modules", "refine", "--batch", batch_path.to_str().unwrap()]);
    assert_eq!(
        middle_before,
        fs::read(fixture.modules.join("middle.yaml")).unwrap(),
        "unrelated module must not be edited"
    );
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "12\n", "");
    let paths: Vec<&str> = split
        .iter()
        .map(|group| group["suggested_module_path"].as_str().unwrap())
        .collect();
    assert_emitted_layout_and_output(&fixture, &paths, "12\n");
}

#[test]
fn proposes_a_realizable_refinement_without_changing_the_spec() {
    let fixture = GraphFixture::rejected(
        "const first = 1;\nconst middle = first + 1;\nconst last = middle + 1;\nconsole.log(last);\n",
        &[
            (
                "bad.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: last}}}]\n",
            ),
            (
                "middle.yaml",
                "members: [{selector: {binding: {name: middle}}}]\n",
            ),
        ],
    );
    let unclaimed = fixture.modules.join("bad/part-1.yaml");
    fs::create_dir_all(unclaimed.parent().unwrap()).unwrap();
    fs::write(&unclaimed, "members: []\n").unwrap();
    let before = ["bad.yaml", "middle.yaml", "bad/part-1.yaml"]
        .into_iter()
        .map(|path| (path, std::fs::read(fixture.modules.join(path)).unwrap()))
        .collect::<Vec<_>>();

    let output = fixture.command(&[
        "modules",
        "decompose",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["strategy"], "seed", "{report}");
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert_eq!(report["coverage_complete"], true, "{report}");

    let bad = report["modules"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["module_id"] == "bad")
        .expect("the invalid module has a suggested refinement");
    let fragments = bad["fragments"].as_array().unwrap();
    assert_eq!(fragments.len(), 2, "{report}");
    let suggested_paths = fragments
        .iter()
        .map(|fragment| fragment["suggested_module_path"].as_str().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(suggested_paths, vec!["bad/part-2", "bad/part-3"]);
    let bindings = fragments
        .iter()
        .flat_map(|fragment| fragment["binding_ids"].as_array().unwrap())
        .map(Value::as_str)
        .collect::<Vec<_>>();
    assert_eq!(bindings, vec![Some("first"), Some("last")]);

    let atomic_report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(atomic_report["strategy"], "atomic", "{atomic_report}");
    assert_eq!(
        atomic_report["candidate_is_realizable"], true,
        "{atomic_report}"
    );
    let atomic_fragments = atomic_report["modules"][0]["fragments"].as_array().unwrap();
    assert_eq!(
        atomic_fragments
            .iter()
            .map(|fragment| fragment["suggested_module_path"].as_str().unwrap())
            .collect::<Vec<_>>(),
        suggested_paths
    );
    assert_eq!(
        before,
        ["bad.yaml", "middle.yaml", "bad/part-1.yaml"]
            .into_iter()
            .map(|path| (path, std::fs::read(fixture.modules.join(path)).unwrap()))
            .collect::<Vec<_>>(),
        "decomposition is a read-only review report"
    );

    fs::remove_file(fixture.modules.join("bad.yaml")).unwrap();
    fs::remove_file(unclaimed).unwrap();
    for fragment in atomic_fragments {
        let module_path = fragment["suggested_module_path"].as_str().unwrap();
        let members = fragment["binding_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|binding| {
                format!(
                    "{{selector: {{binding: {{name: {}}}}}}}",
                    serde_json::to_string(binding.as_str().unwrap()).unwrap()
                )
            })
            .collect::<Vec<_>>()
            .join(", ");
        let path = fixture.modules.join(format!("{module_path}.yaml"));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, format!("members: [{members}]\n")).unwrap();
    }
    fixture.assert_runs("3\n");
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "3\n", "");
}

#[test]
fn atomic_mode_leaves_cross_module_structural_atoms_on_their_current_paths() {
    let fixture = GraphFixture::rejected(
        "let count = 0;\nfunction bump() { count += 1; }\nconst outside = 7;\nconsole.log(count, typeof bump, outside);\n",
        &[
            (
                "state.yaml",
                "members: [{selector: {binding: {name: count}}}]\n",
            ),
            (
                "action.yaml",
                "members: [{selector: {binding: {name: bump}}}]\n",
            ),
            (
                "outside.yaml",
                "members: [{selector: {binding: {name: outside}}}]\n",
            ),
        ],
    );
    let outside_before = fs::read(fixture.modules.join("outside.yaml")).unwrap();
    let missing_scc_mode = fixture.command(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-atomic-conflicts",
    ]);
    assert_eq!(missing_scc_mode.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&missing_scc_mode.stderr).contains("--coarsen-blocking-sccs"));
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["strategy"], "atomic", "{report}");
    assert_eq!(report["candidate_is_realizable"], false, "{report}");
    let conflicts = report["atomic_group_conflicts"].as_array().unwrap();
    assert!(!conflicts.is_empty(), "{report}");
    let modules = conflicts[0]["candidate_modules"].as_array().unwrap();
    assert!(modules.iter().any(|path| path == "state"), "{report}");
    assert!(modules.iter().any(|path| path == "action"), "{report}");

    let coarsened = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(coarsened["candidate_is_realizable"], false, "{coarsened}");
    assert_eq!(
        coarsened["coarsening"]["stop_reason"], "atomic_group_conflicts",
        "{coarsened}"
    );
    assert!(
        coarsened["coarsening"]["rounds"]
            .as_array()
            .unwrap()
            .is_empty(),
        "an atomic conflict must stop before coarsening: {coarsened}"
    );

    let merged = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--coarsen-atomic-conflicts",
        "--rejoin-original-modules",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(merged["candidate_is_realizable"], true, "{merged}");
    assert_eq!(merged["coverage_complete"], true, "{merged}");
    assert!(
        merged["atomic_group_conflicts"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(merged["coarsening"]["stop_reason"], "accepted");
    assert_eq!(
        merged["coarsening"]["original_home_rejoining"]["stop_reason"],
        "converged"
    );
    let atomic_merges = merged["coarsening"]["atomic_conflict_merges"]
        .as_array()
        .unwrap();
    assert_eq!(atomic_merges.len(), 1, "{merged}");
    let atomic_merge = &atomic_merges[0];
    assert_eq!(
        atomic_merge["merged_candidate_paths"],
        serde_json::json!(["action", "state"]),
        "{merged}"
    );
    let reported_atomic_group_ids: std::collections::BTreeSet<_> = conflicts
        .iter()
        .map(|conflict| conflict["atomic_group_id"].as_str().unwrap())
        .collect();
    let merged_atomic_group_ids: std::collections::BTreeSet<_> = atomic_merge["atomic_group_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|atomic_group_id| atomic_group_id.as_str().unwrap())
        .collect();
    assert_eq!(
        merged_atomic_group_ids, reported_atomic_group_ids,
        "{merged}"
    );
    let target = atomic_merge["suggested_module_path"].as_str().unwrap();
    let groups = merged["coarsening"]["groups"].as_array().unwrap();
    let group = groups
        .iter()
        .find(|group| group["suggested_module_path"] == target)
        .expect("merged candidate group");
    assert_eq!(
        group["origin_module_paths"],
        serde_json::json!(["action", "state"])
    );
    assert_eq!(group["binding_ids"], serde_json::json!(["bump", "count"]));
    assert!(
        groups
            .iter()
            .any(|group| group["suggested_module_path"] == "outside"
                && group["binding_ids"] == serde_json::json!(["outside"])),
        "unrelated module must retain its path: {merged}"
    );

    let batch_dir = tempfile::tempdir().unwrap();
    let batch_path = batch_dir.path().join("batch.json");
    let assignments: Vec<_> = group["source_unit_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|source_unit| serde_json::json!({"source_unit_id": source_unit, "module": target}))
        .collect();
    fs::write(&batch_path, serde_json::to_vec(&assignments).unwrap()).unwrap();
    fixture.assert_success(&["modules", "refine", "--batch", batch_path.to_str().unwrap()]);
    assert_eq!(
        outside_before,
        fs::read(fixture.modules.join("outside.yaml")).unwrap()
    );
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "0 function 7\n", "");
    assert_emitted_layout_and_output(&fixture, &[target, "outside"], "0 function 7\n");
}

#[test]
fn coarsening_atomic_conflicts_keeps_residual_import_and_export_declarations() {
    let fixture = GraphFixture::rejected(
        "import './dependency.js';\nlet count = 0;\nfunction bump() { count += 1; }\nexport { count, bump };\n",
        &[
            (
                "state.yaml",
                "members: [{selector: {binding: {name: count}}}]\n",
            ),
            (
                "action.yaml",
                "members: [{selector: {binding: {name: bump}}}]\n",
            ),
        ],
    );
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--include-residual",
        "--coarsen-blocking-sccs",
        "--coarsen-atomic-conflicts",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert!(
        !report["coarsening"]["atomic_conflict_merges"]
            .as_array()
            .unwrap()
            .is_empty(),
        "the option must resolve a real structural conflict: {report}"
    );
    let graph = fixture.dependency_graph();
    let entry_path = graph
        .quotient
        .nodes
        .iter()
        .find(|module| module.residual)
        .unwrap()
        .path
        .to_string();
    let entry = report["coarsening"]["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| group["suggested_module_path"] == entry_path)
        .expect("entry group remains present");
    let entry_source_units: std::collections::BTreeSet<_> = entry["source_unit_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|source_unit| source_unit.as_str().unwrap())
        .collect();
    for source_unit in graph.nodes.iter().filter(|source_unit| {
        matches!(
            source_unit.statement_kind.to_string().as_str(),
            "import" | "export"
        )
    }) {
        assert!(
            entry_source_units.contains(source_unit.id.as_str()),
            "{report}"
        );
    }
}

#[test]
fn atomic_residual_mode_moves_body_atoms_but_keeps_entry_imports_and_exports() {
    let fixture = GraphFixture::new(
        "import './dependency.js';\nconst ready = 1;\nexport { ready };\nconsole.log(ready);\n",
        &[],
    );
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--include-residual",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["strategy"], "atomic_including_residual", "{report}");
    assert_eq!(report["coverage_complete"], true, "{report}");
    assert_eq!(report["candidate_is_realizable"], true, "{report}");

    let graph = fixture.dependency_graph();
    let entry_path = graph
        .quotient
        .nodes
        .iter()
        .find(|module| module.residual)
        .expect("residual entry module")
        .path
        .to_string();
    let entry_row = report["modules"]
        .as_array()
        .unwrap()
        .iter()
        .find(|module| module["module_id"] == entry_path)
        .expect("residual module refinement");
    let fragments = entry_row["fragments"].as_array().unwrap();
    let entry_fragment = fragments
        .iter()
        .find(|fragment| fragment["suggested_module_path"] == entry_path)
        .expect("import/export fragment remains in the entry");
    let entry_source_unit_ids = entry_fragment["source_unit_ids"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    for source_unit in graph.nodes.iter().filter(|source_unit| {
        matches!(
            source_unit.statement_kind.to_string().as_str(),
            "import" | "export"
        )
    }) {
        assert!(
            entry_source_unit_ids.contains(source_unit.id.as_str()),
            "entry declaration {} was moved: {report}",
            source_unit.id
        );
    }

    let suggested_paths = fragments
        .iter()
        .map(|fragment| fragment["suggested_module_path"].as_str().unwrap())
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(suggested_paths.len(), fragments.len(), "{report}");
    assert!(
        suggested_paths
            .iter()
            .any(|path| path.starts_with("decomposition/residual/")),
        "residual body atoms need active module destinations: {report}"
    );
}

#[test]
fn reports_unfixed_cross_module_cycles_instead_of_calling_them_safe() {
    let fixture = GraphFixture::rejected(
        "const alpha = beta;\nconst beta = alpha;\nconsole.log(alpha, beta);\n",
        &[
            (
                "a.yaml",
                "members: [{selector: {binding: {name: alpha}}}]\n",
            ),
            ("b.yaml", "members: [{selector: {binding: {name: beta}}}]\n"),
        ],
    );
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["candidate_is_realizable"], false, "{report}");
    assert_eq!(report["coverage_complete"], true, "{report}");
    assert!(!report["cycles"].as_array().unwrap().is_empty(), "{report}");
}

#[test]
fn coarsens_only_gate_blocking_sccs_and_the_reported_assignment_runs() {
    let fixture = GraphFixture::rejected(
        "const seed_value = \"alpha\";\nconst cross_value = seed_value + \"-beta\";\nfunction lazy_back() { return cross_value; }\nconsole.log(cross_value, lazy_back());\n",
        &[(
            "dependent.yaml",
            "members: [{selector: {binding: {name: cross_value}}}]\n",
        )],
    );
    let before = fs::read(fixture.modules.join("dependent.yaml")).unwrap();
    let atomic = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(atomic["candidate_is_realizable"], false, "{atomic}");
    assert!(!atomic["cycles"].as_array().unwrap().is_empty(), "{atomic}");
    assert!(
        atomic["atomic_group_conflicts"]
            .as_array()
            .unwrap()
            .is_empty(),
        "the red control must isolate a timing SCC: {atomic}"
    );

    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert_eq!(report["cycles"], serde_json::json!([]), "{report}");
    assert_eq!(report["coarsening"]["stop_reason"], "accepted", "{report}");
    assert_eq!(report["coarsening"]["rounds"].as_array().unwrap().len(), 1);
    let entry_path = fixture
        .dependency_graph()
        .quotient
        .nodes
        .iter()
        .find(|module| module.residual)
        .expect("the fixture has an entry module")
        .path
        .to_string();
    let group = report["coarsening"]["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| group["suggested_module_path"] == entry_path)
        .expect("an SCC containing the entry must keep its path");
    assert_eq!(group["suggested_module_path"], entry_path);
    assert_eq!(
        group["origin_module_paths"],
        serde_json::json!([entry_path, "dependent"])
    );
    assert_eq!(
        before,
        fs::read(fixture.modules.join("dependent.yaml")).unwrap()
    );

    debundle_e2e_support::assert_node_output(&fixture.source_path(), "alpha-beta alpha-beta\n", "");
    // The report co-locates cross_value with the original entry group. The
    // corresponding spec assignment is to leave the binding unclaimed.
    fs::remove_file(fixture.modules.join("dependent.yaml")).unwrap();
    fixture.assert_runs("alpha-beta alpha-beta\n");
}

#[test]
fn coarsening_an_entry_scc_keeps_import_and_export_source_units_at_the_entry_path() {
    let fixture = GraphFixture::rejected(
        "import './dependency.js';\nconst seed_value = \"alpha\";\nconst cross_value = seed_value + \"-beta\";\nfunction lazy_back() { return cross_value; }\nexport { seed_value, lazy_back };\n",
        &[(
            "dependent.yaml",
            "members: [{selector: {binding: {name: cross_value}}}]\n",
        )],
    );
    let report = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--graph",
        fixture.graph.to_str().unwrap(),
        "--modules",
        fixture.modules.to_str().unwrap(),
    ]);
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    assert_eq!(report["coarsening"]["stop_reason"], "accepted", "{report}");
    assert_eq!(report["coarsening"]["rounds"].as_array().unwrap().len(), 1);
    assert_eq!(
        report["coarsening"]["original_home_rejoining"]["stop_reason"], "converged",
        "{report}"
    );
    assert!(
        report["coarsening"]["original_home_rejoining"]["unrestored_module_paths"]
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("dependent")),
        "a home absorbed into a mixed-origin SCC is unrestored: {report}"
    );
    let graph = fixture.dependency_graph();
    let entry_path = graph
        .quotient
        .nodes
        .iter()
        .find(|module| module.residual)
        .expect("the fixture has an entry module")
        .path
        .to_string();
    let entry = report["coarsening"]["groups"]
        .as_array()
        .unwrap()
        .iter()
        .find(|group| group["suggested_module_path"] == entry_path)
        .expect("the SCC merge should retain the entry path");
    assert_eq!(
        entry["origin_module_paths"],
        serde_json::json!([entry_path.clone(), "dependent"]),
        "a mixed-origin SCC group must not be broken by rejoining: {report}"
    );
    let entry_source_units = entry["source_unit_ids"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    for source_unit in graph.nodes.iter().filter(|source_unit| {
        matches!(
            source_unit.statement_kind.to_string().as_str(),
            "import" | "export"
        )
    }) {
        assert!(
            entry_source_units.contains(source_unit.id.as_str()),
            "entry declaration {} moved from {entry_path}: {report}",
            source_unit.id
        );
    }
}

#[test]
fn source_aware_coarsening_reports_all_readable_name_collisions() {
    let fixture = GraphFixture::rejected(
        "const seed = 'a';\nconst first_value = seed + 'b', first_aux = seed + 'c';\nconst second_value = seed + 'd', second_aux = seed + 'e';\nfunction first_reader() { return first_value + first_aux; }\nfunction second_reader() { return second_value + second_aux; }\nconst unrelated = 7;\nconsole.log(first_reader(), second_reader(), unrelated);\n",
        &[
            (
                "first.yaml",
                "members:\n  - {name: Shared, selector: {binding: {name: first_value}}}\nsource_matches:\n  - match: \"const value = base + 'b', extra = base + 'c';\"\n    bindings: [{local: extra, name: AlsoShared}]\n",
            ),
            (
                "second.yaml",
                "members:\n  - {name: Shared, selector: {binding: {name: second_value}}}\nsource_matches:\n  - match: \"const value = base + 'd', extra = base + 'e';\"\n    bindings: [{local: extra, name: AlsoShared}]\n",
            ),
            (
                "unrelated.yaml",
                "members: [{name: Unique, selector: {binding: {name: unrelated}}}]\n",
            ),
        ],
    );
    let source_path = fixture.source_path();
    let source_root = source_path.parent().unwrap().to_str().unwrap();
    let args = [
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
        "--rejoin-original-modules",
        "--preserve-selector-anchors",
        "--source-root",
        source_root,
    ];
    let report = fixture.json(&args);
    assert_eq!(report["candidate_is_realizable"], true, "{report}");
    let collisions = report["coarsening"]["naming_collisions"]
        .as_array()
        .expect("source-aware coarsening should list naming collisions");
    assert_eq!(collisions.len(), 2, "{report}");
    assert_eq!(
        collisions
            .iter()
            .map(|collision| collision["readable_name"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["AlsoShared", "Shared"]
    );
    let graph = fixture.dependency_graph();
    for collision in collisions {
        let claims = collision["claims"].as_array().unwrap();
        assert_eq!(claims.len(), 2, "{collision}");
        assert_eq!(claims[0]["original_module_path"], "first");
        assert_eq!(claims[1]["original_module_path"], "second");
        let expected_bindings = if collision["readable_name"] == "Shared" {
            ["first_value", "second_value"]
        } else {
            ["first_aux", "second_aux"]
        };
        assert_eq!(
            claims
                .iter()
                .map(|claim| claim["binding_id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            expected_bindings
        );
        for claim in claims {
            let binding = claim["binding_id"].as_str().unwrap();
            let source_unit = graph
                .nodes
                .iter()
                .find(|source_unit| {
                    source_unit
                        .declared_bindings
                        .iter()
                        .any(|declared| declared.binding.as_str() == binding)
                })
                .unwrap();
            assert_eq!(
                claim["source_unit_ids"],
                serde_json::json!([source_unit.id])
            );
        }
    }
    assert!(
        report["coarsening"]["groups"]
            .as_array()
            .unwrap()
            .iter()
            .any(|group| { group["origin_module_paths"] == serde_json::json!(["unrelated"]) }),
        "the unrelated group should remain separate: {report}"
    );
    assert!(
        collisions
            .iter()
            .all(|collision| collision["suggested_module_path"] != "unrelated")
    );
    let graph_only = fixture.json(&[
        "modules",
        "decompose",
        "--atomic",
        "--coarsen-blocking-sccs",
    ]);
    assert!(graph_only["coarsening"].get("naming_collisions").is_none());
}
