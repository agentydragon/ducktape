//! Corpus proposal runs stay chunk-local and compose into the real tree pipeline.

use super::support::{CHAIN_SOURCE, proposal_args, restore_tree, run};
use debundle_e2e_support::{
    TreeFixture, assert_node_output, parse_stdout_json, read_gzip_json, read_tree,
    run_tree_graph_fixture, write_tree_fixture,
};
use serde_json::Value;
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

const CHUNKS: &[(&str, &str)] = &[
    ("alpha", CHAIN_SOURCE),
    ("beta", CHAIN_SOURCE),
    ("extra", CHAIN_SOURCE),
];
const ROOTS: &[(&str, &str)] = &[("chunks/alpha", "alpha"), ("chunks/beta", "beta")];
const MODULES: &[(&str, &str)] = &[
    (
        "chunks/alpha/home.yaml",
        "members: [{selector: {binding: {name: anchor}}}]\n",
    ),
    (
        "chunks/beta/home.yaml",
        "members: [{selector: {binding: {name: anchor}}}]\n",
    ),
];

fn fixture() -> TreeFixture<'static> {
    TreeFixture {
        chunks: CHUNKS,
        module_roots: ROOTS,
        modules: MODULES,
    }
}

fn graph_ids(graph: &Path) -> BTreeSet<String> {
    read_gzip_json::<Value>(graph)["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|node| node["id"].as_str().unwrap().to_string())
        .collect()
}

fn graph_for(run_root: &Path, chunk: &str) -> PathBuf {
    run_root
        .join("out/reports/tree")
        .join(chunk)
        .join("dependency_graph.json.gz")
}

#[test]
fn corpus_proposes_independently_for_configured_chunks_and_plans_run_under_node() {
    let run_tree = run_tree_graph_fixture(&fixture(), &[]);
    assert!(
        run_tree.result.status.success(),
        "{}",
        run_tree.result.stderr
    );
    let root = run_tree.root();
    let output = run(&proposal_args(root, "--graphs", &run_tree.report_root));
    assert!(
        output.status.success(),
        "stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let corpus = parse_stdout_json(&output);
    assert_eq!(corpus["status"], "found", "{corpus}");
    assert_eq!(corpus["scope"]["evaluated"], "configured_chunks_only");
    assert_eq!(
        corpus["scope"]["configured_chunks"],
        serde_json::json!(["alpha", "beta"])
    );
    assert_eq!(
        corpus["scope"]["skipped_unconfigured_graphs"][0]["chunk_id"],
        "extra"
    );
    let rows = corpus["chunks"].as_array().unwrap();
    assert_eq!(rows.len(), 2, "{corpus}");
    assert_eq!(rows[0]["chunk_id"], "alpha");
    assert_eq!(rows[1]["chunk_id"], "beta");

    let alpha_graph = graph_for(root, "alpha");
    let beta_graph = graph_for(root, "beta");
    assert_eq!(
        graph_ids(&alpha_graph),
        graph_ids(&beta_graph),
        "equal source-unit IDs in different chunks must remain chunk-local"
    );

    let mut plans = Vec::new();
    for (row, graph) in rows.iter().zip([&alpha_graph, &beta_graph]) {
        let single = run(&proposal_args(root, "--graph", graph));
        assert!(
            single.status.success(),
            "single-graph proposal failed: {}",
            String::from_utf8_lossy(&single.stderr)
        );
        let mut single_report = parse_stdout_json(&single);
        let single_plan = single_report["plan"].clone();
        single_report.as_object_mut().unwrap().remove("plan");
        assert_eq!(row["search"], single_report, "{}", row["chunk_id"]);
        assert_eq!(row["plan"], single_plan, "{}", row["chunk_id"]);
        assert_eq!(row["search"]["status"], "found", "{}", row["chunk_id"]);
        assert_eq!(
            row["search"]["scope"]["destination"], "extracted/peel_0000",
            "default destination stays logical and needs no explicit --into: {row}"
        );
        assert!(
            !row["plan"]["changes"].as_array().unwrap().is_empty(),
            "{}",
            row
        );
        plans.push((
            row["chunk_id"].as_str().unwrap().to_string(),
            row["plan"].clone(),
            graph.clone(),
        ));
    }

    // Identical source-unit IDs produce identical logical plans, but each saved
    // plan is bound to its own graph snapshot and is applied in its own module
    // subtree. Restore the starting tree between applies because each snapshot
    // records whole-tree YAML membership.
    assert_eq!(plans[0].1["changes"], plans[1].1["changes"]);
    let modules_root = root.join("modules");
    let original_modules = read_tree(&modules_root);
    let spec_config = root.join("spec_config.yaml");
    let original_spec_config = fs::read(&spec_config).unwrap();
    for (chunk, plan, graph) in &plans {
        let plan_path = root.join(format!("{chunk}-plan.json"));
        fs::write(&plan_path, serde_json::to_vec(plan).unwrap()).unwrap();
        let check = run(&[
            "modules".into(),
            "check".into(),
            "--source-root".into(),
            root.join("snapshot").to_string_lossy().into_owned(),
            "--plan".into(),
            plan_path.to_string_lossy().into_owned(),
            "--graph".into(),
            graph.to_string_lossy().into_owned(),
            "--modules".into(),
            root.join("modules").to_string_lossy().into_owned(),
            "--tree-config".into(),
            root.join("spec_config.yaml").to_string_lossy().into_owned(),
            "--format".into(),
            "json".into(),
        ]);
        assert!(
            check.status.success(),
            "check {chunk} failed:\n{}",
            String::from_utf8_lossy(&check.stderr)
        );
        assert_eq!(parse_stdout_json(&check)["accepted"], true);
        let before = read_tree(&modules_root);
        let preview = run(&[
            "modules".into(),
            "apply".into(),
            "--plan".into(),
            plan_path.to_string_lossy().into_owned(),
            "--graph".into(),
            graph.to_string_lossy().into_owned(),
            "--modules".into(),
            modules_root.to_string_lossy().into_owned(),
            "--tree-config".into(),
            root.join("spec_config.yaml").to_string_lossy().into_owned(),
            "--source-root".into(),
            root.join("snapshot").to_string_lossy().into_owned(),
            "--dry-run".into(),
            "--format".into(),
            "json".into(),
        ]);
        assert!(
            preview.status.success(),
            "dry-run {chunk} failed:\n{}",
            String::from_utf8_lossy(&preview.stderr)
        );
        assert_eq!(read_tree(&modules_root), before);
        let apply = run(&[
            "modules".into(),
            "apply".into(),
            "--plan".into(),
            plan_path.to_string_lossy().into_owned(),
            "--graph".into(),
            graph.to_string_lossy().into_owned(),
            "--modules".into(),
            root.join("modules").to_string_lossy().into_owned(),
            "--tree-config".into(),
            root.join("spec_config.yaml").to_string_lossy().into_owned(),
            "--source-root".into(),
            root.join("snapshot").to_string_lossy().into_owned(),
            "--format".into(),
            "json".into(),
        ]);
        assert!(
            apply.status.success(),
            "apply {chunk} failed:\n{}",
            String::from_utf8_lossy(&apply.stderr)
        );
        let out_after = root.join(format!("out-after-{chunk}"));
        let rerun = run(&[
            "run".into(),
            "--tree-config".into(),
            root.join("spec_config.yaml").to_string_lossy().into_owned(),
            "--tree-modules".into(),
            modules_root.to_string_lossy().into_owned(),
            "--tree-vendor-marks".into(),
            root.join("vendor_marks.yaml")
                .to_string_lossy()
                .into_owned(),
            "--tree-source-root".into(),
            root.to_string_lossy().into_owned(),
            "--out-root".into(),
            out_after.to_string_lossy().into_owned(),
            "--graphs".into(),
        ]);
        assert!(
            rerun.status.success(),
            "tree rerun after applying {chunk} failed:\n{}",
            String::from_utf8_lossy(&rerun.stderr)
        );
        assert_node_output(
            &out_after.join(format!("app/{chunk}/entry.js")),
            "1 3\n",
            "",
        );
        restore_tree(&modules_root, &original_modules);
        fs::write(&spec_config, &original_spec_config).unwrap();
    }
}

#[test]
fn corpus_reports_non_entry_residual_routing_without_claiming_exhaustion() {
    let fixture = TreeFixture {
        chunks: &[("alpha", "const value = 1;\nconsole.log(value);\n")],
        module_roots: &[("chunks/alpha", "alpha")],
        modules: &[],
    };
    let workspace = write_tree_fixture(&fixture);
    let root = workspace.root().to_path_buf();
    let config_path = root.join("spec_config.yaml");
    let mut config: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&config_path).unwrap()).unwrap();
    config["unassigned_mode"]["alpha"] = serde_yaml::from_str("{kind: catchall_file}").unwrap();
    config["import_order"]["alpha"] = serde_yaml::to_value(["residual"]).unwrap();
    fs::write(&config_path, serde_yaml::to_string(&config).unwrap()).unwrap();
    let run_tree = workspace.run(&["--graphs"]);
    assert!(
        run_tree.result.status.success(),
        "{}",
        run_tree.result.stderr
    );

    let output = run(&proposal_args(&root, "--graphs", &run_tree.report_root));
    assert!(
        output.status.success(),
        "stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let corpus = parse_stdout_json(&output);
    assert_eq!(corpus["status"], "unsupported_residual_routing", "{corpus}");
    let row = &corpus["chunks"][0];
    assert_eq!(row["search"]["status"], "unsupported_residual_routing");
    assert!(
        row["plan"].is_null(),
        "unsupported routing is not an executable plan"
    );
    assert_eq!(
        row["search"]["unsupported_residual_routing"][0]["module"],
        "residual"
    );
    assert!(
        !row["search"]["unsupported_residual_routing"][0]["source_units"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn corpus_requires_all_configured_graphs_rejects_duplicates_and_stale_secondary_provenance() {
    let run_tree = run_tree_graph_fixture(&fixture(), &[]);
    assert!(
        run_tree.result.status.success(),
        "{}",
        run_tree.result.stderr
    );
    let root = run_tree.root();
    let graphs_root = run_tree.report_root.clone();

    let beta_graph = graph_for(root, "beta");
    fs::remove_file(&beta_graph).unwrap();
    let missing = run(&proposal_args(root, "--graphs", &graphs_root));
    assert!(!missing.status.success());
    let missing_error = String::from_utf8_lossy(&missing.stderr);
    assert!(
        missing_error.contains("missing configured graph inputs"),
        "{missing_error}"
    );
    fs::create_dir_all(graphs_root.join("duplicate-alpha")).unwrap();
    fs::copy(
        graph_for(root, "alpha"),
        graphs_root.join("duplicate-alpha/dependency_graph.json.gz"),
    )
    .unwrap();
    let duplicate = run(&proposal_args(root, "--graphs", &graphs_root));
    assert!(!duplicate.status.success());
    let duplicate_error = String::from_utf8_lossy(&duplicate.stderr);
    assert!(
        duplicate_error.contains("multiple dependency graphs"),
        "{duplicate_error}"
    );

    // Use a fresh tree with all configured reports, then mutate only the
    // secondary chunk's source after report generation.
    let stale_tree = run_tree_graph_fixture(&fixture(), &[]);
    assert!(
        stale_tree.result.status.success(),
        "{}",
        stale_tree.result.stderr
    );
    let stale_root = stale_tree.root();
    fs::write(
        stale_root.join("snapshot/beta.js"),
        "const anchor = 1;\nconst dep = 2;\nconst consumer = dep + 1;\nconsole.log(anchor, consumer, 'stale');\n",
    )
    .unwrap();
    let stale = run(&proposal_args(
        stale_root,
        "--graphs",
        &stale_tree.report_root,
    ));
    assert!(!stale.status.success());
    assert!(
        String::from_utf8_lossy(&stale.stderr).contains("mismatch"),
        "{}",
        String::from_utf8_lossy(&stale.stderr)
    );
    assert!(
        !String::from_utf8_lossy(&stale.stdout).contains("\"chunks\""),
        "verification must finish for the whole corpus before a search report is emitted: {}\n{}",
        String::from_utf8_lossy(&stale.stdout),
        String::from_utf8_lossy(&stale.stderr)
    );
}

#[test]
fn retired_search_budget_and_destination_modes_are_rejected() {
    let run_tree = run_tree_graph_fixture(&fixture(), &[]);
    assert!(
        run_tree.result.status.success(),
        "{}",
        run_tree.result.stderr
    );
    let root = run_tree.root();
    for (flag, value) in [
        ("--destinations", "new"),
        ("--max-new-modules", "1"),
        ("--max-checks", "0"),
    ] {
        let mut args = proposal_args(root, "--graphs", &run_tree.report_root);
        args.extend([flag.into(), value.into()]);
        let output = run(&args);
        assert_eq!(output.status.code(), Some(2), "{flag}: {output:?}");
        assert!(output.stdout.is_empty(), "{flag}: {output:?}");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(flag),
            "{flag}: {output:?}"
        );
    }
}
