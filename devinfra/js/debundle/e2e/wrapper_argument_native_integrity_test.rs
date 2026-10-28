use debundle_e2e_support::*;

const WRAPPER: &str = "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: {} }).value, cache), cache.value);\n";
const PROVIDER: &str = "import { readLater } from './app.js';\nexport function factory() { const state = new Map(); return { get size() { return state.size; }, label() { return readLater().name; } }; }\n";
const NAMESPACE: &str = "import { wrap } from './wrapper.js';\nexport const load = wrap(function(output, container) { (function(root, fill) { typeof output === 'object' && typeof container < 'u' ? fill(output) : (root = globalThis, root.shared = root.shared || {}, root.shared.walk = {}, fill(root.shared.walk)); })(output, function(value) { value.ready = true; }); });\nload();\n";

fn opts<'a>(chunks: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        "import { factory } from './peer.js';\nimport './namespace.js';\nexport function readLater() { return Later; }\nconst queue = factory();\nclass Later {}\nconsole.log(queue.size, readLater().name);\nexport { queue, Later };\n",
        vec![
            logical_module("queue", &[Member::new("queue"), Member::new("readLater")]),
            logical_module("later", &[Member::new("Later")]),
        ],
    ).with_extra_chunks(chunks)
}

#[test]
fn source_wrapper_arguments_discharge_only_the_unreachable_native_write() {
    let chunks = [
        ("static/peer", PROVIDER),
        ("static/wrapper", WRAPPER),
        ("static/namespace", NAMESPACE),
    ];
    let fixture = run_fixture(opts(&chunks));
    assert_preserved_entry_output(&fixture, "0 Later\n");
    let graph = fixture.dependency_graph();
    let witness = graph
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
        "import { wrap } from './wrapper.js'; function initialize(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.walk = {}); } export const load = wrap(initialize); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function initialize(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.walk = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { output = undefined; typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.walk = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { [output] = [undefined]; typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.walk = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { eval('output = undefined'); typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.walk = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { globalThis.escape = () => arguments.callee; typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.walk = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'function' && typeof container < 'u' ? 0 : (globalThis.shared = {}, globalThis.shared.walk = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? (globalThis.writer = function() { globalThis.shared.walk = {}; }) : (globalThis.shared.walk = {}); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.store = { get value() { globalThis.shared.walk = {}; } }); }); load();",
        "import { wrap } from './wrapper.js'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.store = class { constructor() { globalThis.shared.walk = {}; } }); }); load();",
        "import { wrap } from 'example:wrapper'; export const load = wrap(function(output, container) { typeof output === 'object' && typeof container < 'u' ? 0 : (globalThis.shared.walk = {}); });",
    ] {
        let chunks = [
            ("static/peer", PROVIDER),
            ("static/wrapper", WRAPPER),
            ("static/namespace", namespace),
        ];
        expect_atomic_conflict_rejection(opts(&chunks), &["queue", "later"], &[]);
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
        expect_atomic_conflict_rejection(opts(&chunks), &["queue", "later"], &[]);
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
