//! Immediate reads of known own data fields do not run getter code.
//! The factory's eager effects and unsupported result shapes remain ordered.

use debundle_e2e_support::*;

fn first_reasons(source: &str) -> Vec<analysis::PurityRule> {
    let rejected = run_rejection_fixture(split(source));
    let graph = rejected.dependency_graph();
    let first = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "first")
        })
        .expect("first source unit");
    let analysis::Purity::NotPure { reasons } = &first.purity else {
        panic!("first should retain an effect reason");
    };
    reasons.iter().map(|reason| reason.rule).collect()
}

fn split(source: &str) -> FixtureOpts<'_> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("outer", &[Member::new("first"), Member::new("last")]),
            logical_module("inner", &[Member::new("middle")]),
        ],
    )
}

fn declarations(factory: &str) -> String {
    format!(
        "{factory}\n\
         const {{ selected: first }} = factory();\n\
         const {{ selected: middle }} = factory();\n\
         const {{ selected: last }} = factory();\n\
         console.log(typeof first, typeof middle, typeof last);\n\
         export {{ first, middle, last }};\n"
    )
}

#[test]
fn fresh_factory_returned_record_can_split() {
    let source = declarations(
        "function factory() { const entries = new Set(); return { selected: value => entries.add(value) }; }",
    );
    let fixture = run_fixture(split(&source));
    assert_preserved_entry_output(&fixture, "function function function\n");
}

#[test]
fn fresh_factory_keeps_eager_binding_dependency() {
    let source = r#"const seed = 'ready';
function factory() { return { selected: seed }; }
const { selected: first } = factory();
const { selected: middle } = factory();
const { selected: last } = factory();
console.log(first, middle, last);
export { first, middle, last };
"#;
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![
            logical_module("outer", &[Member::new("first"), Member::new("last")]),
            logical_module("inner", &[Member::new("seed"), Member::new("middle")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "ready ready ready\n");
    let graph = fixture.dependency_graph();
    let first = source_unit_for_binding(&graph, "first");
    let seed = source_unit_for_binding(&graph, "seed");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == first && edge.target == seed && edge.edge_kind == analysis::DepKind::EagerUse
    }));
}

#[test]
fn fresh_literal_reads_can_split() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const { selected: first } = { selected: () => 1 };
const { selected: middle } = { selected: () => 2 };
const { selected: last } = { selected: () => 3 };
console.log(first(), middle(), last());
export { first, middle, last };
"#,
        vec![
            logical_module("outer", &[Member::new("first"), Member::new("last")]),
            logical_module("inner", &[Member::new("middle")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1 2 3\n");
}

#[test]
fn effectful_factory_keeps_call_order() {
    let source = declarations(
        "function factory() { console.log('factory'); return { selected: () => 1 }; }",
    );
    expect_sequenced_cycle_rejection(split(&source), &["outer", "inner"]);
    let reasons = first_reasons(&source);
    assert!(reasons.contains(&analysis::PurityRule::ImpureFunctionCall));
    assert!(!reasons.contains(&analysis::PurityRule::DestructuringPattern));
}

#[test]
fn getter_factory_keeps_destructuring_order() {
    let source = declarations(
        "function factory() { return { get selected() { console.log('getter'); return () => 1; } }; }",
    );
    expect_sequenced_cycle_rejection(split(&source), &["outer", "inner"]);
    assert!(first_reasons(&source).contains(&analysis::PurityRule::DestructuringPattern));
}

#[test]
fn alternate_return_or_missing_key_keeps_destructuring_order() {
    for factory in [
        "function factory(flag) { if (flag) return { other: 1 }; return { selected: () => 1 }; }",
        "function factory() { return { other: () => 1 }; }",
        "async function factory() { return { selected: () => 1 }; }",
        "function* factory() { return { selected: () => 1 }; }",
        "function factory(flag) { if (flag) return { selected: () => 1 }; }",
        "const factory = globalThis.factory;",
    ] {
        let source = declarations(factory);
        expect_sequenced_cycle_rejection(split(&source), &["outer", "inner"]);
    }
}

#[test]
fn rebound_factory_keeps_destructuring_order() {
    let source = declarations(
        "function factory() { return { selected: () => 1 }; }\nfactory = () => ({ selected: () => 2 });",
    );
    assert!(first_reasons(&source).contains(&analysis::PurityRule::DestructuringPattern));
}

#[test]
fn dynamic_scope_keeps_destructuring_order() {
    let source = declarations("function factory() { eval(''); return { selected: () => 1 }; }");
    assert!(first_reasons(&source).contains(&analysis::PurityRule::DestructuringPattern));
}

#[test]
fn defaults_rest_and_nested_patterns_stay_conservative() {
    let factory = "function factory() { return { selected: () => 1 }; }";
    for pattern in [
        "selected: first = () => 2",
        "selected: first, ...unused",
        "selected: { value: first }",
    ] {
        let first = pattern.replace("unused", "unused_first");
        let middle = pattern
            .replace("first", "middle")
            .replace("unused", "unused_middle");
        let last = pattern
            .replace("first", "last")
            .replace("unused", "unused_last");
        let source = format!(
            "{factory}\n\
             const {{ {first} }} = factory();\n\
             const {{ {middle} }} = factory();\n\
             const {{ {last} }} = factory();\n\
             console.log(typeof first, typeof middle, typeof last);\n\
             export {{ first, middle, last }};\n"
        );
        assert!(first_reasons(&source).contains(&analysis::PurityRule::DestructuringPattern));
    }
}
