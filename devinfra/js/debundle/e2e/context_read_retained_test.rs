//! Context-read return pooling must preserve distinct runtime receivers.

use debundle_e2e_support::saved_analysis::staged;
use debundle_e2e_support::*;

fn assert_context_read_cut(write: bool, wrapper_alias: bool) {
    let selected_context = if wrapper_alias {
        "callbackContext"
    } else {
        "ordinaryContext"
    };
    let handler = if write {
        if wrapper_alias {
            "function handler() { ordinaryRead.label = 'changed'; }\n"
        } else {
            "function handler() { ordinaryRead.nested.label = 'changed'; }\n"
        }
    } else {
        ""
    };
    let source = format!(
        "import {{ factory, api, read }} from './peer.js';\n\
         const inner = factory(() => later);\n\
         const wrapped = api.make(inner, () => other);\n\
         const later = 42;\nconst other = 7;\n\
         const ordinaryContext = {{ value: {{ nested: {{}} }} }};\n\
         const callbackContext = {{ value: inner }};\n\
         const ordinaryRead = read({selected_context});\n\
         const callbackRead = read(callbackContext);\n\
         {handler}\
         console.log(wrapped.invoke(), wrapped.compare());\n"
    );
    let provider = "const wrappers = { wrap(value) { return (...args) => value(...args); } };\n\
                    export function factory(callback) {\n\
                      if (typeof callback !== 'function') return callback;\n\
                      return wrappers.wrap(callback);\n\
                    }\n\
                    export function read(context) { return context.value; }\n\
                    export const api = { make(callback, compare) { return { invoke: callback, compare }; } };\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("callbacks/wrapped", &binding_members(["inner", "wrapped"])),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                ),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .with_retained_callback_factory_member("api", "make", 2, &[0, 1])
            .with_callback_factory_members("api", &["make"])
            .with_namespace_independent_members("api", &["make"])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    staged.assert_fresh_saved_parity(&[], 0, "42 7\n", "42 7\n");
}

#[test]
fn distinct_context_record_write_keeps_a_valid_retained_cut() {
    assert_context_read_cut(true, false);
}

#[test]
fn distinct_context_reads_without_write_keep_a_valid_retained_cut() {
    assert_context_read_cut(false, false);
}

#[test]
fn context_alias_to_fresh_wrapper_keeps_a_valid_retained_cut() {
    assert_context_read_cut(true, true);
}

#[test]
fn context_read_alias_to_original_callback_keeps_an_unsafe_cut_rejected() {
    let source = "import { factory, read } from './peer.js';\n\
                  let later = 0;\n\
                  function callback() { return 1; }\n\
                  const context = { value: callback };\n\
                  const alias = read(context);\n\
                  function install() { alias.extra = () => later; }\n\
                  install();\n\
                  const wrapped = factory(callback);\n\
                  const observed = wrapped.invoke.extra();\n\
                  later = 42;\n\
                  console.log(observed, later);\n";
    let provider = "export function factory(callback) { return { invoke: callback }; }\n\
                    export function read(context) { return context.value; }\n";
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_retained_callback_factory("factory", 1, &[0])
        .build()];
    let callbacks = binding_members([
        "callback", "context", "alias", "install", "wrapped", "observed",
    ]);
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon("callbacks/wrapped", &callbacks, &["install();"]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&contracts)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    staged.assert_fresh_saved_parity(&["--emit-on-rejection"], 3, "0 42\n", "42 42\n");
}

#[test]
fn sequence_prefix_keeps_call_effects_with_a_static_property_result() {
    let source = "import { api, read, readCount } from './peer.js';\n\
                  const later = 42;\nconst other = 7;\n\
                  const callback = () => later;\n\
                  const context = { value: callback };\n\
                  const alias = read(context);\n\
                  const wrapped = api.make(alias, () => other);\n\
                  console.log(wrapped.invoke(), wrapped.compare(), readCount());\n";
    let provider = "let calls = 0;\n\
                    function recordCall() { calls += 1; }\n\
                    export function read(context) { return (recordCall(), context.value); }\n\
                    export function readCount() { return calls; }\n\
                    export const api = { make(callback, compare) { return { invoke: callback, compare }; } };\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &binding_members(["callback", "context", "alias", "wrapped"]),
                ),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                ),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_retained_callback_factory_member("api", "make", 2, &[0, 1])
            .with_callback_factory_members("api", &["make"])
            .with_namespace_independent_members("api", &["make"])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    staged.assert_fresh_saved_parity(&[], 0, "42 7 1\n", "42 7 1\n");
}
