//! Nested record-factory wrappers preserve split cycles and refuse effects or unchecked targets.

use super::{execution, members};
use analysis_vocabulary::execution_evidence::ExecutionPrecisionLossReason;
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

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

const NESTED_COMMAND_MEMBERS: [&str; 8] = [
    "warmup", "prefix", "maybe", "Policy", "Command", "make", "wrapper", "command",
];

/// [`nested_wrapper_source`] with `later` colocated beside the command members.
fn run_nested_wrapper_colocated(source: &str, statements: &[&str]) -> Fixture {
    let mut names = NESTED_COMMAND_MEMBERS.to_vec();
    names.push("later");
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &members("make", &names),
                statements,
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
    fixture
}

#[test]
fn nested_record_factory_wrapper_preserves_a_split_cycle() {
    let source = nested_wrapper_source("", NESTED_FACTORY, NESTED_WRAPPER);
    let split = run_fixture_with_graphs(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "command",
                    &members("make", &NESTED_COMMAND_MEMBERS),
                    &["maybe.record = 1;"],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["later", "command"]),
    );
    assert_eq!(split.translation_validation()["accepts"], true);
    assert_preserved_entry_output(&split, "7\n");

    let fixture = run_nested_wrapper_colocated(&source, &["maybe.record = 1;"]);
    let graph = fixture.dependency_graph();
    let command = node_declaring(&graph, "command");
    let later = node_declaring(&graph, "later");
    let execution = execution(&graph, "command");
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
fn nested_record_factory_refuses_effects_and_unchecked_targets() {
    let setter = "Object.defineProperty(Command.prototype, 'record', { set(value) { this._record = value; }, get() { return this._record; } });";
    for (case, pre_factory, factory, wrapper) in [
        (
            "factory mutation",
            "",
            "function make(record) { globalThis.side = 1; return new Command(record, new Policy()); }",
            NESTED_WRAPPER,
        ),
        (
            "conditional factory body",
            "",
            "function make(record) { if (globalThis.flag) return new Command(record, new Policy()); return new Command(record, new Policy()); }",
            NESTED_WRAPPER,
        ),
        ("prototype setter", setter, NESTED_FACTORY, NESTED_WRAPPER),
        (
            "wrapper reentry",
            "",
            NESTED_FACTORY,
            "function wrapper(label, execute) { return make({ name: label, execute: globalThis.read ? globalThis.read(execute) : execute, weight: 0 }); }",
        ),
        (
            "conditional target",
            "",
            NESTED_FACTORY,
            "function wrapper(label, execute) { const selected = globalThis.flag ? make : make; return selected({ name: label, execute, weight: 0 }); }",
        ),
        (
            "unknown template substitution",
            "",
            NESTED_FACTORY,
            "function wrapper(label, execute) { return make({ name: `Create ${globalThis.label}`, execute, weight: 0 }); }",
        ),
    ] {
        let source = nested_wrapper_source(pre_factory, factory, wrapper);
        let mut statements = vec!["maybe.record = 1;"];
        if !pre_factory.is_empty() {
            statements.push(pre_factory);
        }
        let fixture = run_nested_wrapper_colocated(&source, &statements);
        let graph = fixture.dependency_graph();
        let command = node_declaring(&graph, "command");
        let later = node_declaring(&graph, "later");
        let execution = execution(&graph, "command");
        assert!(!execution.complete, "{case}: {execution:#?}");
        assert!(
            execution.local_record_factory_uses.is_empty(),
            "{case}: {execution:#?}"
        );
        assert!(
            execution.precision_losses.iter().any(|loss| matches!(
                &loss.reason,
                ExecutionPrecisionLossReason::UnsupportedConstruction { .. }
            )),
            "{case}: {execution:#?}"
        );
        assert!(
            graph.edges.iter().any(|edge| {
                edge.source == command.id && edge.target == later.id && edge.constrains_init_order
            }),
            "{case}"
        );
    }
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
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &members(
                "make",
                &[
                    "maybe", "Command", "make", "first", "second", "pair", "later",
                ],
            ),
            &["maybe.record = 1;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "14\n");
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "pair");
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
