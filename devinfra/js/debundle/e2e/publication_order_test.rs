//! Real CLI fixtures for private definition visibility and cyclic export availability.

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use spec::{BindingAnnotation, InitializerBindingEffects};

const CLASS_OBSERVER: &str = r#"export function inspect(value) {
  console.log(typeof value.get());
}
"#;

const CLASS_OBSERVER_CHUNKS: &[(&str, &str)] = &[("static/observer", CLASS_OBSERVER)];

const PRIVATE_CLASS_SOURCE: &str = r#"import { inspect } from "./observer.js";
var Holder = class { static get() { return alias; } };
inspect(Holder);
var Hidden = class {};
var alias = Hidden;
console.log(alias === Hidden);
"#;

const PRIVATE_CLASS_OUTPUT: &str = "undefined\ntrue\n";
const PUBLISHED_ALIAS_OUTPUT: &str = "function\ntrue\n";

fn private_class_fixture(members: &[Member]) -> FixtureOpts<'static> {
    FixtureOpts::new(
        PRIVATE_CLASS_SOURCE,
        vec![logical_module("classes/hidden", members)],
    )
    .with_extra_chunks(CLASS_OBSERVER_CHUNKS)
    .with_unassigned_mode(unassigned_mode_inline())
}

fn assert_node_result(result: &CommandResult, expected_stdout: &str, label: &str) {
    assert!(
        result.status.success(),
        "{label} did not execute successfully: stdout={:?}, stderr={:?}",
        result.stdout,
        result.stderr
    );
    assert_eq!(result.stdout, expected_stdout, "{label}");
}

#[test]
fn private_class_can_move_before_first_publication() {
    let outcome = run_fixture_to_gate_outcome(private_class_fixture(&[Member::new("Hidden")]));

    assert!(
        outcome.accepted,
        "moving only the private class should be accepted; source={:?}, emitted={:?}",
        outcome.source.stdout, outcome.emitted.stdout
    );
    assert_node_result(&outcome.source, PRIVATE_CLASS_OUTPUT, "original source");
    assert_node_result(
        &outcome.emitted,
        PRIVATE_CLASS_OUTPUT,
        "accepted emitted entry",
    );
}

#[test]
fn moving_alias_publication_with_private_class_is_rejected() {
    let outcome = run_fixture_to_gate_outcome(private_class_fixture(&[
        Member::new("Hidden"),
        Member::new("alias"),
    ]));

    assert!(
        !outcome.accepted,
        "moving the alias before its observer must be rejected; source={:?}, emitted={:?}",
        outcome.source.stdout, outcome.emitted.stdout
    );
    assert_node_result(&outcome.source, PRIVATE_CLASS_OUTPUT, "original source");
    assert_node_result(
        &outcome.emitted,
        PUBLISHED_ALIAS_OUTPUT,
        "rejected emitted candidate",
    );
}

const CYCLIC_OBSERVER: &str = r#"import { callback } from "./app.js";
export function inspect() {
  try { console.log(typeof callback); }
  catch (error) { console.log(error.name); }
}
"#;

const GLOBAL_CYCLIC_OBSERVER: &str = "import { callback } from './app.js';\nglobalThis.inspect = () => console.log(typeof callback);\n";

fn exported_callback_fixture<'a>(
    source: &'a str,
    chunks: &'a [(&'a str, &'a str)],
) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![logical_module(
            "callbacks/value",
            &[Member::new("callback")],
        )],
    )
    .with_extra_chunks(chunks)
    .with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn cyclic_importer_cannot_observe_exported_callback_before_initialization() {
    for (declaration, original_output) in [("var", "undefined\n"), ("let", "ReferenceError\n")] {
        let source = format!(
            "import {{ inspect }} from './observer.js';\ninspect();\nexport {declaration} callback = () => 1;\n"
        );
        let chunks = [("static/observer", CYCLIC_OBSERVER)];
        let outcome = run_fixture_to_gate_outcome(exported_callback_fixture(&source, &chunks));

        assert!(
            !outcome.accepted,
            "moving the exported {declaration} callback before cyclic observation must be rejected; source={:?}, emitted={:?}",
            outcome.source.stdout, outcome.emitted.stdout
        );
        assert_node_result(&outcome.source, original_output, "original cyclic source");
        assert_node_result(
            &outcome.emitted,
            "function\n",
            "rejected cyclic emitted candidate",
        );
    }
}

#[test]
fn cyclic_importer_can_observe_an_already_initialized_export() {
    let chunks = [("static/observer", CYCLIC_OBSERVER)];
    let fixture = run_fixture(exported_callback_fixture(
        "import { inspect } from './observer.js';\nexport var callback = () => 1;\ninspect();\n",
        &chunks,
    ));
    assert_preserved_entry_output(&fixture, "function\n");
}

#[test]
fn globally_installed_cyclic_observer_cannot_see_an_initialized_future_export() {
    let source = "import './observer.js';\nglobalThis.inspect();\nexport var callback = () => 1;\n";
    let chunks = [("static/observer", GLOBAL_CYCLIC_OBSERVER)];
    let outcome = run_fixture_to_gate_outcome(exported_callback_fixture(source, &chunks));
    assert_node_result(&outcome.source, "undefined\n", "original cyclic source");
    assert_node_result(&outcome.emitted, "function\n", "rejected emitted candidate");
    assert!(
        !outcome.accepted,
        "the global observer must preserve the exported cell's initialization order"
    );
}

#[test]
fn transitive_cyclic_observer_cannot_see_an_initialized_future_export() {
    let source =
        "import { inspect } from './observer.js';\ninspect();\nexport var callback = () => 1;\n";
    let chunks = [
        (
            "static/observer",
            "import { read } from './reader.js';\nexport const inspect = () => read();\n",
        ),
        (
            "static/reader",
            "import { callback } from './app.js';\nexport function read() { console.log(typeof callback); }\n",
        ),
    ];
    let outcome = run_fixture_to_gate_outcome(exported_callback_fixture(source, &chunks));
    assert_node_result(&outcome.source, "undefined\n", "original transitive source");
    assert_node_result(&outcome.emitted, "function\n", "rejected emitted candidate");
    assert!(
        !outcome.accepted,
        "transitive observation must preserve cell initialization order"
    );
}

#[test]
fn global_cyclic_observer_can_read_an_already_initialized_export() {
    let chunks = [("static/observer", GLOBAL_CYCLIC_OBSERVER)];
    let fixture = run_fixture(exported_callback_fixture(
        "import './observer.js';\nexport var callback = () => 1;\nglobalThis.inspect();\n",
        &chunks,
    ));
    assert_preserved_entry_output(&fixture, "function\n");
}

#[test]
fn global_observer_cell_constraints_survive_saved_analysis() {
    let chunks = [("static/observer", GLOBAL_CYCLIC_OBSERVER)];
    let (staged, _fixture) = staged(exported_callback_fixture(
        "import './observer.js';\nglobalThis.inspect();\nexport var callback = () => 1;\n",
        &chunks,
    ));
    let invocation = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_eq!(
        invocation.result.status.code(),
        Some(3),
        "{}",
        invocation.result.stderr
    );
}

#[test]
fn an_unloaded_foreign_reader_does_not_block_export_initialization() {
    let chunks = [("static/observer", CYCLIC_OBSERVER)];
    let fixture = run_fixture(exported_callback_fixture(
        "console.log('ready');\nexport var callback = () => 1;\n",
        &chunks,
    ));
    assert_preserved_entry_output(&fixture, "ready\n");
}

#[test]
fn acyclic_observer_does_not_block_export_initialization_relocation() {
    let chunks = [(
        "static/observer",
        "export function inspect() { console.log('observed'); }\n",
    )];
    let fixture = run_fixture(exported_callback_fixture(
        "import { inspect } from './observer.js';\ninspect();\nexport var callback = () => 1;\n",
        &chunks,
    ));
    assert_preserved_entry_output(&fixture, "observed\n");
}

#[test]
fn aliased_provider_and_importer_cells_reject_in_direct_and_saved_analysis() {
    let source = "import { observe as inspect } from './observer.js';\ninspect();\nvar callback = () => 1;\nexport { callback as published };\n";
    let chunks = [(
        "static/observer",
        "import { published as value } from './app.js';\nfunction read() { console.log(typeof value); }\nexport { read as observe };\n",
    )];
    let (staged, _fixture) = staged(exported_callback_fixture(source, &chunks));
    for args in [
        vec!["--emit-on-rejection"],
        vec!["--emit-on-rejection", "--analysis", path(&staged.artifact)],
    ] {
        let invocation = staged.run(&args);
        assert_eq!(
            invocation.result.status.code(),
            Some(3),
            "{}",
            invocation.result.stderr
        );
    }
}

#[test]
fn deferred_descendant_reads_are_assumptions_not_observed_initializer_effects() {
    let source = "import { make } from './observer.js';\nconst delayed = make();\nexport var callback = () => 1;\nconsole.log(delayed());\n";
    let chunks = [(
        "static/observer",
        "import { callback } from './app.js';\nexport function make() { return () => typeof callback; }\n",
    )];
    let mut deferred = logical_module("readers/deferred", &[Member::new("delayed")]);
    deferred.1.annotations.insert(
        "delayed".to_owned(),
        BindingAnnotation {
            initializer_binding_effects: Some(InitializerBindingEffects::SourceOnly),
            ..Default::default()
        },
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                deferred,
                logical_module("callbacks/value", &[Member::new("callback")]),
            ],
        )
        .with_extra_chunks(&chunks)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "function\n");
}
