//! Real CLI coverage for the author-supplied ESM import priority.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::{
    FixtureOpts, Member, TreeFixture, assert_entry_output, assert_node_output, logical_module,
    run_debundle, run_emit_on_rejection_fixture_with_graphs, run_fixture_with_args,
    run_fixture_with_graphs, run_rejection_fixture, run_rejection_fixture_with_args,
    run_tree_fixture, run_tree_graph_fixture, unassigned_mode_inline, write_tree_fixture,
};
use std::fs;

fn priority_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"const hub = () => [a, b];
const a = { dependency: hub, lazy: () => b };
const b = { dependency: hub };
console.log(a.dependency === hub, a.lazy() === b, b.dependency === hub);
"#,
        vec![
            logical_module("hub", &[Member::new("hub")]),
            logical_module("a", &[Member::new("a")]),
            logical_module("b", &[Member::new("b")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline())
}

fn paths(priority: &[&str]) -> Vec<String> {
    priority.iter().map(|path| (*path).to_string()).collect()
}

#[test]
fn supplied_priority_is_retained_and_accepted_output_runs_under_node() {
    let fixture = run_fixture_with_graphs(priority_fixture().with_import_order(&["a", "b", "hub"]));

    assert_entry_output(&fixture, "true true true\n");
    assert_eq!(
        fixture
            .dependency_graph()
            .import_order
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
        paths(&["a", "b", "hub"]),
    );
}

#[test]
fn rejected_priority_is_not_repaired_and_invalid_universes_are_rejected() {
    let rejected = run_emit_on_rejection_fixture_with_graphs(
        priority_fixture().with_import_order(&["b", "a", "hub"]),
    );
    assert!(
        rejected.gate_rejected_marker().exists(),
        "the incompatible witness must remain rejected: {}",
        rejected.stderr,
    );
    assert_eq!(
        rejected
            .dependency_graph()
            .import_order
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
        paths(&["b", "a", "hub"]),
        "the rejected witness must be reported verbatim, without schedule repair",
    );

    for (description, priority) in [
        ("missing owner", &["a", "hub"][..]),
        ("duplicate owner", &["a", "a", "hub"][..]),
        ("unknown owner", &["a", "b", "hub", "ghost"][..]),
        (
            "entry witness",
            &["a", "b", "hub", "anon_residual_sentinel"][..],
        ),
    ] {
        let rejected = run_rejection_fixture(priority_fixture().with_import_order(priority));
        assert!(
            rejected.stderr.contains(
                "import priority must contain every emitted nonentry module exactly once"
            ),
            "{description} should fail the exact emitted-owner check: {}",
            rejected.stderr,
        );
    }
}

#[test]
fn materialized_all_entry_chunk_requires_explicit_empty_priority() {
    const CHUNKS: &[(&str, &str)] = &[("main", "const value = 7; console.log(value);\n")];
    const NO_MODULES: &[(&str, &str)] = &[];
    let tree = TreeFixture {
        chunks: CHUNKS,
        module_roots: &[],
        modules: NO_MODULES,
    };

    let successful = run_tree_fixture(&tree, &[]);
    assert!(
        successful.result.status.success(),
        "{}",
        successful.result.stderr
    );
    assert_node_output(&successful.out_root.join("app/main/entry.js"), "7\n", "");

    let missing_config = write_tree_fixture(&tree);
    let config_path = missing_config.root().join("spec_config.yaml");
    let mut config: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&config_path).unwrap()).unwrap();
    config
        .as_mapping_mut()
        .unwrap()
        .remove(serde_yaml::Value::String("import_order".to_string()));
    fs::write(&config_path, serde_yaml::to_string(&config).unwrap()).unwrap();
    let rejected = missing_config.run(&[]);
    assert!(!rejected.result.status.success());
    assert!(
        rejected.result.stderr.contains("requires import_order"),
        "missing materialized-chunk order should fail closed: {}",
        rejected.result.stderr,
    );

    let graph_run = run_tree_graph_fixture(&tree, &[]);
    assert!(
        graph_run.result.status.success(),
        "{}",
        graph_run.result.stderr
    );
    assert_node_output(&graph_run.out_root.join("app/main/entry.js"), "7\n", "");
    let graph_path = graph_run.report_root.join("main/dependency_graph.json.gz");
    let mut graph: serde_json::Value = report_io::read_json_gzip(&graph_path).unwrap();
    assert_eq!(graph["import_order"], serde_json::json!([]));
    graph.as_object_mut().unwrap().remove("import_order");
    report_io::write_json_gzip(&graph_path, &graph).unwrap();

    let batch_path = graph_run.root().join("empty-batch.json");
    fs::write(&batch_path, "[]\n").unwrap();
    let output = run_debundle(&[
        "modules",
        "check",
        "--graph",
        graph_path.to_str().unwrap(),
        "--modules",
        graph_run.root().join("modules").to_str().unwrap(),
        "--tree-config",
        graph_run.root().join("spec_config.yaml").to_str().unwrap(),
        "--source-root",
        graph_run.root().to_str().unwrap(),
        "--batch",
        batch_path.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        !output.status.success(),
        "graph-backed assignment verification accepted a report without import_order",
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("missing field") && stderr.contains("import_order"),
        "missing graph witness should be a schema error, got: {stderr}",
    );
}

#[test]
fn ownership_subset_requires_the_exact_final_priority_without_filtering_it() {
    let subset_root = tempfile::tempdir().unwrap();
    let subset_path = subset_root.path().join("subset.json");
    fs::write(
        &subset_path,
        r#"{"modules":[{"chunk":"static/app","module":"kept"}]}"#,
    )
    .unwrap();
    let args = ["--ownership-subset", subset_path.to_str().unwrap()];
    let fixture = || {
        FixtureOpts::new(
            "const kept = 40;\nconst removed = 2;\nconsole.log(kept + removed);\n",
            vec![
                logical_module("kept", &[Member::new("kept")]),
                logical_module("removed", &[Member::new("removed")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
    };
    let accepted = run_fixture_with_args(fixture().with_import_order(&["kept"]), &args);
    assert_entry_output(&accepted, "42\n");
    assert!(
        !accepted
            .out_root
            .join("static/app/modules/removed.js")
            .exists()
    );
    for invalid in [
        &["kept", "removed"][..],
        &["kept", "unknown"][..],
        &["kept", "kept"][..],
    ] {
        let rejected =
            run_rejection_fixture_with_args(fixture().with_import_order(invalid), &args, 1);
        assert!(
            rejected.stderr.contains(
                "import priority must contain every emitted nonentry module exactly once"
            ),
            "the supplied final priority must fail unchanged: {}",
            rejected.stderr,
        );
    }
}
