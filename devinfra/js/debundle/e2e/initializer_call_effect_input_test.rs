//! One exact audited initializer call can remove its own operation-domain
//! barrier while unrelated heap writes and retained callback use remain live.

use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use spec::{
    BundledPartialSwapBundle, BundledPartialSwapMark, BundledPartialSwapPackage,
    InitializerCallEffectiveReplacement, PartialSwapKind, PartialSwapSymbol,
    ResolvedMemberSelector,
};
use std::collections::BTreeMap;
use std::fs;
use tempfile::TempDir;

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
// The result is private at runtime, but try/finally has no ordered kernel
// plan. An operation annotation cannot supply the missing result execution.
const PROVIDER_WITHOUT_RESULT_SUMMARY: &str = "export function wrap(callback) {\n\
  try { return { invoke() { return callback(); } }; } finally {}\n\
}\n";
const SOURCE_COMMA: &str = "import { wrap } from './peer.js';\n\
const neighbor = ['initial'];\n\
const before = 1, first = wrap(function () { return neighbor[0]; });\n\
neighbor.push('later');\n\
const second = wrap(function () { return neighbor[1]; }), after = 2;\n\
console.log(first.invoke(), second.invoke(), neighbor.length);\n\
export { first, second, neighbor };\n";
const SOURCE_THREE_WRAPPERS: &str = "import { wrap } from './peer.js';\n\
const neighbor = ['initial'];\n\
const first = wrap(function () { return neighbor?.length; });\n\
neighbor.push('later');\n\
const second = wrap(function () { return neighbor?.length; });\n\
neighbor.push('latest');\n\
const third = wrap(function () { return neighbor?.length; });\n\
console.log(first.invoke(), second.invoke(), third.invoke(), neighbor.length);\n\
export { first, second, third, neighbor };\n";
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

const STRING_FACTORY_SOURCE: &str = "import { make } from './peer.js';\n\
const values = ['a'];\n\
const seed = 'abc';\n\
const slot = make(seed), sibling = 4;\n\
values.push('b');\n\
const later = slot;\n\
console.log(later(), values.length, sibling);\n\
export { slot, later, values, sibling };\n";
const STRING_FACTORY_PROVIDER: &str = "export function make(seed) {\n\
  const size = seed.length;\n\
  return () => size;\n\
}\n";
const EAGER_STRING_FACTORY_SOURCE: &str = "import { keep, make } from './peer.js';\n\
let observed = 0;\n\
const callback = () => observed;\n\
keep(callback);\n\
const seed = 'abc';\n\
const slot = make(seed), sibling = 4;\n\
observed = 42;\n\
console.log(slot(), sibling);\n\
export { callback, observed, seed, slot, sibling };\n";
const EAGER_STRING_FACTORY_PROVIDER: &str = "let retained;\n\
export function keep(callback) { retained = callback; }\n\
export function make(seed) {\n\
  const captured = retained();\n\
  return () => captured + seed.length;\n\
}\n";
const LATER_CALL_SOURCE: &str = "import { make } from './peer.js';\n\
globalThis.value = 0;\n\
const seed = 'abc';\n\
const slot = make(seed), sibling = 4;\n\
const seen = slot();\n\
globalThis.value = 42;\n\
console.log(seen, sibling);\n\
export { seed, slot, sibling, seen };\n";
const LATER_CALL_PROVIDER: &str = "export function make(seed) {\n\
  return () => globalThis.value + seed.length;\n\
}\n";

fn expect_schedule_refusal(opts: FixtureOpts<'_>, expected_output: &str) -> RejectedFixture {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "expected a conservative evaluation-schedule refusal:\n{}",
        rejected.stderr
    );
    // The fixture runner also compares source and emitted Node execution.
    // Keep the intended runtime result explicit at each downgraded oracle.
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        expected_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), expected_output, "");
    rejected
}

#[test]
fn exact_string_factory_result_allows_separate_data_module() {
    let peer = [("static/peer", STRING_FACTORY_PROVIDER)];
    let modules = vec![
        logical_module(
            "callbacks/slot",
            &[
                Member::new("seed"),
                Member::new("slot").with_initializer_purity(),
            ],
        ),
        logical_module_with_anon(
            "values/data",
            &[
                Member::new("values"),
                Member::new("later"),
                Member::new("sibling"),
            ],
            &["values.push('b');"],
        ),
    ];
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["make"])
        .build()];
    let opts = || {
        FixtureOpts::new(STRING_FACTORY_SOURCE, modules.clone())
            .with_extra_chunks(&peer)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
    };
    let baseline = run_rejection_fixture_with_graphs(opts());
    assert!(
        !baseline.cycles().is_empty(),
        "the unproved factory call must leave the split rejected"
    );
    let mut receipt = input(STRING_FACTORY_SOURCE, "slot");
    receipt.provider_export = "make".into();
    receipt.audited_provider_sha256 = source_sha256(STRING_FACTORY_PROVIDER);
    receipt.resolved_member.module = "callbacks/slot".into();
    receipt.returns_opaque_function_from_const_primitive = true;
    let fixture = run_fixture_with_graphs(
        opts().with_initializer_call_effect_inputs("static/app", vec![receipt]),
    );
    assert_preserved_entry_output(&fixture, "3 2 4\n");
    assert!(
        fixture
            .out_root
            .join("static/app/modules/callbacks/slot.js")
            .exists()
    );
    assert!(
        fixture
            .out_root
            .join("static/app/modules/values/data.js")
            .exists()
    );
    assert_eq!(fixture.translation_validation()["accepts"], true);
}

#[test]
fn exact_result_receipt_does_not_defer_provider_callback_without_timing_premise() {
    let peer = [("static/peer", EAGER_STRING_FACTORY_PROVIDER)];
    let modules = vec![
        logical_module(
            "callbacks/slot",
            &[
                Member::new("seed"),
                Member::new("slot").with_initializer_purity(),
            ],
        ),
        logical_module_with_anon(
            "values/data",
            &[
                Member::new("observed"),
                Member::new("callback"),
                Member::new("sibling"),
            ],
            &["keep(callback);", "observed = 42;"],
        ),
    ];
    let mut receipt = input(EAGER_STRING_FACTORY_SOURCE, "slot");
    receipt.provider_export = "make".into();
    receipt.audited_provider_sha256 = source_sha256(EAGER_STRING_FACTORY_PROVIDER);
    receipt.resolved_member.module = "callbacks/slot".into();
    receipt.returns_opaque_function_from_const_primitive = true;
    let rejection = run_rejection_fixture_with_graphs(
        FixtureOpts::new(EAGER_STRING_FACTORY_SOURCE, modules)
            .with_extra_chunks(&peer)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_initializer_call_effect_inputs("static/app", vec![receipt]),
    );
    assert!(
        !rejection.cycles().is_empty(),
        "the retained callback reads the later-written binding at factory time"
    );
    assert_node_output(&rejection.source_root.join("static/app.js"), "3 4\n", "");
}

#[test]
fn opaque_factory_result_keeps_later_call_effects() {
    let peer = [("static/peer", LATER_CALL_PROVIDER)];
    let modules = vec![
        logical_module(
            "callbacks/slot",
            &[
                Member::new("seed"),
                Member::new("slot").with_initializer_purity(),
                Member::new("seen"),
            ],
        ),
        logical_module_with_anon(
            "values/data",
            &[Member::new("sibling")],
            &["globalThis.value = 0;", "globalThis.value = 42;"],
        ),
    ];
    let mut receipt = input(LATER_CALL_SOURCE, "slot");
    receipt.provider_export = "make".into();
    receipt.audited_provider_sha256 = source_sha256(LATER_CALL_PROVIDER);
    receipt.resolved_member.module = "callbacks/slot".into();
    receipt.returns_opaque_function_from_const_primitive = true;
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["make"])
        .build()];
    let rejection = run_rejection_fixture_with_graphs(
        FixtureOpts::new(LATER_CALL_SOURCE, modules)
            .with_extra_chunks(&peer)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
            .with_initializer_call_effect_inputs("static/app", vec![receipt]),
    );
    assert!(
        !rejection.cycles().is_empty(),
        "a later invocation of the returned callable can read the intervening host write"
    );
    assert_node_output(&rejection.source_root.join("static/app.js"), "3 4\n", "");
}

fn input(source: &str, binding: &str) -> InitializerCallEffectInputContract {
    InitializerCallEffectInputContract {
        resolved_member: ResolvedMemberSelector {
            module: format!("callbacks/{binding}"),
            name: binding.into(),
        },
        audited_source_sha256: source_sha256(source),
        provider_chunk: "static/peer".into(),
        provider_export: "wrap".into(),
        audited_provider_sha256: source_sha256(PROVIDER),
        returns_opaque_function_from_const_primitive: false,
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
        let mut inputs = vec![input(SOURCE, "first")];
        if asserted_calls == 2 {
            inputs.push(input(SOURCE, "second"));
        }
        opts.with_initializer_call_effect_inputs("static/app", inputs)
    } else {
        opts
    }
}

#[test]
fn selected_call_inputs_do_not_override_the_final_schedule_guard() {
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_retained_callback_factory("wrap", 1, &[0])
        .build()];
    let unannotated = run_rejection_fixture_with_graphs(
        FixtureOpts::new(SOURCE_COMMA, modules())
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity),
    );
    assert!(
        !unannotated.cycles().is_empty(),
        "unselected split calls must retain their barriers"
    );
    let fixture = expect_schedule_refusal(
        FixtureOpts::new(SOURCE_COMMA, modules())
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
            .with_initializer_call_effect_inputs(
                "static/app",
                vec![input(SOURCE_COMMA, "first"), input(SOURCE_COMMA, "second")],
            ),
        "initial later 2\n",
    );
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
fn separate_wrappers_with_deferred_callbacks_keep_the_schedule_refusal() {
    let modules = vec![
        logical_module(
            "callbacks/first",
            &[Member::new("first").with_initializer_purity()],
        ),
        logical_module(
            "callbacks/second",
            &[Member::new("second").with_initializer_purity()],
        ),
        logical_module(
            "callbacks/third",
            &[Member::new("third").with_initializer_purity()],
        ),
    ];
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_retained_callback_factory("wrap", 1, &[0])
        .build()];
    let opts = || {
        FixtureOpts::new(SOURCE_THREE_WRAPPERS, modules.clone())
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
    };
    let inputs = ["first", "second", "third"]
        .map(|binding| input(SOURCE_THREE_WRAPPERS, binding))
        .to_vec();
    let rejected = expect_schedule_refusal(
        opts().with_initializer_call_effect_inputs("static/app", inputs),
        "3 3 3 3\n",
    );
    assert_eq!(
        rejected.effect_translation_validation()["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
            {"defining_chunk": "static/app", "input_index": 2},
        ]),
        "the author receipts remain exact even when schedule completion is conservative"
    );
}

#[test]
fn exact_initializer_call_effect_inputs_do_not_bypass_schedule_completion() {
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_retained_callback_factory("wrap", 1, &[0])
        .build()];
    let rejected = run_rejection_fixture_with_graphs(opts(&purity, 0));
    assert!(!rejected.cycles().is_empty());

    let first_only = run_rejection_fixture_with_graphs(opts(&purity, 1));
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

    let fixture = expect_schedule_refusal(opts(&purity, 2), "initial later 2\n");
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
    // The shared provider body allocates only its private record and method.
    // Its creation receipt does not execute the opaque deferred callback.
    expect_schedule_refusal(fixture_opts(), "initial later 2\n");

    let inputs = vec![
        input(SOURCE_DEFERRED_OPAQUE, "first"),
        input(SOURCE_DEFERRED_OPAQUE, "second"),
    ];
    let fixture = expect_schedule_refusal(
        fixture_opts().with_initializer_call_effect_inputs("static/app", inputs),
        "initial later 2\n",
    );
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
        input(SOURCE_DEFERRED_OPAQUE, "first"),
        input(SOURCE_DEFERRED_OPAQUE, "second"),
    ];
    for input in &mut eager_inputs {
        input.audited_provider_sha256 = source_sha256(EAGER_PROVIDER);
    }
    let eager = run_rejection_fixture_with_graphs(
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
fn ordered_imported_callback_factory_keeps_receipts_under_schedule_refusal() {
    // A prepared provider executes its actual allocation-only body. Complete
    // private-cell and method-creation operations can prove this result even
    // without an opaque retaining-factory transfer or operation annotation.
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_callback_factories(&["wrap"])
        .build()];
    expect_schedule_refusal(opts(&purity, 0), "initial later 2\n");

    let source_inferred = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .build()];
    let without_factory = expect_schedule_refusal(opts(&source_inferred, 2), "initial later 2\n");
    let inferred_graph = without_factory.dependency_graph();
    for binding in ["first", "second"] {
        let owner_id = source_unit_for_binding(&inferred_graph, binding);
        let owner = inferred_graph
            .nodes
            .iter()
            .find(|node| node.id == owner_id)
            .expect("source-inferred wrapper owner");
        let execution = serde_json::to_value(
            owner
                .synchronous_execution
                .as_ref()
                .expect("wrapper execution evidence"),
        )
        .unwrap();
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "operation_receipt"
        );
        assert!(
            execution["binding_effect_authority"]["receipt"]["calls"]
                .as_array()
                .unwrap()
                .iter()
                .any(|call| {
                    call["target"]["kind"] == "callable"
                        && call["target"]["source_module"] == "static/peer"
                }),
            "{binding} needs the actual provider body's receipt: {execution:#}"
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
    let mut opaque_inputs = vec![input(SOURCE, "first"), input(SOURCE, "second")];
    for entry in &mut opaque_inputs {
        entry.audited_provider_sha256 = source_sha256(PROVIDER_WITHOUT_RESULT_SUMMARY);
    }
    let without_result_evidence = run_rejection_fixture_with_graphs(
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
    let without_result_graph = without_result_evidence.dependency_graph();
    for binding in ["first", "second"] {
        let owner_id = source_unit_for_binding(&without_result_graph, binding);
        let owner = without_result_graph
            .nodes
            .iter()
            .find(|node| node.id == owner_id)
            .unwrap();
        let execution =
            serde_json::to_value(owner.synchronous_execution.as_ref().unwrap()).unwrap();
        assert_ne!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "operation-domain inputs cannot seal an unmodeled provider body: {execution:#}"
        );
    }

    let no_initializer_purity = vec![
        logical_module("callbacks/first", &[Member::new("first")]),
        logical_module("callbacks/second", &[Member::new("second")]),
    ];
    let without_initializer_purity = expect_schedule_refusal(
        FixtureOpts::new(SOURCE, no_initializer_purity)
            .with_extra_chunks(EXTRA_CHUNKS)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
            .with_initializer_call_effect_inputs(
                "static/app",
                vec![input(SOURCE, "first"), input(SOURCE, "second")],
            ),
        "initial later 2\n",
    );
    let without_purity_report = without_initializer_purity.effect_translation_validation();
    assert_eq!(
        without_purity_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::Value::Null,
        "missing initializer purity must not consume the operation inputs"
    );

    let first_only = expect_schedule_refusal(opts(&purity, 1), "initial later 2\n");
    let first_report = first_only.effect_translation_validation();
    assert_eq!(
        first_report["coverage"]["covered_owner_dependencies"]["initializer_call_effect_inputs"],
        serde_json::json!([{"defining_chunk": "static/app", "input_index": 0}]),
        "the first exact call must earn ordinary root coverage: {first_report:#}"
    );

    let both = expect_schedule_refusal(opts(&purity, 2), "initial later 2\n");
    // `invoke` runs after the neighbor mutation; the callback and its later
    // heap read must remain reachable through the returned opaque wrapper.
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
fn ordered_inline_callback_factory_keeps_receipts_under_schedule_refusal() {
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["wrap"])
        .with_inline_callback_factories(&["wrap"])
        .build()];
    let mut inputs = vec![
        input(SOURCE_INLINE_FACTORY, "first"),
        input(SOURCE_INLINE_FACTORY, "second"),
    ];
    for entry in &mut inputs {
        entry.audited_provider_sha256 = source_sha256(PROVIDER_INLINE_FACTORY);
    }
    let extra_chunks = [("static/peer", PROVIDER_INLINE_FACTORY)];
    let fixture = expect_schedule_refusal(
        FixtureOpts::new(SOURCE_INLINE_FACTORY, modules())
            .with_extra_chunks(&extra_chunks)
            .with_unassigned_mode(unassigned_mode_inline())
            .with_chunk_export_purity(&purity)
            .with_initializer_call_effect_inputs("static/app", inputs),
        "initial later 2\n",
    );
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
fn selected_bundled_replacement_needs_exact_effective_identity_in_a_phase_preserving_layout() {
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
        let phase_preserving_layout = expected_error.is_none();
        let mut inputs = vec![input(SOURCE, "first"), input(SOURCE, "second")];
        if phase_preserving_layout {
            for input in &mut inputs {
                input.resolved_member.module = "callbacks/combined".into();
            }
        }
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
                        source_sha256(EFFECTIVE_BUNDLE)
                    },
                });
            }
        }
        let modules = if phase_preserving_layout {
            vec![logical_module_with_anon(
                "callbacks/combined",
                &[
                    Member::new("neighbor"),
                    Member::new("first").with_initializer_purity(),
                    Member::new("second").with_initializer_purity(),
                ],
                &["neighbor.push('later');"],
            )]
        } else {
            modules()
        };
        let fixture = write_validate_fixture_spec(
            FixtureOpts::new(SOURCE, modules)
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
            "exact effective replacement and phase-preserving layout should pass\nstdout:\n{}\nstderr:\n{}",
            result.stdout,
            result.stderr
        );
        let workspace = fixture.workspace();
        assert!(
            workspace
                .path()
                .join("out/app/vendors/generated/static/peer/neutral-wrapper.js")
                .is_file(),
            "the accepted CLI run must emit the selected replacement facade"
        );
        assert_node_output(
            &workspace.path().join("snapshot/static/app.js"),
            "initial later 2\n",
            "",
        );
        assert_node_output(
            &workspace.path().join("out/app/static/app/entry.js"),
            "initial later 2\n",
            "",
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
        let mut inputs = vec![input(SOURCE, "first"), input(SOURCE, "second")];
        for entry in &mut inputs {
            entry.audited_provider_sha256 = source_sha256(provider);
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
