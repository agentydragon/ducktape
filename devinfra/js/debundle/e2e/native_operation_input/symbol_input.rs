//! Selected `Symbol` calls drop only their own re-entry barrier and reject contradicting slot writes.

use super::{SOURCE, rejected_opts};
use debundle_e2e_graph_support::reflective_provider::{
    REFLECT_CHUNKS, REFLECT_FILES, unknown_receiver_witness,
};
use debundle_e2e_graph_support::{
    FixtureGraphExt, json_execution_rows, json_has_unknown_call_target, json_node_declaring,
};
use debundle_e2e_support::*;

const BASELINE_CONFLICT_MODULES: &[&str] = &["marker", "later"];

fn symbol_input(source: &str, declaration: &str, binding: &str) -> NativeOperationInputContract {
    native_operation_input(
        source,
        declared_source_unit(declaration.trim(), binding),
        NativeOperationKind::Symbol,
        "Neutral fixture audits this one initialization call.",
    )
}

fn marker_input(source: &str) -> NativeOperationInputContract {
    let declaration = source
        .lines()
        .find(|line| line.starts_with("const marker ="))
        .expect("fixture has a direct marker initializer");
    symbol_input(source, declaration, "marker")
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
    )
    .with_import_order(&["marker", "factory", "later", "residual"]);
    let opts = opts
        .with_extra_chunks(REFLECT_CHUNKS)
        .with_extra_files(REFLECT_FILES);
    match input {
        Some(input) => opts.with_native_operation_inputs("static/app", vec![input]),
        None => opts,
    }
}

#[test]
fn selected_symbol_call_removes_only_its_synthetic_reentry_barrier() {
    let control = run_rejection_fixture_with_graphs(split_opts(None));
    let control_graph = serde_json::to_value(control.dependency_graph()).unwrap();
    unknown_receiver_witness(&serde_json::to_value(control.program_graph()).unwrap());
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
        json_has_unknown_call_target(json_node_declaring(&control_graph, "marker")),
        "without the input, the selected initializer retains its unknown-call precision loss: {control_graph}"
    );

    let fixture = run_fixture_with_graphs(split_opts(Some(marker_input(SOURCE))));
    assert_preserved_entry_output(&fixture, "marker LaterError safe\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const entry = await import('./static/app/entry.js');\nconsole.log(entry.marker.description, entry.factory().name, entry.LaterError.name);\nconst receiver = [];\nconst target = Object.getPrototypeOf(receiver);\nconst key = '__native_operation_input_probe__';\nconst before = Object.getOwnPropertyDescriptor(target, key);\ntry { entry.reflect(receiver, key); console.log(target[key]); } finally { if (before) Object.defineProperty(target, key, before); else delete target[key]; }\n",
        "marker LaterError LaterError\nchanged\n",
    );

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
    assert_eq!(
        uses.len(),
        1,
        "only the selected call consumes this input: {graph}"
    );
    assert_eq!(uses[0]["evidence"]["operation"], "symbol");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
    assert_eq!(uses[0]["source_location"]["source_path"], "static/app.js");
    assert_eq!(uses[0]["source_location"]["start_line"], 3);

    let unknown_receiver_witness = unknown_receiver_witness(&program_graph);
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
    assert!(
        unknown_receiver_witness["target_slots"]
            .as_array()
            .is_some_and(|slots| slots
                .iter()
                .any(|slot| { slot["receiver"] == "unknown" && slot["key"] == "unknown" })),
        "the opaque reflective write must retain its unknown slot witness: {graph}"
    );
}

#[test]
fn selected_literal_symbol_breaks_order_cycle_with_live_array_contents() {
    let source = "const values = [];\nconsole.log('before', values.length);\nconst marker = Symbol('fixture.key');\nvalues.push('after');\nconsole.log('after', values.length);\nexport { values, marker };\n";
    let modules = || {
        vec![
            logical_module_with_anon(
                "outer",
                &[Member::new("values")],
                &[
                    "console.log('before', values.length);",
                    "values.push('after');",
                    "console.log('after', values.length);",
                ],
            ),
            logical_module("inner", &[Member::new("marker")]),
        ]
    };
    for (call, description) in [
        ("Symbol()", "undefined"),
        ("Symbol('fixture.key')", "fixture.key"),
    ] {
        let source = source.replace("Symbol('fixture.key')", call);
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, modules())
                .with_native_operation_inputs("static/app", vec![marker_input(&source)]),
        );
        assert_preserved_entry_output(&fixture, "before 0\nafter 1\n");
        assert_generated_module_after_entry_script(
            &fixture,
            "const entry = await import('./static/app/entry.js');\nconsole.log(typeof entry.marker, entry.marker.description, entry.values.join(','));\n",
            &format!("symbol {description} after\n"),
        );
        let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
        let marker = json_node_declaring(&graph, "marker");
        assert_eq!(marker["purity"]["kind"], "pure", "{call}: {graph}");
        assert_eq!(
            marker["purity"]["dependencies"]["native_operation_inputs"][0]["input_index"],
            0
        );
        let uses = json_execution_rows(&graph, "native_operation_input_uses");
        assert_eq!(uses.len(), 1);
        assert_eq!(uses[0]["evidence"]["operation"], "symbol");
    }
}

#[test]
fn selected_symbol_rejects_spread_and_nonprimitive_arguments() {
    for (source, expected) in [
        (
            "const marker = Symbol(...['key']);\nexport { marker };\n",
            "unsupported Symbol call shape",
        ),
        (
            "const marker = Symbol({ toString() { return 'key'; } });\nexport { marker };\n",
            "unsupported Symbol call shape",
        ),
        (
            "const marker = Symbol(unknown);\nexport { marker };\n",
            "unsupported Symbol call shape",
        ),
    ] {
        expect_rejection_containing_all(
            rejected_opts(source, "marker", marker_input(source)),
            &[expected],
        );
    }
}

#[test]
fn selected_symbol_converts_primitive_arguments() {
    let source =
        "const marker = Symbol(1);\nconsole.log(marker.description);\nexport { marker };\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![marker_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "1\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_eq!(
        json_execution_rows(&graph, "native_operation_input_uses").len(),
        1
    );
}

/// An assignment to the unqualified global binding replaces the global slot
/// and leaves the name global, so writes through it stay positively known.
#[test]
fn global_binding_assignments_contradict_symbol_input() {
    for (mutation, expected) in [
        ("Symbol = () => 'changed';\n", "global Symbol replacement"),
        (
            "[Symbol] = [() => 'changed'];\n",
            "global Symbol replacement",
        ),
        (
            "function never() { Symbol = null; }\n",
            "global Symbol replacement",
        ),
    ] {
        let source = "import './mutation.js';\nconst marker = Symbol('key');\nexport { marker };\n";
        expect_rejection_containing_all(
            FixtureOpts::new(
                source,
                vec![logical_module("marker", &[Member::new("marker")])],
            )
            .with_extra_chunks(&[("static/mutation", mutation)])
            .with_native_operation_inputs("static/app", vec![marker_input(source)]),
            &[expected, "static/mutation"],
        );
    }
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
        let input = symbol_input(source, declaration, selected_name);
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
    .with_import_order(&["marker", "other", "factory", "later", "residual"])
    .with_extra_chunks(REFLECT_CHUNKS)
    .with_extra_files(REFLECT_FILES)
    .with_native_operation_inputs("static/app", vec![marker_input(source)]);
    let rejected = run_rejection_fixture_with_graphs(opts);
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
        json_has_unknown_call_target(json_node_declaring(&graph, "other")),
        "the unselected primitive Symbol call must retain its unknown-call precision loss: {graph}"
    );
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
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
fn exact_global_symbol_slot_rejects_aliases_and_reflective_writes() {
    for mutation in [
        "const root = globalThis; root.Symbol = function replacement() {};",
        "const root = globalThis; delete root.Symbol;",
        "const root = globalThis; Object.defineProperty(root, 'Symbol', { get() { return function replacement() {}; } });",
        "const root = globalThis; Object.assign(root, { Symbol: function replacement() {} });",
        "const root = globalThis; Object.defineProperties(root, { Symbol: { value: function replacement() {} } });",
    ] {
        let source =
            format!("const marker = Symbol('marker');\n{mutation}\nexport {{ marker }};\n");
        expect_rejection_containing_all(
            rejected_opts(&source, "marker", marker_input(&source)),
            &["global Symbol replacement"],
        );
    }
}

#[test]
fn unrelated_global_write_preserves_exact_symbol_input() {
    let source = "const marker = Symbol('marker');\nconst root = globalThis;\nObject.assign(root, { unrelated: 1 });\nconsole.log(marker.description);\nexport { marker };\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![marker_input(source)]),
    );
    assert_preserved_entry_output(&fixture, "marker\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    assert_eq!(
        json_execution_rows(&graph, "native_operation_input_uses").len(),
        1
    );
    // A literal source names only `unrelated`, which holds no intrinsic.
    assert!(
        program_graph["native_integrity_witnesses"].is_null(),
        "{program_graph}"
    );
}

#[test]
fn native_slot_census_preserves_paths_keys_and_opaque_writes_across_chunks() {
    let source = "import './mutation.js';\nconsole.log('ok');\n";
    let mutation = "export function mutate() { const proto = Set.prototype; proto.add = function(value) { return value; }; Set.sibling = 1; Array.prototype[Symbol.iterator] = function* () {}; Object.defineProperty(proto, 'delete', { value: function() {} }); const unknown = globalThis.maybe; Object.defineProperty(unknown, 'add', { value: function() {} }); }\n";
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(source, vec![]).with_extra_chunks(&[("static/mutation", mutation)]),
    );
    assert_preserved_entry_output(&fixture, "ok\n");
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    let slots: Vec<_> = program_graph["native_integrity_witnesses"]
        .as_array()
        .expect("native mutation witnesses")
        .iter()
        .filter(|witness| witness["source_location"]["source_path"] == "static/mutation.js")
        .flat_map(|witness| witness["target_slots"].as_array().into_iter().flatten())
        .collect();
    for expected in [
        serde_json::json!({"receiver": {"prototype": "set"}, "key": {"string": "add"}}),
        serde_json::json!({"receiver": {"constructor": "set"}, "key": {"string": "sibling"}}),
        serde_json::json!({"receiver": {"prototype": "array"}, "key": "symbol_iterator"}),
        serde_json::json!({"receiver": {"prototype": "set"}, "key": {"string": "delete"}}),
        serde_json::json!({"receiver": "unknown", "key": {"string": "add"}}),
    ] {
        assert!(
            slots.contains(&&expected),
            "missing {expected} from {slots:?}"
        );
    }
}

#[test]
fn primitive_argument_throw_is_preserved_by_the_selected_call_path() {
    let source = "console.log('before');\nconst marker = Symbol(1n + 1);\nconsole.log('after');\nexport { marker };\n";
    let opts = FixtureOpts::new(source, vec![])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![marker_input(source)]);
    let fixture = run_fixture_with_graphs(opts);
    assert_preserved_entry_error(&fixture, "TypeError");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let uses = json_execution_rows(&graph, "native_operation_input_uses");
    assert_eq!(uses.len(), 1, "the exact selected call is still reported");
    assert_eq!(uses[0]["evidence"]["selected_binding"], "marker");
}
