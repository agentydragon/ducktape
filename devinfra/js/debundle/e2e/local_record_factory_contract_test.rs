//! The record-factory assertion supplies only the prototype premises for the
//! existing checked private-constructor proof.

use debundle_e2e_support::*;

const HOLDER: &str = r#"class Holder {
  constructor(record, callback) {
    this.record = record;
    this.callback = callback;
    this.weight = record.weight ?? 0;
  }
  run() {
    return this.record.execute() + this.record.nested.callback() + this.record.items[0].callback() + this.callback() + this.record.mode;
  }
}
"#;

const FACTORY: &str = "function create(record) { return new Holder(record, () => later); }\n";
const COMPUTED_MUTATOR: &str = "function mutate(target, key, value) { target[key] = value; }\n";
const PREFIX: &str = "const prefix = new globalThis.Error();\n";
const LATER: &str = "const later = 42;\n";

const FRESH_RECORD: &str = r#"{
  execute: () => later,
  nested: { callback: function () { return later; }, enabled: true },
  items: [{ callback: () => later }],
  values: [1, "ready", null, false],
  mode: Flags.FIRST,
}"#;

const FRESH_RECORD_WITH_NAME: &str = r#"{
  name: label,
  execute: () => later,
  nested: { callback: function () { return later; }, enabled: true },
  items: [{ callback: () => later }],
  values: [1, "ready", null, false],
  mode: Flags.FIRST,
}"#;

const FLAGS: &str = r#"var Flags = ((values) => ((values[(values.FIRST = 1)] = "FIRST"), (values[(values.SECOND = 2)] = "SECOND"), values))(Flags || {});
"#;

fn fixture_source(
    prefix: &str,
    factory: &str,
    before_command: &str,
    call: &str,
    before_later: &str,
    after_later: &str,
) -> String {
    format!(
        "{prefix}{COMPUTED_MUTATOR}{FLAGS}{HOLDER}{factory}{PREFIX}{before_command}const command = {call};\n{before_later}{LATER}{after_later}\n"
    )
}

fn fixture_opts<'a>(
    source: &'a str,
    annotated: bool,
    callback_members: &[&'static str],
    later_members: &[&'static str],
    callback_statements: &[&str],
) -> FixtureOpts<'a> {
    let create = if annotated {
        Member::new("create").with_retained_record_factory()
    } else {
        Member::new("create")
    };
    let mut callback_bindings = vec![
        Member::new("Flags"),
        Member::new("mutate"),
        Member::new("Holder"),
        create,
        Member::new("prefix"),
        Member::new("command"),
    ];
    let mut label_bindings = Vec::new();
    for name in callback_members {
        if *name == "label" {
            label_bindings.push(Member::new(name));
        } else {
            callback_bindings.push(Member::new(name));
        }
    }
    let callbacks = if callback_statements.is_empty() {
        logical_module("callbacks/wrapped", &callback_bindings)
    } else {
        logical_module_with_anon("callbacks/wrapped", &callback_bindings, callback_statements)
    };
    let mut later_bindings = vec![Member::new("later")];
    later_bindings.extend(later_members.iter().map(|name| Member::new(name)));
    let mut modules = vec![callbacks];
    if !label_bindings.is_empty() {
        modules.push(logical_module("values/label", &label_bindings));
    }
    modules.push(logical_module("values/later", &later_bindings));
    FixtureOpts::new(source, modules)
}

fn assert_atomic_fallback(
    source: &str,
    callback_members: &[&'static str],
    later_members: &[&'static str],
    callback_statements: &[&str],
) {
    expect_atomic_conflict_rejection(
        fixture_opts(
            source,
            true,
            callback_members,
            later_members,
            callback_statements,
        ),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn annotation_admits_a_direct_fresh_record_to_the_checked_constructor_factory() {
    let source = fixture_source(
        "",
        FACTORY,
        "",
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    let fixture = run_fixture(fixture_opts(&source, true, &[], &[], &[]));

    assert_preserved_entry_output(&fixture, "169 0\n");
    let graph = fixture.dependency_graph();
    let uses: Vec<_> = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .flat_map(|report| &report.local_record_factory_uses)
        .collect();
    assert_eq!(uses.len(), 1);
    assert_eq!(uses[0].factory_binding, "create");
    assert!(
        uses[0]
            .prototype_properties
            .iter()
            .any(|property| property == "weight")
    );
    assert!(uses[0].source_location.is_some());
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["command"],
        &["later"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["class Holder", "function create", "const command"],
        &[],
    );
}

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
fn only_initialized_unreassigned_top_level_primitive_literals_are_closed_inputs() {
    let cases = [
        (
            "const label = { value: \"Example\" };\n",
            "",
            "create({ name: label, execute: () => later })",
            &["label"][..],
        ),
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
fn unsupported_record_shapes_fall_back() {
    for (before_command, call, callback_members) in [
        (
            "",
            "create({ get execute() { return () => later; } })",
            vec![],
        ),
        ("", "create({ set execute(value) {} })", vec![]),
        (
            "",
            "create(globalThis.saved = { execute: () => later })",
            vec![],
        ),
        ("", "create((0, { execute: () => later }))", vec![]),
        (
            "",
            "create({ nested: (globalThis.saved = { execute: () => later }) })",
            vec![],
        ),
        (
            "",
            "create({ nested: (0, { execute: () => later }) })",
            vec![],
        ),
        ("", "create({ ...{ execute: () => later } })", vec![]),
        (
            "",
            "create({ __proto__: { execute: () => later } })",
            vec![],
        ),
        (
            "const externalRecord = { execute: () => later };\n",
            "create(externalRecord)",
            vec!["externalRecord"],
        ),
    ] {
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            call,
            "",
            "console.log(command.run());",
        );
        assert_atomic_fallback(&source, &callback_members, &[], &[]);
    }
}

#[test]
fn unsupported_callee_shapes_fall_back() {
    for (before_command, call, callback_members) in [
        (
            "const factoryAlias = create;\n",
            "factoryAlias({ execute: () => later })",
            vec!["factoryAlias"],
        ),
        ("", "create.call(null, { execute: () => later })", vec![]),
        ("", "create?.({ execute: () => later })", vec![]),
        ("", "create?.call(null, { execute: () => later })", vec![]),
        ("", "new create({ execute: () => later })", vec![]),
    ] {
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            call,
            "",
            "console.log(command.run());",
        );
        assert_atomic_fallback(&source, &callback_members, &[], &[]);
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
                    &[
                        Member::new("Flags"),
                        Member::new("mutate"),
                        Member::new("create").with_retained_record_factory(),
                        Member::new("prefix"),
                        Member::new("command"),
                    ],
                ),
                logical_module(
                    "values/later",
                    &[Member::new("Holder"), Member::new("later")],
                ),
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
    let members = [
        Member::new("Flags"),
        Member::new("mutate"),
        Member::new("Holder"),
        Member::new("create").with_retained_record_factory(),
        Member::new("prefix"),
        Member::new("command"),
        Member::new("later"),
    ];
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &members,
            &[
                "console.log(\"before\");",
                "console.log(\"after factory\");",
                "console.log(command.run(), command.weight);",
            ],
        )],
    ));

    assert_preserved_entry_output(&fixture, "before\nfactory\nafter factory\n148 0\n");
}
