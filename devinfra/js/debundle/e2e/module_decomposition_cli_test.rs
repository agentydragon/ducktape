//! Read-only module decomposition from gated same-module seed fragments.

use debundle_e2e_support::GraphFixture;
use serde_json::Value;
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

#[test]
fn rejoining_restores_a_safe_home_and_keeps_an_unrelated_module() {
    let fixture = GraphFixture::new(
        "const alpha = 1;\nconst beta = 2;\nconst outside = 3;\nconsole.log(alpha + beta + outside);\n",
        &[
            (
                "home.yaml",
                "members: [{selector: {binding: {name: alpha}}}, {selector: {binding: {name: beta}}}]\n",
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
    assert!(rejoining["accepted_contractions"].as_u64().unwrap() >= 1);
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
    assert_eq!(home["binding_ids"], serde_json::json!(["alpha", "beta"]));
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
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "6\n", "");
    assert_emitted_layout_and_output(&fixture, &["home", "outside"], "6\n");
}

#[test]
fn rejoining_keeps_an_unsafe_home_split_and_refine_applies_accepted_groups() {
    let fixture = GraphFixture::rejected(
        "const first = 1;\nconst middle = first + 1;\nconst last = middle + 1;\nconsole.log(last);\n",
        &[
            (
                "home.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: last}}}]\n",
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
    assert!(rejoining["attempted_contractions"].as_u64().unwrap() >= 1);
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
            group["owner_ids"].as_array().unwrap().iter().map(|owner| {
                serde_json::json!({
                    "owner_id": owner,
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
    debundle_e2e_support::assert_node_output(&fixture.source_path(), "3\n", "");
    let paths: Vec<&str> = split
        .iter()
        .map(|group| group["suggested_module_path"].as_str().unwrap())
        .collect();
    assert_emitted_layout_and_output(&fixture, &paths, "3\n");
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
    let conflicts = report["atomic_unit_conflicts"].as_array().unwrap();
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
        coarsened["coarsening"]["stop_reason"], "atomic_unit_conflicts",
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
        merged["atomic_unit_conflicts"]
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
    let reported_units: std::collections::BTreeSet<_> = conflicts
        .iter()
        .map(|conflict| conflict["unit_id"].as_str().unwrap())
        .collect();
    let merged_units: std::collections::BTreeSet<_> = atomic_merge["unit_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|unit| unit.as_str().unwrap())
        .collect();
    assert_eq!(merged_units, reported_units, "{merged}");
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
    let assignments: Vec<_> = group["owner_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|owner| serde_json::json!({"owner_id": owner, "module": target}))
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
    let graph = fixture.owner_graph();
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
    let entry_owners: std::collections::BTreeSet<_> = entry["owner_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|owner| owner.as_str().unwrap())
        .collect();
    for owner in graph.nodes.iter().filter(|owner| {
        matches!(
            owner.statement_kind.to_string().as_str(),
            "import" | "export"
        )
    }) {
        assert!(entry_owners.contains(owner.id.as_str()), "{report}");
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

    let graph = fixture.owner_graph();
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
    let entry_owner_ids = entry_fragment["owner_ids"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    for owner in graph.nodes.iter().filter(|owner| {
        matches!(
            owner.statement_kind.to_string().as_str(),
            "import" | "export"
        )
    }) {
        assert!(
            entry_owner_ids.contains(owner.id.as_str()),
            "entry declaration {} was moved: {report}",
            owner.id
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
fn coarsens_only_gate_blocking_sccs_and_the_reported_partition_runs() {
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
        atomic["atomic_unit_conflicts"]
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
        .owner_graph()
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
fn coarsening_an_entry_scc_keeps_import_and_export_owners_at_the_entry_path() {
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
    let graph = fixture.owner_graph();
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
    let entry_owners = entry["owner_ids"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    for owner in graph.nodes.iter().filter(|owner| {
        matches!(
            owner.statement_kind.to_string().as_str(),
            "import" | "export"
        )
    }) {
        assert!(
            entry_owners.contains(owner.id.as_str()),
            "entry declaration {} moved from {entry_path}: {report}",
            owner.id
        );
    }
}
