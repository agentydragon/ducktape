//! Context-read return pooling must preserve distinct runtime receivers.

use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged as stage};
use debundle_e2e_support::*;

fn assert_context_read_cut(write: bool, true_alias: bool) {
    let selected_context = if true_alias {
        "callbackContext"
    } else {
        "ordinaryContext"
    };
    let handler = if write {
        if true_alias {
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
    let (staged, _fixture) = stage(
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
    assert_node_output(&staged.root().join("snapshot/static/app.js"), "42 7\n", "");
    let direct = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    eprintln!(
        "context-read case write={write} true_alias={true_alias}: original=42 7 direct={:?} saved={:?}",
        direct.result.status.code(),
        saved.result.status.code(),
    );
    for invocation in [&direct, &saved] {
        if true_alias {
            assert!(
                !invocation.result.status.success()
                    && invocation.result.stderr.contains("atomic-group conflict"),
                "a true callback receiver alias must stay conservative:\n{}",
                invocation.result.stderr,
            );
        } else {
            assert!(
                invocation.result.status.success(),
                "distinct literal contexts were pooled (write={write}):\n{}",
                invocation.result.stderr,
            );
            assert_node_output(
                &invocation.output_root().join("app/static/app/entry.js"),
                "42 7\n",
                "",
            );
        }
    }
    if !true_alias {
        assert_same_invocation_trees(&direct, &saved);
    }
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
fn actual_context_callback_alias_write_remains_conservative() {
    assert_context_read_cut(true, true);
}
