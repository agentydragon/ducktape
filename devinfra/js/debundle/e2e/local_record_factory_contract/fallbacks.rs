//! Unsupported callbacks, callees, bindings, arguments and provider effects fall back conservatively.

use super::{
    COMPUTED_MUTATOR, FACTORY, FLAGS, FRESH_RECORD, HOLDER, LATER, PREFIX, assert_atomic_fallback,
    factory_effect_order_opts, factory_effect_order_source, fixture_opts, fixture_source,
    harness_members, members,
};
use debundle_e2e_support::*;

#[test]
fn computed_unknown_writer_still_rejects_without_the_assertion() {
    let source = fixture_source(
        "",
        FACTORY,
        "",
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    expect_atomic_conflict_rejection(
        fixture_opts(&source, false, &[], &[], &[]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn invoking_or_extracting_a_result_callback_before_later_remains_rejected() {
    for (before_later, members) in [
        ("const observed = command.run();\n", vec!["observed"]),
        (
            "const callback = command.record.execute;\nconst observed = callback();\n",
            vec!["callback", "observed"],
        ),
    ] {
        let source = fixture_source(
            "",
            FACTORY,
            "",
            &format!("create({FRESH_RECORD})"),
            before_later,
            "console.log(observed);",
        );
        assert_atomic_fallback(&source, &members, &[], &[]);
    }
}

#[test]
fn publishing_then_consuming_the_result_keeps_its_callback_graph_atomic() {
    let source = fixture_source(
        "",
        FACTORY,
        "",
        &format!("create({FRESH_RECORD})"),
        "globalThis.savedCommand = command;\n",
        "const observed = globalThis.savedCommand.run();\nconsole.log(observed);",
    );
    assert_atomic_fallback(
        &source,
        &[],
        &["observed"],
        &["globalThis.savedCommand = command;"],
    );
}

#[test]
fn unsupported_record_and_callee_shapes_fall_back() {
    for (before_command, call, callback_members) in [
        // Record shapes.
        (
            "",
            "create({ get execute() { return () => later; } })",
            &[][..],
        ),
        ("", "create({ set execute(value) {} })", &[]),
        (
            "",
            "create(globalThis.saved = { execute: () => later })",
            &[],
        ),
        ("", "create((0, { execute: () => later }))", &[]),
        (
            "",
            "create({ nested: (globalThis.saved = { execute: () => later }) })",
            &[],
        ),
        ("", "create({ nested: (0, { execute: () => later }) })", &[]),
        ("", "create({ ...{ execute: () => later } })", &[]),
        ("", "create({ __proto__: { execute: () => later } })", &[]),
        (
            "const externalRecord = { execute: () => later };\n",
            "create(externalRecord)",
            &["externalRecord"],
        ),
        // Callee shapes.
        (
            "const factoryAlias = create;\n",
            "factoryAlias({ execute: () => later })",
            &["factoryAlias"],
        ),
        ("", "create.call(null, { execute: () => later })", &[]),
        ("", "create?.({ execute: () => later })", &[]),
        ("", "create?.call(null, { execute: () => later })", &[]),
        ("", "new create({ execute: () => later })", &[]),
    ] {
        eprintln!("call: {call}");
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            call,
            "",
            "console.log(command.run());",
        );
        assert_atomic_fallback(&source, callback_members, &[], &[]);
    }
}

#[test]
fn rebound_or_exposed_factory_bindings_fall_back() {
    let rebound = "create = function (record) { return new Holder(record, () => later); };\n";
    let exposed = "globalThis.savedFactory = create;\n";
    for (before_command, statement) in [(rebound, rebound), (exposed, exposed)] {
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            &format!("create({FRESH_RECORD})"),
            "",
            "console.log(command.run());",
        );
        assert_atomic_fallback(&source, &[], &[], &[statement]);
    }
}

#[test]
fn argument_side_effects_and_eager_tdz_reads_fall_back() {
    let side_effect = fixture_source(
        "",
        FACTORY,
        "",
        "create((console.log(\"argument\"), { execute: () => later }))",
        "",
        "console.log(command.run());",
    );
    assert_atomic_fallback(&side_effect, &[], &[], &[]);

    let tdz = fixture_source(
        "",
        FACTORY,
        "",
        "create({ execute: callback })",
        "const callback = () => later;\n",
        "console.log(command.run());",
    );
    assert_atomic_fallback(&tdz, &[], &["callback"], &[]);
}

#[test]
fn an_eager_provider_binding_read_is_not_erased_by_the_annotation() {
    let source = fixture_source(
        "",
        "function create(record) { const snapshot = later; return new Holder(record, () => snapshot); }\n",
        "",
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run());",
    );
    assert_atomic_fallback(&source, &[], &[], &[]);
}

#[test]
fn provider_class_binding_after_factory_call_keeps_its_tdz_order() {
    let source = format!(
        "{PREFIX}{COMPUTED_MUTATOR}{FLAGS}{FACTORY}const command = create({{ execute: () => later }});\n{HOLDER}{LATER}console.log(command.run());\n"
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &members(
                        "create",
                        &["Flags", "mutate", "create", "prefix", "command"],
                    ),
                ),
                logical_module("values/later", &binding_members(["Holder", "later"])),
            ],
        ),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn provider_effects_keep_source_order_without_factory_effect_suppression() {
    let factory = "function create(record) { console.log(\"factory\"); return new Holder(record, () => 21); }\n";
    let source = fixture_source(
        "console.log(\"before\");\n",
        factory,
        "",
        &format!("create({FRESH_RECORD})"),
        "console.log(\"after factory\");\n",
        "console.log(command.run(), command.weight);",
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&[]),
            &[
                "console.log(\"before\");",
                "console.log(\"after factory\");",
                "console.log(command.run(), command.weight);",
            ],
        )],
    ));

    assert_preserved_entry_output(&fixture, "before\nfactory\nafter factory\n148 0\n");
}

#[test]
fn constructor_effects_are_not_removed_by_the_factory_call_certificate() {
    let holder = r#"class Holder {
  constructor(record) {
    console.log("constructor");
    this.record = record;
    this.label = label;
  }
}
"#;
    let source = factory_effect_order_source("create({ execute: () => later })", holder);
    expect_atomic_conflict_rejection(
        factory_effect_order_opts(&source, true),
        &["callbacks/wrapped", "values/label", "values/later"],
        &["sequenced"],
    );
}
