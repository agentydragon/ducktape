//! A native own-data function reference has no heap observation of caller arrays.

use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

const SOURCE: &str = "const sentinel = [1];\nconst before = external();\nvar alias = Object.defineProperty;\nconst after = alias;\n";
const SOURCE_INFERRED: &str = "const sentinel = [1];\nconst before = Math.random();\nvar alias = Object.defineProperty;\nconst after = alias;\n";

fn input(source: &str, declaration: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        selector: SourceUnitSelector::SourceMatch(
            serde_yaml::from_str(&format!(
                "match: {:?}\nbindings: [alias]\n",
                declaration.trim()
            ))
            .unwrap(),
        ),
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

fn decorator_trio(suffix: &str, class: &str, role: &str) -> String {
    format!(
        "var g{suffix} = Object.getOwnPropertyDescriptor;\nvar p{suffix} = Object.defineProperty;\n\
         var d{suffix} = (decorators, target, key) => {{\n  const desc = g{suffix}(target, key);\n\
           for (const decorator of decorators) decorator(target, key, desc);\n\
           p{suffix}(target, key, desc);\n}};\n\
         class {class} {{ m() {{ return '{role}'; }} }}\n\
         d{suffix}([], {class}.prototype, 'm', 1);\n"
    )
}

fn relational_opts<'a>(source: &'a str, input: NativeOperationInputContract) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "alpha",
                &[
                    Member::source_alpha("Alpha", "class Alpha { m() { return 'alpha role'; } }"),
                    Member::makes_decorate_call("decorate", "Alpha", None, None),
                    Member::intrinsic_alias("alphaDefine", "defineProperty", "decorate"),
                    Member::intrinsic_alias("alphaGetter", "getOwnPropertyDescriptor", "decorate"),
                ],
            ),
            logical_module(
                "beta",
                &[
                    Member::source_alpha("Beta", "class Beta { m() { return 'beta role'; } }"),
                    Member::makes_decorate_call("decorate", "Beta", None, None),
                    Member::intrinsic_alias("betaDefine", "defineProperty", "decorate"),
                ],
            ),
        ],
    )
    .with_native_operation_inputs("static/app", vec![input])
}

fn relational_input(source: &str, module: &str, name: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        selector: SourceUnitSelector::ResolvedMember(ResolvedMemberSelector {
            module: module.to_owned(),
            name: name.to_owned(),
        }),
        operation: NativeOperationKind::ObjectOwnDataFunctionRead,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("One resolved original read is audited.".to_owned()),
    }
}

#[test]
fn resolved_member_certifies_only_the_selected_relational_alias() {
    let source = format!(
        "{}{}",
        decorator_trio("a", "Alpha", "alpha role"),
        decorator_trio("b", "Beta", "beta role")
    );
    let fixture = run_fixture(relational_opts(
        &source,
        relational_input(&source, "alpha", "alphaDefine"),
    ));
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let selected = node(&graph, "pa");
    let other = node(&graph, "pb");
    let evidence = &selected["synchronous_execution"]["native_operation_input_uses"][0]["evidence"];
    assert_eq!(evidence["reference"]["defining_chunk"], "static/app");
    assert_eq!(evidence["reference"]["input_index"], 0);
    assert!(
        evidence["spec_location"]
            .as_str()
            .unwrap()
            .contains("native_operation_inputs[0]")
    );
    assert_eq!(
        evidence["audited_source_sha256"],
        relational_input(&source, "alpha", "alphaDefine").audited_source_sha256
    );
    assert_eq!(
        selected["synchronous_execution"]["native_operation_input_uses"][0]["evidence"]["selector"]
            ["resolved_member"]["module"],
        "alpha"
    );
    assert_eq!(
        selected["synchronous_execution"]["native_operation_input_uses"][0]["evidence"]["selector"]
            ["resolved_member"]["name"],
        "alphaDefine"
    );
    assert!(
        other["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .is_none_or(Vec::is_empty)
    );
}

#[test]
fn resolved_member_rejects_stale_hash_and_wrong_reference_or_shape() {
    let source = format!(
        "{}{}",
        decorator_trio("a", "Alpha", "alpha role"),
        decorator_trio("b", "Beta", "beta role")
    );
    let mut stale = relational_input(&source, "alpha", "alphaDefine");
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        relational_opts(&source, stale),
        &["stale audited_source_sha256"],
    );
    expect_rejection_containing_all(
        relational_opts(&source, relational_input(&source, "alpha", "missing")),
        &["resolved_member names no logical module member"],
    );
    expect_rejection_containing_all(
        relational_opts(&source, relational_input(&source, "alpha", "decorate")),
        &["unshadowed static Object own-data function read initializer"],
    );
}

#[test]
fn resolved_member_cannot_reference_another_provider_chunk() {
    const CONSUMER: &str = "import { alias } from './peer.js';\nconsole.log(alias);\n";
    const PROVIDER: &str = "var alias = Object.defineProperty;\nexport { alias };\n";
    expect_rejection_containing_all(
        FixtureOpts::new(CONSUMER, vec![])
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_extra_chunk_logical_modules(&[(
                "static/peer",
                vec![logical_module("peer", &[Member::new("alias")])],
            )])
            .with_native_operation_inputs(
                "static/app",
                vec![relational_input(CONSUMER, "peer", "alias")],
            ),
        &["resolved_member belongs to another provider"],
    );
}

#[test]
fn native_input_requires_exactly_one_selector_variant() {
    let common = "operation: object_own_data_function_read\naudited_source_sha256: ";
    let hash = "0".repeat(64);
    let none = format!("{common}{hash}\n");
    assert!(
        serde_yaml::from_str::<NativeOperationInputContract>(&none)
            .unwrap_err()
            .to_string()
            .contains("exactly one of source_match or resolved_member")
    );
    let both = format!(
        "{common}{hash}\nsource_match:\n  match: 'var alias = Object.defineProperty;'\n  bindings: [alias]\nresolved_member:\n  module: alpha\n  name: alphaDefine\n"
    );
    assert!(
        serde_yaml::from_str::<NativeOperationInputContract>(&both)
            .unwrap_err()
            .to_string()
            .contains("exactly one of source_match or resolved_member")
    );
    let legacy = format!(
        "{common}{hash}\nsource_match:\n  match: 'var alias = Object.defineProperty;'\n  bindings: [alias]\n"
    );
    assert!(matches!(
        serde_yaml::from_str::<NativeOperationInputContract>(&legacy)
            .unwrap()
            .selector,
        SourceUnitSelector::SourceMatch(_)
    ));
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
