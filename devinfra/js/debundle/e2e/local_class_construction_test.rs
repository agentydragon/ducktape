//! A checked implicit constructor can be reordered without executing its
//! deferred methods or field callbacks during module initialization.

use debundle_e2e_support::*;

#[test]
fn inferred_implicit_class_construction_breaks_sequenced_cycle() {
    let source = r#"globalThis.events = [];
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
    let fixture = assert_pure_cycle_break(
        source,
        vec![logical_module(
            "registry",
            &[Member::new("Registry"), Member::new("b")],
        )],
        "registry",
        &["class Registry", "const b = new Registry"],
        &["const a"],
        "1 clear\n",
    );
    assert_preserved_entry_output(&fixture, "1 clear\n");
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
