//! The audit is a fresh full-pipeline probe of one authored assertion at a time.

use debundle_e2e_support::*;
use serde_json::Value;
use std::fs;

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

fn audit(spec: &std::path::Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec![
        "spec",
        "audit-contracts",
        "--spec",
        spec.to_str().unwrap(),
        "--policy-key",
        "static/peer",
        "--format",
        "json",
    ];
    args.extend_from_slice(extra);
    if !extra.contains(&"--max-probes") {
        args.extend(["--max-probes", "8"]);
    }
    run_debundle(&args)
}

fn parsed(output: &std::process::Output) -> Value {
    assert!(
        output.status.success(),
        "audit failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
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
    let output = audit(&fixture.spec_path, &["--kind", "pure_exports"]);
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("baseline full acceptance failed"),
        "{stderr}"
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains("removable"));
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
    assert_eq!(
        fs::read_dir(fixture.spec_path.parent().unwrap().join("out"))
            .unwrap()
            .count(),
        0
    );

    let needed = parsed(&audit(
        &fixture.spec_path,
        &["--kind", "no_reentry_exports"],
    ));
    assert_eq!(needed["results"][0]["target"]["export"], "makeSlot");
    assert_eq!(needed["results"][0]["verdict"], "failed_full_acceptance");

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
