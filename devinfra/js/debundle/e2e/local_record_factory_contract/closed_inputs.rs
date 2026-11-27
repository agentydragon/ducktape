//! Closed primitive inputs keep constructor results deferred without dropping eager reads.

use super::{
    FACTORY, assert_atomic_fallback, execution, factory_effect_order_opts,
    factory_effect_order_source, fixture_opts, fixture_source,
};
use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

const FRESH_RECORD_WITH_NAME: &str = r#"{
  name: label,
  execute: () => later,
  nested: { callback: function () { return later; }, enabled: true },
  items: [{ callback: () => later }],
  values: [1, "ready", null, false],
  mode: Flags.FIRST,
}"#;

#[test]
fn initialized_primitive_const_property_keeps_the_constructor_result_deferred() {
    let source = fixture_source(
        "",
        FACTORY,
        "const label = \"Example\";\n",
        &format!("create({FRESH_RECORD_WITH_NAME})"),
        "",
        "console.log(command.record.name, command.run(), command.weight);",
    );
    let fixture = run_fixture(fixture_opts(&source, true, &["label"], &[], &[]));

    assert_preserved_entry_output(&fixture, "Example 169 0\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["command"],
        &["label", "later"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["import { label", "function create", "const command"],
        &["const label"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/label.js",
        &["label"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/values/label.js",
        &["const label = \"Example\""],
        &[],
    );
}

#[test]
fn only_initialized_unreassigned_top_level_primitive_values_are_closed_inputs() {
    let cases = [
        (
            "const label = { get value() { return \"Example\"; } };\n",
            "",
            "create({ name: label.value, execute: () => later })",
            &["label"][..],
        ),
        (
            "const label = \"Example\";\nfunction updateLabel() { label = \"Changed\"; }\n",
            "",
            "create({ name: label, execute: () => later })",
            &["label", "updateLabel"][..],
        ),
        (
            "const label = typeof missingValue;\n",
            "",
            "create({ name: label, execute: () => later })",
            &["label"][..],
        ),
        (
            "const label = true ? \"Example\" : \"Other\";\n",
            "",
            "create({ name: label, execute: () => later })",
            &["label"][..],
        ),
        (
            "let label = \"Example\";\nlabel = \"Changed\";\n",
            "",
            "create({ name: label, execute: () => later })",
            &["label"][..],
        ),
    ];

    for (before_command, before_later, call, callback_members) in cases {
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            call,
            before_later,
            "console.log(command.run());",
        );
        assert_atomic_fallback(&source, callback_members, &[], &[]);
    }
}

#[test]
fn a_literal_constant_after_the_call_keeps_its_eager_tdz_dependency() {
    let source = fixture_source(
        "",
        FACTORY,
        "",
        "create({ name: label, execute: () => later })",
        "const label = \"Example\";\n",
        "console.log(command.run());",
    );
    expect_atomic_conflict_rejection(
        fixture_opts(&source, true, &["label"], &[], &[]),
        &["callbacks/wrapped", "values/label"],
        &["eager_use"],
    );
}

#[test]
fn checked_factory_call_purity_is_site_specific_and_keeps_eager_reads() {
    let holder = r#"class Holder {
  constructor(record) { this.record = record; this.label = label; }
}
"#;
    let source = factory_effect_order_source("create({ execute: () => later })", holder);
    let fixture = run_fixture_with_graphs(factory_effect_order_opts(&source, true));
    assert_preserved_entry_output(&fixture, "barrier\nExample 42 Example 42\n");

    let graph = fixture.dependency_graph();
    let label = node_declaring(&graph, "label");
    for binding in ["command", "commandAgain"] {
        let command = node_declaring(&graph, binding);
        assert!(command.purity.is_pure(), "{:?}", command.purity);
        let execution = execution(&graph, binding);
        assert!(execution.complete);
        assert_eq!(execution.local_record_factory_uses.len(), 1);
        assert!(
            graph.edges.iter().any(|edge| {
                edge.source == command.id
                    && edge.target == label.id
                    && edge.edge_kind == DepKind::EagerUse
                    && edge.binding.as_deref() == Some("label")
            }),
            "constructor's eager lexical read must remain an init dependency: {:#?}",
            graph.edges
        );
    }

    // Without the author premises, the same generic provider call remains an
    // ordering effect and cannot cross the barrier in a split graph.
    expect_atomic_conflict_rejection(
        factory_effect_order_opts(&source, false),
        &["callbacks/wrapped", "values/label", "values/later"],
        &["sequenced"],
    );

    // The literal construction is a pure control with the same eager label
    // read and deferred callback graph.
    let literal =
        factory_effect_order_source("{ label, record: { execute: () => later } }", holder);
    let literal_fixture = run_fixture_with_graphs(factory_effect_order_opts(&literal, true));
    assert_preserved_entry_output(&literal_fixture, "barrier\nExample 42 Example 42\n");
}
