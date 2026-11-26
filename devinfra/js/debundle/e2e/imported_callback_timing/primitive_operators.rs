//! Primitive-operator coercion around namespace factories keeps callback re-entry visible.

use super::{modules, peer_api_callback_factories};
use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn unrelated_primitive_operator_results_do_not_disable_namespace_factories() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function makeKey(name, value) {\n\
                 Symbol.for('prefix' + name);\n\
                 return 1 / 0 + (value + 1) + (value > 0) + typeof value + void value + !value + value++;\n\
             }\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            modules(),
        ).with_import_order(&["callbacks/wrapped", "values/later", "residual"])
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn primitive_operator_operands_keep_callback_reentry_visible() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function makeKey(read) { return Symbol.for('prefix' + read()); }\n\
             const key = makeKey(() => later);\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(key.description, wrapped.invoke());\n",
            vec![
                logical_module("callbacks/wrapped", &binding_members(["key", "wrapped"])),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_import_order(&["callbacks/wrapped", "values/later", "residual"])
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &["eager_use"],
    );
}

#[test]
fn primitive_operator_coercion_keeps_key_with_later_value() {
    let source = "function makeKey(value) { return 'prefix' + value; }\n\
                 let later = 0;\n\
                 const key = makeKey({ [Symbol.toPrimitive]() { return later; } });\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/key", "values/later", "residual"]),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values",
                &binding_members(["key", "later"]),
                &["later = 42;"],
            )],
        )
        .with_import_order(&["values", "residual"]),
    );
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
}

#[test]
fn ordinary_to_primitive_fallback_keeps_key_with_later_value() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "function makeKey(value) { return 'prefix' + value; }\n\
             let later = 0;\n\
             const key = makeKey({\n\
                 valueOf() { return {}; },\n\
                 toString() { return later; },\n\
             });\n\
             later = 42;\n\
             console.log(key, later);\n",
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/key", "values/later", "residual"]),
        &["values/key", "values/later"],
        &["eager_use"],
    );
}

#[test]
fn compound_assignment_member_target_keeps_key_with_later_value() {
    let source = "let later = 0;\n\
                 const box = { value: { [Symbol.toPrimitive]() { return later; } } };\n\
                 const key = box.value += 'x';\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &binding_members(["box", "key"])),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &binding_members(["box", "key", "later"]),
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "0x 42\n");
}

#[test]
fn symbol_to_primitive_getter_return_keeps_key_with_later_value() {
    let source = "function makeKey(value) { return 'prefix' + value; }\n\
                 let later = 0;\n\
                 const key = makeKey({\n\
                     get [Symbol.toPrimitive]() { return () => later; },\n\
                 });\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/key", "values/later", "residual"]),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "values",
                &binding_members(["key", "later"]),
                &["later = 42;"],
            )],
        )
        .with_import_order(&["values", "residual"]),
    );
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
}

#[test]
fn regex_literal_with_custom_to_primitive_keeps_key_with_later_value() {
    let source = "let later = 0;\n\
                 const object = /x/;\n\
                 object[Symbol.toPrimitive] = () => later;\n\
                 const key = 'prefix' + object;\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values/key",
                    &binding_members(["object", "key"]),
                    &["object[Symbol.toPrimitive] = () => later;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &binding_members(["object", "key", "later"]),
            &["object[Symbol.toPrimitive] = () => later;", "later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
    let graph = fixture.dependency_graph();
    let key = source_unit_for_binding(&graph, "key");
    let later = source_unit_for_binding(&graph, "later");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == key
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
}

#[test]
fn strict_equality_and_known_primitive_operands_skip_coercion_callbacks() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "const object = { [Symbol.toPrimitive]() { return later; } };\n\
         const same = object === null;\n\
         const key = 'prefix' + (1 + 2);\n\
         const later = 42;\n\
         console.log(same, key, later);\n",
            vec![
                logical_module("values/key", &binding_members(["same", "key"])),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_import_order(&["values/key", "values/later", "residual"]),
    );
    assert_preserved_entry_output(&fixture, "false prefix3 42\n");
    let graph = fixture.dependency_graph();
    let comparison = source_unit_for_binding(&graph, "same");
    let later = source_unit_for_binding(&graph, "later");
    assert!(!graph.edges.iter().any(|edge| {
        edge.source == comparison
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
}

#[test]
fn logical_operator_results_still_preserve_namespace_alternatives() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => api || later);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[peer_api_callback_factories(&["make"]).build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}
