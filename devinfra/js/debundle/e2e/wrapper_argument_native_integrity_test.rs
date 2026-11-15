use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

// Each guarded write names `Map`, so through the unknown `globalThis.shared`
// receiver it reaches the `%Map%` global slot `factory()` constructs from.

const WRAPPER: &str = "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: {} }).value, cache), cache.value);\n";
const PROVIDER: &str = "import { readLater } from './app.js';\nexport function factory() { const state = new Map(); return { get size() { return state.size; }, label() { return readLater().name; } }; }\n";
const NAMESPACE: &str = "import { wrap } from './wrapper.js';\nexport const load = wrap(function(output, container) { (function(root, fill) { typeof output === 'object' && typeof container < 'u' ? fill(output) : (root = globalThis, root.shared = root.shared || {}, root.shared.Map = {}, fill(root.shared.Map)); })(output, function(value) { value.ready = true; }); });\nload();\n";
const APP_SOURCE: &str = "import { factory } from './peer.js';\nimport './namespace.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nconsole.log(queue.size, readLater().name);\nexport { queue, Later };\n";

fn opts<'a>(chunks: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        APP_SOURCE,
        vec![
            logical_module("queue", &[Member::new("queue"), Member::new("readLater")]),
            logical_module("later", &[Member::new("Later")]),
        ],
    )
    .with_extra_chunks(chunks)
}

fn assert_split_refused_with_map_guard(chunks: &[(&str, &str)], reason: &str) {
    let rejected = assert_conservative_schedule_refusal(opts(chunks), reason, "0 Later\n");
    let assessment = rejected.candidate_relocation_assessment();
    let program_graph = serde_json::to_value(rejected.program_graph()).unwrap();
    assert!(
        program_graph["native_integrity_witnesses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|witness| {
                witness["target_slots"]
                    .as_array()
                    .is_some_and(|slots| slots.iter().any(|slot| slot["key"]["string"] == "Map"))
                    && witness["affected_roots"]
                        .as_array()
                        .is_some_and(|roots| !roots.is_empty())
                    && witness["unreachable_branch"].is_null()
            }),
        "unsupported wrapper callbacks must retain the affected, reachable Map write witness: {program_graph:#?}"
    );
    assert_eq!(assessment["decision"]["havoc"], true, "{assessment:#}");
    assert_eq!(
        assessment["decision"]["uncovered_havoc"], true,
        "{assessment:#}"
    );
    let control =
        run_fixture_with_graphs(FixtureOpts::new(APP_SOURCE, vec![]).with_extra_chunks(chunks));
    assert_preserved_entry_output(&control, "0 Later\n");
}

fn assert_refused_with_matching_node_error(
    chunks: &[(&str, &str)],
    reason: &str,
    node_error: &str,
) {
    let rejected = run_emit_on_rejection_fixture_with_graphs(opts(chunks));
    let schedule: serde_json::Value =
        read_json(&rejected.report_root.join("evaluation_schedule.json"));
    assert_eq!(
        schedule["kind"], "evaluation_schedule_refused",
        "{schedule:#}"
    );
    assert!(
        schedule["rejection"]["reason"]
            .as_str()
            .is_some_and(|actual| actual.starts_with(reason)),
        "{schedule:#}"
    );
    assert!(rejected.gate_rejected_marker().exists());
    assert_node_error(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        node_error,
    );
    assert_node_error(&rejected.entry_path(), node_error);

    let program_graph = serde_json::to_value(rejected.program_graph()).unwrap();
    assert!(
        program_graph["native_integrity_witnesses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|witness| {
                witness["target_slots"]
                    .as_array()
                    .is_some_and(|slots| slots.iter().any(|slot| slot["key"]["string"] == "Map"))
                    && witness["affected_roots"]
                        .as_array()
                        .is_some_and(|roots| !roots.is_empty())
                    && witness["unreachable_branch"].is_null()
            }),
        "the rejected source retains its reachable Map write: {program_graph:#?}"
    );

    let safe_chunks = [
        ("static/peer", PROVIDER),
        ("static/wrapper", WRAPPER),
        ("static/namespace", NAMESPACE),
    ];
    let control = run_fixture_with_graphs(
        FixtureOpts::new(APP_SOURCE, vec![]).with_extra_chunks(&safe_chunks),
    );
    assert_preserved_entry_output(&control, "0 Later\n");
}

#[test]
fn source_wrapper_arguments_discharge_only_the_unreachable_native_write() {
    let chunks = [
        ("static/peer", PROVIDER),
        ("static/wrapper", WRAPPER),
        ("static/namespace", NAMESPACE),
    ];
    let rejected = assert_conservative_schedule_refusal(
        opts(&chunks),
        "evaluation_schedule_availability_unknown",
        "0 Later\n",
    );
    let assessment = rejected.candidate_relocation_assessment();
    assert_eq!(assessment["legacy_gate_accepts"], true, "{assessment:#}");
    let rejected_program = serde_json::to_value(rejected.program_graph()).unwrap();
    assert!(
        rejected_program["native_integrity_witnesses"]
            .as_array()
            .is_some_and(|witnesses| witnesses.iter().any(|witness| {
                witness["unreachable_branch"].is_object()
                    && witness["affected_roots"] == serde_json::json!([])
            })),
        "the source wrapper proof remains present on a refused split: {rejected_program}"
    );
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(APP_SOURCE, vec![]).with_extra_chunks(&chunks));
    assert_preserved_entry_output(&fixture, "0 Later\n");
    let program_graph = fixture.program_graph();
    let witness = program_graph
        .native_integrity_witnesses
        .iter()
        .find(|witness| {
            witness
                .receiver_roots
                .contains(&analysis::analysis_inputs::NativeIntrinsic::UnknownGlobalValue)
                && witness.unreachable_branch.is_some()
        })
        .expect("retained unreachable write witness");
    assert!(witness.affected_roots.is_empty());
    let proof = witness.unreachable_branch.as_ref().unwrap();
    assert_eq!(proof.arguments.object_arguments, [0, 1].into());
    assert_eq!(
        proof.guard_location.as_ref().unwrap().source_path,
        "static/namespace.js"
    );
    assert_eq!(proof.imported_local, "wrap");
    assert_eq!(
        proof.wrapper_call_location.as_ref().unwrap().source_path,
        "static/namespace.js"
    );
    assert_eq!(
        proof.callback_location.as_ref().unwrap().source_path,
        "static/namespace.js"
    );
    match &proof.arguments.wrapper {
        analysis::CallbackFactoryEvidence::InferredImmutableWrapper {
            defining_module,
            export,
            source_location,
            ..
        } => {
            assert_eq!(defining_module, "static/wrapper");
            assert_eq!(export, "wrap");
            assert_eq!(
                source_location.as_ref().unwrap().source_path,
                "static/wrapper.js"
            );
        }
        other => panic!("source wrapper evidence required: {other:?}"),
    }
}

#[test]
fn unsupported_or_escaping_callback_entries_keep_the_native_guard() {
    for namespace in [
        "import { wrap } from './wrapper.js'; function initialize(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.Map = {}); } export const load = wrap(initialize); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function initialize(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.Map = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { output = undefined; typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.Map = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { [output] = [undefined]; typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.Map = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { eval('output = undefined'); typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.Map = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { globalThis.escape = () => arguments.callee; typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.Map = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'function' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.Map = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? (globalThis.writer = function() { globalThis.shared.Map = {}; }) : (globalThis.shared.Map = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.store = { get value() { globalThis.shared.Map = {}; } }); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.store = class { constructor() { globalThis.shared.Map = {}; } }); }); load();",
        "import { wrap } from 'example:wrapper'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.Map = {}); });",
    ] {
        let chunks = [
            ("static/peer", PROVIDER),
            ("static/wrapper", WRAPPER),
            ("static/namespace", namespace),
        ];
        if namespace.contains("from 'example:wrapper'") {
            assert_refused_with_matching_node_error(
                &chunks,
                "evaluation_schedule_unsupported",
                "ERR_UNSUPPORTED_ESM_URL_SCHEME",
            );
        } else {
            assert_split_refused_with_map_guard(&chunks, "evaluation_schedule_unknown_reordered");
        }
    }
}

#[test]
fn callback_return_or_unknown_argument_wrapper_does_not_supply_entry_coverage() {
    for wrapper in [
        "export const wrap = (callback, cache) => () => callback;",
        "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: {} }).value, globalThis.input), cache.value);",
        "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: callback }).value, cache), cache.value);",
        "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: {} }).value, cache), callback);",
        "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: {} }).value, cache), globalThis.consume(callback), cache.value);",
        "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: {} }).value, cache), cache.value = callback, cache.value);",
    ] {
        let chunks = [
            ("static/peer", PROVIDER),
            ("static/wrapper", wrapper),
            ("static/namespace", NAMESPACE),
        ];
        if wrapper.contains("globalThis.consume(callback)") {
            assert_refused_with_matching_node_error(
                &chunks,
                "evaluation_schedule_unknown_reordered",
                "globalThis.consume is not a function",
            );
        } else {
            assert_split_refused_with_map_guard(&chunks, "evaluation_schedule_unknown_reordered");
        }
    }
}

#[test]
fn callback_mutated_loader_return_still_invokes_its_accessor() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { wrap } from './wrapper.js';\nconst load = wrap(function(output, container) { Object.defineProperty(container, 'value', { get() { console.log('getter'); return 'ready'; } }); });\nconsole.log(load());\n",
        vec![],
    ).with_extra_chunks(&[("static/wrapper", WRAPPER)]));
    assert_preserved_entry_output(&fixture, "getter\nready\n");
}
