//! Ownership subsets keep semantic contracts from the full authored spec.

use debundle_e2e_support::*;
use std::process::Command;

fn subset(modules: &str) -> tempfile::NamedTempFile {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(file.path(), format!(r#"{{"modules":{modules}}}"#)).unwrap();
    file
}

#[test]
fn omitted_owner_keeps_its_callable_purity_contract() {
    const RUNTIME: &str = "globalThis.opaque = value => value;\nexport {};\n";
    let options = |purity| {
        FixtureOpts::new(
            r#"import './runtime.js';
function wrapper(value) { return globalThis.opaque(value); }
const A = wrapper("a");
const B = wrapper("b");
const C = wrapper("c");
console.log(A, B, C);
export { wrapper, A, B, C };
"#,
            vec![
                logical_module(
                    "functions/omitted",
                    &[Member::renamed("readableWrapper", "wrapper").with_purity(purity)],
                ),
                logical_module(
                    "values/outer",
                    &[
                        Member::renamed("readableA", "A"),
                        Member::renamed("readableC", "C"),
                    ],
                ),
                logical_module("values/middle", &[Member::renamed("readableB", "B")]),
            ],
        )
        .with_extra_files(&[
            ("static/runtime.js", RUNTIME),
            ("static/app/runtime.js", RUNTIME),
        ])
        .with_import_order(&["values/outer", "values/middle", "residual"])
    };
    let selection = subset(
        r#"[{"chunk":"static/app","module":"values/outer"},{"chunk":"static/app","module":"values/middle"}]"#,
    );
    let args = ["--ownership-subset", selection.path().to_str().unwrap()];
    let fixture = run_fixture(
        options(MemberPurity::Pure)
            .with_graphs()
            .with_extra_args(&args),
    );
    let graph_path = fixture
        .report_root
        .join(&fixture.chunk_id)
        .join("dependency_graph.json.gz");
    let graph: serde_json::Value = read_gzip_json(&graph_path);
    let expected_selection = serde_json::json!([
        {"module": "values/middle"},
        {"module": "values/outer"}
    ]);
    assert_eq!(graph["ownership_subset"], expected_selection);
    let program_path = graph_path
        .parent()
        .unwrap()
        .join(graph["program_graph"].as_str().unwrap());
    let program: serde_json::Value = read_gzip_json(&program_path);
    assert_eq!(
        program["provenance"]["selected_ownership_by_chunk"]["static/app"],
        expected_selection
    );
    assert!(program["provenance"]["roots"]["ownership_subset"].is_string());
    assert!(
        program["provenance"]["files"]
            .as_array()
            .unwrap()
            .iter()
            .any(|file| { file["root"] == "ownership_subset" && file["sha256"].is_string() })
    );
    assert_preserved_entry_output(&fixture, "a b c\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const outer = await import('./static/app/modules/values/outer.js');\nconst middle = await import('./static/app/modules/values/middle.js');\nconsole.log(JSON.stringify([Object.keys(outer).sort(), Object.keys(middle).sort()]));\n",
        "[[\"readableA\",\"readableC\"],[\"readableB\"]]\n",
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["function wrapper"],
        &["readableWrapper", "const A", "const B", "const C"],
    );
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/functions/omitted.js")
            .exists()
    );
    let rejected = run_rejection_fixture(
        options(MemberPurity::Default)
            .with_extra_args(&args)
            .with_rejection_exit(1),
    );
    assert!(
        rejected.stderr.contains("unrealizable"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn omitted_import_owner_keeps_retained_callback_timing() {
    const RUNTIME: &str = "globalThis.retained = callback => ({ invoke: callback });\nglobalThis.value = () => 42;\nexport {};\n";
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 1, &[0])
        .build()];
    let options = |assert_retention| {
        let options = FixtureOpts::new(
            r#"import './runtime.js';
import { factory as make } from './peer.js';
const callback = () => later;
const wrapped = make(callback);
const later = globalThis.value();
console.log(wrapped.invoke());
export { wrapped, later };
"#,
            vec![
                logical_module(
                    "imports/omitted",
                    &[Member::renamed_with_kind(
                        "readableFactory",
                        "make",
                        Some("import_specifier"),
                    )],
                ),
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("callback"),
                        Member::renamed("readableWrapped", "wrapped"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function factory(callback) { return globalThis.retained(callback); }\n",
        )])
        .with_extra_files(&[
            ("static/runtime.js", RUNTIME),
            ("static/app/runtime.js", RUNTIME),
        ])
        .with_import_order(&["values/later", "callbacks/wrapped"]);
        if assert_retention {
            options.with_chunk_export_purity(&contracts)
        } else {
            options
        }
    };
    let selection = subset(
        r#"[{"chunk":"static/app","module":"callbacks/wrapped"},{"chunk":"static/app","module":"values/later"}]"#,
    );
    let args = ["--ownership-subset", selection.path().to_str().unwrap()];
    let fixture = run_fixture(options(true).with_graphs().with_extra_args(&args));
    assert_preserved_entry_output(&fixture, "42\n");
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/imports/omitted.js")
            .exists()
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["const readableWrapped", "const callback"],
        &["readableFactory", "const later"],
    );
    let rejected =
        run_rejection_fixture(options(false).with_extra_args(&args).with_rejection_exit(1));
    assert!(
        rejected.stderr.contains("unrealizable"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn selected_graphs_preserve_crud_refusals_and_reject_stale_reuse() {
    let module_a =
        serde_yaml::to_string(&logical_module("values/a", &[Member::new("A")]).1).unwrap();
    let module_b =
        serde_yaml::to_string(&logical_module("values/b", &[Member::new("B")]).1).unwrap();
    let fixture = TreeFixture {
        chunks: &[(
            "app",
            "const A = 1;\nconst B = A + 1;\nconsole.log(B);\nexport { A, B };\n",
        )],
        module_roots: &[("main", "app")],
        modules: &[
            ("main/values/a.yaml", &module_a),
            ("main/values/b.yaml", &module_b),
        ],
    };
    let workspace = write_tree_fixture(&fixture);
    let root = workspace.root().to_path_buf();
    // The selected run has one final explicit owner. Its omitted claim remains
    // in the semantic base and routes back to the entry.
    let config_path = root.join("spec_config.yaml");
    let mut config: serde_yaml::Value =
        serde_yaml::from_slice(&std::fs::read(&config_path).unwrap()).unwrap();
    config["import_order"]["app"] = serde_yaml::to_value(["values/a"]).unwrap();
    std::fs::write(&config_path, serde_yaml::to_string(&config).unwrap()).unwrap();
    let subset_path = root.join("selected.json");
    std::fs::write(
        &subset_path,
        r#"{"modules":[{"chunk":"app","module":"values/a"}]}"#,
    )
    .unwrap();
    let run = workspace.run(&[
        "--graphs",
        "--ownership-subset",
        subset_path.to_str().unwrap(),
    ]);
    assert_node_output(&run.out_root.join("app/app/entry.js"), "2\n", "");

    let graph_path = run.report_root.join("app/dependency_graph.json.gz");
    let graph: serde_json::Value = read_gzip_json(&graph_path);
    assert_eq!(
        graph["ownership_subset"],
        serde_json::json!([{"module": "values/a"}])
    );
    let workers = tempfile::tempdir().unwrap();
    let destination = workers.path().join("worker");
    let snapshot_create = Command::new(debundler_path())
        .args(["snapshot", "create", "--graph"])
        .arg(&graph_path)
        .arg("--destination")
        .arg(&destination)
        .output()
        .unwrap();
    assert!(
        snapshot_create.status.success(),
        "{}",
        String::from_utf8_lossy(&snapshot_create.stderr)
    );
    let snapshot_outcome: serde_json::Value =
        serde_json::from_slice(&snapshot_create.stdout).unwrap();
    assert!(
        snapshot_outcome["regeneration_argv"]
            .as_array()
            .unwrap()
            .iter()
            .any(|arg| arg == "--ownership-subset")
    );
    let snapshot_manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(destination.join("snapshot.json")).unwrap()).unwrap();
    let copied_subset = destination.join(
        snapshot_manifest["run"]["ownership_subset"]
            .as_str()
            .unwrap(),
    );
    assert_eq!(
        std::fs::read(&copied_subset).unwrap(),
        std::fs::read(&subset_path).unwrap()
    );
    let snapshot_verify = Command::new(debundler_path())
        .args(["snapshot", "verify"])
        .arg(&destination)
        .output()
        .unwrap();
    assert!(
        snapshot_verify.status.success(),
        "{}",
        String::from_utf8_lossy(&snapshot_verify.stderr)
    );
    std::fs::write(&copied_subset, r#"{"modules":[]}"#).unwrap();
    let stale_snapshot = Command::new(debundler_path())
        .args(["snapshot", "verify"])
        .arg(&destination)
        .output()
        .unwrap();
    assert!(!stale_snapshot.status.success());

    let common = [
        "--graph-ownership-subset-root".to_owned(),
        root.display().to_string(),
    ];
    let chunk_modules = root.join("modules/main").display().to_string();
    let source_root = root.display().to_string();
    let config_path = config_path.display().to_string();
    let graph_path = graph_path.display().to_string();
    let mut cases = vec![
        (
            "bindings assign",
            vec![
                "bindings".to_owned(),
                "assign".to_owned(),
                "--graph".to_owned(),
                graph_path.clone(),
                "--modules".to_owned(),
                chunk_modules.clone(),
                "--source-root".to_owned(),
                source_root.clone(),
                "A:values/b".to_owned(),
            ],
        ),
        (
            "bindings unassign",
            vec![
                "bindings".to_owned(),
                "unassign".to_owned(),
                "--graph".to_owned(),
                graph_path.clone(),
                "--modules".to_owned(),
                chunk_modules.clone(),
                "--source-root".to_owned(),
                source_root.clone(),
                "A".to_owned(),
            ],
        ),
        (
            "modules merge",
            vec![
                "modules".to_owned(),
                "merge".to_owned(),
                "--graph".to_owned(),
                graph_path.clone(),
                "--modules".to_owned(),
                chunk_modules.clone(),
                "--source-root".to_owned(),
                source_root.clone(),
                "--target".to_owned(),
                "values/a".to_owned(),
                "values/b".to_owned(),
            ],
        ),
        (
            "modules delete",
            vec![
                "modules".to_owned(),
                "delete".to_owned(),
                "--graph".to_owned(),
                graph_path.clone(),
                "--modules".to_owned(),
                chunk_modules,
                "--source-root".to_owned(),
                source_root,
                "--force".to_owned(),
                "values/a".to_owned(),
            ],
        ),
    ];
    let dry_run_cases = cases
        .iter()
        .filter_map(|(name, args)| {
            let dry_run_name = match *name {
                "bindings assign" => "bindings assign dry-run",
                "bindings unassign" => "bindings unassign dry-run",
                "modules merge" => "modules merge dry-run",
                "modules delete" => "modules delete dry-run",
                _ => return None,
            };
            let mut dry_run_args = args.clone();
            dry_run_args.push("--dry-run".to_owned());
            Some((dry_run_name, dry_run_args))
        })
        .collect::<Vec<_>>();
    cases.extend(dry_run_cases);
    let spec_before_edits = read_tree(&root);
    for (command_name, command_args) in cases {
        let mut args = common.to_vec();
        args.extend(command_args);
        let result = Command::new(debundler_path()).args(&args).output().unwrap();
        assert!(
            !result.status.success(),
            "{command_name} unexpectedly accepted selected-ownership graph:\n{}",
            String::from_utf8_lossy(&result.stdout)
        );
        assert!(
            String::from_utf8_lossy(&result.stderr).contains("selected-ownership"),
            "{command_name} did not report the explicit selected-view refusal:\n{}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert_eq!(
            read_tree(&root),
            spec_before_edits,
            "{command_name} changed some spec, config, graph, or materialized file"
        );
    }

    std::fs::write(&subset_path, r#"{"modules":[]}"#).unwrap();
    let stale = Command::new(debundler_path())
        .args([
            "--graph-ownership-subset-root",
            root.to_str().unwrap(),
            "modules",
            "propose",
            "--graph",
            graph_path.as_str(),
            "--modules",
            root.join("modules").to_str().unwrap(),
            "--source-root",
            root.to_str().unwrap(),
            "--tree-config",
            config_path.as_str(),
            "--into",
            "candidate",
            "--format",
            "json",
        ])
        .output()
        .unwrap();
    assert_eq!(stale.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&stale.stderr).contains("graph compatibility mismatch"));
}
