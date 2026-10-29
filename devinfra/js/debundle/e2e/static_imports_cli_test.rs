//! Original static import reachability is queryable even when authored claims fail.

use debundle_e2e_support::{TreeFixture, debundler_path, run_tree_fixture};
use serde_json::{Value, json};
use std::process::Command;

fn query(args: &[&str]) -> Value {
    query_with_target_source(args, "import './middle.js'; export const target = 1;\n")
}

fn query_with_target_source(args: &[&str], target_source: &str) -> Value {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[
                (
                    "main",
                    "import './middle.js'; import './missing.js'; import './other.js' with { type: 'javascript' }; export const value = 1;\n",
                ),
                ("middle", "import './target.js'; export const middle = 1;\n"),
                ("target", target_source),
                ("other", "export const other = 1;\n"),
                ("isolated", "export const isolated = 1;\n"),
            ],
            module_roots: &[("main", "main")],
            modules: &[(
                "main/invalid.yaml",
                "members: [{selector: {binding: {name: doesNotExist}}}]\n",
            )],
        },
        &[],
    );
    assert!(
        !run.result.status.success(),
        "fixture must reject authored selector"
    );
    let root = run.root();
    let output = Command::new(debundler_path())
        .arg("static-imports")
        .arg("--tree-config")
        .arg(root.join("spec_config.yaml"))
        .arg("--tree-modules")
        .arg(root.join("modules"))
        .arg("--tree-vendor-marks")
        .arg(root.join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(root)
        .arg("--out-root")
        .arg(&run.out_root)
        .args(args)
        .args(["--format", "json"])
        .output()
        .expect("run query");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).expect("JSON report");
    let relative = Command::new(debundler_path())
        .current_dir(root)
        .args([
            "static-imports",
            "--tree-config",
            "spec_config.yaml",
            "--tree-modules",
            "modules",
            "--tree-vendor-marks",
            "vendor_marks.yaml",
            "--tree-source-root",
            ".",
            "--out-root",
            "out",
        ])
        .args(args)
        .args(["--format", "json"])
        .output()
        .expect("run relative-path query");
    assert!(
        relative.status.success(),
        "{}",
        String::from_utf8_lossy(&relative.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&relative.stdout).expect("relative JSON report"),
        report
    );
    report
}

#[test]
fn target_to_importer_path_is_independent_of_target_self_cycle() {
    let report = query_with_target_source(
        &["--from", "main", "--target", "target"],
        "import './main.js'; export const target = 1;\n",
    );
    assert_eq!(report["targets"][0]["target_to_from"][0]["to"], "main");
    assert_eq!(
        report["targets"][0]["target_self_cycle"]
            .as_array()
            .unwrap()
            .len(),
        3
    );
}

#[test]
fn reports_transitive_witnesses_absence_and_distinct_cycles() {
    let report = query(&[
        "--from", "main", "--target", "target", "--target", "isolated", "--target", "main",
    ]);
    assert_eq!(
        report["targets"][0]["from_to_target"],
        json!([
            {"from":"main", "specifier":"./middle.js", "edge_kind":"ordinary_static_esm", "resolution":"source_path", "target_file":"entry.js", "to":"middle"},
            {"from":"middle", "specifier":"./target.js", "edge_kind":"ordinary_static_esm", "resolution":"source_path", "target_file":"entry.js", "to":"target"}
        ])
    );
    assert_eq!(report["targets"][0]["target_to_from"], Value::Null);
    assert_eq!(
        report["targets"][0]["target_self_cycle"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(report["targets"][1]["from_to_target"], Value::Null);
    assert_eq!(report["targets"][1]["target_self_cycle"], Value::Null);
    assert_eq!(report["targets"][2]["same_chunk"], true);
    assert_eq!(report["targets"][2]["from_to_target"], Value::Null);
    assert_eq!(report["targets"][2]["target_self_cycle"], Value::Null);
}

#[test]
fn distinguishes_unresolved_and_nonordinary_requests() {
    let report = query(&["--from", "main", "--target", "other", "--max-issues", "1"]);
    assert_eq!(report["targets"][0]["from_to_target"], Value::Null);
    assert_eq!(report["request_issue_count"], 2);
    assert_eq!(report["request_issues_truncated"], true);
    assert_eq!(
        report["request_issues"][0]["reason"],
        "unresolved_static_request"
    );
    let full = query(&["--from", "main", "--target", "other"]);
    assert_eq!(
        full["request_issues"][1]["reason"],
        "nonordinary_static_request"
    );
    assert_eq!(
        full["request_issues"][1]["resolved_target"],
        "other/entry.js"
    );
}

#[test]
fn rejects_target_absent_from_the_input_chunk_table() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("main", "export const value = 1;\n")],
            module_roots: &[("main", "main")],
            modules: &[],
        },
        &[],
    );
    let output = Command::new(debundler_path())
        .arg("static-imports")
        .arg("--tree-config")
        .arg(run.root().join("spec_config.yaml"))
        .arg("--tree-modules")
        .arg(run.root().join("modules"))
        .arg("--tree-vendor-marks")
        .arg(run.root().join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(run.root())
        .arg("--out-root")
        .arg(&run.out_root)
        .args(["--from", "main", "--target", "absent", "--format", "json"])
        .output()
        .expect("run query");
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("unknown --target chunk"));
}

#[test]
fn conditional_literal_loader_graph_includes_deferred_return_edge() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[
                ("main", "import './middle.js'; export const value = 1;\n"),
                (
                    "middle",
                    "const lazy = () => import.meta.require('./main.js'); export { lazy };\n",
                ),
            ],
            module_roots: &[("main", "main")],
            modules: &[],
        },
        &[],
    );
    let command = |include: bool| {
        let mut command = Command::new(debundler_path());
        command
            .arg("static-imports")
            .arg("--tree-config")
            .arg(run.root().join("spec_config.yaml"))
            .arg("--tree-modules")
            .arg(run.root().join("modules"))
            .arg("--tree-vendor-marks")
            .arg(run.root().join("vendor_marks.yaml"))
            .arg("--tree-source-root")
            .arg(run.root())
            .arg("--out-root")
            .arg(&run.out_root)
            .args(["--from", "middle", "--target", "main", "--format", "json"]);
        if include {
            command.args(["--include-host-loader-candidates", "--interop-census"]);
        }
        let output = command.output().expect("query");
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).expect("JSON report")
    };
    assert_eq!(command(false)["targets"][0]["from_to_target"], Value::Null);
    let report = command(true);
    let edge = &report["targets"][0]["from_to_target"][0];
    assert_eq!(edge["edge_kind"], "conditional_import_meta_require");
    assert_eq!(edge["to"], "main");
    assert_eq!(edge["source_item"], 0);
    assert_eq!(
        report["targets"][0]["target_to_from"][0]["edge_kind"],
        "ordinary_static_esm"
    );
    assert_eq!(report["interop_census"]["chunks"][0]["site_count"], 1);
    assert_eq!(
        report["interop_census"]["sites"][0]["resolved_target"],
        "main/entry.js"
    );
}

#[test]
fn census_keeps_unknown_loader_forms_and_unresolved_literals_visible() {
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[
                (
                    "main",
                    "const alias = import.meta.require; const dynamic = import.meta.require(name); const missing = import.meta.require('./missing.js'); const global = globalThis.require('./target.js'); const common = require('./target.js'); module.exports = alias; export { dynamic, missing, global, common };\n",
                ),
                ("target", "export const value = 1;\n"),
            ],
            module_roots: &[("main", "main")],
            modules: &[],
        },
        &[],
    );
    let output = Command::new(debundler_path())
        .arg("static-imports")
        .arg("--tree-config")
        .arg(run.root().join("spec_config.yaml"))
        .arg("--tree-modules")
        .arg(run.root().join("modules"))
        .arg("--tree-vendor-marks")
        .arg(run.root().join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(run.root())
        .arg("--out-root")
        .arg(&run.out_root)
        .args([
            "--from",
            "main",
            "--target",
            "target",
            "--include-host-loader-candidates",
            "--interop-census",
            "--format",
            "json",
        ])
        .output()
        .expect("query");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).expect("JSON report");
    assert_eq!(report["targets"][0]["from_to_target"], Value::Null);
    assert!(
        report["request_issues"]
            .as_array()
            .unwrap()
            .iter()
            .any(|issue| issue["reason"] == "unresolved_host_loader_candidate")
    );
    let kinds = &report["interop_census"]["chunks"][0]["kinds"];
    for kind in [
        "import_meta_require_reference",
        "import_meta_require_dynamic_call",
        "import_meta_require_literal_call",
        "global_this_require",
        "unbound_require",
        "unbound_module",
    ] {
        assert_eq!(kinds[kind], 1, "missing {kind}");
    }
    assert!(
        report["interop_census"]["sites"]
            .as_array()
            .unwrap()
            .iter()
            .any(|site| site["kind"] == "import_meta_require_literal_call"
                && site["resolution"] == "unresolved")
    );
}
