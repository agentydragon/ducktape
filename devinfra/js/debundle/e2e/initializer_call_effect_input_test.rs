//! One exact audited initializer call can remove its own operation-domain
//! barrier while unrelated heap writes and retained callback use remain live.

use analysis::analysis_provenance::InferenceRule;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use sha2::{Digest, Sha256};
use spec::{
    BundledPartialSwapBundle, BundledPartialSwapMark, BundledPartialSwapPackage,
    InitializerCallEffectiveReplacement, PartialSwapKind, PartialSwapSymbol,
};
use std::collections::BTreeMap;
use std::fs;
use tempfile::TempDir;

const FIRST: &str = "const first = wrap(function () { return neighbor[0]; });";
const SECOND: &str = "const second = wrap(function () { return neighbor[1]; });";
const FIRST_COMMA: &str = "const before = 1, first = wrap(function () { return neighbor[0]; });";
const SECOND_COMMA: &str = "const second = wrap(function () { return neighbor[1]; }), after = 2;";
const FIRST_DEFERRED_OPAQUE: &str = "const first = wrap(function () { return neighbor?.[0]; });";
const SECOND_DEFERRED_OPAQUE: &str = "const second = wrap(function () { return neighbor?.[1]; });";
// Optional chaining marks only the deferred callback body opaque. Creating
// either fresh function still executes no body code at module initialization.
const SOURCE_DEFERRED_OPAQUE: &str = "import { wrap } from './peer.js';\n\
const neighbor = ['initial'];\n\
const first = wrap(function () { return neighbor?.[0]; });\n\
neighbor.push('later');\n\
const second = wrap(function () { return neighbor?.[1]; });\n\
console.log(first.invoke(), second.invoke(), neighbor.length);\n\
export { first, second, neighbor };\n";
const SOURCE: &str = "import { wrap } from './peer.js';\n\
const neighbor = ['initial'];\n\
const first = wrap(function () { return neighbor[0]; });\n\
neighbor.push('later');\n\
const second = wrap(function () { return neighbor[1]; });\n\
console.log(first.invoke(), second.invoke(), neighbor.length);\n\
export { first, second, neighbor };\n";
const PROVIDER: &str = "export function wrap(callback) {\n\
  const record = { callback };\n\
  return { invoke() { return record.callback(); } };\n\
}\n";
const EAGER_PROVIDER: &str = "export function wrap(callback) {\n\
  const result = callback();\n\
  return { invoke() { return result; } };\n\
}\n";
// Both branches allocate a fresh wrapper, but the conditional/metadata read
// leaves this provider outside the straight-line imported-call result summary.
const PROVIDER_WITHOUT_RESULT_SUMMARY: &str = "export function wrap(callback) {\n\
  if (callback.length === 0) return { invoke() { return callback(); } };\n\
  return { invoke() { return callback(); } };\n\
}\n";
const SOURCE_COMMA: &str = "import { wrap } from './peer.js';\n\
const neighbor = ['initial'];\n\
const before = 1, first = wrap(function () { return neighbor[0]; });\n\
neighbor.push('later');\n\
const second = wrap(function () { return neighbor[1]; }), after = 2;\n\
console.log(first.invoke(), second.invoke(), neighbor.length);\n\
export { first, second, neighbor };\n";
const SOURCE_INLINE_FACTORY: &str = "import { wrap } from './peer.js';\n\
const neighbor = ['initial'];\n\
const first = wrap(function () { return neighbor[0]; });\n\
neighbor.push('later');\n\
const second = wrap(function () { return neighbor[1]; });\n\
console.log(first(), second(), neighbor.length);\n\
export { first, second, neighbor };\n";
const PROVIDER_INLINE_FACTORY: &str = "export function wrap(callback) {\n\
  return function () { return callback(); };\n\
}\n";
const EXTRA_CHUNKS: &[(&str, &str)] = &[("static/peer", PROVIDER)];
const EAGER_CHUNKS: &[(&str, &str)] = &[("static/peer", EAGER_PROVIDER)];
const EFFECTIVE_BUNDLE: &str = "const bundleWrap = callback => {\n\
  const record = { callback };\n\
  return { invoke() { return record.callback(); } };\n\
};\n\
export { bundleWrap };\n";

fn digest(source: &str) -> String {
    Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn input(source: &str, binding: &str, declaration: &str) -> InitializerCallEffectInputContract {
    InitializerCallEffectInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {declaration:?}\nbindings: [{binding}]\n"
        ))
        .expect("source selector"),
        audited_source_sha256: digest(source),
        provider_chunk: "static/peer".into(),
        provider_export: "wrap".into(),
        audited_provider_sha256: digest(PROVIDER),
        effective_replacement: None,
        note: Some(
            "Audited wrapper writes only fresh per-call records and reads stable protocol slots."
                .into(),
        ),
    }
}

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "callbacks/first",
            &[Member::new("first").with_initializer_purity()],
        ),
        logical_module(
            "callbacks/second",
            &[Member::new("second").with_initializer_purity()],
        ),
    ]
}

fn opts<'a>(purity: &'a [(&'a str, ChunkExportPurity)], asserted_calls: usize) -> FixtureOpts<'a> {
    let opts = FixtureOpts::new(SOURCE, modules())
        .with_extra_chunks(EXTRA_CHUNKS)
        .with_unassigned_mode(unassigned_mode_inline())
        .with_chunk_export_purity(purity);
    if asserted_calls > 0 {
        let mut inputs = vec![input(SOURCE, "first", FIRST)];
        if asserted_calls == 2 {
            inputs.push(input(SOURCE, "second", SECOND));
        }
        opts.with_initializer_call_effect_inputs("static/app", inputs)
    } else {
        opts
    }
}

#[test]
fn selected_call_in_prepared_comma_list_initializers_is_covered() {
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_retained_callback_factory("wrap", 1, &[0])
        .build()];
    let unannotated = run_rejection_fixture(
        FixtureOpts::new(SOURCE_COMMA, modules())
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity),
    );
    assert!(
        !unannotated.cycles().is_empty(),
        "unselected split calls must retain their barriers"
    );
    let fixture = run_fixture(
        FixtureOpts::new(SOURCE_COMMA, modules())
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
            .with_initializer_call_effect_inputs(
                "static/app",
                vec![
                    input(SOURCE_COMMA, "first", FIRST_COMMA),
                    input(SOURCE_COMMA, "second", SECOND_COMMA),
                ],
            ),
    );
    assert_preserved_entry_output(&fixture, "initial later 2\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "prepared split owners must retain both exact author inputs: {report:#}"
    );
}

#[test]
fn exact_initializer_call_effect_inputs_break_only_selected_operation_cycles() {
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_retained_callback_factory("wrap", 1, &[0])
        .build()];
    let rejected = run_rejection_fixture(opts(&purity, 0));
    assert!(!rejected.cycles().is_empty());

    let first_only = run_rejection_fixture(opts(&purity, 1));
    let cycles = first_only.cycles();
    assert_eq!(
        cycles.len(),
        1,
        "the unselected second initializer should retain one cycle: {cycles:#?}"
    );
    assert_eq!(
        cycles[0]["modules"],
        serde_json::json!(["callbacks/second", "anon_residual_sentinel"]),
        "only the unselected second initializer may remain in the cycle: {cycles:#?}"
    );
    let first_only_report = first_only.effect_translation_validation();
    assert_eq!(
        first_only_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([{"defining_chunk": "static/app", "input_index": 0}]),
        "the selected first initializer needs normal root coverage: {first_only_report:#}"
    );

    let fixture = run_fixture(opts(&purity, 2));
    assert_preserved_entry_output(&fixture, "initial later 2\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "completed owner coverage must identify both author inputs: {report:#}"
    );
}

#[test]
fn deferred_opaque_callback_body_does_not_poison_fresh_function_creation() {
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_callback_factories(&["wrap"])
        .build()];
    let fixture_opts = || {
        FixtureOpts::new(SOURCE_DEFERRED_OPAQUE, modules())
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
    };
    let no_input = run_rejection_fixture(fixture_opts());
    assert!(
        !no_input.cycles().is_empty(),
        "callback timing and purity alone do not cover the wrapper operation"
    );

    let inputs = vec![
        input(SOURCE_DEFERRED_OPAQUE, "first", FIRST_DEFERRED_OPAQUE),
        input(SOURCE_DEFERRED_OPAQUE, "second", SECOND_DEFERRED_OPAQUE),
    ];
    let fixture =
        run_fixture(fixture_opts().with_initializer_call_effect_inputs("static/app", inputs));
    assert_preserved_entry_output(&fixture, "initial later 2\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "both exact operation inputs need completed owner receipts: {report:#}"
    );
}

#[test]
fn exact_wrapper_effect_input_does_not_cover_eager_callback_body() {
    // The exact operation input does not defer a callback. This provider
    // invokes it during initialization; its optional-chain body must retain
    // its own effects and uncertainty, independently of the wrapper input.
    let eager_purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .build()];
    let mut eager_inputs = vec![
        input(SOURCE_DEFERRED_OPAQUE, "first", FIRST_DEFERRED_OPAQUE),
        input(SOURCE_DEFERRED_OPAQUE, "second", SECOND_DEFERRED_OPAQUE),
    ];
    for input in &mut eager_inputs {
        input.audited_provider_sha256 = digest(EAGER_PROVIDER);
    }
    let eager = run_rejection_fixture(
        FixtureOpts::new(SOURCE_DEFERRED_OPAQUE, modules())
            .with_extra_chunks(EAGER_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&eager_purity)
            .with_initializer_call_effect_inputs("static/app", eager_inputs),
    );
    assert!(
        !eager.cycles().is_empty(),
        "actual eager callback effects must remain after the wrapper input"
    );
}

#[test]
fn ordered_imported_callback_factory_transfers_a_retained_result() {
    // This is the plain callback-factory assertion. The older positive case
    // above uses the separate retained-callback-factory signature, which has
    // its own ordered return transfer.
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_callback_factories(&["wrap"])
        .build()];
    let no_input = run_rejection_fixture(opts(&purity, 0));
    assert!(
        !no_input.cycles().is_empty(),
        "callback timing and provider purity do not cover the initializer operation"
    );

    let source_inferred = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .build()];
    let without_factory = run_fixture(opts(&source_inferred, 2));
    assert_preserved_entry_output(&without_factory, "initial later 2\n");
    let inferred_graph = without_factory.dependency_graph();
    for binding in ["first", "second"] {
        let owner_id = source_unit_for_binding(&inferred_graph, binding);
        let owner = inferred_graph
            .nodes
            .iter()
            .find(|node| node.id == owner_id)
            .expect("source-inferred wrapper owner");
        assert!(
            owner
                .synchronous_execution
                .as_ref()
                .expect("wrapper execution evidence")
                .inference_uses
                .iter()
                .any(|usage| usage.conclusion == InferenceRule::ForeignCallSummary),
            "{binding} needs independent source-derived result evidence"
        );
    }
    let without_factory_report = without_factory.effect_translation_validation();
    assert_eq!(
        without_factory_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "source-derived result evidence may independently complete both selected calls"
    );

    let opaque_provider = [("static/peer", PROVIDER_WITHOUT_RESULT_SUMMARY)];
    let mut opaque_inputs = vec![
        input(SOURCE, "first", FIRST),
        input(SOURCE, "second", SECOND),
    ];
    for entry in &mut opaque_inputs {
        entry.audited_provider_sha256 = digest(PROVIDER_WITHOUT_RESULT_SUMMARY);
    }
    let without_result_evidence = run_rejection_fixture(
        FixtureOpts::new(SOURCE, modules())
            .with_extra_chunks(&opaque_provider)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&source_inferred)
            .with_initializer_call_effect_inputs("static/app", opaque_inputs),
    );
    assert!(
        !without_result_evidence.cycles().is_empty(),
        "operation and purity inputs cannot invent a result for an opaque provider"
    );
    let without_result_report = without_result_evidence.effect_translation_validation();
    assert_eq!(
        without_result_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::Value::Null,
        "a provider without result evidence must not consume the operation inputs"
    );

    let no_initializer_purity = vec![
        logical_module("callbacks/first", &[Member::new("first")]),
        logical_module("callbacks/second", &[Member::new("second")]),
    ];
    let without_initializer_purity = run_rejection_fixture(
        FixtureOpts::new(SOURCE, no_initializer_purity)
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
            .with_initializer_call_effect_inputs(
                "static/app",
                vec![
                    input(SOURCE, "first", FIRST),
                    input(SOURCE, "second", SECOND),
                ],
            ),
    );
    assert!(
        !without_initializer_purity.cycles().is_empty(),
        "the factory carrier and operation input cannot certify a nonthrowing initializer"
    );
    let without_purity_report = without_initializer_purity.effect_translation_validation();
    assert_eq!(
        without_purity_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::Value::Null,
        "missing initializer purity must not consume the operation inputs"
    );

    let first_only = run_rejection_fixture(opts(&purity, 1));
    assert_eq!(
        first_only.cycles().len(),
        1,
        "only the unselected second wrapper should remain ordered with the neighbor"
    );
    let first_report = first_only.effect_translation_validation();
    assert_eq!(
        first_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([{"defining_chunk": "static/app", "input_index": 0}]),
        "the first exact call must earn ordinary root coverage: {first_report:#}"
    );

    let both = run_fixture(opts(&purity, 2));
    // `invoke` runs after the neighbor mutation; the callback and its later
    // heap read must remain reachable through the returned opaque wrapper.
    assert_preserved_entry_output(&both, "initial later 2\n");
    let both_report = both.effect_translation_validation();
    assert_eq!(
        both_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "both exact calls must earn independent operation receipts: {both_report:#}"
    );
}

#[test]
fn ordered_inline_callback_factory_transfers_its_fresh_callable() {
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_inline_callback_factories(&["wrap"])
        .build()];
    let mut inputs = vec![
        input(SOURCE_INLINE_FACTORY, "first", FIRST),
        input(SOURCE_INLINE_FACTORY, "second", SECOND),
    ];
    for entry in &mut inputs {
        entry.audited_provider_sha256 = digest(PROVIDER_INLINE_FACTORY);
    }
    let extra_chunks = [("static/peer", PROVIDER_INLINE_FACTORY)];
    let fixture = run_fixture(
        FixtureOpts::new(SOURCE_INLINE_FACTORY, modules())
            .with_extra_chunks(&extra_chunks)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
            .with_initializer_call_effect_inputs("static/app", inputs),
    );
    assert_preserved_entry_output(&fixture, "initial later 2\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "the inline-only factory needs separate callback and operation evidence: {report:#}"
    );
}

#[test]
fn selected_bundled_replacement_needs_exact_effective_identity() {
    for (case, expected_error) in [
        ("valid", None),
        ("missing", Some("effective_replacement is required")),
        (
            "stale_digest",
            Some("stale effective_replacement.audited_bundle_sha256"),
        ),
        (
            "mismapped_export",
            Some("disagrees with the selected vendor mapping"),
        ),
    ] {
        let external = TempDir::with_prefix("debundle-initializer-effective-")
            .expect("replacement test directory");
        let bundle_path = external.path().join("effective-bundle.js");
        write_text_file(&bundle_path, EFFECTIVE_BUNDLE);
        let package_root = external.path().join("upstream-package");
        write_text_file(
            &package_root.join("package.json"),
            "{\"name\":\"neutral-wrapper\",\"version\":\"1.0.0\"}\n",
        );
        write_text_file(
            &package_root.join("index.js"),
            "export default function wrap(callback) { return { invoke: callback }; }\n",
        );
        let package = "neutral-wrapper";
        let bundle_export = "bundleWrap";
        let vendor_marks = [(
            "static/peer.js",
            VendorMark {
                identity: "neutral replacement wrapper".into(),
                role: Default::default(),
                level: VendorLevel::BundledPartialSwap(BundledPartialSwapMark {
                    bundle: BundledPartialSwapBundle {
                        path: bundle_path.clone(),
                    },
                    packages: BTreeMap::from([(
                        package.into(),
                        BundledPartialSwapPackage {
                            version: "1.0.0".into(),
                            subpath: "index.js".into(),
                            bundle_export: bundle_export.into(),
                            namespace: None,
                        },
                    )]),
                    symbols: BTreeMap::from([(
                        "wrap".into(),
                        PartialSwapSymbol {
                            package: package.into(),
                            kind: PartialSwapKind::Default,
                            upstream_export: None,
                            local: None,
                        },
                    )]),
                }),
            },
        )];
        let purity = [ChunkExportPurityBuilder::new("static/peer")
            .with_pure_exports(&["wrap"])
            .with_retained_callback_factory("wrap", 1, &[0])
            .build()];
        let mut inputs = vec![
            input(SOURCE, "first", FIRST),
            input(SOURCE, "second", SECOND),
        ];
        for input in &mut inputs {
            if case != "missing" {
                input.effective_replacement = Some(InitializerCallEffectiveReplacement {
                    package: package.into(),
                    bundle_export: if case == "mismapped_export" {
                        "wrongExport"
                    } else {
                        bundle_export
                    }
                    .into(),
                    symbol_kind: PartialSwapKind::Default,
                    upstream_export: None,
                    audited_bundle_sha256: if case == "stale_digest" {
                        "0".repeat(64)
                    } else {
                        digest(EFFECTIVE_BUNDLE)
                    },
                });
            }
        }
        let fixture = write_validate_fixture_spec(
            FixtureOpts::new(SOURCE, modules())
                .with_extra_chunks(EXTRA_CHUNKS)
                .with_unassigned_mode(unassigned_mode_inline())
                .with_chunk_export_purity(&purity)
                .with_initializer_call_effect_inputs("static/app", inputs)
                .with_vendor_marks(&vendor_marks),
        );
        let root = fixture.spec_path.parent().expect("fixture root");
        let mut authored_spec: serde_json::Value =
            serde_yaml::from_str(&fs::read_to_string(&fixture.spec_path).unwrap()).unwrap();
        authored_spec["swap_vendor_chunks"] = serde_json::json!({
            "output_manifest_path": root.join("out/reports/vendor_swaps.json"),
            "output_wrapper_dir": root.join("out/app/vendors/generated"),
            "write": true,
        });
        write_yaml_file(&fixture.spec_path, &authored_spec);
        let result = run_debundler(&fixture.spec_path, &[(package, &package_root)]);
        if let Some(expected_error) = expected_error {
            assert!(
                !result.status.success(),
                "{case} unexpectedly passed\nstdout:\n{}\nstderr:\n{}",
                result.stdout,
                result.stderr
            );
            assert!(
                result.stderr.contains(expected_error),
                "{case} did not report {expected_error:?}\nstdout:\n{}\nstderr:\n{}",
                result.stdout,
                result.stderr
            );
            continue;
        }
        assert!(
            result.status.success(),
            "valid replacement failed\nstdout:\n{}\nstderr:\n{}",
            result.stdout,
            result.stderr
        );
        assert_node_output(
            &root.join("out/app/static/app/entry.js"),
            "initial later 2\n",
            "",
        );
        let report: serde_json::Value =
            read_json(&root.join("out/reports/tree/static/app/effect_translation_validation.json"));
        assert_eq!(
            report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
            serde_json::json!([
                {"defining_chunk": "static/app", "input_index": 0},
                {"defining_chunk": "static/app", "input_index": 1},
            ]),
            "effective replacement must preserve both normal owner receipts: {report:#}"
        );
    }
}

#[test]
fn source_known_original_provider_global_write_rejects_audited_call() {
    for (case, provider) in [
        (
            "direct",
            "export function wrap(callback) { globalThis.neutralProbe = 1; const record = { callback }; return { invoke() { return record.callback(); } }; }\n",
        ),
        (
            "helper",
            "function writeProbe() { globalThis.neutralProbe = 1; } export function wrap(callback) { writeProbe(); const record = { callback }; return { invoke() { return record.callback(); } }; }\n",
        ),
        (
            "aliased_helper",
            "function writeProbe() { globalThis.neutralProbe = 1; } const helper = writeProbe; export function wrap(callback) { helper(); const record = { callback }; return { invoke() { return record.callback(); } }; }\n",
        ),
    ] {
        let extra_chunks = [("static/peer", provider)];
        let purity = [ChunkExportPurityBuilder::new("static/peer")
            .with_pure_exports(&["wrap"])
            .with_callback_factories(&["wrap"])
            .build()];
        let mut inputs = vec![
            input(SOURCE, "first", FIRST),
            input(SOURCE, "second", SECOND),
        ];
        for entry in &mut inputs {
            entry.audited_provider_sha256 = digest(provider);
        }
        let fixture = write_validate_fixture_spec(
            FixtureOpts::new(SOURCE, modules())
                .with_extra_chunks(&extra_chunks)
                .with_unassigned_mode(unassigned_mode_inline())
                .with_chunk_export_purity(&purity)
                .with_initializer_call_effect_inputs("static/app", inputs),
        );
        let result = run_debundler(&fixture.spec_path, &[]);
        assert!(
            !result.status.success(),
            "{case} source-known global write was accepted\nstdout:\n{}\nstderr:\n{}",
            result.stdout,
            result.stderr
        );
        assert!(
            result
                .stderr
                .contains("source-known original provider write"),
            "{case} should identify the provider contradiction\nstdout:\n{}\nstderr:\n{}",
            result.stdout,
            result.stderr
        );
    }
}
