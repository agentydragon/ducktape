//! The record-factory assertion supplies only the prototype premises for the
//! existing checked private-constructor proof.

use analysis::execution_evidence::{ExecutionOperation, ExecutionPrecisionLossReason};
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
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
const PREFIX: &str = "const prefix = new globalThis.Date();\n";
const LATER: &str = "const later = 42;\n";

#[test]
fn fresh_local_class_own_field_read_has_pure_value() {
    let source = r#"class Box {
  constructor(record) { this.record = record; }
  value = () => this.record.callback();
}

function make(record) { return new Box(record); }
console.log('early');
const first = make({ callback: () => 1 });
const readers = [first.value];
const later = readers;
console.log(later.length, later[0]());
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module(
                "factory",
                &[
                    Member::new("Box"),
                    Member::new("make").with_retained_record_factory(),
                ],
            ),
            logical_module("commands", &[Member::new("first")]),
            logical_module("readers", &[Member::new("readers")]),
            logical_module_with_anon(
                "early_late",
                &[Member::new("later")],
                &["console.log('early');"],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "early\n1 1\n");
    assert_eq!(fixture.translation_validation()["accepts"], true);
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/readers.js",
        &["readers"],
        &[],
    );
    let graph = fixture.dependency_graph();
    let readers = node_declaring(&graph, "readers");
    assert!(readers.purity.is_pure(), "{:?}", readers.purity);
}

#[test]
fn private_own_field_purity_refuses_mutation_getters_escape_and_skipped_reads() {
    let cases = [
        (
            "const changed = first.value = () => 8;",
            "[first.value]",
            "8\n",
        ),
        (
            "const changed = Object.defineProperty(first, 'value', { get() { return () => 9; } });",
            "[first.value]",
            "9\n",
        ),
        (
            "const changed = delete first.value;",
            "[first.value]",
            "9\n",
        ),
        (
            "const changed = (globalThis.saved = first, globalThis.saved.value = () => 8);",
            "[first.value]",
            "8\n",
        ),
        (
            "const changed = (() => { const invoke = f => f(first); invoke(x => { delete x.value; }); return 1; })();",
            "[first.value]",
            "9\n",
        ),
        (
            "const changed = Date.now() < 0;",
            "changed ? [first.value] : []",
            "0\n",
        ),
        (
            "const changed = Date.now() < 0;",
            "changed && [first.value]",
            "0\n",
        ),
        (
            "let changed = Date.now() < 0;",
            "changed &&= [first.value]",
            "0\n",
        ),
        ("const changed = null;", "changed?.[first.value]", "0\n"),
        (
            "let changed;",
            "({ changed = first.value } = { changed: [] })",
            "0\n",
        ),
    ];
    for (prefix, read, expected) in cases {
        let source = format!(
            r#"class Box {{
  constructor(record) {{ this.record = record; }}
  get value() {{ return () => 9; }}
  value = () => this.record.callback();
}}
function make(record) {{ return new Box(record); }}
const first = make({{ callback: () => 1 }});
{prefix}
const readers = {read};
console.log(typeof readers?.[0] === 'function' ? readers[0]() : 0);
"#
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[
                        Member::new("Box"),
                        Member::new("make").with_retained_record_factory(),
                        Member::new("first"),
                        Member::new("changed"),
                        Member::new("readers"),
                    ],
                    &[],
                )],
            )
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, expected);
        let graph = fixture.dependency_graph();
        let readers = node_declaring(&graph, "readers");
        assert!(
            !readers.purity.is_pure(),
            "{prefix} / {read}: {:?}",
            readers.purity
        );
    }
}
#[test]
fn reading_a_constructed_own_field_does_not_publish_an_unrelated_getter_receiver() {
    let source = r#"class Holder {
  constructor(record) { this.record = record; }
  get unrelated() { return this.record.callback(); }
  search = () => this.record.callback();
}

function create(record) { return new Holder(record); }
const first = create({ callback: () => 1 });
const searches = [first.search];
const second = create({ callback: () => 2 });
console.log(searches[0](), second.search());
"#;
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "factory",
                    &[
                        Member::new("Holder"),
                        Member::new("create").with_retained_record_factory(),
                    ],
                ),
                logical_module("commands", &[Member::new("first"), Member::new("second")]),
                logical_module("searches", &[Member::new("searches")]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "1 2\n");
    let graph = fixture.dependency_graph();
    let second = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "second")
        })
        .expect("second source unit");
    let execution = second
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert!(execution.complete, "{execution:#?}");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        1,
        "{execution:#?}"
    );
    let searches = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "searches")
        })
        .expect("searches source unit");
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.edge_kind == analysis_model::DepKind::Sequenced
                && edge.source == second.id
                && edge.target == searches.id
        }),
        "the private own-field read must not sequence the later factory: {:#?}",
        graph.edges
    );
}

#[test]
fn replaced_own_field_keeps_getter_receiver_publication() {
    for replace in [
        "Object.defineProperty(first, 'search', { get() { globalThis.retained = this; return () => 1; } });",
        "delete first.search;",
        "delete ({ first }).first.search;",
        "delete ({ [first]: 1, target: first }).target.search;",
        "delete (() => first)().search;",
        "globalThis.retained = first, delete globalThis.retained.search;",
    ] {
        let source = format!(
            r#"class Holder {{
  constructor(record) {{ this.record = record; }}
  get search() {{ globalThis.retained = this; return () => 1; }}
  search = () => this.record.callback();
}}
function create(record) {{ return new Holder(record); }}
const first = create({{ callback: () => 1 }});
{replace}
const searches = [first.search];
const second = create({{ callback: () => 2 }});
console.log(searches[0](), second.search());
"#
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[
                        Member::new("Holder"),
                        Member::new("create").with_retained_record_factory(),
                        Member::new("first"),
                        Member::new("searches"),
                        Member::new("second"),
                    ],
                    &[replace],
                )],
            )
            .with_dataflow_aware_s_chain(),
        );
        assert_preserved_entry_output(&fixture, "1 2\n");
        let graph = fixture.dependency_graph();
        let second = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "second")
            })
            .expect("second source unit");
        let execution = second
            .synchronous_execution
            .as_ref()
            .expect("execution report");
        assert!(
            !execution.local_record_factory_rejections.is_empty(),
            "{replace}: {execution:#?}"
        );
    }
}

#[test]
fn reading_an_inherited_getter_still_publishes_its_receiver() {
    let source = r#"class Holder {
  constructor(record) { this.record = record; }
  get search() { globalThis.retained = this; return () => this.record.callback(); }
}
function create(record) { return new Holder(record); }
const first = create({ callback: () => 1 });
const searches = [first.search];
const second = create({ callback: () => 2 });
console.log(searches[0](), second.search());
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[
                Member::new("Holder"),
                Member::new("create").with_retained_record_factory(),
                Member::new("first"),
                Member::new("searches"),
                Member::new("second"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 2\n");
    let graph = fixture.dependency_graph();
    let second = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "second")
        })
        .expect("second source unit");
    let execution = second
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert!(
        !execution.local_record_factory_rejections.is_empty(),
        "{execution:#?}"
    );
}

#[test]
fn a_single_lexical_read_inside_a_function_can_run_after_its_field_exposes_this() {
    let source = r#"class Holder {
  constructor(record) { this.record = record; }
  get search() { globalThis.retained = this; return () => this; }
  search = () => this;
}
function create(record) { return new Holder(record); }
const first = create({ callback: () => 1 });
function read() { return first.search; }
const alias = read()();
delete alias.search;
const searches = [read()];
const second = create({ callback: () => 2 });
console.log(searches[0]() === alias, globalThis.retained === alias);
"#;
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("Holder"),
                    Member::new("create").with_retained_record_factory(),
                    Member::new("first"),
                    Member::new("read"),
                    Member::new("alias"),
                    Member::new("searches"),
                    Member::new("second"),
                ],
                &["delete alias.search"],
            )],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "true true\n");
    let graph = fixture.dependency_graph();
    let second = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "second")
        })
        .expect("second source unit");
    let execution = second
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert!(
        !execution.local_record_factory_rejections.is_empty(),
        "{execution:#?}"
    );
}

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

const EXPORTED_RECORD_PROVIDER: &str = r#"class Holder {
  constructor(record) { this.record = record; this.missing = record.missing; }
  run() { return this.record.execute(); }
}
function create(record) { return new Holder(record); }
const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 42;
})({});
const command = create({ execute: () => later });
const later = dynamic;
export { command };
"#;

const ACYCLIC_COMMAND_IMPORTER: &str = r#"import { command } from "./provider.js";
export const observed = command.run();
console.log(observed);
"#;

fn nested_wrapper_source(pre_factory: &str, factory: &str, wrapper: &str) -> String {
    let callbacks = vec!["() => 0"; 65].join(", ");
    format!(
        r#"const warmup = JSON.stringify([{callbacks}]);
const prefix = new globalThis.Date();
const maybe = globalThis.unknownObject || {{}};
maybe.record = 1;
class Policy {{}}
class Command {{
  constructor(record, policy) {{
    this.record = record;
    this.policy = policy;
    this.weight = record.weight ?? 0;
  }}
  run() {{ return this.record.execute(); }}
}}
{pre_factory}
{factory}
{wrapper}
const command = wrapper("x", () => later);
const later = command ? 7 : 0;
console.log(command.run());
export {{ command, later }};
"#
    )
}

const NESTED_FACTORY: &str = "function make(record) { return new Command(record, new Policy()); }";
const NESTED_WRAPPER: &str = "function wrapper(label, execute) { return make({ name: `Create ${label}`, execute, weight: 0 }); }";

#[test]
fn nested_record_factory_wrapper_preserves_a_split_cycle() {
    let source = nested_wrapper_source("", NESTED_FACTORY, NESTED_WRAPPER);
    let split = run_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "command",
                    &[
                        Member::new("warmup"),
                        Member::new("prefix"),
                        Member::new("maybe"),
                        Member::new("Policy"),
                        Member::new("Command"),
                        Member::new("make").with_retained_record_factory(),
                        Member::new("wrapper"),
                        Member::new("command"),
                    ],
                    &["maybe.record = 1;"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_eq!(split.translation_validation()["accepts"], true);
    assert_preserved_entry_output(&split, "7\n");

    let members = [
        Member::new("warmup"),
        Member::new("prefix"),
        Member::new("maybe"),
        Member::new("Policy"),
        Member::new("Command"),
        Member::new("make").with_retained_record_factory(),
        Member::new("wrapper"),
        Member::new("command"),
        Member::new("later"),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "all",
                &members,
                &["maybe.record = 1;"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
    let graph = fixture.dependency_graph();
    let command = node_declaring(&graph, "command");
    let later = node_declaring(&graph, "later");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert!(execution.complete, "{execution:#?}");
    assert!(
        execution.local_record_factory_uses.iter().any(|use_| {
            use_.factory_binding == "make"
                && use_
                    .prototype_properties
                    .iter()
                    .any(|property| property == "record")
        }),
        "{execution:#?}"
    );
    assert!(!graph.edges.iter().any(|edge| {
        edge.source == command.id && edge.target == later.id && edge.constrains_init_order
    }));
}

#[test]
fn literal_own_data_argument_preserves_a_nested_factory_split() {
    let enum_writes = (0..1)
        .map(|index| format!("(n[(n.feature{index} = {index})] = \"feature{index}\")"))
        .collect::<Vec<_>>()
        .join(", ");
    let source = nested_wrapper_source(
        "",
        NESTED_FACTORY,
        "function wrapper(label, execute, marker) { return make({ name: `Create ${label}`, execute, weight: 0, marker }); }",
    )
    .replace(
        "const warmup =",
        &format!("var $ = ((n) => ({enum_writes}, n))($ || {{}});\nconst ids = {{ day: \"SYS_T124\" }};\nconst warmup ="),
    )
    .replace(
        "const command = wrapper(\"x\", () => later);",
        "const flags = [{ id: ids.day, feature: $.feature0 }];\nconst command = wrapper(\"x\", () => later, ids.day);",
    )
    .replace(
        "console.log(command.run());",
        "console.log(command.run());\nglobalThis.consume?.($);",
    )
    .replace("export { command, later };", "export { command, later, ids };");
    let split = run_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "command",
                    &[
                        Member::new("warmup"),
                        Member::new("prefix"),
                        Member::new("maybe"),
                        Member::new("Policy"),
                        Member::new("Command"),
                        Member::new("make").with_retained_record_factory(),
                        Member::new("wrapper"),
                        Member::new("ids"),
                        Member::new("$"),
                        Member::new("flags"),
                        Member::new("command"),
                    ],
                    &["maybe.record = 1;"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_eq!(split.translation_validation()["accepts"], true);
    assert_preserved_entry_output(&split, "7\n");
    let command_module = format!("{}/modules/command.js", split.chunk_id);
    let later_module = format!("{}/modules/later.js", split.chunk_id);
    assert_module_source(
        &split.out_root,
        &command_module,
        &["feature0", "SYS_T124", "const flags", "const command"],
        &["const later"],
    );
    assert_module_source(
        &split.out_root,
        &later_module,
        &["const later"],
        &["const command"],
    );
    let graph = split.dependency_graph();
    let command = node_declaring(&graph, "command");
    let later = node_declaring(&graph, "later");
    let ids = node_declaring(&graph, "ids");
    let flags = node_declaring(&graph, "flags");
    let flags_execution = flags
        .synchronous_execution
        .as_ref()
        .expect("flags execution");
    assert!(flags_execution.complete, "{flags_execution:#?}");
    assert!(
        flags_execution.precision_losses.is_empty(),
        "{flags_execution:#?}"
    );
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert!(execution.complete, "{execution:#?}");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == command.id && edge.target == ids.id && edge.constrains_init_order
    }));
    assert!(!graph.edges.iter().any(|edge| {
        edge.source == command.id && edge.target == later.id && edge.constrains_init_order
    }));
}

#[test]
fn earlier_alias_changes_do_not_gain_a_current_prefix_own_data_receipt() {
    for (case, change, expected_type) in [
        ("direct_delete", "delete $.feature1;", "undefined"),
        ("alias_delete", "delete alias.feature1;", "undefined"),
        (
            "unknown_receiver_delete",
            "delete (globalThis.unknownReceiver || $).feature1;",
            "undefined",
        ),
        (
            "unknown_alias_delete",
            "delete (globalThis.unknownReceiver || alias).feature1;",
            "undefined",
        ),
        (
            "multiple_alias_delete",
            "delete (globalThis.choose ? $ : alias).feature1;",
            "undefined",
        ),
        (
            "reflect_delete",
            "Reflect.deleteProperty(alias, 'feature1');",
            "undefined",
        ),
        (
            "getter",
            "Object.defineProperty(alias, 'feature1', { get() { return 1; } });",
            "number",
        ),
        (
            "setter",
            "Object.defineProperty(alias, 'feature1', { get() { return 1; }, set(value) { globalThis.seen = value; } });",
            "number",
        ),
        (
            "direct_getter",
            "Object.defineProperty($, 'feature1', { get() { return 1; } });",
            "number",
        ),
        (
            "direct_prototype",
            "Object.setPrototypeOf($, { get feature1() { return 9; } });",
            "number",
        ),
        (
            "alias_prototype",
            "Object.setPrototypeOf(alias, { get feature1() { return 9; } });",
            "number",
        ),
        (
            "function_value",
            "alias.feature1 = () => later;",
            "function",
        ),
        (
            "dynamic_key",
            "alias[globalThis.unknownKey] = () => later;",
            "number",
        ),
        (
            "direct_dynamic_key",
            "$[globalThis.unknownKey] = () => later;",
            "number",
        ),
        ("early_escape", "globalThis.consume?.(alias);", "number"),
        (
            "reentrant_callback",
            "globalThis.consume?.(() => { Object.defineProperty(alias, 'feature1', { get() { return 1; } }); });",
            "number",
        ),
        (
            "unknown_identity",
            "$ = new Proxy($, { get(target, key) { return target[key]; } });",
            "number",
        ),
    ] {
        let source = nested_wrapper_source(
            "",
            NESTED_FACTORY,
            "function wrapper(label, execute, marker) { return make({ name: `Create ${label}`, execute, weight: 0, marker }); }",
        )
        .replace(
            "const warmup =",
            "var $ = ((n) => ((n[(n.feature1 = 1)] = \"feature1\"), n))($ || {});\nconst ids = { day: \"SYS_T124\" };\nconst warmup =",
        )
        .replace(
            "const command = wrapper(\"x\", () => later);",
            &format!("const alias = $;\n{change}\nconst flags = [{{ id: ids.day, feature: $.feature1 }}];\nconst command = wrapper(\"x\", () => later, ids.day);"),
        )
        .replace(
            "console.log(command.run());",
            "console.log(command.run(), typeof flags[0].feature);",
        )
        .replace("export { command, later };", "export { command, later, ids };");
        if case == "alias_delete" {
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    &source,
                    vec![
                        logical_module_with_anon(
                            "early",
                            &[
                                Member::new("warmup"),
                                Member::new("prefix"),
                                Member::new("maybe"),
                                Member::new("Policy"),
                                Member::new("Command"),
                                Member::new("make").with_retained_record_factory(),
                                Member::new("wrapper"),
                                Member::new("ids"),
                                Member::new("$"),
                                Member::new("alias"),
                                Member::new("flags"),
                                Member::new("command"),
                            ],
                            &["maybe.record = 1;", change],
                        ),
                        logical_module("later", &[Member::new("later")]),
                    ],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
                &["early", "later"],
                &[],
            );
        }
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[
                        Member::new("warmup"),
                        Member::new("prefix"),
                        Member::new("maybe"),
                        Member::new("Policy"),
                        Member::new("Command"),
                        Member::new("make").with_retained_record_factory(),
                        Member::new("wrapper"),
                        Member::new("ids"),
                        Member::new("$"),
                        Member::new("alias"),
                        Member::new("flags"),
                        Member::new("command"),
                        Member::new("later"),
                    ],
                    &["maybe.record = 1;", change],
                )],
            )
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, &format!("7 {expected_type}\n"));
        let graph = fixture.dependency_graph();
        let flags = node_declaring(&graph, "flags");
        let execution = flags
            .synchronous_execution
            .as_ref()
            .expect("flags execution");
        assert!(!execution.complete, "{case}: {execution:#?}");
        assert!(
            execution.precision_losses.iter().any(|loss| matches!(
                &loss.reason,
                ExecutionPrecisionLossReason::UnsupportedOperation {
                    operation: ExecutionOperation::MemberRead,
                    ..
                }
            )),
            "{case}: {execution:#?}"
        );
    }
}

#[test]
fn same_owner_delete_before_enum_member_read_refuses_the_receipt() {
    let source = nested_wrapper_source(
        "",
        NESTED_FACTORY,
        "function wrapper(label, execute, marker) { return make({ name: `Create ${label}`, execute, weight: 0, marker }); }",
    )
    .replace(
        "const warmup =",
        "var $ = ((n) => ((n[(n.feature1 = 1)] = 'feature1'), n))($ || {});\nconst ids = { day: 'SYS_T124' };\nconst warmup =",
    )
    .replace(
        "const command = wrapper(\"x\", () => later);",
        "const flags = [(delete $.feature1, { id: ids.day, feature: $.feature1 })];\nconst command = wrapper(\"x\", () => later, ids.day);",
    )
    .replace(
        "console.log(command.run());",
        "console.log(command.run(), typeof flags[0].feature);",
    )
    .replace("export { command, later };", "export { command, later, ids };");
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("warmup"),
                    Member::new("prefix"),
                    Member::new("maybe"),
                    Member::new("Policy"),
                    Member::new("Command"),
                    Member::new("make").with_retained_record_factory(),
                    Member::new("wrapper"),
                    Member::new("ids"),
                    Member::new("$"),
                    Member::new("flags"),
                    Member::new("command"),
                    Member::new("later"),
                ],
                &["maybe.record = 1;"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7 undefined\n");
    let graph = fixture.dependency_graph();
    let execution = node_declaring(&graph, "flags")
        .synchronous_execution
        .as_ref()
        .expect("flags execution");
    assert!(!execution.complete, "{execution:#?}");
    assert!(execution.inference_uses.is_empty(), "{execution:#?}");
    assert!(
        execution.precision_losses.iter().any(|loss| matches!(
            &loss.reason,
            ExecutionPrecisionLossReason::UnsupportedOperation {
                operation: ExecutionOperation::PropertyWrite,
                ..
            }
        )),
        "{execution:#?}"
    );
}

#[test]
fn unknown_computed_enum_key_cannot_gain_a_current_prefix_own_data_receipt() {
    // The unknown key could be "__proto__" or an inherited setter name.
    // The TS enum recognizer excludes this initializer, even when execution
    // currently sees only primitive payloads and a concrete member slot.
    let source = nested_wrapper_source(
        "",
        NESTED_FACTORY,
        "function wrapper(label, execute, marker) { return make({ name: `Create ${label}`, execute, weight: 0, marker }); }",
    )
    .replace(
        "const warmup =",
        "var $ = ((n) => ((n[(n.feature1 = 1)] = \"feature1\"), (n[globalThis.unknownKey || \"__proto__\"] = 2), n))($ || {});\nconst ids = { day: \"SYS_T124\" };\nconst warmup =",
    )
    .replace(
        "const command = wrapper(\"x\", () => later);",
        "const flags = [{ id: ids.day, feature: $.feature1 }];\nconst command = wrapper(\"x\", () => later, ids.day);",
    )
    .replace("export { command, later };", "export { command, later, ids };");
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("warmup"),
                    Member::new("prefix"),
                    Member::new("maybe"),
                    Member::new("Policy"),
                    Member::new("Command"),
                    Member::new("make").with_retained_record_factory(),
                    Member::new("wrapper"),
                    Member::new("ids"),
                    Member::new("$"),
                    Member::new("flags"),
                    Member::new("command"),
                    Member::new("later"),
                ],
                &["maybe.record = 1;"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
    let graph = fixture.dependency_graph();
    let flags = node_declaring(&graph, "flags");
    let execution = flags
        .synchronous_execution
        .as_ref()
        .expect("flags execution");
    assert!(!execution.complete, "{execution:#?}");
    assert!(
        execution.precision_losses.iter().any(|loss| matches!(
            &loss.reason,
            ExecutionPrecisionLossReason::UnsupportedOperation {
                operation: ExecutionOperation::MemberRead,
                ..
            }
        )),
        "{execution:#?}"
    );
}

#[test]
fn conditional_rebound_or_reentrant_enum_initializers_keep_the_member_read_fallback() {
    for (case, initializer) in [
        (
            "conditional",
            "if (true) { var $ = ((n) => ((n[(n.feature1 = 1)] = 'feature1'), n))($ || {}); }",
        ),
        (
            "same_owner_rebind",
            "var $ = ((n) => ((n[(n.feature1 = 1)] = 'feature1'), n))($ || {}), $ = new Proxy($, { get(target, key) { return target[key]; } });",
        ),
        (
            "initializer_reentry",
            "var $ = ((n) => (globalThis.maybeCall?.(n), (n[(n.feature1 = 1)] = 'feature1'), n))($ || {});",
        ),
    ] {
        let source = nested_wrapper_source(
            "",
            NESTED_FACTORY,
            "function wrapper(label, execute, marker) { return make({ name: `Create ${label}`, execute, weight: 0, marker }); }",
        )
        .replace(
            "const warmup =",
            &format!("{initializer}\nconst ids = {{ day: 'SYS_T124' }};\nconst warmup ="),
        )
        .replace(
            "const command = wrapper(\"x\", () => later);",
            "const flags = [{ id: ids.day, feature: $.feature1 }];\nconst command = wrapper(\"x\", () => later, ids.day);",
        )
        .replace("export { command, later };", "export { command, later, ids };");
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[
                        Member::new("warmup"),
                        Member::new("prefix"),
                        Member::new("maybe"),
                        Member::new("Policy"),
                        Member::new("Command"),
                        Member::new("make").with_retained_record_factory(),
                        Member::new("wrapper"),
                        Member::new("ids"),
                        Member::new("$"),
                        Member::new("flags"),
                        Member::new("command"),
                        Member::new("later"),
                    ],
                    &["maybe.record = 1;"],
                )],
            )
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, "7\n");
        let graph = fixture.dependency_graph();
        let execution = node_declaring(&graph, "flags")
            .synchronous_execution
            .as_ref()
            .expect("flags execution");
        assert!(!execution.complete, "{case}: {execution:#?}");
        assert!(
            execution.precision_losses.iter().any(|loss| matches!(
                &loss.reason,
                ExecutionPrecisionLossReason::UnsupportedOperation {
                    operation: ExecutionOperation::MemberRead,
                    ..
                }
            )),
            "{case}: {execution:#?}"
        );
    }
}

#[test]
fn checked_enum_member_read_before_its_var_initializer_keeps_the_throw() {
    let source = r#"const flags = [$.feature1];
var $ = ((n) => ((n[(n.feature1 = 1)] = "feature1"), n))($ || {});
console.log(flags[0]);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_error(&fixture, "TypeError");
    let graph = fixture.dependency_graph();
    let execution = node_declaring(&graph, "flags")
        .synchronous_execution
        .as_ref()
        .expect("flags execution");
    assert!(
        execution
            .heap_frontier
            .as_ref()
            .is_none_or(|frontier| frontier.non_assumed_heap_accesses.reads.count == 0),
        "a pre-initializer throw cannot consume an own-slot receipt: {execution:#?}"
    );
}

#[test]
fn inherited_setter_before_enum_initialization_cannot_certify_an_own_slot() {
    let setter = "Object.defineProperty(Object.prototype, 'feature1', { set(value) { globalThis.setterSaw = later; }, get() { globalThis.getterSaw = later; return -1; } });";
    let source = nested_wrapper_source(
        "",
        NESTED_FACTORY,
        "function wrapper(label, execute, marker) { return make({ name: `Create ${label}`, execute, weight: 0, marker }); }",
    )
    .replace(
        "const warmup =",
        &format!("{setter}\nvar $ = ((n) => ((n[(n.feature1 = 1)] = 'feature1'), n))($ || {{}});\nconst ids = {{ day: 'SYS_T124' }};\nconst warmup ="),
    )
    .replace(
        "const command = wrapper(\"x\", () => later);",
        "const flags = [{ id: ids.day, feature: $.feature1 }];\nconst command = wrapper(\"x\", () => later, ids.day);",
    )
    .replace(
        "console.log(command.run());",
        "console.log(command.run(), flags[0].feature, globalThis.setterSaw, globalThis.getterSaw);",
    )
    .replace("const later = command ? 7 : 0;", "var later = command ? 7 : 0;")
    .replace("export { command, later };", "export { command, later, ids };");
    let early_members = || {
        vec![
            Member::new("warmup"),
            Member::new("prefix"),
            Member::new("maybe"),
            Member::new("Policy"),
            Member::new("Command"),
            Member::new("make").with_retained_record_factory(),
            Member::new("wrapper"),
            Member::new("ids"),
            Member::new("$"),
            Member::new("flags"),
            Member::new("command"),
        ]
    };
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon("early", &early_members(), &["maybe.record = 1;", setter]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["early", "later"],
        &[],
    );
    let mut all_members = early_members();
    all_members.push(Member::new("later"));
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "all",
                &all_members,
                &["maybe.record = 1;", setter],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7 -1 undefined undefined\n");
    let graph = fixture.dependency_graph();
    let execution = node_declaring(&graph, "flags")
        .synchronous_execution
        .as_ref()
        .expect("flags execution");
    assert!(
        execution
            .heap_frontier
            .as_ref()
            .is_none_or(|frontier| frontier.non_assumed_heap_accesses.reads.count == 0),
        "an inherited getter cannot use an own-slot receipt: {execution:#?}"
    );
    let later = node_declaring(&graph, "later");
    let accessor = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 0)
        .expect("prototype accessor definition");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == accessor.id
                && edge.target == later.id
                && edge.binding.as_deref() == Some("later")
                && edge.constrains_init_order
        }),
        "the accessor bodies' lexical reads must constrain initialization order: {:#?}",
        graph.edges
    );
    assert!(
        graph.edges.iter().any(|edge| {
            matches!(
                edge.sequenced_cause.as_ref(),
                Some(analysis::graph::SequencedCause::BindingTimeline {
                    binding,
                    target: analysis::graph::EffectProvenance::Fallback {
                        reason: analysis::execution_evidence::AssumedEffectReason::PossibleSetter,
                    },
                    ..
                }) if binding == "later"
            ) && edge.constrains_init_order
        }),
        "the inherited setter path must preserve its read-before-write timeline: {:#?}",
        graph.edges
    );
}

fn assert_nested_wrapper_falls_back(source: &str, extra_anon: &[&str]) {
    let mut anon = vec!["maybe.record = 1;"];
    anon.extend_from_slice(extra_anon);
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("warmup"),
                    Member::new("prefix"),
                    Member::new("maybe"),
                    Member::new("Policy"),
                    Member::new("Command"),
                    Member::new("make").with_retained_record_factory(),
                    Member::new("wrapper"),
                    Member::new("command"),
                    Member::new("later"),
                ],
                &anon,
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
    let graph = fixture.dependency_graph();
    let command = node_declaring(&graph, "command");
    let later = node_declaring(&graph, "later");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert!(!execution.complete, "{execution:#?}");
    assert!(
        execution.local_record_factory_uses.is_empty(),
        "{execution:#?}"
    );
    assert!(
        execution.precision_losses.iter().any(|loss| matches!(
            &loss.reason,
            ExecutionPrecisionLossReason::UnsupportedConstruction { .. }
        )),
        "{execution:#?}"
    );
    assert!(graph.edges.iter().any(|edge| {
        edge.source == command.id && edge.target == later.id && edge.constrains_init_order
    }));
}

#[test]
fn nested_record_factory_refuses_effects_and_unchecked_targets() {
    let mutation = nested_wrapper_source(
        "",
        "function make(record) { globalThis.side = 1; return new Command(record, new Policy()); }",
        NESTED_WRAPPER,
    );
    assert_nested_wrapper_falls_back(&mutation, &[]);

    let conditional_body = nested_wrapper_source(
        "",
        "function make(record) { if (globalThis.flag) return new Command(record, new Policy()); return new Command(record, new Policy()); }",
        NESTED_WRAPPER,
    );
    assert_nested_wrapper_falls_back(&conditional_body, &[]);

    let setter_statement = "Object.defineProperty(Command.prototype, 'record', { set(value) { this._record = value; }, get() { return this._record; } });";
    let setter = nested_wrapper_source(setter_statement, NESTED_FACTORY, NESTED_WRAPPER);
    assert_nested_wrapper_falls_back(&setter, &[setter_statement]);

    let reentry = nested_wrapper_source(
        "",
        NESTED_FACTORY,
        "function wrapper(label, execute) { return make({ name: label, execute: globalThis.read ? globalThis.read(execute) : execute, weight: 0 }); }",
    );
    assert_nested_wrapper_falls_back(&reentry, &[]);

    let conditional_target = nested_wrapper_source(
        "",
        NESTED_FACTORY,
        "function wrapper(label, execute) { const selected = globalThis.flag ? make : make; return selected({ name: label, execute, weight: 0 }); }",
    );
    assert_nested_wrapper_falls_back(&conditional_target, &[]);

    let unknown_template = nested_wrapper_source(
        "",
        NESTED_FACTORY,
        "function wrapper(label, execute) { return make({ name: `Create ${globalThis.label}`, execute, weight: 0 }); }",
    );
    assert_nested_wrapper_falls_back(&unknown_template, &[]);
}

#[test]
fn nested_record_factory_reports_each_reused_class_premise() {
    let source = r#"const maybe = globalThis.unknownObject || {};
maybe.record = 1;
class Command {
  constructor(record) { this.record = record; }
  run() { return this.record.execute(); }
}
function make(record) { return new Command(record); }
function first(execute) { return make({ execute }); }
function second(execute) { return make({ execute }); }
const pair = [first(() => later), second(() => later)];
const later = pair ? 7 : 0;
console.log(pair[0].run() + pair[1].run());
export { pair, later };
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("maybe"),
                Member::new("Command"),
                Member::new("make").with_retained_record_factory(),
                Member::new("first"),
                Member::new("second"),
                Member::new("pair"),
                Member::new("later"),
            ],
            &["maybe.record = 1;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "14\n");
    let graph = fixture.dependency_graph();
    let pair = node_declaring(&graph, "pair");
    let execution = pair
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        2,
        "{execution:#?}"
    );
    assert!(
        execution.local_record_factory_uses.iter().all(|use_| {
            use_.factory_binding == "make"
                && use_
                    .prototype_properties
                    .iter()
                    .any(|property| property == "record")
        }),
        "{execution:#?}"
    );
}

#[test]
fn independent_retained_arrays_do_not_create_a_constructor_heap_frontier() {
    let source = r#"class Empty {}
class Holder {
  constructor(record, checker) { this.record = record; this.checker = checker; this.size = record.size ?? 0; }
  size;
  run() { return this.record.callback(); }
}
function create(record) { return new Holder(record, new Empty()); }
const shared = [0];
const first = create({ size: 1, items: [1], callback: () => shared.push(1) });
const second = create({ size: 2, items: [2], callback: () => shared.push(2) });
console.log(first.size, second.size, first.run(), second.run(), shared.join(','));
export { first, second, shared };
"#;
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "framework",
                    &[
                        Member::new("Empty"),
                        Member::new("Holder"),
                        Member::new("create").with_retained_record_factory(),
                    ],
                ),
                logical_module("first", &[Member::new("first")]),
                logical_module("second", &[Member::new("second")]),
                logical_module("shared", &[Member::new("shared")]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "1 2 2 3 0,1,2\n");
    let graph = fixture.dependency_graph();
    let ordinal = |binding: &str| {
        graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .expect("named source unit")
            .id
            .clone()
    };
    for result in ["first", "second"] {
        let heap = graph
            .nodes
            .iter()
            .find(|node| node.id == ordinal(result))
            .and_then(|node| node.synchronous_execution.as_ref())
            .and_then(|execution| execution.heap_frontier.as_ref())
            .expect("literal allocations keep heap evidence");
        assert!(!heap.own_at_init.opaque, "{result}: {heap:#?}");
        assert_eq!(heap.own_at_init.reads.count, 0, "{result}: {heap:#?}");
        assert_eq!(heap.own_at_init.writes.count, 0, "{result}: {heap:#?}");
        assert!(
            heap.own_at_init.allocations.count > 0,
            "{result}: {heap:#?}"
        );
        assert!(
            !heap.composed_synchronous_calls.opaque,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.composed_synchronous_calls.reads.count, 0,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.composed_synchronous_calls.writes.count, 0,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.non_assumed_heap_accesses.reads.count, 0,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.non_assumed_heap_accesses.writes.count, 0,
            "{result}: {heap:#?}"
        );
        assert!(
            !graph
                .edges
                .iter()
                .any(|edge| edge.edge_kind == analysis_model::DepKind::Sequenced
                    && edge.source == ordinal(result)
                    && edge.target == ordinal("shared")),
            "retaining a callback is not a read or write of the captured array: {:#?}",
            graph.edges
        );
    }
}

#[test]
fn argument_mutation_keeps_its_heap_edge_beside_a_checked_call() {
    let source = r#"class Holder {
  constructor(record) { this.items = record.items; }
}
function create(record) { return new Holder(record); }
const shared = [0];
function mutateSharedArray() { shared.push(1); return [1]; }
const first = create({ items: mutateSharedArray() });
const second = create({ items: [2] });
console.log(shared.join(','), first.items[0], second.items[0]);
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("Holder"),
                Member::new("create").with_retained_record_factory(),
                Member::new("shared"),
                Member::new("mutateSharedArray"),
                Member::new("first"),
                Member::new("second"),
            ],
            &["console.log(shared.join(','), first.items[0], second.items[0]);"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "0,1 1 2\n");
    let graph = fixture.dependency_graph();
    let node = |binding: &str| {
        graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .expect("named source unit")
    };
    let first_heap = node("first")
        .synchronous_execution
        .as_ref()
        .and_then(|execution| execution.heap_frontier.as_ref())
        .expect("the nested call retains its heap effects");
    assert!(first_heap.own_at_init.opaque, "{first_heap:#?}");
    assert!(
        first_heap.composed_synchronous_calls.writes.count > 0,
        "{first_heap:#?}"
    );
    assert!(
        first_heap.non_assumed_heap_accesses.writes.count > 0,
        "{first_heap:#?}"
    );
    assert!(
        graph
            .edges
            .iter()
            .any(|edge| edge.edge_kind == analysis_model::DepKind::Sequenced
                && edge.source == node("first").id
                && edge.target == node("shared").id),
        "the nested mutation must remain ordered after the shared array: {:#?}",
        graph.edges
    );
    assert!(
        !graph
            .edges
            .iter()
            .any(|edge| edge.edge_kind == analysis_model::DepKind::Sequenced
                && edge.source == node("second").id
                && edge.target == node("shared").id),
        "the independent checked call must not inherit the first call's heap effects: {:#?}",
        graph.edges
    );
}

fn exported_record_factory_modules(callback_statements: &[&str]) -> Vec<LogicalModuleEntry> {
    let callback_bindings = [
        Member::new("Holder"),
        Member::new("create").with_retained_record_factory(),
        Member::new("command"),
    ];
    let callbacks = if callback_statements.is_empty() {
        logical_module("callbacks/wrapped", &callback_bindings)
    } else {
        logical_module_with_anon("callbacks/wrapped", &callback_bindings, callback_statements)
    };
    vec![
        callbacks,
        logical_module(
            "values/dynamic",
            &[Member::new("prefix"), Member::new("dynamic")],
        ),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

fn run_exported_record_factory(source: &str, modules: Vec<LogicalModuleEntry>) -> Fixture {
    run_fixture(
        FixtureOpts::new(ACYCLIC_COMMAND_IMPORTER, vec![])
            .with_chunk_id("static/consumer")
            .with_extra_chunks(&[("static/provider", source)])
            .with_extra_chunk_logical_modules(&[("static/provider", modules)]),
    )
}

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

fn factory_effect_order_source(command: &str, holder: &str) -> String {
    format!(
        "function mutate(target, key, value) {{ target[key] = value; }}\n{holder}function create(record) {{ return new Holder(record); }}\nconst label = \"Example\";\nconst command = {command};\nconst commandAgain = {command};\nconst barrier = console.log(\"barrier\");\nconst later = 42;\nconsole.log(command.label, command.record.execute(), commandAgain.label, commandAgain.record.execute());\n"
    )
}

fn factory_effect_order_opts<'a>(source: &'a str, annotated: bool) -> FixtureOpts<'a> {
    let create = if annotated {
        Member::new("create").with_retained_record_factory()
    } else {
        Member::new("create")
    };
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "callbacks/wrapped",
                &[
                    Member::new("mutate"),
                    Member::new("Holder"),
                    create,
                    Member::new("command"),
                    Member::new("commandAgain"),
                ],
            ),
            logical_module(
                "values/label",
                &[Member::new("label"), Member::new("barrier")],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
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

fn local_factory_result_rejection(
    source: &str,
    anonymous_statements: &[&str],
) -> (
    analysis::reports::schema::RecordFactoryRejectionReason,
    Option<usize>,
) {
    let members = vec![
        Member::new("Flags"),
        Member::new("mutate"),
        Member::new("Holder"),
        Member::new("create").with_retained_record_factory(),
        Member::new("prefix"),
        Member::new("command"),
        Member::new("later"),
    ];
    let all = if anonymous_statements.is_empty() {
        logical_module("all", &members)
    } else {
        logical_module_with_anon("all", &members, anonymous_statements)
    };
    let fixture = run_fixture(FixtureOpts::new(source, vec![all]));
    assert_preserved_entry_output(&fixture, "169 0\n");
    let graph = fixture.dependency_graph();
    let command = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == "command")
        })
        .expect("command source unit");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("command synchronous execution report");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected one local constructor-factory rejection: {execution:#?}");
    };
    assert_eq!(rejection.factory_binding, "create");
    assert!(rejection.source_location.is_some());
    (
        rejection.reason,
        rejection
            .first_callback_publication_owner
            .map(|owner| owner.0),
    )
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
fn factory_identity_refusals_report_the_first_guard() {
    use analysis::reports::schema::RecordFactoryRejectionReason as Reason;

    let escaped = fixture_source(
        "",
        FACTORY,
        "globalThis.saved = create;\n",
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    let exported_factory = FACTORY.replace("function create", "export function create");
    let exported = fixture_source(
        "",
        &exported_factory,
        "",
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    let reassigned = fixture_source(
        "",
        FACTORY,
        "create = function (record) { return new Holder(record, () => later); };\n",
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );

    for (source, anonymous_statements, expected, has_publication_owner) in [
        (
            &escaped,
            &["globalThis.saved = create;"][..],
            Reason::FactoryEscaped,
            false,
        ),
        (&exported, &[][..], Reason::FactoryExported, false),
        (
            &reassigned,
            &["create = function (record) { return new Holder(record, () => later); };"][..],
            Reason::FactoryFlowIdentity,
            false,
        ),
    ] {
        let (reason, publication_owner) =
            local_factory_result_rejection(source, anonymous_statements);
        assert_eq!(reason, expected);
        assert_eq!(publication_owner.is_some(), has_publication_owner);
    }
}

#[test]
fn escaped_class_members_report_the_first_global_member_write() {
    use analysis::execution_evidence::ConstructionRejection;
    use analysis::reports::schema::{
        ClassMembersPublicationWriter as Writer, RecordFactoryRejectionReason,
    };

    let factory = FACTORY.replace("() => later", "() => 42");
    let source = format!(
        "{HOLDER}{factory}const first = create({{}});\nglobalThis.saved = first;\nconst command = create({{}});\nconsole.log(typeof command.run);\n"
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("Holder"),
                Member::new("create").with_retained_record_factory(),
                Member::new("first"),
                Member::new("command"),
            ],
            &[
                "globalThis.saved = first;",
                "console.log(typeof command.run);",
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "function\n");

    let graph = fixture.dependency_graph();
    let command = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == "command")
        })
        .expect("command source unit");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("command synchronous execution report");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected one local constructor-factory rejection: {execution:#?}");
    };
    assert_eq!(
        rejection.reason,
        RecordFactoryRejectionReason::ConstructorProof
    );
    assert_eq!(
        rejection.construction,
        Some(ConstructionRejection::ClassMembersEscaped)
    );
    let publication = rejection
        .first_class_members_publication
        .as_ref()
        .expect("first actual class-members publication");
    assert_eq!(publication.writer, Writer::GlobalOrIntrinsicMemberWrite);
    let publication_owner = publication.top_level_owner.expect("publication owner");
    let start = publication
        .span_start
        .expect("publication span start")
        .checked_sub(1)
        .expect("SWC source starts at BytePos(1)") as usize;
    let end = publication
        .span_end
        .expect("publication span end")
        .checked_sub(1)
        .expect("SWC source starts at BytePos(1)") as usize;
    assert!(start < end);
    assert_eq!(&source[start..end], "globalThis.saved = first");
    let publication_node = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal == publication_owner)
        .expect("publication owner source unit");
    let publication_location = publication
        .source_location
        .as_ref()
        .expect("publication source location");
    assert_eq!(
        publication_node.source_location.as_ref(),
        Some(publication_location)
    );
    assert_eq!(
        publication_location.start_line,
        source
            .lines()
            .position(|line| line == "globalThis.saved = first;")
            .expect("publication line")
            + 1
    );
}

#[test]
fn terminal_forwarding_keeps_nested_factory_results_private() {
    let before_command = "const make = label => create({ label: () => label, values: [Flags.FIRST], execute: async () => {} });\nconst first = make(\"a\");\nconst second = make(\"b\");\n";
    let fixture = forwarding_fixture(FACTORY, before_command, &[]);
    let graph = fixture.dependency_graph();
    for binding in ["first", "second", "command"] {
        let node = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .expect("declared source unit");
        let execution = node
            .synchronous_execution
            .as_ref()
            .expect("synchronous execution");
        assert!(execution.complete, "{binding}: {execution:#?}");
        assert!(
            execution.precision_losses.is_empty(),
            "{binding}: {execution:#?}"
        );
        if binding == "command" {
            assert!(node.purity.is_pure(), "{:?}", node.purity);
            assert_eq!(
                execution.local_record_factory_uses.len(),
                1,
                "{execution:#?}"
            );
        }
    }
}

fn forwarding_fixture(
    factory: &str,
    before_command: &str,
    additional_statements: &[&str],
) -> Fixture {
    let source = fixture_source(
        "",
        factory,
        before_command,
        &format!("create({FRESH_RECORD})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    let mut statements = vec!["console.log(command.run(), command.weight);"];
    statements.extend_from_slice(additional_statements);
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("Flags"),
                Member::new("mutate"),
                Member::new("Holder"),
                Member::new("create").with_retained_record_factory(),
                Member::new("prefix"),
                Member::new("make"),
                Member::new("first"),
                Member::new("second"),
                Member::new("command"),
                Member::new("later"),
            ],
            &statements,
        )],
    ));
    assert_preserved_entry_output(&fixture, "169 0\n");
    fixture
}

#[test]
fn nonterminal_forwarding_retains_publication_from_the_caller_suffix() {
    let before_command = "const make = label => { const result = create({ label: () => label, values: [Flags.FIRST], execute: async () => {} }); return result; };\nconst first = make(\"a\");\nconst second = make(\"b\");\n";
    let private = forwarding_fixture(FACTORY, before_command, &[]);
    let graph = private.dependency_graph();
    let command = node_declaring(&graph, "command");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("execution report");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        1,
        "{execution:#?}"
    );
    let publishing_suffix = "const make = label => { const result = create({ label: () => label, values: [Flags.FIRST], execute: async () => {} }); globalThis.retained = create; return result; };\nconst first = make(\"a\");\nconst second = make(\"b\");\n";
    assert_forwarding_factory_refused(&forwarding_fixture(FACTORY, publishing_suffix, &[]), None);
}

#[test]
fn terminal_forwarding_preserves_real_factory_escapes() {
    let before_command = "const make = label => create({ label: () => label, values: [Flags.FIRST], execute: async () => {} });\nconst first = make(\"a\");\nconst second = make(\"b\");\n";
    let publishing_factory = "function create(record) { globalThis.retained = create; return new Holder(record, () => later); }\n";
    assert_forwarding_factory_refused(
        &forwarding_fixture(publishing_factory, before_command, &[]),
        None,
    );

    let prior_escape = "globalThis.retained = create;\n".to_owned() + before_command;
    assert_forwarding_factory_refused(
        &forwarding_fixture(FACTORY, &prior_escape, &["globalThis.retained = create;"]),
        None,
    );
}

fn assert_forwarding_factory_refused(
    fixture: &Fixture,
    expected_publication_binding: Option<&str>,
) {
    let graph = fixture.dependency_graph();
    let command = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == "command")
        })
        .expect("command source unit");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("command execution");
    assert!(
        execution.local_record_factory_uses.is_empty(),
        "{execution:#?}"
    );
    assert_eq!(
        execution.local_record_factory_rejections.len(),
        1,
        "{execution:#?}"
    );
    assert_eq!(
        execution.local_record_factory_rejections[0].reason,
        analysis::reports::schema::RecordFactoryRejectionReason::FactoryEscaped,
        "{execution:#?}"
    );
    if let Some(binding) = expected_publication_binding {
        let first_publication = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .expect("publication source unit");
        assert_eq!(
            execution.local_record_factory_rejections[0].first_callback_publication_owner,
            Some(first_publication.statement_ordinal),
            "{execution:#?}"
        );
    }
}

#[test]
fn exported_factory_result_stays_deferred_until_acyclic_importer_use() {
    let fixture = run_exported_record_factory(
        EXPORTED_RECORD_PROVIDER,
        exported_record_factory_modules(&[]),
    );
    assert_preserved_entry_output(&fixture, "42\n");

    let graph: analysis::DependencyGraphReport = read_gzip_json(
        &fixture
            .report_root
            .join("static/provider/dependency_graph.json.gz"),
    );
    let command = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == "command")
        })
        .expect("provider command source unit");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("provider synchronous execution report");
    assert!(execution.complete, "{execution:#?}");
    assert_eq!(execution.local_record_factory_uses.len(), 1);
}

#[test]
fn exported_factory_result_rejects_a_rebound_factory() {
    let rebound_provider = EXPORTED_RECORD_PROVIDER.replace(
        "const command = create({ execute: () => later });",
        "create = function (record) { return new Holder(record); };\nconst command = create({ execute: () => later });",
    );
    let rebound_chunks = [("static/consumer", ACYCLIC_COMMAND_IMPORTER)];
    let rejected = run_rejection_fixture(
        FixtureOpts::new(&rebound_provider, exported_record_factory_modules(&[]))
            .with_chunk_id("static/provider")
            .with_extra_chunks(&rebound_chunks),
    );
    assert!(rejected.atomic_group_conflicts().iter().any(|conflict| {
        conflict["causes"]
            .as_array()
            .is_some_and(|causes| causes.iter().any(|cause| cause == "eager_rebind"))
            && conflict["claims"].as_array().is_some_and(|claims| {
                claims
                    .iter()
                    .any(|claim| claim["module"] == "callbacks/wrapped")
            })
    }));
    let graph = rejected.dependency_graph();
    let command = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "command")
        })
        .expect("provider command");
    assert!(
        command
            .synchronous_execution
            .as_ref()
            .expect("command execution")
            .local_record_factory_uses
            .is_empty()
    );
}

#[test]
fn exported_factory_result_preserves_later_mutation() {
    let mutated_provider = EXPORTED_RECORD_PROVIDER.replace(
        "const later = dynamic;",
        "command.record.execute = () => later + 1;\nconst later = dynamic;",
    );
    let consumer_chunks = [("static/consumer", ACYCLIC_COMMAND_IMPORTER)];
    let mutation = ["command.record.execute = () => later + 1;"];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &mutated_provider,
            exported_record_factory_modules(&mutation),
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&consumer_chunks),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
    let fixture = run_exported_record_factory(
        &mutated_provider,
        vec![logical_module_with_anon(
            "callbacks/wrapped",
            &[
                Member::new("Holder"),
                Member::new("create").with_retained_record_factory(),
                Member::new("prefix"),
                Member::new("dynamic"),
                Member::new("command"),
                Member::new("later"),
            ],
            &mutation,
        )],
    );
    assert_preserved_entry_output(&fixture, "43\n");
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
    let fixture = run_fixture(factory_effect_order_opts(&source, true));
    assert_preserved_entry_output(&fixture, "barrier\nExample 42 Example 42\n");

    let graph = fixture.dependency_graph();
    let command_nodes: Vec<_> = ["command", "commandAgain"]
        .into_iter()
        .map(|binding| {
            graph
                .nodes
                .iter()
                .find(|node| {
                    node.declared_bindings
                        .iter()
                        .any(|declared| declared.binding == binding)
                })
                .unwrap_or_else(|| panic!("missing source unit for {binding}"))
        })
        .collect();
    let label = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == "label")
        })
        .expect("label source unit");
    for command in &command_nodes {
        assert!(command.purity.is_pure(), "{:?}", command.purity);
        let execution = command
            .synchronous_execution
            .as_ref()
            .expect("checked source execution");
        assert!(execution.complete);
        assert_eq!(execution.local_record_factory_uses.len(), 1);
    }

    let label_unit = label.id.as_str();
    for command in &command_nodes {
        let command_unit = command.id.as_str();
        assert!(
            graph.edges.iter().any(|edge| {
                edge.source == command_unit
                    && edge.target == label_unit
                    && edge.edge_kind == analysis_model::DepKind::EagerUse
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
    let literal_fixture = run_fixture(factory_effect_order_opts(&literal, true));
    assert_preserved_entry_output(&literal_fixture, "barrier\nExample 42 Example 42\n");
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

fn assert_admitted(source: &str, callback_members: &[&'static str], expected_output: &str) {
    let fixture = run_fixture(fixture_opts(source, true, callback_members, &[], &[]));
    assert_preserved_entry_output(&fixture, expected_output);
    let graph = fixture.dependency_graph();
    let reports: Vec<_> = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .collect();
    assert_eq!(
        reports
            .iter()
            .map(|report| report.local_record_factory_uses.len())
            .sum::<usize>(),
        1
    );
    assert!(
        reports
            .iter()
            .all(|report| report.local_record_factory_rejections.is_empty())
    );
    let command = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == "command")
        })
        .expect("command source unit");
    assert!(command.purity.is_pure(), "{:?}", command.purity);
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("command synchronous execution");
    assert!(execution.complete, "{execution:#?}");
    assert_eq!(execution.local_record_factory_uses.len(), 1);
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/callbacks/wrapped.js",
        &["command"],
        &["later"],
    );
}

/// Runs the call unsplit (so the gate accepts) and returns the typed reason
/// the annotated call's record was refused for.
fn record_rejection(
    before_command: &str,
    call: &str,
) -> (
    analysis::reports::schema::RecordFactoryRejectionReason,
    Option<String>,
) {
    let source = fixture_source(
        "",
        FACTORY,
        before_command,
        call,
        "",
        "console.log(typeof command.run);",
    );
    let mut members = vec![
        Member::new("Flags"),
        Member::new("mutate"),
        Member::new("Holder"),
        Member::new("create").with_retained_record_factory(),
        Member::new("prefix"),
        Member::new("command"),
        Member::new("later"),
    ];
    members.extend(
        ["handler", "label"]
            .into_iter()
            .filter(|name| before_command.contains(&format!("const {name} ")))
            .map(Member::new),
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &members,
            &["console.log(typeof command.run);"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "function\n");
    let graph = fixture.dependency_graph();
    let [rejection] = graph
        .nodes
        .iter()
        .filter_map(|node| node.synchronous_execution.as_ref())
        .flat_map(|report| &report.local_record_factory_rejections)
        .collect::<Vec<_>>()[..]
    else {
        panic!("expected one record-factory rejection");
    };
    assert_eq!(rejection.factory_binding, "create");
    assert!(rejection.source_location.is_some());
    (rejection.reason, rejection.property.clone())
}

#[test]
fn method_shorthand_is_a_retained_callback() {
    let record = FRESH_RECORD.replace("execute: () => later,", "execute() { return later; },");
    let source = fixture_source(
        "",
        FACTORY,
        "",
        &format!("create({record})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    assert_admitted(&source, &[], "169 0\n");
}

#[test]
fn function_valued_bindings_are_retained_callbacks() {
    for before_command in [
        "function handler() { return later; }\n",
        "const handler = () => later;\n",
    ] {
        let record = FRESH_RECORD.replace("execute: () => later,", "execute: handler,");
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            &format!("create({record})"),
            "",
            "console.log(command.run(), command.weight);",
        );
        assert_admitted(&source, &["handler"], "169 0\n");
    }
    let shorthand = FRESH_RECORD.replace("execute: () => later,", "execute,");
    let source = fixture_source(
        "",
        FACTORY,
        "const execute = () => later;\n",
        &format!("create({shorthand})"),
        "",
        "console.log(command.run(), command.weight);",
    );
    assert_admitted(&source, &["execute"], "169 0\n");
}

#[test]
fn shared_primitive_arrays_and_records_are_closed_inputs() {
    for (before_command, value) in [
        ("const modes = [Flags.FIRST, 2, \"ready\"];\n", "modes[0]"),
        (
            "const modes = { first: Flags.FIRST, label: \"ready\" };\n",
            "modes.first",
        ),
    ] {
        let record =
            FRESH_RECORD.replace("execute: () => later,", "execute: () => later,\n  modes,");
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            &format!("create({record})"),
            "",
            &format!("console.log(command.record.{value}, command.run());"),
        );
        assert_admitted(&source, &["modes"], "1 169\n");
    }
}

#[test]
fn template_literals_of_proven_primitives_are_admitted() {
    let record = FRESH_RECORD.replace(
        "execute: () => later,",
        "execute: () => later,\n  name: `${label}-${Flags.SECOND}-${1}`,",
    );
    let source = fixture_source(
        "",
        FACTORY,
        "const label = \"Example\";\n",
        &format!("create({record})"),
        "",
        "console.log(command.record.name, command.run());",
    );
    assert_admitted(&source, &["label"], "Example-2-1 169\n");
}

#[test]
fn logical_not_literal_is_a_closed_constructor_input() {
    let record = FRESH_RECORD.replace("enabled: true", "enabled: !false");
    let source = fixture_source(
        "",
        FACTORY,
        "",
        &format!("create({record})"),
        "",
        "console.log(command.record.nested.enabled, command.run());",
    );
    assert_admitted(&source, &[], "true 169\n");
}

#[test]
fn logical_not_does_not_hide_an_effectful_constructor_operand() {
    let record = FRESH_RECORD.replace("enabled: true", "enabled: !effect()");
    let before_command =
        "const state = { count: 0 };\nfunction effect() { state.count++; return false; }\n";
    let source = fixture_source(
        "",
        FACTORY,
        before_command,
        &format!("create({record})"),
        "",
        "console.log(command.record.nested.enabled, state.count, command.run());",
    );
    let members = [
        Member::new("Flags"),
        Member::new("mutate"),
        Member::new("Holder"),
        Member::new("create").with_retained_record_factory(),
        Member::new("prefix"),
        Member::new("state"),
        Member::new("effect"),
        Member::new("command"),
        Member::new("later"),
    ];
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &members,
            &["console.log(command.record.nested.enabled, state.count, command.run());"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "true 1 169\n");

    let graph = fixture.dependency_graph();
    let command = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == "command")
        })
        .expect("command source unit");
    let execution = command
        .synchronous_execution
        .as_ref()
        .expect("command synchronous execution");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected the effectful operand to refuse the record-factory proof: {execution:#?}");
    };
    assert_eq!(
        rejection.reason,
        analysis::reports::schema::RecordFactoryRejectionReason::UnsupportedValue
    );
}

#[test]
fn unproven_values_in_admitted_positions_fall_back() {
    for (before_command, call, callback_members) in [
        // A method under a computed key.
        (
            "const key = \"execute\";\n",
            "create({ [key]() { return later; } })",
            vec!["key"],
        ),
        // A reassigned function binding.
        (
            "let handler = () => later;\nhandler = () => later + 1;\n",
            "create({ execute: handler })",
            vec!["handler"],
        ),
        // An object binding, whose identity is shared and mutable.
        (
            "const handler = { valueOf() { return later; } };\n",
            "create({ execute: handler })",
            vec!["handler"],
        ),
        // A function binding initialized only after the call (TDZ).
        ("", "create({ execute: handler })", vec![]),
        // A shared array holding a callback.
        (
            "const handler = [() => later];\n",
            "create({ execute: () => later, nested: handler })",
            vec!["handler"],
        ),
        // A shared array given a callback before the call.
        (
            "const handler = [1];\nhandler.push(() => later);\n",
            "create({ execute: () => later, nested: handler })",
            vec!["handler"],
        ),
        // A shared record exposed to unknown code before the call.
        (
            "const handler = { mode: 1 };\nglobalThis.saved = handler;\n",
            "create({ execute: () => later, nested: handler })",
            vec!["handler"],
        ),
        // `ToString` of an object runs its `toString`.
        (
            "const label = { toString() { return String(later); } };\n",
            "create({ execute: () => later, name: `${label}` })",
            vec!["label"],
        ),
        // A proxy can intercept the conversion's property reads.
        (
            "const label = new Proxy({}, { get: () => () => later });\n",
            "create({ execute: () => later, name: `${label}` })",
            vec!["label"],
        ),
        // A substitution evaluated by a call.
        (
            "",
            "create({ execute: () => later, name: `${String(1)}` })",
            vec![],
        ),
        // A getter-backed member read.
        (
            "const label = { get value() { return later; } };\n",
            "create({ execute: () => later, name: `${label.value}` })",
            vec!["label"],
        ),
    ] {
        let after = if callback_members.is_empty() && call.contains("handler") {
            "const handler = () => later;\n"
        } else {
            ""
        };
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            call,
            after,
            "console.log(command.run());",
        );
        let later_members: &[&str] = if after.is_empty() { &[] } else { &["handler"] };
        let statements: Vec<_> = before_command
            .lines()
            .filter(|line| !line.starts_with("const ") && !line.starts_with("let "))
            .collect();
        assert_atomic_fallback(&source, &callback_members, later_members, &statements);
    }
}

#[test]
fn refused_record_shapes_report_a_typed_reason() {
    use analysis::reports::schema::RecordFactoryRejectionReason as Reason;
    for (before_command, call, reason, property) in [
        (
            "",
            "create({ ...{ execute: () => later } })",
            Reason::Spread,
            None,
        ),
        (
            "",
            "create({ execute: () => later, get name() { return \"x\"; } })",
            Reason::Accessor,
            None,
        ),
        (
            "const label = { toString() { return \"x\"; } };\n",
            "create({ execute: () => later, name: `${label}` })",
            Reason::TemplateSubstitution,
            Some("name"),
        ),
        (
            "",
            "create({ execute: () => later, name: `${String(1)}` })",
            Reason::TemplateSubstitution,
            Some("name"),
        ),
        (
            "const handler = { execute: () => later };\n",
            "create({ execute: () => later, nested: handler })",
            Reason::UnprovenBinding,
            Some("nested"),
        ),
        (
            "const label = { value: \"x\" };\n",
            "create({ execute: () => later, name: label.value })",
            Reason::UnprovenMemberRead,
            Some("name"),
        ),
        (
            "",
            "create({ execute: () => later, name: String(1) })",
            Reason::UnsupportedValue,
            Some("name"),
        ),
    ] {
        assert_eq!(
            record_rejection(before_command, call),
            (reason, property.map(str::to_owned)),
            "{call}"
        );
    }
}
