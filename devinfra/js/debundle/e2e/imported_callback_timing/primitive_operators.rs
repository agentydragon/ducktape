//! Primitive-operator coercion around namespace factories keeps callback re-entry visible.

use super::{API_MAKE_PEER, SplitCase, peer_api_callback_factories};
use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn unrelated_primitive_operator_results_do_not_disable_namespace_factories() {
    SplitCase {
        name: "unrelated primitive operator results",
        source: "import { api } from './peer.js';\n\
                 function makeKey(name, value) {\n\
                     Symbol.for('prefix' + name);\n\
                     return 1 / 0 + (value + 1) + (value > 0) + typeof value + void value + !value + value++;\n\
                 }\n\
                 const wrapped = api.make(() => later);\n\
                 const later = 42;\n\
                 console.log(wrapped.invoke());\n",
        wrapped: &["wrapped"],
        import_order: Some(&["callbacks/wrapped", "values/later", "residual"]),
        peers: &[("static/peer", API_MAKE_PEER)],
        contracts: &[peer_api_callback_factories(&["make"]).build()],
        ..Default::default()
    }
    .assert_split_runs("42\n");
}

#[test]
fn operator_operands_keep_namespace_factory_callbacks_atomic() {
    let peers = [("static/peer", API_MAKE_PEER)];
    let contracts = [peer_api_callback_factories(&["make"]).build()];
    for (case, causes) in [
        (
            SplitCase {
                name: "primitive operator operand re-enters a callback",
                source: "import { api } from './peer.js';\n\
                         function makeKey(read) { return Symbol.for('prefix' + read()); }\n\
                         const key = makeKey(() => later);\n\
                         const wrapped = api.make(() => later);\n\
                         const later = 42;\n\
                         console.log(key.description, wrapped.invoke());\n",
                wrapped: &["key", "wrapped"],
                import_order: Some(&["callbacks/wrapped", "values/later", "residual"]),
                peers: &peers,
                contracts: &contracts,
                ..Default::default()
            },
            &["eager_use"][..],
        ),
        (
            SplitCase {
                name: "logical operator result preserves the namespace alternative",
                source: "import { api } from './peer.js';\n\
                         const wrapped = api.make(() => api || later);\n\
                         const later = 42;\n\
                         console.log(wrapped.invoke());\n",
                wrapped: &["wrapped"],
                peers: &peers,
                contracts: &contracts,
                ..Default::default()
            },
            &["eager_use", "sequenced"][..],
        ),
    ] {
        case.assert_split_rejected(causes);
    }
}

#[test]
fn to_primitive_coercion_keeps_key_with_later_value() {
    const ORDER: Option<&[&str]> = Some(&["values/key", "values/later", "residual"]);
    for (name, source, key_members, import_order, colocated) in [
        (
            "Symbol.toPrimitive method",
            "function makeKey(value) { return 'prefix' + value; }\n\
             let later = 0;\n\
             const key = makeKey({ [Symbol.toPrimitive]() { return later; } });\n\
             later = 42;\n\
             console.log(key, later);\n",
            &["key"][..],
            ORDER,
            Some("prefix0 42\n"),
        ),
        (
            "ordinary toPrimitive fallback",
            "function makeKey(value) { return 'prefix' + value; }\n\
             let later = 0;\n\
             const key = makeKey({\n\
                 valueOf() { return {}; },\n\
                 toString() { return later; },\n\
             });\n\
             later = 42;\n\
             console.log(key, later);\n",
            &["key"][..],
            ORDER,
            None,
        ),
        (
            "compound assignment member target",
            "let later = 0;\n\
             const box = { value: { [Symbol.toPrimitive]() { return later; } } };\n\
             const key = box.value += 'x';\n\
             later = 42;\n\
             console.log(key, later);\n",
            &["box", "key"][..],
            None,
            Some("0x 42\n"),
        ),
        (
            "Symbol.toPrimitive getter return",
            "function makeKey(value) { return 'prefix' + value; }\n\
             let later = 0;\n\
             const key = makeKey({\n\
                 get [Symbol.toPrimitive]() { return () => later; },\n\
             });\n\
             later = 42;\n\
             console.log(key, later);\n",
            &["key"][..],
            ORDER,
            Some("prefix0 42\n"),
        ),
    ] {
        eprintln!("case: {name}");
        let members: Vec<Member> = key_members.iter().map(|name| Member::new(name)).collect();
        let split = FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &members),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        );
        expect_atomic_conflict_rejection(
            match import_order {
                Some(order) => split.with_import_order(order),
                None => split,
            },
            &["values/key", "values/later"],
            &["eager_use"],
        );
        if let Some(output) = colocated {
            let mut members = members;
            members.push(Member::new("later"));
            let values = FixtureOpts::new(
                source,
                vec![logical_module_with_anon(
                    "values",
                    &members,
                    &["later = 42;"],
                )],
            );
            let values = match import_order {
                Some(_) => values.with_import_order(&["values", "residual"]),
                None => values,
            };
            assert_preserved_entry_output(&run_fixture(values), output);
        }
    }
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
