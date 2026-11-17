//! Neutral retained-wrapper gate replay on historical engines.

use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged as stage};
use debundle_e2e_support::*;

fn assert_independent_factory_wrapper_cut(always_fresh: bool, mutate_second: bool) {
    let write = if mutate_second {
        "second.label = 'second';\n"
    } else {
        ""
    };
    let source = format!(
        "import {{ factory, api }} from './peer.js';\n\
         const inner = factory(() => later);\n\
         const second = factory(() => 0);\n\
         {write}\
         const wrapped = api.make(inner, () => other);\n\
         const later = 42;\nconst other = 7;\n\
         console.log(wrapped.invoke(), wrapped.compare());\n"
    );
    let identity_branch = if always_fresh {
        ""
    } else {
        "if (typeof callback !== 'function') return callback;\n"
    };
    let provider = format!(
        "const wrappers = {{ wrap(value) {{ return (...args) => value(...args); }} }};\n\
         export function factory(callback) {{\n\
           {identity_branch}\
           return wrappers.wrap(callback);\n\
         }}\n\
         export const api = {{ make(callback, compare) {{ return {{ invoke: callback, compare }}; }} }};\n"
    );
    let writes = if mutate_second {
        vec!["second.label = 'second';"]
    } else {
        vec![]
    };
    let (staged, _fixture) = stage(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &binding_members(["inner", "second", "wrapped"]),
                    &writes,
                ),
                logical_module(
                    "values/later",
                    &[Member::new("later"), Member::new("other")],
                ),
            ],
        )
        .with_extra_chunks(&[("static/peer", &provider)])
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
    for invocation in [&direct, &saved] {
        assert!(
            invocation.result.status.success(),
            "independent wrapper mutation rejected a valid cut (always_fresh={always_fresh}, mutate_second={mutate_second}):\n{}",
            invocation.result.stderr,
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "42 7\n",
            "",
        );
    }
    assert_same_invocation_trees(&direct, &saved);
}

#[test]
fn independent_factory_wrapper_mutation_keeps_a_valid_cut() {
    assert_independent_factory_wrapper_cut(false, true);
}

#[test]
fn independent_factory_wrapper_without_mutation_keeps_a_valid_cut() {
    assert_independent_factory_wrapper_cut(false, false);
}

#[test]
fn independent_factory_wrapper_always_fresh_mutation_keeps_a_valid_cut() {
    assert_independent_factory_wrapper_cut(true, true);
}
