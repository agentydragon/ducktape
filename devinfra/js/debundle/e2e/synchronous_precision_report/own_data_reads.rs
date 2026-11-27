//! Own-data and enum member reads stay complete while skipped mutations keep later reads conservative.

use super::{assert_init_order_edge, assert_reason, execution, execution_from_graph};
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn complete_execution_has_no_precision_loss() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            "function factory() { return 3; }\nconst result = factory();\nconsole.log(result);",
            vec![],
        )
        .with_import_order(&["residual"]),
    );
    assert_entry_output(&fixture, "3\n");
    let result = execution(&fixture, "result");
    assert_eq!(result["complete"], true, "{result:#}");
    assert_eq!(result["precision_losses"], serde_json::json!([]));
    assert_eq!(
        result["binding_effect_authority"]["authority"],
        "operation_receipt"
    );
    assert_eq!(
        result["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn incomplete_local_call_preserves_unrelated_own_data_reads() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"const record = { name: "ready" };
function ignore(value) {}
ignore(...[{}]);
const selected = [record.name];
console.log(selected[0]);
"#,
            vec![],
        )
        .with_import_order(&["residual"]),
    );
    assert_preserved_entry_output(&fixture, "ready\n");
    let selected = execution(&fixture, "selected");
    assert_eq!(selected["complete"], true, "{selected}");
    assert_eq!(selected["precision_losses"], serde_json::json!([]));
    assert_eq!(
        selected["binding_effect_authority"],
        serde_json::json!({
            "authority": "legacy_execution", "complete": true
        })
    );
}

#[test]
fn initialized_enum_own_data_read_is_complete_and_keeps_eager_dependency() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"var Status = ((values) => ((values.READY = "ready"), values))(Status || {});
const selected = [Status.READY];
console.log(selected[0]);
"#,
            vec![],
        )
        .with_import_order(&["residual"]),
    );

    assert_preserved_entry_output(&fixture, "ready\n");
    let graph = fixture.dependency_graph();
    let selected = execution_from_graph(&graph, "selected");
    assert_eq!(selected["complete"], true, "{selected}");
    assert_eq!(selected["precision_losses"], serde_json::json!([]));
    assert_eq!(
        selected["binding_effect_authority"],
        serde_json::json!({
            "authority": "legacy_execution", "complete": true
        })
    );
    let read = source_unit_for_binding(&graph, "selected");
    let initializer = source_unit_for_binding(&graph, "Status");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == read
                && edge.target == initializer
                && edge.binding.as_deref() == Some("Status")
                && edge.edge_kind == analysis_model::DepKind::EagerUse
                && edge.constrains_init_order
        }),
        "the member-read proof must keep the binding's eager initialization dependency"
    );
}

#[test]
fn enum_member_read_proof_rejects_mutation_escape_getter_unknown_key_and_reflection() {
    let source = r#"var Changed = ((values) => ((values.READY = "ready"), values))(Changed || {});
Changed.READY = "changed";
const changed = [Changed.READY];

var Escaped = ((values) => ((values.READY = "ready"), values))(Escaped || {});
globalThis.consume = (value) => { globalThis.retained = value; };
globalThis.consume(Escaped);
const escaped = [Escaped.READY];

const Accessor = { get READY() { return "getter"; } };
const getter = [Accessor.READY];

var Dynamic = ((values) => ((values.READY = "ready"), values))(Dynamic || {});
const key = "READY";
const dynamic = [Dynamic[key]];

var Reflected = ((values) => ((values.READY = "ready"), values))(Reflected || {});
Object.defineProperty(Reflected, "READY", { get() { return "reflected"; } });
const reflected = [Reflected.READY];

console.log(changed[0], escaped[0], getter[0], dynamic[0], reflected[0]);
"#;
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(source, vec![]).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "changed ready getter ready reflected\n");

    let graph = fixture.dependency_graph();
    for binding in ["changed", "escaped", "getter", "dynamic", "reflected"] {
        let report = execution_from_graph(&graph, binding);
        assert_eq!(report["complete"], false, "{binding}: {report}");
        assert!(
            report["precision_losses"].as_array().is_some_and(|losses| {
                losses.iter().any(|loss| {
                    loss["reason"]["code"] == "unsupported_operation"
                        && loss["reason"]["operation"] == "member_read"
                })
            }),
            "{binding} should retain the conservative member-read path: {report}"
        );
    }
}

#[test]
fn skipped_local_mutations_keep_later_property_reads_conservative() {
    for (mutation, observed, output) in [
        (
            "Object.defineProperty(value, 'name', { get() { globalThis.observed = true; return 'changed'; } });",
            "record.name",
            "changed true\n",
        ),
        (
            "value.name = 'changed';",
            "record.name",
            "changed undefined\n",
        ),
        (
            "Object.defineProperty(value, 'name', { get() { globalThis.observed = true; return () => 'changed'; } });",
            "record.name()",
            "changed true\n",
        ),
    ] {
        let source = format!(
            "const record = {{ name: () => 'original' }};\n\
             function change(value) {{ {mutation} }}\n\
             change(...[record]);\n\
             const selected = {observed};\n\
             console.log(selected, globalThis.observed);\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, output);
        let report = execution(&fixture, "selected");
        assert_eq!(report["complete"], false, "{mutation}: {report}");
        assert!(
            report["precision_losses"].as_array().is_some_and(|losses| {
                losses.iter().any(|loss| {
                    loss["reason"]["code"] == "unsupported_operation"
                        && loss["reason"]["operation"] == "member_read"
                        && loss["reach"]["kind"] == "global"
                })
            }),
            "a skipped mutation may install a getter: {report}"
        );
    }
}

#[test]
fn skipped_getter_installation_keeps_binding_effects_at_the_later_read() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"const state = 'ready';
const record = { name: 'original' };
function change(value) {
  Object.defineProperty(value, 'name', { get() { return state; } });
}

change(...[record]);
const selected = record.name;
console.log(selected);
"#,
            vec![],
        )
        .with_import_order(&["residual"]),
    );
    assert_preserved_entry_output(&fixture, "ready\n");
    let graph = fixture.dependency_graph();
    let reader = source_unit_for_binding(&graph, "selected");
    let state = source_unit_for_binding(&graph, "state");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == reader
                && edge.target == state
                && edge.binding.as_deref() == Some("state")
                && edge.constrains_init_order
        }),
        "the later read may invoke the installed getter and read state"
    );
}

#[test]
fn skipped_post_initializer_writes_disable_the_closed_enum_certificate() {
    for mutation in [
        "value.READY = 'changed';",
        "Object.defineProperty(value, 'READY', { get() { return 'changed'; } });",
    ] {
        let source = format!(
            "var Status = ((values) => ((values.READY = 'ready'), values))(Status || {{}});\n\
             function change(value) {{ {mutation} }}\n\
             change(...[Status]);\n\
             const selected = Status.READY;\n\
             console.log(selected);\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "changed\n");
        let report = execution(&fixture, "selected");
        assert_eq!(report["complete"], false, "{mutation}: {report}");
    }
}

#[test]
fn skipped_local_mutations_invalidate_reads_in_the_same_statement() {
    for mutation in ["change(...[record])", "tag`${record}`"] {
        let source = format!(
            "const record = {{ name: 'original' }};\n\
             function change(value) {{ value.name = 'changed'; }}\n\
             function tag(_, value) {{ value.name = 'changed'; }}\n\
             const selected = ({mutation}, record.name);\n\
             console.log(selected);\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "changed\n");
        let report = execution(&fixture, "selected");
        assert_reason(
            &report,
            serde_json::json!({"code": "unsupported_operation", "operation": "member_read"}),
        );
    }
}

#[test]
fn opaque_body_does_not_cache_property_values_before_its_skipped_code() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        r#"function change(value) { value.name = { get current() { return state; }, toString() { return 'changed'; } }; }
function read(record) {
  const selected = (change(...[record]), record.name);
  return selected;
}

const value = read({ name: 'original' });
const observed = value.current;
var state = 'late';
const coerced = { [value]: true };
console.log(observed, Object.keys(coerced)[0]);
"#,
        vec![],
    ).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "undefined changed\n");
    let graph = fixture.dependency_graph();
    assert_reason(
        &execution_from_graph(&graph, "observed"),
        serde_json::json!({"code": "unsupported_operation", "operation": "member_read"}),
    );
    assert_init_order_edge(&graph, "observed", "state");
    let coerced = source_unit_for_binding(&graph, "coerced");
    let node = graph.nodes.iter().find(|node| node.id == coerced).unwrap();
    assert_eq!(
        serde_json::to_value(node).unwrap()["purity"]["kind"],
        "not_pure"
    );
}

#[test]
fn skipped_operation_events_preserve_same_owner_getter_effects() {
    for source in [
        "const record = { name: 'original' };\nfunction tag(_, value) { Object.defineProperty(value, 'name', { get() { return state; } }); }\nconst selected = (tag`${record}`, record.name);\nvar state = 'changed';\nconsole.log(selected);\n",
        "const record = { name: 'original' };\nconst trigger = { get value() { return 0; }, set value(_) { Object.defineProperty(record, 'name', { get() { return state; } }); } };\nconst selected = (trigger.value++, record.name);\nvar state = 'changed';\nconsole.log(selected);\n",
    ] {
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "undefined\n");
        let graph = fixture.dependency_graph();
        let report = execution_from_graph(&graph, "selected");
        assert_reason(
            &report,
            serde_json::json!({"code": "unsupported_operation", "operation": "member_read"}),
        );
        assert_init_order_edge(&graph, "selected", "state");
    }
}

#[test]
fn stable_ts_enum_primitive_member_is_safe_as_computed_property_key() {
    let source = r#"var Status = ((values) => ((values.READY = "ready"), values))(Status || {});
const preferences = { [Status.READY]: true };
console.log(preferences.ready);
"#;
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(source, vec![]).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "true\n");

    let graph = fixture.dependency_graph();
    let preferences_unit = source_unit_for_binding(&graph, "preferences");
    let status_unit = source_unit_for_binding(&graph, "Status");
    let preferences = graph
        .nodes
        .iter()
        .find(|node| node.id == preferences_unit)
        .expect("preferences source unit");
    assert_eq!(
        serde_json::to_value(preferences).expect("serialize node")["purity"]["kind"],
        "pure"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == preferences_unit
                && edge.target == status_unit
                && edge.binding.as_deref() == Some("Status")
                && edge.edge_kind == analysis_model::DepKind::EagerUse
                && edge.constrains_init_order
        }),
        "the primitive enum proof must retain the eager binding dependency"
    );
}

#[test]
fn enum_property_key_proof_rejects_rebinds_mutations_unknown_keys_accessors_and_escapes() {
    let source = r#"var Rebound = ((values) => ((values.READY = "ready"), values))(Rebound || {});
let reboundCalls = 0;
Rebound = { READY: { toString() { reboundCalls++; return "rebound"; } } };
const rebound = { [Rebound.READY]: true };

var Looped = ((values) => ((values.READY = "ready"), values))(Looped || {});
let loopedCalls = 0;
for (Looped of [{ READY: { toString() { loopedCalls++; return "looped"; } } }]) {}
const looped = { [Looped.READY]: true };

var Mutated = ((values) => ((values.READY = "ready"), values))(Mutated || {});
Mutated.READY = { toString() { return "mutated"; } };
const mutated = { [Mutated.READY]: true };

var Dynamic = ((values) => ((values.READY = "ready"), values))(Dynamic || {});
const memberName = "READY";
const dynamic = { [Dynamic[memberName]]: true };

let getterCalls = 0;
const Accessor = { get READY() { getterCalls++; return "accessor"; } };
const accessor = { [Accessor.READY]: true };

var Escaped = ((values) => ((values.READY = "ready"), values))(Escaped || {});
globalThis.retainEnum = (value) => value;
globalThis.retainEnum(Escaped);
const escaped = { [Escaped.READY]: true };

console.log(rebound.rebound, reboundCalls, looped.looped, loopedCalls, mutated.mutated, dynamic.ready, accessor.accessor, getterCalls, escaped.ready);
"#;
    let fixture =
        run_fixture_with_graphs(FixtureOpts::new(source, vec![]).with_import_order(&["residual"]));
    assert_preserved_entry_output(&fixture, "true 1 true 1 true true true 1 true\n");

    let graph = fixture.dependency_graph();
    for binding in [
        "rebound", "looped", "mutated", "dynamic", "accessor", "escaped",
    ] {
        let unit = source_unit_for_binding(&graph, binding);
        let node = graph
            .nodes
            .iter()
            .find(|node| node.id == unit)
            .expect("source unit");
        let purity = serde_json::to_value(node).expect("serialize node")["purity"].clone();
        assert_eq!(purity["kind"], "not_pure", "{binding}: {purity}");
        assert!(
            purity["reasons"].as_array().is_some_and(|reasons| {
                reasons
                    .iter()
                    .any(|reason| reason["rule"] == "to_property_key_coercion")
            }),
            "{binding} must retain the coercion concern: {purity}"
        );
    }
}

#[test]
fn enum_member_read_before_initializer_remains_a_throwing_read() {
    let source = r#"const selected = [Status.READY];
var Status = ((values) => ((values.READY = "ready"), values))(Status || {});
console.log(selected[0]);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_import_order(&["residual"]));

    assert_preserved_entry_error(&fixture, "TypeError");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("early", &[Member::new("selected")]),
                logical_module("enum", &[Member::new("Status")]),
            ],
        )
        .with_import_order(&["early", "enum"]),
        &["early", "enum"],
        &["eager_use"],
    );
}
