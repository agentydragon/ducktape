//! Declared fresh factories refine direct result identity, preserving captures.

use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged as stage};
use debundle_e2e_support::*;

#[derive(Clone, Copy, Debug)]
enum ProviderShape {
    Independent,
    DependencyBackedge,
    ReexportBackedge,
}

fn assert_factory_cut(
    always_fresh: bool,
    mutate_second: bool,
    shape: ProviderShape,
    accepts: bool,
    nested: bool,
) {
    let callback = |source: &str| {
        if nested {
            format!("api.forward({source})")
        } else {
            source.to_owned()
        }
    };
    let first = callback("() => later");
    let second = callback("() => 0");
    let write = if mutate_second {
        "second.label = 'second';\n"
    } else {
        ""
    };
    let local_factory = if matches!(shape, ProviderShape::ReexportBackedge) {
        "export function localFactory(callback) { if (typeof callback !== 'function') return callback; return (...args) => callback(...args); }\n"
    } else {
        ""
    };
    let source = format!(
        "import {{ factory, api }} from './peer.js';\n\
         {local_factory}\
         const inner = factory({first});\n\
         const second = factory({second});\n\
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
    let mut provider = format!(
        "const wrappers = {{ wrap(value) {{ return (...args) => value(...args); }} }};\n\
         export function factory(callback) {{\n\
           {identity_branch}\
           return wrappers.wrap(callback);\n\
         }}\n\
         export const api = {{\n\
           forward(callback) {{ {identity_branch} return wrappers.wrap(callback); }} ,\n\
           make(callback, compare) {{ return {{ invoke: callback, compare }}; }}\n\
         }};\n"
    );
    match shape {
        ProviderShape::Independent => {},
        ProviderShape::DependencyBackedge => provider.insert_str(0, "import './app.js';\n"),
        ProviderShape::ReexportBackedge => provider = "import { localFactory } from './app.js';\nexport { localFactory as factory };\nexport const api = { make(callback, compare) { return { invoke: callback, compare }; } };\n".into(),
    }
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
            .with_callback_factory_members("api", &["make", "forward"])
            .with_namespace_independent_members("api", &["make", "forward"])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(&staged.root().join("snapshot/static/app.js"), "42 7\n", "");
    let direct = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    for invocation in [&direct, &saved] {
        eprintln!(
            "factory cut shape={shape:?} always_fresh={always_fresh} mutate_second={mutate_second}: original=42 7 status={:?}",
            invocation.result.status.code()
        );
        if !accepts {
            assert!(
                !invocation.result.status.success(),
                "a provider backedge must decline direct-return refinement"
            );
            continue;
        }
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
    if accepts {
        assert_same_invocation_trees(&direct, &saved);
    }
}

#[test]
fn independent_factory_wrapper_mutation_keeps_a_valid_cut() {
    assert_factory_cut(false, true, ProviderShape::Independent, true, false);
}

#[test]
fn independent_factory_wrapper_without_mutation_keeps_a_valid_cut() {
    assert_factory_cut(false, false, ProviderShape::Independent, true, false);
}

#[test]
fn independent_factory_wrapper_always_fresh_mutation_keeps_a_valid_cut() {
    assert_factory_cut(true, true, ProviderShape::Independent, true, false);
}

#[test]
fn nested_factory_wrapper_mutation_keeps_a_valid_cut() {
    assert_factory_cut(false, true, ProviderShape::Independent, true, true);
}

#[test]
fn nested_factory_wrapper_without_mutation_keeps_a_valid_cut() {
    assert_factory_cut(false, false, ProviderShape::Independent, true, true);
}

#[test]
fn nested_factory_wrapper_always_fresh_mutation_keeps_a_valid_cut() {
    assert_factory_cut(true, true, ProviderShape::Independent, true, true);
}

#[test]
fn provider_static_dependency_backedge_declines_fresh_result_refinement() {
    assert_factory_cut(false, true, ProviderShape::DependencyBackedge, false, false);
}

#[test]
fn provider_reexport_backedge_declines_fresh_result_refinement() {
    assert_factory_cut(false, true, ProviderShape::ReexportBackedge, false, false);
}

fn assert_unsafe_cut(source: &str, provider: &str, members: &[&'static str], setup: &[&str]) {
    let (staged, _fixture) = stage(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/run",
                    &members.iter().copied().map(Member::new).collect::<Vec<_>>(),
                    setup,
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .with_retained_callback_factory("factory", 1, &[0])
            .build()])
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(&staged.root().join("snapshot/static/app.js"), "0 42\n", "");
    let direct = staged.run(&["--emit-on-rejection"]);
    let saved = staged.run(&["--emit-on-rejection", "--analysis", path(&staged.artifact)]);
    for invocation in [&direct, &saved] {
        eprintln!(
            "unsafe factory cut: original=0 42 status={:?}",
            invocation.result.status.code()
        );
        assert_eq!(
            invocation.result.status.code(),
            Some(3),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "42 42\n",
            "",
        );
    }
    assert_same_invocation_trees(&direct, &saved);
}

#[test]
fn original_callback_member_alias_mutation_keeps_an_unsafe_cut_rejected() {
    assert_unsafe_cut(
        "import { factory } from './peer.js';\n\
         let later = 0;\n\
         function callback() { return 1; }\n\
         const alias = callback;\n\
         function install() { alias.extra = () => later; }\n\
         install();\n\
         const wrapped = factory(callback);\n\
         const observed = wrapped.invoke.extra();\n\
         later = 42;\n\
         console.log(observed, later);\n",
        "export function factory(callback) { return { invoke: callback }; }\n",
        &["callback", "alias", "install", "wrapped", "observed"],
        &["install();"],
    );
}

#[test]
fn inline_factory_result_member_alias_mutation_keeps_an_unsafe_cut_rejected() {
    assert_unsafe_cut(
        "import { factory } from './peer.js';\n\
         let later = 0;\n\
         const wrapped = factory(() => 1);\n\
         const alias = wrapped.invoke;\n\
         function install() { alias.extra = () => later; }\n\
         install();\n\
         const observed = wrapped.invoke.extra();\n\
         later = 42;\n\
         console.log(observed, later);\n",
        "export function factory(callback) { return { invoke: callback }; }\n",
        &["wrapped", "alias", "install", "observed"],
        &["install();"],
    );
}

#[test]
fn result_rebinding_to_original_callback_keeps_an_unsafe_cut_rejected() {
    assert_unsafe_cut(
        "import { factory } from './peer.js';\n\
         let later = 0;\n\
         let callback = factory(() => 1);\n\
         callback = () => later;\n\
         const observed = callback();\n\
         later = 42;\n\
         console.log(observed, later);\n",
        "export function factory(callback) { if (typeof callback !== 'function') return callback; return () => callback(); }\n",
        &["callback", "observed"],
        &["callback = () => later;"],
    );
}
