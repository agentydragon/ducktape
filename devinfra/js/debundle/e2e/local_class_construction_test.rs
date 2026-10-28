//! A checked implicit constructor can be reordered without executing its
//! deferred methods or field callbacks during module initialization.

use debundle_e2e_support::*;

const REGISTRY_SOURCE: &str = r#"globalThis.events = [];
const a = (() => 1)();
class Registry {
  #items = new Set();
  #active = !1;
  onExit = () => this.clear();
  get size() { return this.#items.size; }
  clear() { globalThis.events.push("clear"); }
}
const b = new Registry;
const c = b.size + a;
b.onExit();
console.log(c, globalThis.events.join(","));
export { a, b, c, Registry };
"#;

fn registry_modules() -> Vec<LogicalModuleEntry> {
    vec![logical_module(
        "registry",
        &[Member::new("Registry"), Member::new("b")],
    )]
}

#[test]
fn implicit_class_construction_preserves_a_live_array_heap_barrier() {
    let rejected = run_rejection_fixture(FixtureOpts::new(REGISTRY_SOURCE, registry_modules()));
    let graph = rejected.dependency_graph();
    let construction = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "b")
        })
        .unwrap();
    assert!(construction.purity.is_pure());
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == construction.id && edge.edge_kind == analysis::DepKind::Sequenced
        }),
        "pure construction still needs its missing heap-effect capability"
    );
    assert!(!rejected.cycles().is_empty());
    // Preserve the original runtime control while the requested split remains
    // rejected: constructor purity does not summarize unrelated array effects.
    let fixture = run_fixture(FixtureOpts::new(REGISTRY_SOURCE, vec![]));
    assert_preserved_entry_output(&fixture, "1 clear\n");
}

#[test]
fn inferred_implicit_class_construction_breaks_an_array_free_sequenced_cycle() {
    let source = REGISTRY_SOURCE
        .replace("globalThis.events = [];", "globalThis.events = '';")
        .replace(
            "globalThis.events.push(\"clear\");",
            "globalThis.events += 'clear';",
        )
        .replace("globalThis.events.join(\",\")", "globalThis.events");
    let fixture = assert_pure_cycle_break(
        &source,
        registry_modules(),
        "registry",
        &["class Registry", "const b = new Registry"],
        &["const a"],
        "1 clear\n",
    );
    assert_preserved_entry_output(&fixture, "1 clear\n");
}

#[test]
fn void_zero_field_does_not_fabricate_callback_reentry() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const saved = () => later;
globalThis.saved = saved;
class Checker {
  result = void 0;
  isAllowed() { return true; }
}
globalThis.savedChecker = Checker;
class Command {
  constructor(config, checker) { this.config = config; this.checker = checker; }
  execute() { return this.config.execute(); }
}
function create(config) { return new Command(config, new Checker()); }
const command = create({ execute: () => later });
const later = 7;
console.log(command.execute());
export { saved, Checker, Command, create, command, later };
"#,
        vec![
            logical_module_with_anon(
                "command",
                &[
                    Member::new("saved"),
                    Member::new("Checker"),
                    Member::new("Command"),
                    Member::new("create"),
                    Member::new("command"),
                ],
                &[
                    "globalThis.saved = saved;",
                    "globalThis.savedChecker = Checker;",
                ],
            ),
            logical_module("value", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7\n");
}

fn expect_callback_reentry_for_class(checker: &str, prelude: &str) {
    let source = format!(
        r#"const saved = () => later;
globalThis.saved = saved;
{prelude}
{checker}
globalThis.savedChecker = Checker;
class Command {{
  constructor(config, checker) {{ this.config = config; this.checker = checker; }}
}}
function create(config) {{ return new Command(config, new Checker()); }}
const command = create({{ execute: () => later }});
const later = 7;
export {{ saved, Checker, Command, create, command, later }};
"#
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("command", &[Member::new("command")]),
                logical_module("value", &[Member::new("later")]),
            ],
        ),
        &["command", "value"],
        &[],
    );
}

#[test]
fn void_effectful_and_property_operands_keep_callback_reentry() {
    expect_callback_reentry_for_class("class Checker { result = void globalThis.saved(); }", "");
    expect_callback_reentry_for_class(
        "class Checker { result = void receiver.value; }",
        "const receiver = { get value() { globalThis.saved(); } };",
    );
    expect_callback_reentry_for_class(
        "class Checker { result = void receiver[key]; }",
        r#"const receiver = { value: 1 };
const key = { [Symbol.toPrimitive]() { globalThis.saved(); return "value"; } };"#,
    );
}

#[test]
fn an_invoking_explicit_constructor_keeps_callback_reentry() {
    expect_callback_reentry_for_class(
        "class Checker { result = void 0; constructor() { globalThis.saved(); } }",
        "",
    );
}

fn assert_unproven_construction_stays_sequenced(prefix: &str, class: &str, initializer: &str) {
    let source = format!(
        r#"globalThis.events = [];
{prefix}
const a = (() => 1)();
{class}
const b = {initializer};
const c = b ? a : 0;
console.log(c);
export {{ a, b, c, Registry }};
"#
    );
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            &source,
            vec![logical_module(
                "registry",
                &[Member::new("Registry"), Member::new("b")],
            )],
        ),
        &["registry", "residual"],
    );
}

#[test]
fn effectful_and_unknown_field_initializers_stay_sequenced() {
    assert_unproven_construction_stays_sequenced(
        "",
        "class Registry { value = globalThis.events.push('init'); }",
        "new Registry()",
    );
    assert_unproven_construction_stays_sequenced(
        "const values = [];",
        "class Registry { value = new Set(values); }",
        "new Registry()",
    );
    assert_unproven_construction_stays_sequenced(
        "function Set() { globalThis.events.push('init'); }",
        "class Registry { value = new Set(); }",
        "new Registry()",
    );
}

#[test]
fn unsupported_class_surfaces_stay_sequenced() {
    for class in [
        "class Registry { constructor() { this.value = 1; } }",
        "class Registry extends Object {}",
        "class Registry { [globalThis.key] = 1; }",
        "class Registry { static value = globalThis.events.push('init'); }",
    ] {
        assert_unproven_construction_stays_sequenced("", class, "new Registry()");
    }
}

#[test]
fn rebound_class_binding_stays_sequenced() {
    let source = r#"const a = (() => 1)();
class Registry { value = 1; }
Registry = class { value = 2; };
const b = new Registry();
const c = b ? a : 0;
console.log(c);
export { a, b, c, Registry };
"#;
    let rejected = run_rejection_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "registry",
            &[Member::new("Registry"), Member::new("b")],
            &["Registry = class { value = 2; };"],
        )],
    ));
    let graph = rejected.dependency_graph();
    let constructed = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "b")
        })
        .expect("construction source unit");
    let purity = serde_json::to_value(&constructed.purity).unwrap();
    assert_eq!(purity["kind"], "not_pure");
    assert!(
        purity["reasons"]
            .as_array()
            .unwrap()
            .iter()
            .any(|reason| reason["rule"] == "unknown_new")
    );
}

#[test]
fn constructor_arguments_stay_sequenced() {
    for initializer in ["new Registry(1)", "new Registry(...[])"] {
        assert_unproven_construction_stays_sequenced(
            "",
            "class Registry { value = 1; }",
            initializer,
        );
    }
}
