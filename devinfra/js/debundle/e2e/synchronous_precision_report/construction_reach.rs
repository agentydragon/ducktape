//! Rejected constructions report bounded or global reach and the use that caused it.

use super::{assert_reason, execution, global_reach};
use debundle_e2e_support::*;
use serde_json::Value;

/// The reach of the rejected construction of `result` in `source`, whose
/// entry prints `3`.
fn construction_reach(source: &str, extra_chunks: &[(&str, &str)]) -> Value {
    construction_reach_of(
        FixtureOpts::new(source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(extra_chunks),
    )
}

fn construction_reach_of(opts: FixtureOpts<'_>) -> Value {
    let fixture = run_fixture_with_graphs(opts);
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    result["precision_losses"]
        .as_array()
        .expect("losses")
        .iter()
        .find(|loss| loss["reason"]["code"] == "unsupported_construction")
        .unwrap_or_else(|| panic!("missing construction loss in {result}"))["reach"]
        .clone()
}

#[test]
fn rejected_construction_of_local_code_with_inline_callback_is_bounded() {
    assert_eq!(
        construction_reach(
            "class Box { constructor(read) { this.read = read; } }\nconst result = new Box(...[() => 3]);\nconsole.log(result.read());",
            &[],
        ),
        serde_json::json!({"kind": "bounded"})
    );
}

#[test]
fn constructor_calling_a_global_function_has_global_reach() {
    assert_eq!(
        construction_reach(
            "globalThis.log = () => {};\nclass Box { constructor(read) { this.read = read; log(read); } }\nconst result = new Box(() => 3);\nconsole.log(result.read());",
            &[],
        ),
        global_reach("unknown_callee")
    );
}

#[test]
fn constructor_reaching_an_import_through_a_local_helper_has_global_reach() {
    assert_eq!(
        construction_reach(
            "import { pass } from './lib.js';\nfunction helper(value) { return pass(value); }\nclass Box { constructor(read) { this.read = helper(read); } }\nconst result = new Box(...[() => 3]);\nconsole.log(result.read());",
            &[(
                "static/lib",
                "export function pass(value) { return value; }"
            )],
        ),
        global_reach("unknown_callee")
    );
}

#[test]
fn argument_escaped_to_opaque_code_has_global_reach() {
    assert_eq!(
        construction_reach(
            "class Box { constructor(read) { this.read = read; } }\nconst read = () => 3;\nJSON.stringify({ read });\nconst result = new Box(...[read]);\nconsole.log(result.read());",
            &[],
        ),
        global_reach("tainted_input")
    );
}

#[test]
fn published_class_with_a_borrowed_method_has_global_reach() {
    assert_eq!(
        construction_reach(
            "class Box { constructor(read) { this.read = read; } get() { return this.read(); } }\nglobalThis.borrowed = Box.prototype.get;\nconst result = new Box(...[() => 3]);\nconsole.log(result.get());",
            &[],
        ),
        global_reach("borrowed_receiver")
    );
}

#[test]
fn dynamic_scope_in_the_chunk_has_global_reach() {
    // A direct `eval` is refused at admission (A1); the override keeps
    // the analysis running to pin its reach.
    assert_eq!(
        construction_reach_of(
            FixtureOpts::new(
                "function probe() { return eval('0'); }\nclass Box { constructor(read) { this.read = read; } }\nconst result = new Box(...[() => 3]);\nconsole.log(result.read());",
                vec![],
            ).with_import_order(&["residual"])
            .with_admission_overrides(&["a1_eval"]),
        ),
        global_reach("dynamic_scope")
    );
}

/// Each fixture constructs `result` from a local class whose construction
/// proof fails at one check; the loss names that check.
fn assert_construction_rejection(source: &str, expected_output: &str, rejection: Value) {
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(source, vec![]).with_import_order(&["residual"]));
    assert_entry_output(&fixture, expected_output);
    let result = execution(&fixture, "result");
    assert_reason(
        &result,
        serde_json::json!({"code": "unsupported_construction", "rejection": rejection}),
    );
}

#[test]
fn spread_construction_reports_spread_arguments() {
    assert_construction_rejection(
        "class Box { constructor(value) { this.value = value; } }\nconst inputs = [3];\nconst result = new Box(...inputs);\nconsole.log(result.value);",
        "3\n",
        serde_json::json!({"check": "spread_arguments"}),
    );
}

#[test]
fn aliased_class_reports_use_outside_construction() {
    assert_construction_rejection(
        "class Box { constructor(value) { this.value = value; } }\nconst alias = Box;\nconst result = new Box(3);\nconsole.log(result.value, alias === Box);",
        "3 true\n",
        serde_json::json!({"check": "class_used_outside_construction"}),
    );
}

#[test]
fn possible_object_prototype_field_reports_the_field() {
    assert_construction_rejection(
        "function define(name) { Object.defineProperty(Object.prototype, name, { value: 1 }); }\nclass Box { constructor(value) { this.value = value; } }\nconst result = new Box(3);\nconsole.log(result.value, typeof define);",
        "3 function\n",
        serde_json::json!({"check": "field_may_exist_on_object_prototype", "property": "value"}),
    );
}

#[test]
fn unknown_property_source_reports_its_parameter() {
    assert_construction_rejection(
        "class Box { constructor(config) { this.value = config.value; } }\nconst result = new Box(JSON.parse('{\"value\": 3}'));\nconsole.log(result.value);",
        "3\n",
        serde_json::json!({"check": "unchecked_property_source", "parameter": 0}),
    );
}
