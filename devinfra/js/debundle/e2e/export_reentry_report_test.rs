//! Exposes the predicates behind export-reentry policy selection.

use debundle_e2e_support::*;
use serde_json::Value;
use std::path::PathBuf;

const CHUNK_ID: &str = "static/provider";
const REPORT_NAME: &str = "export_reentry.json";

fn report_path(report_root: &std::path::Path, chunk_id: &str) -> PathBuf {
    report_root.join(chunk_id).join(REPORT_NAME)
}

fn has_loader_failure(report: &Value, reason: &str, source_chunk: &str) -> bool {
    report["loader"]["failures"]
        .as_array()
        .is_some_and(|failures| {
            failures.iter().any(|failure| {
                failure["reason"] == reason
                    && failure["first"]["site"]["source_chunk"] == source_chunk
            })
        })
}

fn has_reason(report: &Value, group: &str, reason: &str, source_chunk: Option<&str>) -> bool {
    report[group]["examples"]
        .as_array()
        .is_some_and(|blockers| {
            blockers.iter().any(|blocker| {
                blocker["reason"] == reason
                    && source_chunk.is_none_or(|source| blocker["source_chunk"] == source)
            })
        })
}

fn builtin_policy(leaves: &[NodeBuiltinModule]) -> HostBehavior {
    HostBehavior {
        reentry_free_builtin_modules: leaves.iter().copied().collect(),
        ..HostBehavior::default()
    }
}

#[test]
fn selected_builtin_leaves_are_reported_with_source_uses() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { join } from 'path';\nimport { readFile } from 'node:fs/promises';\nexport const value = join('a', 'b');\n",
            vec![],
        )
        .with_chunk_id(CHUNK_ID)
        .with_host_behavior(builtin_policy(&[
            NodeBuiltinModule::Path,
            NodeBuiltinModule::FsPromises,
        ])),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));
    assert_eq!(report["policy"], "no_static_importer_reentry");
    assert_eq!(report["closure_blockers"]["examples"], Value::Array(vec![]));
    assert_eq!(
        report["host_builtin_leaves"]["examples"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        report["host_builtin_leaves"]["examples"][0]["source_chunk"],
        CHUNK_ID
    );
    assert_eq!(
        report["host_builtin_leaves"]["examples"][0]["specifier"],
        "node:fs/promises"
    );
    assert_eq!(
        report["host_builtin_leaves"]["examples"][0]["builtin"],
        "fs/promises"
    );
    assert_eq!(
        report["host_builtin_leaves"]["examples"][0]["request_count"],
        1
    );
    assert_eq!(
        report["host_builtin_leaves"]["examples"][1]["specifier"],
        "path"
    );
    assert_eq!(
        report["host_builtin_leaves"]["examples"][1]["builtin"],
        "path"
    );
    assert_eq!(
        report["host_builtin_leaves"]["examples"][1]["request_count"],
        1
    );
    let graph = fixture.dependency_graph();
    let value = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "value")
        })
        .expect("exported imported-call unit");
    assert!(
        !value.purity.is_pure(),
        "builtin leaf must not grant call purity"
    );
}

#[test]
fn declared_bun_modules_and_node_builtins_are_static_leaves() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import 'ws';\nimport 'undici';\nimport 'node:querystring';\nimport 'assert';\nexport const value = 1;\n",
            vec![],
        )
        .with_chunk_id(CHUNK_ID)
        .with_host_behavior(HostBehavior {
            reentry_free_bun_modules: [BunModule::Ws, BunModule::Undici].into_iter().collect(),
            ..builtin_policy(&[NodeBuiltinModule::Querystring, NodeBuiltinModule::Assert])
        }),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));
    assert_eq!(report["policy"], "no_static_importer_reentry");
    let leaves: Vec<_> = report["host_builtin_leaves"]["examples"]
        .as_array()
        .unwrap()
        .iter()
        .map(|leaf| {
            (
                leaf["specifier"].as_str().unwrap(),
                leaf["builtin"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        leaves,
        [
            ("assert", "assert"),
            ("node:querystring", "querystring"),
            ("undici", "undici"),
            ("ws", "ws"),
        ]
    );
}

#[test]
fn unselected_bare_and_replacement_packages_remain_unresolved() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import 'node:path';\nimport 'crypto';\nimport 'unknown_package';\nimport 'ws';\nexport const value = 1;\n",
            vec![],
        )
        .with_chunk_id(CHUNK_ID)
        .with_host_behavior(builtin_policy(&[NodeBuiltinModule::Path])),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));
    assert_eq!(report["policy"], "conservative");
    assert_eq!(
        report["host_builtin_leaves"]["examples"][0]["specifier"],
        "node:path"
    );
    let unresolved: Vec<_> = report["closure_blockers"]["examples"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|blocker| blocker["reason"] == "unresolved_static_request")
        .map(|blocker| blocker["specifier"].as_str().unwrap())
        .collect();
    assert_eq!(unresolved, ["crypto", "unknown_package", "ws"]);
}

#[test]
fn attributed_builtin_request_remains_unsupported() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import 'path' with { type: 'javascript' };\nexport const value = 1;\n",
            vec![],
        )
        .with_chunk_id(CHUNK_ID)
        .with_host_behavior(builtin_policy(&[NodeBuiltinModule::Path])),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));
    assert_eq!(report["policy"], "conservative");
    assert!(has_reason(
        &report,
        "global_blockers",
        "non_ordinary_static_request",
        Some(CHUNK_ID)
    ));
    assert!(has_reason(
        &report,
        "closure_blockers",
        "unsupported_static_request",
        Some(CHUNK_ID)
    ));
    assert_eq!(report["host_builtin_leaves"]["total"], 0);
}

#[test]
fn builtin_leaf_set_is_closed_and_unused_assertion_is_not_evidence() {
    assert!(serde_json::from_str::<NodeBuiltinModule>("\"ws\"").is_err());
    assert!(serde_json::from_str::<NodeBuiltinModule>("\"unknown_package\"").is_err());
    let fixture = run_fixture(
        FixtureOpts::new("export const value = 1;", vec![])
            .with_chunk_id(CHUNK_ID)
            .with_host_behavior(builtin_policy(&[NodeBuiltinModule::Path])),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));
    assert_eq!(report["host_builtin_leaves"]["total"], 0);
}

#[test]
fn builtin_leaf_does_not_remove_application_require_or_static_cycle_blockers() {
    let extra_chunks = [(
        "static/other",
        "import './provider.js';\nexport const other = 1;\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import 'path';\nimport './other.js';\nconst load = import.meta.require('./other.js');\nexport const value = 1;\n",
            vec![],
        )
        .with_chunk_id(CHUNK_ID)
        .with_extra_chunks(&extra_chunks)
        .with_admission_overrides(&["a5_import_meta"])
        .with_host_behavior(builtin_policy(&[NodeBuiltinModule::Path])),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));
    assert_eq!(report["policy"], "conservative");
    assert_eq!(
        report["host_builtin_leaves"]["examples"][0]["specifier"],
        "path"
    );
    assert!(has_loader_failure(
        &report,
        "loader_request_reaches_target",
        CHUNK_ID
    ));
    assert!(has_reason(
        &report,
        "closure_blockers",
        "target_cycle",
        None
    ));
}

#[test]
fn supported_case_reports_no_static_importer_reentry() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "export const value = 1;\n",
            vec![logical_module("values", &[Member::new("value")])],
        )
        .with_chunk_id(CHUNK_ID),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));

    assert_eq!(report["policy"], "no_static_importer_reentry");
    assert_eq!(
        report["host_behavior"]["synchronous_module_loader"],
        "present"
    );
    assert_eq!(report["loader"]["rule"], "no_host_loader_sites");
    assert_eq!(report["global_blockers"]["examples"], Value::Array(vec![]));
    assert_eq!(report["closure_blockers"]["examples"], Value::Array(vec![]));
}

#[test]
fn static_import_of_empty_source_does_not_block_export_reentry_proof() {
    for (source, expected_policy) in [
        (
            "// no executable statements\n",
            "no_static_importer_reentry",
        ),
        ("globalThis.count = 1;\n", "conservative"),
    ] {
        let extra_chunks = [("static/dependency", source)];
        let fixture = run_fixture(
            FixtureOpts::new(
                "import './dependency.js';\nexport const value = 1;\n",
                vec![logical_module("values", &[Member::new("value")])],
            )
            .with_chunk_id(CHUNK_ID)
            .with_extra_chunks(&extra_chunks),
        );
        let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));
        assert_eq!(report["policy"], expected_policy);
        assert_eq!(
            has_reason(
                &report,
                "closure_blockers",
                "non_esm_chunk",
                Some("static/dependency"),
            ),
            expected_policy == "conservative",
        );
    }
}

#[test]
fn reports_independent_global_and_static_closure_blockers() {
    let vendor_marks = [(
        "static/vendor.js",
        VendorMark {
            identity: "fake-package".to_string(),
            role: Default::default(),
            level: VendorLevel::Suppress,
        },
    )];
    let extra_chunks = [(
        "static/vendor",
        "import './provider.js';\nawait Promise.resolve();\nexport const other = 1;\n",
    )];
    let extra_files = [("static/missing.js", "export {};\n")];
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const load = globalThis["require"];
import "./vendor.js" with { type: "javascript" };
import "./missing.js";
export const value = 1;
"#,
            vec![],
        )
        .with_chunk_id(CHUNK_ID)
        .with_extra_chunks(&extra_chunks)
        .with_extra_files(&extra_files)
        .with_vendor_marks(&vendor_marks),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));

    assert_eq!(report["policy"], "conservative");
    assert!(has_loader_failure(&report, "opaque_loader_site", CHUNK_ID));
    assert!(has_reason(
        &report,
        "global_blockers",
        "non_ordinary_static_request",
        Some(CHUNK_ID),
    ));
    assert_eq!(
        report["global_blockers"]["examples"]
            .as_array()
            .unwrap()
            .iter()
            .find(|blocker| blocker["reason"] == "non_ordinary_static_request")
            .unwrap()["resolved_target"]["chunk"],
        "static/vendor",
    );
    assert!(has_reason(
        &report,
        "closure_blockers",
        "top_level_await",
        Some("static/vendor"),
    ));
    assert!(has_reason(
        &report,
        "closure_blockers",
        "unresolved_static_request",
        Some(CHUNK_ID),
    ));
    assert!(has_reason(
        &report,
        "closure_blockers",
        "target_cycle",
        None,
    ));
}

#[test]
fn incomplete_artifact_is_reported_for_selected_chunk() {
    let extra_chunks = [("static/other", "export const other = 1;\n")];
    let fixture = run_fixture(
        FixtureOpts::new(
            "export const value = 1;\n",
            vec![logical_module("values", &[Member::new("value")])],
        )
        .with_chunk_id(CHUNK_ID)
        .with_extra_chunks(&extra_chunks)
        .with_prune_other_chunks(),
    );
    let report: Value = read_json(&report_path(&fixture.report_root, CHUNK_ID));

    assert_eq!(report["policy"], "conservative");
    assert!(has_reason(
        &report,
        "global_blockers",
        "incomplete_artifact",
        None,
    ));
}

#[test]
fn dry_run_rejection_keeps_report_and_accept_path_stays_silent() {
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        r#"function wrap(x) { return { ref: x }; }
const a = "a";
const b = wrap(a);
const c = "c";
const d = wrap(c);
export { a, b, c, d };
"#,
        vec![
            logical_module("mod_x", &[Member::new("a"), Member::new("d")]),
            logical_module("mod_y", &[Member::new("b"), Member::new("c")]),
        ],
    ));
    assert!(!rejected.cycles().is_empty());
    let rejection_report: Value = read_json(&report_path(&rejected.report_root, "static/app"));
    assert_eq!(rejection_report["policy"], "no_static_importer_reentry");

    let accepted = run_dry_run_fixture(FixtureOpts::new(
        "export const value = 1;\n",
        vec![logical_module("values", &[Member::new("value")])],
    ));
    assert!(
        !report_path(&accepted.report_root, "static/app").exists(),
        "accepted dry run must not write export-reentry diagnostics",
    );
}

#[test]
fn bounded_loader_evidence_keeps_complete_explicit_export() {
    let ids: Vec<_> = (0..32).map(|i| format!("static/peer{i}")).collect();
    let chunks: Vec<_> = ids
        .iter()
        .map(|id| {
            (
                id.as_str(),
                "import 'node:path';\nexport const value = 1;\n",
            )
        })
        .collect();
    let mut source = String::new();
    for i in 0..32 {
        source.push_str(&format!("import './peer{i}.js';\n"));
    }
    source.push_str("export function load() {\n");
    for i in 0..32 {
        source.push_str(&format!("import.meta.require('./peer{i}.js');\n"));
    }
    source.push_str("}\nconsole.log('ready');\n");
    let opts = || {
        FixtureOpts::new(&source, vec![])
            .with_chunk_id(CHUNK_ID)
            .with_extra_chunks(&chunks)
            .with_host_behavior(HostBehavior {
                loader_result_for_evaluating_module: Some(LoaderResultForEvaluatingModule::Throws),
                synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Never),
                ..builtin_policy(&[NodeBuiltinModule::Path])
            })
    };
    let default = run_fixture(opts());
    assert_entry_output(&default, "ready\n");
    let default_summary: Value = read_json(&report_path(&default.report_root, CHUNK_ID));
    // Fail old unbounded output before requesting the newly introduced flag.
    let requests = &default_summary["loader"]["requests"];
    if let Some(legacy) = requests.as_array() {
        assert!(
            legacy.len() <= 20,
            "default report retained more than20 full request rows"
        );
    } else {
        assert_eq!(requests["total"], 32);
        assert_eq!(requests["examples"].as_array().unwrap().len(), 20);
        assert_eq!(requests["truncated"], true);
    }
    let full_path = |root: &std::path::Path| root.join(CHUNK_ID).join("export_reentry.full.json");
    assert!(!full_path(&default.report_root).exists());
    let exported = run_fixture_with_args(opts(), &["--full-report", "export-reentry"]);
    let summary: Value = read_json(&report_path(&exported.report_root, CHUNK_ID));
    assert_eq!(
        summary,
        read_json::<Value>(&report_path(&default.report_root, CHUNK_ID))
    );
    let complete: Value = read_json(&full_path(&exported.report_root));
    assert_eq!(summary["policy"], complete["policy"]);
    assert_eq!(summary["loader"]["closed"], complete["loader"]["closed"]);
    for (examples, rows) in [
        (
            &summary["loader"]["requests"],
            &complete["loader"]["requests"],
        ),
        (
            &summary["loader"]["loaded_chunks"],
            &complete["loader"]["loaded_chunks"],
        ),
        (
            &summary["host_builtin_leaves"],
            &complete["host_builtin_leaves"],
        ),
    ] {
        let rows = rows.as_array().unwrap();
        assert!(rows.len() > 20, "{complete:#?}");
        assert_eq!(examples["total"], rows.len());
        assert_eq!(examples["truncated"], true);
        assert_eq!(
            examples["examples"],
            serde_json::to_value(&rows[..20]).unwrap()
        );
    }
    let site_total: u64 = complete["loader"]["requests"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["request_count"].as_u64().unwrap())
        .sum();
    assert_eq!(summary["loader"]["request_site_count"], site_total);
    let builtin_total: u64 = complete["host_builtin_leaves"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["request_count"].as_u64().unwrap())
        .sum();
    assert_eq!(summary["host_builtin_request_count"], builtin_total);
    assert_eq!(summary["example_limit"], 20);
    assert_eq!(summary["full_detail_export_available"], true);
    assert!(complete.get("example_limit").is_none());
}

#[test]
fn full_reentry_selection_preserves_errors_and_refuses_suppressed_reports() {
    let fixture =
        write_validate_fixture_spec(FixtureOpts::new("export const value = 7;\n", vec![]));
    for flags in [
        vec!["--full-report", "export-reentry", "--dry-run"],
        vec![
            "--full-report",
            "export-reentry",
            "--full-report-chunk",
            "static/typo",
        ],
    ] {
        let mut args = vec!["run", "--spec", fixture.spec_path.to_str().unwrap()];
        args.extend(flags);
        let result = run_debundle(&args);
        assert_eq!(result.status.code(), Some(1), "{result:?}");
    }
    let invalid = write_validate_fixture_spec(FixtureOpts::new(
        "export const value = 7;\n",
        vec![logical_module("missing", &[Member::new("absent")])],
    ));
    let result = run_debundle(&[
        "run",
        "--spec",
        invalid.spec_path.to_str().unwrap(),
        "--emit-on-rejection",
        "--full-report",
        "export-reentry",
    ]);
    assert_eq!(result.status.code(), Some(1), "{result:?}");
    assert!(
        !invalid
            .spec_path
            .parent()
            .unwrap()
            .join("out/GATE_REJECTED")
            .exists()
    );
}
