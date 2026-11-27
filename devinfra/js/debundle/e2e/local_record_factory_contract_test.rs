//! The record-factory assertion supplies only the prototype premises for the
//! existing checked private-constructor proof.

use analysis_model::DepKind;
use analysis_vocabulary::execution_evidence::ExecutionPrecisionLossReason;
use analysis_vocabulary::report_schema::{
    RecordFactoryRejectionReason, SynchronousExecutionReport,
};
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
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
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![
            logical_module("factory", &members("make", &["Box", "make"])),
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
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "all",
                    &members("make", &["Box", "make", "first", "changed", "readers"]),
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
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![
                logical_module("factory", &members("create", &["Holder", "create"])),
                logical_module("commands", &binding_members(["first", "second"])),
                logical_module("searches", &[Member::new("searches")]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_preserved_entry_output(&fixture, "1 2\n");
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "second");
    assert!(execution.complete, "{execution:#?}");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        1,
        "{execution:#?}"
    );
    let second = node_declaring(&graph, "second");
    let searches = node_declaring(&graph, "searches");
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && edge.source == second.id
                && edge.target == searches.id
        }),
        "the private own-field read must not sequence the later factory: {:#?}",
        graph.edges
    );
}

/// Runs `source` colocated in one module and asserts that the getter-receiver
/// publication before `second` refuses its record-factory proof.
fn assert_second_factory_refused(
    source: &str,
    names: &[&'static str],
    statements: &[&str],
    dataflow_aware: bool,
    expected_output: &str,
) {
    let mut opts = FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &members("create", names),
            statements,
        )],
    );
    if dataflow_aware {
        opts = opts.with_dataflow_aware_s_chain();
    }
    let fixture = run_fixture_with_graphs(opts);
    assert_preserved_entry_output(&fixture, expected_output);
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "second");
    assert!(
        !execution.local_record_factory_rejections.is_empty(),
        "{source}: {execution:#?}"
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
        assert_second_factory_refused(
            &source,
            &["Holder", "create", "first", "searches", "second"],
            &[replace],
            true,
            "1 2\n",
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
    assert_second_factory_refused(
        source,
        &["Holder", "create", "first", "searches", "second"],
        &[],
        false,
        "1 2\n",
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
    assert_second_factory_refused(
        source,
        &[
            "Holder", "create", "first", "read", "alias", "searches", "second",
        ],
        &["delete alias.search"],
        true,
        "true true\n",
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

/// Two factory results whose retained callbacks capture `shared`; `after_second`
/// runs before the final log.
fn captured_array_source(after_second: &str, log_first: &str, exports: &str) -> String {
    format!(
        r#"class Empty {{}}
class Holder {{
  constructor(record, checker) {{ this.record = record; this.checker = checker; this.size = record.size ?? 0; }}
  size;
  run() {{ return this.record.callback(); }}
}}
function create(record) {{ return new Holder(record, new Empty()); }}
const shared = [0];
const first = create({{ size: 1, items: [1], callback: () => shared.push(1) }});
const second = create({{ size: 2, items: [2], callback: () => shared.push(2) }});
{after_second}console.log({log_first}, second.size, first.run(), second.run(), shared.join(','));
export {{ {exports} }};
"#
    )
}

fn captured_array_modules(shared: &[&'static str]) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "framework",
            &members("create", &["Empty", "Holder", "create"]),
        ),
        logical_module("first", &[Member::new("first")]),
        logical_module("second", &[Member::new("second")]),
        logical_module("shared", &members("create", shared)),
    ]
}

#[test]
fn independent_retained_arrays_do_not_create_a_constructor_heap_frontier() {
    let source = captured_array_source("", "first.size", "first, second, shared");
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(&source, captured_array_modules(&["shared"]))
            .with_dataflow_aware_s_chain()
            .with_import_order(&["framework", "shared", "first", "second"]),
    );
    assert_preserved_entry_output(&fixture, "1 2 2 3 0,1,2\n");
    let graph = fixture.dependency_graph();
    let shared = node_declaring(&graph, "shared");
    for result in ["first", "second"] {
        let heap = execution(&graph, result)
            .heap_frontier
            .as_ref()
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
        let result_id = &node_declaring(&graph, result).id;
        assert!(
            !graph
                .edges
                .iter()
                .any(|edge| edge.edge_kind == DepKind::Sequenced
                    && &edge.source == result_id
                    && edge.target == shared.id),
            "retaining a callback is not a read or write of the captured array: {:#?}",
            graph.edges
        );
    }
}

#[test]
fn later_reader_can_share_module_with_a_captured_array() {
    let source = captured_array_source(
        "const after = first.size;\n",
        "after",
        "first, second, shared, after",
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, captured_array_modules(&["shared", "after"]))
            .with_dataflow_aware_s_chain()
            .with_import_order(&["shared", "first", "second", "framework"]),
    );
    assert_preserved_entry_output(&fixture, "1 2 2 3 0,1,2\n");
    assert_eq!(fixture.translation_validation()["accepts"], true);
    for module in ["first", "second", "shared"] {
        assert!(
            fixture
                .out_root
                .join(format!("static/app/modules/{module}.js"))
                .is_file(),
            "missing emitted {module} module"
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
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &members(
                "create",
                &[
                    "Holder",
                    "create",
                    "shared",
                    "mutateSharedArray",
                    "first",
                    "second",
                ],
            ),
            &["console.log(shared.join(','), first.items[0], second.items[0]);"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "0,1 1 2\n");
    let graph = fixture.dependency_graph();
    let node = |binding: &str| node_declaring(&graph, binding);
    let first_heap = execution(&graph, "first")
        .heap_frontier
        .as_ref()
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
    let sequenced_after_shared = |binding: &str| {
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && edge.source == node(binding).id
                && edge.target == node("shared").id
        })
    };
    assert!(
        sequenced_after_shared("first"),
        "the nested mutation must remain ordered after the shared array: {:#?}",
        graph.edges
    );
    assert!(
        !sequenced_after_shared("second"),
        "the independent checked call must not inherit the first call's heap effects: {:#?}",
        graph.edges
    );
}

fn exported_record_factory_modules(callback_statements: &[&str]) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "callbacks/wrapped",
            &members("create", &["Holder", "create", "command"]),
            callback_statements,
        ),
        logical_module("values/dynamic", &binding_members(["prefix", "dynamic"])),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

/// Runs the provider `source` split into `modules` behind [`ACYCLIC_COMMAND_IMPORTER`].
fn run_exported_record_factory(
    run: fn(FixtureOpts<'_>) -> Fixture,
    source: &str,
    modules: Vec<LogicalModuleEntry>,
) -> Fixture {
    let provider_paths: Vec<String> = modules.iter().map(|(module, _)| module.clone()).collect();
    let provider_priority: Vec<&str> = provider_paths.iter().map(String::as_str).collect();
    run(FixtureOpts::new(ACYCLIC_COMMAND_IMPORTER, vec![])
        .with_chunk_id("static/consumer")
        .with_import_order(&["residual"])
        .with_extra_chunks(&[("static/provider", source)])
        .with_extra_chunk_logical_modules(&[("static/provider", modules)])
        .with_chunk_import_order("static/provider", &provider_priority))
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

fn local_factory_result_rejection(
    source: &str,
    anonymous_statements: &[&str],
) -> (RecordFactoryRejectionReason, Option<usize>) {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&[]),
            anonymous_statements,
        )],
    ));
    assert_preserved_entry_output(&fixture, "169 0\n");
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
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
    let fixture = run_fixture_with_graphs(fixture_opts(&source, true, &[], &[], &[]));

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
    let escape = "globalThis.saved = create;";
    let reassign = "create = function (record) { return new Holder(record, () => later); };";
    let exported_factory = FACTORY.replace("function create", "export function create");
    for (factory, statement, expected) in [
        (
            FACTORY,
            Some(escape),
            RecordFactoryRejectionReason::FactoryEscaped,
        ),
        (
            exported_factory.as_str(),
            None,
            RecordFactoryRejectionReason::FactoryExported,
        ),
        (
            FACTORY,
            Some(reassign),
            RecordFactoryRejectionReason::FactoryFlowIdentity,
        ),
    ] {
        let before_command = statement.map_or(String::new(), |statement| format!("{statement}\n"));
        let source = fixture_source(
            "",
            factory,
            &before_command,
            &format!("create({FRESH_RECORD})"),
            "",
            "console.log(command.run(), command.weight);",
        );
        let (reason, publication_owner) =
            local_factory_result_rejection(&source, statement.as_slice());
        assert_eq!(reason, expected);
        assert_eq!(publication_owner, None, "{expected:?}");
    }
}

#[test]
fn escaped_class_members_report_the_first_global_member_write() {
    use analysis_vocabulary::execution_evidence::ConstructionRejection;
    use analysis_vocabulary::report_schema::ClassMembersPublicationWriter;

    let factory = FACTORY.replace("() => later", "() => 42");
    let source = format!(
        "{HOLDER}{factory}const first = create({{}});\nglobalThis.saved = first;\nconst command = create({{}});\nconsole.log(typeof command.run);\n"
    );
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &members("create", &["Holder", "create", "first", "command"]),
            &[
                "globalThis.saved = first;",
                "console.log(typeof command.run);",
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "function\n");

    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
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
    assert_eq!(
        publication.writer,
        ClassMembersPublicationWriter::GlobalOrIntrinsicMemberWrite
    );
    let publication_owner = publication.top_level_owner.expect("publication owner");
    let source_offset = |position: Option<u32>| {
        position
            .expect("publication span")
            .checked_sub(1)
            .expect("SWC source starts at BytePos(1)") as usize
    };
    let start = source_offset(publication.span_start);
    let end = source_offset(publication.span_end);
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

const TERMINAL_FORWARDING: &str = "const make = label => create({ label: () => label, values: [Flags.FIRST], execute: async () => {} });\nconst first = make(\"a\");\nconst second = make(\"b\");\n";

#[test]
fn terminal_forwarding_keeps_nested_factory_results_private() {
    let fixture = forwarding_fixture(FACTORY, TERMINAL_FORWARDING, &[]);
    let graph = fixture.dependency_graph();
    for binding in ["first", "second", "command"] {
        let execution = execution(&graph, binding);
        assert!(execution.complete, "{binding}: {execution:#?}");
        assert!(
            execution.precision_losses.is_empty(),
            "{binding}: {execution:#?}"
        );
    }
    let command = node_declaring(&graph, "command");
    assert!(command.purity.is_pure(), "{:?}", command.purity);
    assert_eq!(
        execution(&graph, "command").local_record_factory_uses.len(),
        1
    );
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
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&["make", "first", "second"]),
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
    let execution = execution(&graph, "command");
    assert_eq!(
        execution.local_record_factory_uses.len(),
        1,
        "{execution:#?}"
    );
    let publishing_suffix = "const make = label => { const result = create({ label: () => label, values: [Flags.FIRST], execute: async () => {} }); globalThis.retained = create; return result; };\nconst first = make(\"a\");\nconst second = make(\"b\");\n";
    assert_forwarding_factory_refused(&forwarding_fixture(FACTORY, publishing_suffix, &[]));
}

#[test]
fn terminal_forwarding_preserves_real_factory_escapes() {
    let publishing_factory = "function create(record) { globalThis.retained = create; return new Holder(record, () => later); }\n";
    assert_forwarding_factory_refused(&forwarding_fixture(
        publishing_factory,
        TERMINAL_FORWARDING,
        &[],
    ));

    let prior_escape = "globalThis.retained = create;\n".to_owned() + TERMINAL_FORWARDING;
    assert_forwarding_factory_refused(&forwarding_fixture(
        FACTORY,
        &prior_escape,
        &["globalThis.retained = create;"],
    ));
}

fn assert_forwarding_factory_refused(fixture: &Fixture) {
    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
    assert!(
        execution.local_record_factory_uses.is_empty(),
        "{execution:#?}"
    );
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected one local constructor-factory rejection: {execution:#?}");
    };
    assert_eq!(
        rejection.reason,
        RecordFactoryRejectionReason::FactoryEscaped,
        "{execution:#?}"
    );
}

#[test]
fn exported_factory_result_stays_deferred_until_acyclic_importer_use() {
    let fixture = run_exported_record_factory(
        run_fixture_with_graphs,
        EXPORTED_RECORD_PROVIDER,
        exported_record_factory_modules(&[]),
    );
    assert_preserved_entry_output(&fixture, "42\n");

    let graph: DependencyGraphReport = read_gzip_json(
        &fixture
            .report_root
            .join("static/provider/dependency_graph.json.gz"),
    );
    let execution = execution(&graph, "command");
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
    let rejected = run_rejection_fixture_with_graphs(
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
    assert!(
        execution(&graph, "command")
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
        .with_chunk_import_order(
            "static/provider",
            &["callbacks/wrapped", "values/dynamic", "values/later"],
        )
        .with_chunk_id("static/provider")
        .with_extra_chunks(&consumer_chunks),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
    let fixture = run_exported_record_factory(
        run_fixture,
        &mutated_provider,
        vec![logical_module_with_anon(
            "callbacks/wrapped",
            &members(
                "create",
                &["Holder", "create", "prefix", "dynamic", "command", "later"],
            ),
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

/// Runs the call unsplit (so the gate accepts) and returns the typed reason
/// the annotated call's record was refused for.
fn record_rejection(
    before_command: &str,
    call: &str,
) -> (RecordFactoryRejectionReason, Option<String>) {
    let source = fixture_source(
        "",
        FACTORY,
        before_command,
        call,
        "",
        "console.log(typeof command.run);",
    );
    let extra: Vec<_> = ["handler", "label"]
        .into_iter()
        .filter(|name| before_command.contains(&format!("const {name} ")))
        .collect();
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&extra),
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
fn proven_record_values_are_admitted() {
    const RUN_AND_WEIGHT: &str = "console.log(command.run(), command.weight);";
    const EXECUTE: &str = "execute: () => later,";
    for (case, before_command, replaced, replacement, callback_members, log, output) in [
        (
            "method shorthand is a retained callback",
            "",
            EXECUTE,
            "execute() { return later; },",
            &[][..],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "function declaration is a retained callback",
            "function handler() { return later; }\n",
            EXECUTE,
            "execute: handler,",
            &["handler"],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "const arrow is a retained callback",
            "const handler = () => later;\n",
            EXECUTE,
            "execute: handler,",
            &["handler"],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "shorthand property names a retained callback",
            "const execute = () => later;\n",
            EXECUTE,
            "execute,",
            &["execute"],
            RUN_AND_WEIGHT,
            "169 0\n",
        ),
        (
            "shared primitive array is a closed input",
            "const modes = [Flags.FIRST, 2, \"ready\"];\n",
            EXECUTE,
            "execute: () => later,\n  modes,",
            &["modes"],
            "console.log(command.record.modes[0], command.run());",
            "1 169\n",
        ),
        (
            "shared primitive record is a closed input",
            "const modes = { first: Flags.FIRST, label: \"ready\" };\n",
            EXECUTE,
            "execute: () => later,\n  modes,",
            &["modes"],
            "console.log(command.record.modes.first, command.run());",
            "1 169\n",
        ),
        (
            "template literal of proven primitives",
            "const label = \"Example\";\n",
            EXECUTE,
            "execute: () => later,\n  name: `${label}-${Flags.SECOND}-${1}`,",
            &["label"],
            "console.log(command.record.name, command.run());",
            "Example-2-1 169\n",
        ),
        (
            "logical not of a literal",
            "",
            "enabled: true",
            "enabled: !false",
            &[],
            "console.log(command.record.nested.enabled, command.run());",
            "true 169\n",
        ),
    ] {
        eprintln!("case: {case}");
        let record = FRESH_RECORD.replace(replaced, replacement);
        let source = fixture_source(
            "",
            FACTORY,
            before_command,
            &format!("create({record})"),
            "",
            log,
        );
        let fixture =
            run_fixture_with_graphs(fixture_opts(&source, true, callback_members, &[], &[]));
        assert_preserved_entry_output(&fixture, output);
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
            1,
            "{case}"
        );
        assert!(
            reports
                .iter()
                .all(|report| report.local_record_factory_rejections.is_empty()),
            "{case}"
        );
        let command = node_declaring(&graph, "command");
        assert!(command.purity.is_pure(), "{case}: {:?}", command.purity);
        let execution = execution(&graph, "command");
        assert!(execution.complete, "{case}: {execution:#?}");
        assert_eq!(execution.local_record_factory_uses.len(), 1, "{case}");
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/callbacks/wrapped.js",
            &["command"],
            &["later"],
        );
    }
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
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module_with_anon(
            "all",
            &harness_members(&["state", "effect"]),
            &["console.log(command.record.nested.enabled, state.count, command.run());"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "true 1 169\n");

    let graph = fixture.dependency_graph();
    let execution = execution(&graph, "command");
    let [rejection] = execution.local_record_factory_rejections.as_slice() else {
        panic!("expected the effectful operand to refuse the record-factory proof: {execution:#?}");
    };
    assert_eq!(
        rejection.reason,
        RecordFactoryRejectionReason::UnsupportedValue
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
    for (before_command, call, reason, property) in [
        (
            "",
            "create({ ...{ execute: () => later } })",
            RecordFactoryRejectionReason::Spread,
            None,
        ),
        (
            "",
            "create({ execute: () => later, get name() { return \"x\"; } })",
            RecordFactoryRejectionReason::Accessor,
            None,
        ),
        (
            "const label = { toString() { return \"x\"; } };\n",
            "create({ execute: () => later, name: `${label}` })",
            RecordFactoryRejectionReason::TemplateSubstitution,
            Some("name"),
        ),
        (
            "",
            "create({ execute: () => later, name: `${String(1)}` })",
            RecordFactoryRejectionReason::TemplateSubstitution,
            Some("name"),
        ),
        (
            "const handler = { execute: () => later };\n",
            "create({ execute: () => later, nested: handler })",
            RecordFactoryRejectionReason::UnprovenBinding,
            Some("nested"),
        ),
        (
            "const label = { value: \"x\" };\n",
            "create({ execute: () => later, name: label.value })",
            RecordFactoryRejectionReason::UnprovenMemberRead,
            Some("name"),
        ),
        (
            "",
            "create({ execute: () => later, name: String(1) })",
            RecordFactoryRejectionReason::UnsupportedValue,
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
