//! The record-factory assertion supplies only the prototype premises for the
//! existing checked private-constructor proof.

mod closed_inputs;
mod exported_result;
mod factory_identity;
mod fallbacks;
mod forwarding;
mod nested_factory;
mod own_field_reads;
mod record_shapes;
mod retained_arrays;

use analysis_vocabulary::report_schema::SynchronousExecutionReport;
use debundle_e2e_graph_support::node_declaring;
use debundle_e2e_support::*;
use graph::DependencyGraphReport;

/// The synchronous-execution report of the source unit declaring `binding`.
fn execution<'g>(
    graph: &'g DependencyGraphReport,
    binding: &str,
) -> &'g SynchronousExecutionReport {
    node_declaring(graph, binding)
        .synchronous_execution
        .as_ref()
        .unwrap_or_else(|| panic!("no synchronous execution report for `{binding}`"))
}

/// Binding members, with `factory` carrying the retained-record-factory annotation.
fn members(factory: &str, names: &[&'static str]) -> Vec<Member> {
    names
        .iter()
        .map(|&name| {
            if name == factory {
                Member::new(name).with_retained_record_factory()
            } else {
                Member::new(name)
            }
        })
        .collect()
}

/// One module holding the [`fixture_source`] harness, `extra`, `command` and `later`.
fn harness_members(extra: &[&'static str]) -> Vec<Member> {
    let mut names = vec!["Flags", "mutate", "Holder", "create", "prefix"];
    names.extend_from_slice(extra);
    names.extend(["command", "later"]);
    members("create", &names)
}

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

const FRESH_RECORD: &str = r#"{
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

/// The binding annotated as a retained record factory, if any.
fn annotated_factory(annotated: bool) -> &'static str {
    if annotated { "create" } else { "" }
}

/// Splits a [`fixture_source`] program: the harness, `command` and
/// `callback_members` stay in `callbacks/wrapped` (except `label`, which goes to
/// `values/label`); `later` and `later_members` go to `values/later`.
fn fixture_opts<'a>(
    source: &'a str,
    annotated: bool,
    callback_members: &[&'static str],
    later_members: &[&'static str],
    callback_statements: &[&str],
) -> FixtureOpts<'a> {
    let mut callback_names = vec!["Flags", "mutate", "Holder", "create", "prefix", "command"];
    callback_names.extend(callback_members.iter().filter(|&&name| name != "label"));
    let mut modules = vec![logical_module_with_anon(
        "callbacks/wrapped",
        &members(annotated_factory(annotated), &callback_names),
        callback_statements,
    )];
    if callback_members.contains(&"label") {
        modules.push(logical_module("values/label", &[Member::new("label")]));
    }
    let mut later_names = vec!["later"];
    later_names.extend_from_slice(later_members);
    modules.push(logical_module("values/later", &members("", &later_names)));
    FixtureOpts::new(source, modules)
}

fn factory_effect_order_source(command: &str, holder: &str) -> String {
    format!(
        "function mutate(target, key, value) {{ target[key] = value; }}\n{holder}function create(record) {{ return new Holder(record); }}\nconst label = \"Example\";\nconst command = {command};\nconst commandAgain = {command};\nconst barrier = console.log(\"barrier\");\nconst later = 42;\nconsole.log(command.label, command.record.execute(), commandAgain.label, commandAgain.record.execute());\n"
    )
}

fn factory_effect_order_opts<'a>(source: &'a str, annotated: bool) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "callbacks/wrapped",
                &members(
                    annotated_factory(annotated),
                    &["mutate", "Holder", "create", "command", "commandAgain"],
                ),
            ),
            logical_module("values/label", &binding_members(["label", "barrier"])),
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
