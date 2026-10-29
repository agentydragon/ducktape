//! A narrowly selected native call can retain the existing call model while
//! unknown receiver writes elsewhere in the provider remain visible.

use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

const SOURCE: &str = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst marker = Symbol('marker');\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst made = factory();\nglobalThis.unrelatedKey = 'safe';\nconsole.log(marker.description, made.name, globalThis.unrelatedKey);\nexport { marker, factory, LaterError, reflect };\n";
const REFLECT_SOURCE: &str = "export function reflect(receiver, key) { Object.defineProperty(Object.getPrototypeOf(receiver), key, { value: 'changed', configurable: true }); }\n";
const REFLECT_CHUNKS: &[(&str, &str)] = &[("static/reflect", REFLECT_SOURCE)];
const BASELINE_CONFLICT_MODULES: &[&str] = &["marker", "later"];

fn source_hash(source: &str) -> String {
    Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn input_for(source: &str, declaration: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [marker]\n",
            declaration.trim()
        ))
        .expect("valid declaration source match"),
        operation: NativeOperationKind::Symbol,
        audited_source_sha256: source_hash(source),
        note: Some("Neutral fixture audits this one initialization call.".to_string()),
    }
}

fn marker_input(source: &str) -> NativeOperationInputContract {
    let declaration = source
        .lines()
        .find(|line| line.starts_with("const marker ="))
        .expect("fixture has a direct marker initializer");
    input_for(source, declaration)
}

fn split_opts(input: Option<NativeOperationInputContract>) -> FixtureOpts<'static> {
    let opts = FixtureOpts::new(
        SOURCE,
        vec![
            logical_module_with_anon(
                "marker",
                &[Member::new("marker")],
                &["globalThis.saved = () => factory();"],
            ),
            logical_module("factory", &[Member::new("factory")]),
            logical_module("later", &[Member::new("LaterError")]),
        ],
    );
    let opts = opts
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(&[("static/reflect.js", REFLECT_SOURCE)]);
    match input {
        Some(input) => opts.with_native_operation_inputs("static/app", vec![input]),
        None => opts,
    }
}

fn rejected_opts(
    source: &'static str,
    selected_binding: &'static str,
    input: NativeOperationInputContract,
) -> FixtureOpts<'static> {
    FixtureOpts::new(
        source,
        vec![logical_module("selected", &[Member::new(selected_binding)])],
    )
    .with_native_operation_inputs("static/app", vec![input])
}

fn native_input_uses(graph: &Value) -> Vec<Value> {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .flat_map(|node| {
            node["synchronous_execution"]["native_operation_input_uses"]
                .as_array()
                .into_iter()
                .flatten()
                .cloned()
        })
        .collect()
}

fn node_for_binding<'a>(graph: &'a Value, binding: &str) -> &'a Value {
    graph["nodes"]
        .as_array()
        .expect("dependency graph nodes")
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|candidate| candidate["binding"] == binding)
                })
        })
        .unwrap_or_else(|| panic!("no source node for {binding}: {graph}"))
}

fn has_unknown_call_target(node: &Value) -> bool {
    node["synchronous_execution"]["precision_losses"]
        .as_array()
        .is_some_and(|losses| {
            losses
                .iter()
                .any(|loss| loss["reason"]["code"] == "unknown_call_target")
        })
}

fn unknown_receiver_witness(graph: &Value) -> &Value {
    graph["native_integrity_witnesses"]
        .as_array()
        .expect("native mutation witnesses")
        .iter()
        .find(|witness| {
            witness["operation"] == "mutation_call"
                && witness["receiver_roots"]
                    .as_array()
                    .is_some_and(|roots| roots.iter().any(|root| root == "unknown_global_value"))
                && witness["source_location"]["source_path"] == "static/reflect.js"
        })
        .unwrap_or_else(|| panic!("reflective helper's unknown receiver is absent: {graph}"))
}

#[test]
fn selected_symbol_call_removes_only_its_synthetic_reentry_barrier() {
    let control = run_rejection_fixture(split_opts(None));
    let control_graph = serde_json::to_value(control.dependency_graph()).unwrap();
    unknown_receiver_witness(&control_graph);
    let conflicts = control.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            BASELINE_CONFLICT_MODULES.iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "unexpected no-input atomic-group conflicts: {conflicts:#?}"
    );
    assert!(
        has_unknown_call_target(node_for_binding(&control_graph, "marker")),
        "without the input, the selected initializer retains its unknown-call precision loss: {control_graph}"
    );

    let fixture = run_fixture(split_opts(Some(marker_input(SOURCE))));
    assert_preserved_entry_output(&fixture, "marker LaterError safe\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const entry = await import('./static/app/entry.js');\nconsole.log(entry.marker.description, entry.factory().name, entry.LaterError.name);\nconst receiver = [];\nconst target = Object.getPrototypeOf(receiver);\nconst key = '__native_operation_input_probe__';\nconst before = Object.getOwnPropertyDescriptor(target, key);\ntry { entry.reflect(receiver, key); console.log(target[key]); } finally { if (before) Object.defineProperty(target, key, before); else delete target[key]; }\n",
        "marker LaterError LaterError\nchanged\n",
    );

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = native_input_uses(&graph);
    assert_eq!(
        uses.len(),
        1,
        "only the selected call consumes this input: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["operation"], "symbol");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
    assert_eq!(uses[0]["source_location"]["source_path"], "static/app.js");
    assert_eq!(uses[0]["source_location"]["start_line"], 3);

    let unknown_receiver_witness = unknown_receiver_witness(&graph);
    assert!(
        unknown_receiver_witness["affected_roots"]
            .as_array()
            .is_some_and(|roots| roots.iter().any(|root| root == "global_object")),
        "the unknown receiver remains connected to the global object: {unknown_receiver_witness}"
    );
    assert!(
        unknown_receiver_witness["value_input_refs"]
            .as_array()
            .is_none_or(Vec::is_empty),
        "this premise must not refine the reflective helper's receiver: {graph}"
    );
}

#[test]
fn source_selected_symbol_input_rejects_unsupported_or_contradicted_facts() {
    for (source, expected) in [
        (
            "const marker = Symbol('marker');\nconst Symbol = function(value) { return value; };\nexport { marker };\n",
            "shadowed Symbol",
        ),
        (
            "const marker = Symbol('marker');\nconst originalSymbol = globalThis.Symbol;\nglobalThis.Symbol = function replacement() {};\nglobalThis.Symbol = originalSymbol;\nexport { marker };\n",
            "global Symbol replacement",
        ),
        (
            "Object.defineProperty(globalThis, 'Symbol', { configurable: true, get() { return function replacement() {}; } });\nconst marker = Symbol('marker');\nexport { marker };\n",
            "global Symbol replacement",
        ),
        (
            "const marker = Symbol(globalThis.markerDescription);\nexport { marker };\n",
            "syntactically primitive argument",
        ),
        (
            "const marker = Symbol?.('marker');\nexport { marker };\n",
            "unsupported Symbol call shape",
        ),
        (
            "const marker = Symbol(...['marker']);\nexport { marker };\n",
            "unsupported Symbol call shape",
        ),
        (
            "const selected = globalThis['Symbol']('marker');\nexport { selected };\n",
            "direct uncomputed Symbol identifier",
        ),
    ] {
        let declaration = source
            .lines()
            .find(|line| line.contains("const marker =") || line.contains("const selected ="))
            .expect("negative fixture has selected declaration");
        let selected_name = if declaration.starts_with("const selected") {
            "selected"
        } else {
            "marker"
        };
        let mut input = input_for(source, declaration);
        if selected_name == "selected" {
            input.source_match = serde_yaml::from_str(&format!(
                "match: {:?}\nbindings: [selected]\n",
                declaration.trim()
            ))
            .unwrap();
        }
        expect_rejection_containing_all(rejected_opts(source, selected_name, input), &[expected]);
    }
}

#[test]
fn source_hash_is_checked_before_a_symbol_input_is_used() {
    let source = "const marker = Symbol('marker');\nexport { marker };\n";
    let mut input = marker_input(source);
    input.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        rejected_opts(source, "marker", input),
        &["audited_source_sha256"],
    );
}

#[test]
fn unselected_symbol_site_keeps_the_strict_fallback() {
    let source = "import { reflect } from './reflect.js';\nglobalThis.saved = () => factory();\nconst marker = Symbol('marker');\nconst other = Symbol('other');\nfunction factory() { return LaterError; }\nclass LaterError extends Error {}\nconst made = factory();\nconsole.log(marker.description, other.description, made.name);\nexport { marker, other, factory, LaterError, reflect };\n";
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "marker",
                &[Member::new("marker")],
                &["globalThis.saved = () => factory();"],
            ),
            logical_module("other", &[Member::new("other")]),
            logical_module("factory", &[Member::new("factory")]),
            logical_module("later", &[Member::new("LaterError")]),
        ],
    )
    .with_extra_chunks(REFLECT_CHUNKS)
    .with_extra_files(&[("static/reflect.js", REFLECT_SOURCE)])
    .with_native_operation_inputs("static/app", vec![marker_input(source)]);
    let rejected = run_rejection_fixture(opts);
    let conflicts = rejected.atomic_group_conflicts();
    assert!(
        conflicts.iter().any(|conflict| {
            let claims = conflict["claims"].as_array().expect("conflict claims");
            ["other", "later"].iter().all(|module| {
                claims
                    .iter()
                    .any(|claim| claim["module"].as_str() == Some(*module))
            })
        }),
        "unselected Symbol call must retain the synthetic reentry conflict: {conflicts:#?}"
    );
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    assert!(
        has_unknown_call_target(node_for_binding(&graph, "other")),
        "the unselected primitive Symbol call must retain its unknown-call precision loss: {graph}"
    );
    let uses = native_input_uses(&graph);
    assert_eq!(
        uses.len(),
        1,
        "other Symbol call has no author evidence: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
}

#[test]
fn known_symbol_replacement_in_another_analyzed_chunk_contradicts_the_input() {
    const MUTATION: &str = "const originalSymbol = globalThis.Symbol;\nglobalThis.Symbol = function replacement() {};\nglobalThis.Symbol = originalSymbol;\nexport {};\n";
    let source = "import './mutation.js';\nconst marker = Symbol('marker');\nexport { marker };\n";
    let input = marker_input(source);
    let opts = FixtureOpts::new(
        source,
        vec![logical_module("marker", &[Member::new("marker")])],
    )
    .with_extra_chunks(&[("static/mutation", MUTATION)])
    .with_native_operation_inputs("static/app", vec![input]);
    expect_rejection_containing_all(opts, &["global Symbol replacement", "static/mutation"]);
}

#[test]
fn primitive_argument_throw_is_preserved_by_the_selected_call_path() {
    let source = "console.log('before');\nconst marker = Symbol(1n + 1);\nconsole.log('after');\nexport { marker };\n";
    let opts = FixtureOpts::new(source, vec![])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![marker_input(source)]);
    let fixture = run_fixture(opts);
    assert_preserved_entry_error(&fixture, "TypeError");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = native_input_uses(&graph);
    assert_eq!(uses.len(), 1, "the exact selected call is still reported");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
}
