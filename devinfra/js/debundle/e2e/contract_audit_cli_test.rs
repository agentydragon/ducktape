//! The audit is a fresh full-pipeline probe of one authored assertion at a time.

use debundle_e2e_support::*;
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};

const SOURCE: &str = "import { makeSlot as localMakeSlot } from './peer.js';\n\
    let later = 0;\n\
    const callback = () => later;\n\
    globalThis.saved = callback;\n\
    const slot = localMakeSlot();\n\
    later = 42;\n\
    console.log(slot.kind, globalThis.saved());\n";
const PEER: &[(&str, &str)] = &[(
    "static/peer",
    "// neutral-provider-marker\nexport function makeSlot() { return { kind: 'slot' }; }\nexport function spare() { return 1; }\n",
)];

fn split_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "callbacks/slot",
            &[Member::new("callback"), Member::new("slot")],
            &["globalThis.saved = callback;"],
        ),
        logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
    ]
}

fn audit_with_format(spec: &std::path::Path, format: &str, extra: &[&str]) -> std::process::Output {
    let mut args = vec![
        "spec",
        "audit-contracts",
        "--spec",
        spec.to_str().unwrap(),
        "--policy-key",
        "static/peer",
        "--format",
        format,
    ];
    args.extend_from_slice(extra);
    if !extra.contains(&"--max-probes") {
        args.extend(["--max-probes", "8"]);
    }
    run_debundle(&args)
}

fn audit(spec: &std::path::Path, extra: &[&str]) -> std::process::Output {
    audit_with_format(spec, "json", extra)
}

fn parsed(output: &std::process::Output) -> Value {
    assert!(
        output.status.success(),
        "audit failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

fn progress_lines(output: &std::process::Output) -> Vec<String> {
    String::from_utf8_lossy(&output.stderr)
        .lines()
        .filter(|line| line.starts_with("audit-progress "))
        .map(str::to_owned)
        .collect()
}

fn configure_separate_report_dir(spec_path: &Path) -> PathBuf {
    let report_dir = spec_path.parent().unwrap().join("separate-reports");
    let mut spec: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(spec_path).unwrap()).unwrap();
    spec["materialize_logical_modules"]["report_out_dir"] =
        serde_yaml::Value::String(report_dir.to_str().unwrap().to_string());
    fs::write(spec_path, serde_yaml::to_string(&spec).unwrap()).unwrap();
    report_dir
}

fn assert_no_pipeline_artifacts(spec_path: &Path, report_dir: &Path) {
    assert!(!report_dir.exists(), "probe wrote {}", report_dir.display());
    let out = spec_path.parent().unwrap().join("out");
    assert_eq!(fs::read_dir(out).unwrap().count(), 0);
}

#[test]
fn baseline_failure_stops_before_any_removal_verdict() {
    let policy = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["spare"])
        .build()];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(SOURCE, split_modules())
            .with_extra_chunks(PEER)
            .with_chunk_export_purity(&policy),
    );
    let report_dir = configure_separate_report_dir(&fixture.spec_path);
    let output = audit(&fixture.spec_path, &["--kind", "pure_exports"]);
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("baseline full acceptance failed"),
        "{stderr}"
    );
    assert!(output.stdout.is_empty());
    let progress = progress_lines(&output);
    assert_eq!(progress.len(), 2, "{progress:#?}");
    assert!(progress[0].starts_with("audit-progress baseline start "));
    assert!(progress[0].contains("eligible=1 completed=0"));
    assert!(progress[1].starts_with("audit-progress baseline complete "));
    assert!(progress[1].contains("eligible=1 completed=0 verdict=failed"));
    assert_no_pipeline_artifacts(&fixture.spec_path, &report_dir);
}

#[test]
fn one_removal_rechecks_full_acceptance_and_keeps_other_hints() {
    let policy = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["makeSlot"])
        .with_pure_exports(&["makeSlot", "spare"])
        .build()];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(SOURCE, split_modules())
            .with_extra_chunks(PEER)
            .with_chunk_export_purity(&policy),
    );
    let report_dir = configure_separate_report_dir(&fixture.spec_path);
    let before = fs::read(&fixture.spec_path).unwrap();
    let report = parsed(&audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--max-probes", "1"],
    ));
    assert_eq!(report["baseline"], "passed");
    assert_eq!(report["independently_probed"], 1);
    assert_eq!(report["budget_skipped"], 1);
    assert_eq!(report["results"][0]["target"]["export"], "makeSlot");
    assert_eq!(
        report["results"][0]["verdict"],
        "removable_under_remaining_assumptions"
    );
    assert_eq!(report["results"][1]["verdict"], "budget_skipped");
    assert_eq!(before, fs::read(&fixture.spec_path).unwrap());
    assert_no_pipeline_artifacts(&fixture.spec_path, &report_dir);

    let needed_output = audit(&fixture.spec_path, &["--kind", "no_reentry_exports"]);
    let needed = parsed(&needed_output);
    assert_eq!(needed["results"][0]["target"]["export"], "makeSlot");
    assert_eq!(needed["results"][0]["verdict"], "failed_full_acceptance");
    let needed_progress = progress_lines(&needed_output);
    assert_eq!(needed_progress.len(), 4, "{needed_progress:#?}");
    assert!(needed_progress[0].starts_with("audit-progress baseline start "));
    assert!(needed_progress[1].starts_with("audit-progress baseline complete "));
    assert!(needed_progress[2].starts_with("audit-progress probe start "));
    assert!(needed_progress[2].contains("ordinal=1/1 completed=0"));
    assert!(needed_progress[2].contains("kind: NoReentryExports"));
    assert!(needed_progress[3].starts_with("audit-progress probe complete "));
    assert!(needed_progress[3].contains("ordinal=1/1 completed=1"));
    assert!(needed_progress[3].contains("verdict=failed_full_acceptance"));
    assert_no_pipeline_artifacts(&fixture.spec_path, &report_dir);

    // The accepted removal also emits the same behavior under the Node oracle.
    let after_first_removal = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["makeSlot"])
        .with_pure_exports(&["spare"])
        .build()];
    let emitted = run_fixture(
        FixtureOpts::new(SOURCE, split_modules())
            .with_extra_chunks(PEER)
            .with_chunk_export_purity(&after_first_removal),
    );
    assert_preserved_entry_output(&emitted, "slot 42\n");
}

#[test]
fn bounded_audit_reports_ordered_progress_and_preserves_report_formats() {
    let policy = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["makeSlot"])
        .with_pure_exports(&["makeSlot", "spare"])
        .build()];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(SOURCE, split_modules())
            .with_extra_chunks(PEER)
            .with_chunk_export_purity(&policy),
    );

    let json = audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--max-probes", "1"],
    );
    let report = parsed(&json);
    assert_eq!(report["independently_probed"], 1);
    assert_eq!(report["budget_skipped"], 1);
    assert_eq!(report["results"][0]["target"]["kind"], "pure_exports");
    assert_eq!(report["results"][0]["target"]["export"], "makeSlot");
    assert_eq!(
        report["results"][0]["verdict"],
        "removable_under_remaining_assumptions"
    );
    assert_eq!(report["results"][1]["verdict"], "budget_skipped");

    let progress = progress_lines(&json);
    assert_eq!(progress.len(), 4, "{progress:#?}");
    assert!(progress[0].starts_with("audit-progress baseline start "));
    assert!(progress[0].contains("eligible=1 completed=0"));
    assert!(progress[1].starts_with("audit-progress baseline complete "));
    assert!(progress[1].contains("eligible=1 completed=0 verdict=passed"));
    assert!(progress[2].starts_with("audit-progress probe start "));
    assert!(progress[2].contains("ordinal=1/1 completed=0"));
    assert!(progress[2].contains("kind: PureExports"));
    assert!(progress[2].contains("export: \"makeSlot\""));
    assert!(progress[3].starts_with("audit-progress probe complete "));
    assert!(progress[3].contains("ordinal=1/1 completed=1"));
    assert!(progress[3].contains("verdict=removable_under_remaining_assumptions"));
    assert!(progress[3].contains("kind: PureExports"));
    assert!(progress[3].contains("export: \"makeSlot\""));

    let ndjson = audit_with_format(
        &fixture.spec_path,
        "ndjson",
        &["--kind", "pure_exports", "--max-probes", "1"],
    );
    assert!(
        ndjson.status.success(),
        "{}",
        String::from_utf8_lossy(&ndjson.stderr)
    );
    let ndjson_report: Value = serde_json::from_slice(&ndjson.stdout).unwrap();
    assert_eq!(ndjson_report["section"], "audit");
    assert_eq!(
        ndjson_report["independently_probed"],
        report["independently_probed"]
    );
    assert_eq!(ndjson_report["budget_skipped"], report["budget_skipped"]);
    assert_eq!(ndjson_report["results"], report["results"]);
    let ndjson_progress = progress_lines(&ndjson);
    assert_eq!(ndjson_progress.len(), 4, "{ndjson_progress:#?}");
    assert!(ndjson_progress[0].starts_with("audit-progress baseline start "));
    assert!(ndjson_progress[1].starts_with("audit-progress baseline complete "));
    assert!(ndjson_progress[2].starts_with("audit-progress probe start "));
    assert!(ndjson_progress[3].starts_with("audit-progress probe complete "));
}

#[test]
fn source_marker_policy_is_reported_as_label() {
    let policy = [ChunkExportPurityBuilder::new("provider-policy")
        .with_source_markers(&["neutral-provider-marker"])
        .with_no_reentry_exports(&["makeSlot"])
        .with_pure_exports(&["spare"])
        .build()];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(SOURCE, split_modules())
            .with_extra_chunks(PEER)
            .with_chunk_export_purity(&policy),
    );
    let output = run_debundle(&[
        "spec",
        "audit-contracts",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--policy-key",
        "provider-policy",
        "--kind",
        "pure_exports",
        "--max-probes",
        "1",
        "--format",
        "json",
    ]);
    let report = parsed(&output);
    assert_eq!(report["policy_key"], "provider-policy");
    assert_eq!(report["source_markers"][0], "neutral-provider-marker");
    assert_eq!(
        report["results"][0]["target"]["policy_key"],
        "provider-policy"
    );
    assert_eq!(
        report["results"][0]["verdict"],
        "removable_under_remaining_assumptions"
    );
}

#[test]
fn targeted_member_removal_does_not_remove_neighbor() {
    let source = "import { api } from './peer.js';\nlet later = 0;\nconst callback = () => later;\nglobalThis.saved = callback;\nconst slot = api.makeSlot(void 0);\nlater = 42;\nconsole.log(slot.kind, globalThis.saved());\n";
    let peer = [(
        "static/peer",
        "export const api = { makeSlot() { return { kind: 'slot' }; } };\nexport const otherApi = { spare() { return 1; } };\n",
    )];
    let policy = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_members("api", &["makeSlot"])
        .with_no_reentry_members("otherApi", &["spare"])
        .with_namespace_independent_members("api", &["makeSlot"])
        .build()];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(source, split_modules())
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&policy),
    );
    let report = parsed(&audit(
        &fixture.spec_path,
        &[
            "--kind",
            "no_reentry_members",
            "--export",
            "otherApi",
            "--member",
            "spare",
        ],
    ));
    assert_eq!(report["results"].as_array().unwrap().len(), 1);
    assert_eq!(report["results"][0]["target"]["member"], "spare");
    assert_eq!(
        report["results"][0]["verdict"],
        "removable_under_remaining_assumptions"
    );
    let needed = parsed(&audit(
        &fixture.spec_path,
        &[
            "--kind",
            "no_reentry_members",
            "--export",
            "api",
            "--member",
            "makeSlot",
        ],
    ));
    assert_eq!(needed["results"][0]["verdict"], "failed_full_acceptance");
}

#[test]
fn typed_signature_selection_removes_only_one_call_shape() {
    let source = "import { factory as make } from './peer.js';\nconst callback = () => later;\nconst wrapped = make(callback);\nconst later = 42;\nconsole.log(wrapped.invoke());\n";
    let peer = [(
        "static/peer",
        "export function factory(callback) { return { invoke: callback }; }\n",
    )];
    let policy = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 1, &[0])
        .with_retained_callback_factory("factory", 2, &[0, 1])
        .build()];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("callback"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&policy),
    );
    let selected = parsed(&audit(
        &fixture.spec_path,
        &[
            "--kind",
            "retained_callback_factory_exports",
            "--export",
            "factory",
            "--signature",
            r#"{"argument_count":2,"callback_arguments":[0,1]}"#,
        ],
    ));
    assert_eq!(selected["results"].as_array().unwrap().len(), 1);
    assert_eq!(
        selected["results"][0]["target"]["signature"]["argument_count"],
        2
    );
    assert_eq!(
        selected["results"][0]["verdict"],
        "removable_under_remaining_assumptions"
    );
    let needed = parsed(&audit(
        &fixture.spec_path,
        &[
            "--kind",
            "retained_callback_factory_exports",
            "--export",
            "factory",
            "--signature",
            r#"{"argument_count":1,"callback_arguments":[0]}"#,
        ],
    ));
    assert_eq!(needed["results"].as_array().unwrap().len(), 1);
    assert_eq!(needed["results"][0]["verdict"], "failed_full_acceptance");
}
