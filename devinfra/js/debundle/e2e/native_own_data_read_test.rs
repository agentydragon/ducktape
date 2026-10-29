//! A native own-data function reference has no heap observation of caller arrays.

use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

const SOURCE: &str = "const sentinel = [1];\nconst before = external();\nvar alias = Object.defineProperty;\nconst after = alias;\n";
const SOURCE_INFERRED: &str = "const sentinel = [1];\nconst before = Math.random();\nvar alias = Object.defineProperty;\nconst after = alias;\n";

fn input(source: &str, declaration: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [alias]\n",
            declaration.trim()
        ))
        .unwrap(),
        operation: NativeOperationKind::ObjectOwnDataFunctionRead,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("Only this original Object own-data read is audited.".to_owned()),
    }
}

fn split(source: &'static str, certified: bool) -> FixtureOpts<'static> {
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module("alias", &[Member::new("sentinel"), Member::new("alias")]),
            logical_module("consumer", &[Member::new("before"), Member::new("after")]),
        ],
    );
    if certified {
        opts.with_native_operation_inputs(
            "static/app",
            vec![input(source, "var alias = Object.defineProperty;")],
        )
    } else {
        opts
    }
}

fn node<'a>(graph: &'a Value, name: &str) -> &'a Value {
    graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| bindings.iter().any(|binding| binding["binding"] == name))
        })
        .unwrap()
}

#[test]
fn selected_original_object_read_removes_the_heap_frontier_cycle() {
    let fixture = run_fixture(split(SOURCE, true));
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let alias = node(&graph, "alias");
    assert_eq!(
        alias["synchronous_execution"]["native_operation_input_uses"][0]["evidence"]["operation"],
        "object_own_data_function_read"
    );
    assert_eq!(
        alias["synchronous_execution"]["inference_uses"][0]["conclusion"],
        "native_own_data_read"
    );
}

#[test]
fn intact_object_read_has_an_independent_source_certificate() {
    let fixture = run_fixture(split(SOURCE_INFERRED, false));
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let alias = node(&graph, "alias");
    assert_eq!(
        alias["synchronous_execution"]["inference_uses"][0]["conclusion"],
        "native_own_data_read"
    );
    assert!(alias["synchronous_execution"]["native_operation_input_uses"].is_null());
}

#[test]
fn exact_input_rejects_unsupported_and_stale_sites() {
    for (source, expected) in [
        (
            "const Object = { get defineProperty() { throw Error('getter'); } };\nvar alias = Object.defineProperty;\n",
            "unshadowed static Object own-data function read",
        ),
        (
            "const Object = [];\nvar alias = Object.defineProperty;\n",
            "unshadowed static Object own-data function read",
        ),
        ("var alias = Object?.defineProperty;\n", "source_match"),
        ("var alias = Object['defineProperty'];\n", "source_match"),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new(
                source,
                vec![logical_module("alias", &[Member::new("alias")])],
            )
            .with_native_operation_inputs(
                "static/app",
                vec![input(source, "var alias = Object.defineProperty;")],
            ),
            &[expected],
        );
    }
    let mut stale = input(SOURCE, "var alias = Object.defineProperty;");
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        split(SOURCE, false).with_native_operation_inputs("static/app", vec![stale]),
        &["audited_source_sha256"],
    );
    expect_rejection_containing_all(
        split(SOURCE, false).with_native_operation_inputs(
            "static/app",
            vec![input(SOURCE, "const after = alias;")],
        ),
        &["source_match"],
    );
}

#[test]
fn sibling_initializer_effect_survives_a_certified_read() {
    let source = "var alias = Object.defineProperty, other = external();\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![input(
                    source,
                    "var alias = Object.defineProperty, other = external();",
                )],
            ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_ne!(node(&graph, "other")["purity"]["kind"], "pure");
    assert_eq!(
        node(&graph, "alias")["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn uncertified_second_native_read_in_one_declaration_stays_observable() {
    let source = "Object.entries = function replacement() {};\nvar alias = Object.defineProperty, other = Object.entries;\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![input(
                    source,
                    "var alias = Object.defineProperty, other = Object.entries;",
                )],
            ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_ne!(node(&graph, "other")["purity"]["kind"], "pure");
    let alias_uses = node(&graph, "alias")["synchronous_execution"]["native_operation_input_uses"]
        .as_array()
        .unwrap();
    assert_eq!(alias_uses.len(), 1);
}

#[test]
fn known_exact_slot_writes_contradict_only_the_relevant_premise() {
    for write in [
        "Object.defineProperty = function replacement() {};",
        "Object.defineProperty(Object, 'defineProperty', { get() { throw Error('getter'); } });",
        "const ctor = Object; ctor.defineProperty = function replacement() {};",
        "globalThis.Object = function replacement() {};",
    ] {
        let source = format!("{write}\nvar alias = Object.defineProperty;\n");
        expect_rejection_containing_all(
            FixtureOpts::new(
                &source,
                vec![logical_module("alias", &[Member::new("alias")])],
            )
            .with_native_operation_inputs(
                "static/app",
                vec![input(&source, "var alias = Object.defineProperty;")],
            ),
            &["contradicted by a known Object binding or Object.defineProperty write"],
        );
    }
    let source =
        "Object.entries = function replacement() {};\nvar alias = Object.defineProperty;\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("alias", &[Member::new("alias")])],
        )
        .with_native_operation_inputs(
            "static/app",
            vec![input(source, "var alias = Object.defineProperty;")],
        ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_eq!(
        node(&graph, "alias")["synchronous_execution"]["native_operation_input_uses"][0]["evidence"]
            ["operation"],
        "object_own_data_function_read"
    );
}

#[test]
fn known_exact_slot_write_in_an_imported_chunk_contradicts_the_input() {
    let source = "import './mutation.js';\nvar alias = Object.defineProperty;\nexport { alias };\n";
    let mutation = "Object.defineProperty = function replacement() {};\nexport {};\n";
    expect_rejection_containing_all(
        FixtureOpts::new(
            source,
            vec![logical_module("alias", &[Member::new("alias")])],
        )
        .with_extra_chunks(&[("static/mutation", mutation)])
        .with_native_operation_inputs(
            "static/app",
            vec![input(source, "var alias = Object.defineProperty;")],
        ),
        &[
            "contradicted by a known Object binding or Object.defineProperty write",
            "static/mutation",
        ],
    );
}

#[test]
fn invoking_the_alias_keeps_call_heap_effects() {
    let source = "const sentinel = [1];\nvar alias = Object.defineProperty;\nconst after = alias(sentinel, 'x', { value: 2 });\nconsole.log(sentinel.x);\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![input(source, "var alias = Object.defineProperty;")],
            ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_preserved_entry_output(&fixture, "2\n");
    assert_ne!(node(&graph, "after")["purity"]["kind"], "pure");
    assert!(
        node(&graph, "after")["synchronous_execution"]["native_operation_input_uses"].is_null()
    );
}
