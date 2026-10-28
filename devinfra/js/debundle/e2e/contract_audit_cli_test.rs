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

    let checkpoint = fixture
        .spec_path
        .parent()
        .unwrap()
        .join("needed-audit.json");
    let needed_output = audit(
        &fixture.spec_path,
        &[
            "--kind",
            "no_reentry_exports",
            "--checkpoint",
            checkpoint.to_str().unwrap(),
        ],
    );
    let needed = parsed(&needed_output);
    let resumed = audit(
        &fixture.spec_path,
        &[
            "--kind",
            "no_reentry_exports",
            "--resume-from",
            checkpoint.to_str().unwrap(),
        ],
    );
    assert_eq!(needed, parsed(&resumed));
    assert!(String::from_utf8_lossy(&resumed.stderr).contains("audit-progress probe reused"));
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

fn checkpoint_fixture() -> ValidateFixture {
    let policy = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["makeSlot"])
        .with_pure_exports(&["makeSlot", "spare"])
        .build()];
    write_validate_fixture_spec(
        FixtureOpts::new(SOURCE, split_modules())
            .with_extra_chunks(PEER)
            .with_chunk_export_purity(&policy),
    )
}

#[test]
fn audit_checkpoint_resumes_the_total_prefix_and_preserves_later_results() {
    let fixture = checkpoint_fixture();
    let checkpoint = fixture.spec_path.parent().unwrap().join("audit.json");
    let path = checkpoint.to_str().unwrap();
    let first = parsed(&audit(
        &fixture.spec_path,
        &[
            "--kind",
            "pure_exports",
            "--max-probes",
            "1",
            "--checkpoint",
            path,
        ],
    ));
    assert_eq!(first["independently_probed"], 1);
    assert_eq!(first["budget_skipped"], 1);
    let initial: Value = serde_json::from_slice(&fs::read(&checkpoint).unwrap()).unwrap();
    assert_eq!(initial["results"].as_array().unwrap().len(), 1);
    let resumed = audit(
        &fixture.spec_path,
        &[
            "--kind",
            "pure_exports",
            "--max-probes",
            "2",
            "--resume-from",
            path,
        ],
    );
    let report = parsed(&resumed);
    assert_eq!(report["independently_probed"], 2);
    assert_eq!(report["budget_skipped"], 0);
    assert_eq!(
        String::from_utf8_lossy(&resumed.stderr)
            .matches("audit-progress probe reused")
            .count(),
        1
    );
    assert_eq!(
        String::from_utf8_lossy(&resumed.stderr)
            .matches("audit-progress probe complete")
            .count(),
        1
    );
    let complete = fs::read(&checkpoint).unwrap();
    let stored: Value = serde_json::from_slice(&complete).unwrap();
    assert_eq!(stored["results"].as_array().unwrap().len(), 2);
    let narrowed = audit(
        &fixture.spec_path,
        &[
            "--kind",
            "pure_exports",
            "--max-probes",
            "1",
            "--resume-from",
            path,
        ],
    );
    assert_eq!(parsed(&narrowed)["budget_skipped"], 1);
    assert_eq!(fs::read(&checkpoint).unwrap(), complete);
    let mut incomplete = stored.clone();
    incomplete["results"][0]["verdict"] = Value::String("inconclusive".to_string());
    incomplete["results"][0]["error"] = Value::String("interrupted probe".to_string());
    fs::write(&checkpoint, serde_json::to_vec(&incomplete).unwrap()).unwrap();
    let retried = audit(
        &fixture.spec_path,
        &[
            "--kind",
            "pure_exports",
            "--kind",
            "pure_exports",
            "--resume-from",
            path,
        ],
    );
    assert_eq!(
        parsed(&retried)["results"][0]["verdict"],
        "removable_under_remaining_assumptions"
    );
    assert_eq!(
        String::from_utf8_lossy(&retried.stderr)
            .matches("audit-progress probe reused")
            .count(),
        1
    );
    assert_eq!(fs::read(&checkpoint).unwrap(), complete);
    let duplicate = audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--checkpoint", path],
    );
    assert!(!duplicate.status.success());
    assert!(duplicate.stdout.is_empty());
    assert_eq!(fs::read(&checkpoint).unwrap(), complete);
}

#[test]
fn audit_checkpoint_rejects_changed_identity_selection_and_baseline_without_writes() {
    let fixture = checkpoint_fixture();
    let checkpoint = fixture.spec_path.parent().unwrap().join("audit.json");
    let path = checkpoint.to_str().unwrap();
    parsed(&audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--checkpoint", path],
    ));
    let original = fs::read(&checkpoint).unwrap();
    let record: Value = serde_json::from_slice(&original).unwrap();
    for field in [
        "engine_sha256",
        "generation_command",
        "roots",
        "files",
        "yaml_memberships",
    ] {
        let mut modified = record.clone();
        if field == "engine_sha256" {
            modified["provenance"][field] = Value::String("different-engine".to_string());
        } else if field == "generation_command" {
            modified["provenance"][field]
                .as_array_mut()
                .unwrap()
                .push(Value::String("--different-options".to_string()));
        } else if field == "roots" {
            modified["provenance"][field]["source"] =
                Value::String("/different-source-root".to_string());
        } else {
            modified["provenance"][field] = Value::Array(vec![]);
            // Flat fixtures have no YAML membership; use another valid but different record.
            if field == "yaml_memberships" {
                modified["provenance"][field] =
                    serde_json::json!([{"root":"spec", "directory":"different", "members":[]}]);
            }
        }
        fs::write(&checkpoint, serde_json::to_vec(&modified).unwrap()).unwrap();
        let before = fs::read(&checkpoint).unwrap();
        let output = audit(
            &fixture.spec_path,
            &["--kind", "pure_exports", "--resume-from", path],
        );
        assert!(!output.status.success(), "{field}");
        assert!(output.stdout.is_empty());
        assert_eq!(fs::read(&checkpoint).unwrap(), before);
    }
    fs::write(&checkpoint, &original).unwrap();
    let changed_selection = audit(
        &fixture.spec_path,
        &[
            "--kind",
            "pure_exports",
            "--export",
            "spare",
            "--resume-from",
            path,
        ],
    );
    assert!(!changed_selection.status.success());
    assert_eq!(fs::read(&checkpoint).unwrap(), original);
    let mut baseline: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&fixture.spec_path).unwrap()).unwrap();
    baseline["chunk_export_purity"]["static/peer"]["no_reentry_exports"] =
        serde_yaml::from_str("[]").unwrap();
    write_text_file(
        &fixture.spec_path,
        &serde_yaml::to_string(&baseline).unwrap(),
    );
    let failed_baseline = audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--resume-from", path],
    );
    assert!(!failed_baseline.status.success());
    assert!(failed_baseline.stdout.is_empty());
    assert!(
        String::from_utf8_lossy(&failed_baseline.stderr)
            .contains("baseline full acceptance failed")
    );
    assert_eq!(fs::read(&checkpoint).unwrap(), original);
}

#[test]
fn audit_checkpoint_refuses_a_live_writer_and_malformed_records() {
    let fixture = checkpoint_fixture();
    let checkpoint = fixture.spec_path.parent().unwrap().join("audit.json");
    let path = checkpoint.to_str().unwrap();
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(checkpoint.with_file_name("audit.json.lock"))
        .unwrap();
    lock.try_lock().unwrap();
    let busy = audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--checkpoint", path],
    );
    assert!(!busy.status.success());
    assert!(busy.stdout.is_empty());
    assert!(!checkpoint.exists());
    drop(lock);
    parsed(&audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--checkpoint", path],
    ));
    fs::write(&checkpoint, "{").unwrap();
    let malformed = audit(
        &fixture.spec_path,
        &["--kind", "pure_exports", "--resume-from", path],
    );
    assert!(!malformed.status.success());
    assert!(malformed.stdout.is_empty());
    assert_eq!(fs::read(&checkpoint).unwrap(), b"{");
}

#[test]
fn audit_checkpoint_rechecks_tree_membership_source_and_effective_package_roots() {
    let fixture = run_tree_fixture(
        &TreeFixture {
            chunks: &[
                ("main", "console.log('ready');"),
                ("peer", "export function spare() { return 1; }"),
            ],
            module_roots: &[("main", "main")],
            modules: &[],
        },
        &[],
    );
    assert!(fixture.result.status.success(), "{}", fixture.result.stderr);
    let root = fixture.root();
    let config = root.join("spec_config.yaml");
    let mut yaml: serde_yaml::Value = serde_yaml::from_slice(&fs::read(&config).unwrap()).unwrap();
    yaml["chunk_export_purity"] = serde_yaml::from_str("peer: {pure_exports: [spare]}").unwrap();
    write_text_file(&config, &serde_yaml::to_string(&yaml).unwrap());
    let modules = root.join("modules");
    let vendor = root.join("vendor_marks.yaml");
    let checkpoint = root.join("audit.json");
    let run = |mode: &str, extra: &[&str]| {
        let mut args = vec![
            "spec",
            "audit-contracts",
            "--tree-config",
            config.to_str().unwrap(),
            "--tree-modules",
            modules.to_str().unwrap(),
            "--tree-vendor-marks",
            vendor.to_str().unwrap(),
            "--tree-source-root",
            root.to_str().unwrap(),
            "--out-root",
            fixture.out_root.to_str().unwrap(),
            "--policy-key",
            "peer",
            "--max-probes",
            "1",
            "--format",
            "json",
            mode,
            checkpoint.to_str().unwrap(),
        ];
        args.extend_from_slice(extra);
        run_debundle(&args)
    };
    parsed(&run("--checkpoint", &[]));
    let before = fs::read(&checkpoint).unwrap();
    let source = root.join("snapshot/main.js");
    let original = fs::read(&source).unwrap();
    write_text_file(&source, "console.log('changed');");
    let changed_source = run("--resume-from", &[]);
    assert!(!changed_source.status.success());
    assert!(changed_source.stdout.is_empty());
    assert!(
        String::from_utf8_lossy(&changed_source.stderr)
            .contains("checkpoint input identity differs"),
        "{}",
        String::from_utf8_lossy(&changed_source.stderr)
    );
    assert_eq!(fs::read(&checkpoint).unwrap(), before);
    fs::write(&source, original).unwrap();
    let extra_module = modules.join("main/empty.yaml");
    write_text_file(&extra_module, "members: []\n");
    let changed_membership = run("--resume-from", &[]);
    assert!(!changed_membership.status.success());
    assert!(changed_membership.stdout.is_empty());
    assert!(
        String::from_utf8_lossy(&changed_membership.stderr)
            .contains("checkpoint input identity differs"),
        "{}",
        String::from_utf8_lossy(&changed_membership.stderr)
    );
    assert_eq!(fs::read(&checkpoint).unwrap(), before);
    fs::remove_file(extra_module).unwrap();
    let package_root = root.join("unused-package");
    fs::create_dir(&package_root).unwrap();
    let mapping = format!("example={}", package_root.display());
    let changed_options = run("--resume-from", &["--package-root", &mapping]);
    assert!(!changed_options.status.success());
    assert!(changed_options.stdout.is_empty());
    assert!(
        String::from_utf8_lossy(&changed_options.stderr)
            .contains("checkpoint input identity differs"),
        "{}",
        String::from_utf8_lossy(&changed_options.stderr)
    );
    assert_eq!(fs::read(&checkpoint).unwrap(), before);
    let unchanged = run("--resume-from", &[]);
    assert_eq!(parsed(&unchanged)["independently_probed"], 1);
    assert!(String::from_utf8_lossy(&unchanged.stderr).contains("audit-progress probe reused"));
}

#[test]
fn prepared_probes_match_independent_fresh_pipeline_runs() {
    let policy = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["makeSlot", "spare"])
        .with_pure_exports(&["makeSlot", "spare"])
        .build()];
    let fixture = write_validate_fixture_spec(
        FixtureOpts::new(SOURCE, split_modules())
            .with_extra_chunks(PEER)
            .with_chunk_export_purity(&policy),
    );
    // Both paths must validate final reconstruction without writing reports.
    let mut original: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&fixture.spec_path).unwrap()).unwrap();
    original["materialize_logical_modules"]["report_out_dir"] = serde_yaml::Value::Null;
    let original_bytes = serde_yaml::to_string(&original).unwrap();
    fs::write(&fixture.spec_path, &original_bytes).unwrap();
    let report = parsed(&audit(&fixture.spec_path, &[]));
    let rows = report["results"].as_array().unwrap();
    assert_eq!(rows.len(), 4);
    assert!(
        rows.iter()
            .any(|row| row["verdict"] == "failed_full_acceptance")
    );
    assert!(
        rows.iter()
            .any(|row| row["verdict"] == "removable_under_remaining_assumptions")
    );

    for row in rows {
        let mut candidate = original.clone();
        let kind = row["target"]["kind"].as_str().unwrap();
        let export = row["target"]["export"].as_str().unwrap();
        candidate["chunk_export_purity"]["static/peer"][kind]
            .as_sequence_mut()
            .unwrap()
            .retain(|value| value.as_str() != Some(export));
        fs::write(
            &fixture.spec_path,
            serde_yaml::to_string(&candidate).unwrap(),
        )
        .unwrap();
        let fresh = run_debundle(&[
            "run",
            "--spec",
            fixture.spec_path.to_str().unwrap(),
            "--dry-run",
        ]);
        assert_eq!(
            fresh.status.success(),
            row["verdict"] == "removable_under_remaining_assumptions",
            "target={:?}, fresh stderr={}",
            row["target"],
            String::from_utf8_lossy(&fresh.stderr)
        );
    }
    fs::write(&fixture.spec_path, &original_bytes).unwrap();
    assert_eq!(report, parsed(&audit(&fixture.spec_path, &[])));
    assert_no_pipeline_artifacts(
        &fixture.spec_path,
        &fixture.spec_path.parent().unwrap().join("reports"),
    );
}
