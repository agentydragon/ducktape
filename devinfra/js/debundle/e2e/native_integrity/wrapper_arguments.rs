use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

// Each guarded write names `Map`, so through the unknown `globalThis.shared`
// receiver it reaches the `%Map%` global slot `factory()` constructs from.

const WRAPPER: &str = "export const wrap = (callback, cache) => () => (cache || callback((cache = { value: {} }).value, cache), cache.value);\n";
const NAMESPACE: &str = "import { wrap } from './wrapper.js';\nexport const load = wrap(function(output, container) { (function(root, fill) { typeof output === 'object' && typeof container < 'u' ? fill(output) : (root = globalThis, root.shared = root.shared || {}, root.shared.Map = {}, fill(root.shared.Map)); })(output, function(value) { value.ready = true; }); });\nload();\n";

fn wrapper_chunks<'a>(wrapper: &'a str, namespace: &'a str) -> [(&'a str, &'a str); 3] {
    [
        source_record_namespace::PEER,
        ("static/wrapper", wrapper),
        ("static/namespace", namespace),
    ]
}

#[test]
fn source_wrapper_arguments_discharge_only_the_unreachable_native_write() {
    let chunks = wrapper_chunks(WRAPPER, NAMESPACE);
    let fixture = run_fixture_with_graphs(source_record_namespace::opts(&chunks));
    assert_preserved_entry_output(&fixture, "0 Later\n");
    let program_graph = fixture.program_graph();
    let witness = program_graph
        .native_integrity_witnesses
        .iter()
        .find(|witness| {
            witness
                .receiver_roots
                .contains(&analysis_vocabulary::inputs::NativeIntrinsic::UnknownGlobalValue)
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
        analysis_vocabulary::inputs::CallbackFactoryEvidence::InferredImmutableWrapper {
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
        source_record_namespace::expect_split_conflict(&wrapper_chunks(WRAPPER, namespace));
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
        source_record_namespace::expect_split_conflict(&wrapper_chunks(wrapper, NAMESPACE));
    }
}

#[test]
fn callback_mutated_loader_return_still_invokes_its_accessor() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { wrap } from './wrapper.js';\nconst load = wrap(function(output, container) { Object.defineProperty(container, 'value', { get() { console.log('getter'); return 'ready'; } }); });\nconsole.log(load());\n",
        vec![],
    ).with_import_order(&["residual"]).with_extra_chunks(&[("static/wrapper", WRAPPER)]));
    assert_preserved_entry_output(&fixture, "getter\nready\n");
}
