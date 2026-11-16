//! Immediate reads of known own data fields do not run getter code.
//! The factory's eager effects and unsupported result shapes remain ordered.

use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

fn first_reasons(source: &str) -> Vec<analysis::PurityRule> {
    first_reasons_of(split(source))
}

fn first_reasons_of(opts: FixtureOpts<'_>) -> Vec<analysis::PurityRule> {
    let rejected = run_rejection_fixture_with_graphs(opts);
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

fn assert_fresh_destructuring_proof(rejected: &RejectedFixture) {
    let graph = rejected.dependency_graph();
    let first_id = source_unit_for_binding(&graph, "first");
    let first = graph
        .nodes
        .iter()
        .find(|node| node.id == first_id)
        .expect("first source unit");
    if let analysis::Purity::NotPure { reasons } = &first.purity {
        assert!(
            !reasons
                .iter()
                .any(|reason| reason.rule == analysis::PurityRule::DestructuringPattern),
            "the graph should retain its own-data destructuring proof: {first:#?}"
        );
    }
}

fn assert_destructuring_completion_refusal(
    opts: FixtureOpts<'_>,
    expected_output: &str,
) -> RejectedFixture {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "expected the ordered-IR completion boundary to refuse this split:\n{}",
        rejected.stderr
    );
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        expected_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), expected_output, "");
    assert_fresh_destructuring_proof(&rejected);
    rejected
}

#[test]
fn fresh_factory_returned_record_split_reports_destructuring_completion_limit() {
    let source = declarations(
        "function factory() { const entries = new Set(); return { selected: value => entries.add(value) }; }",
    );
    let rejected =
        assert_destructuring_completion_refusal(split(&source), "function function function\n");
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/outer.js",
        &["first", "last"],
        &["middle"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/inner.js",
        &["middle"],
        &["first", "last"],
    );
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        &source,
        vec![logical_module(
            "records/all",
            &[
                Member::new("factory"),
                Member::new("first"),
                Member::new("middle"),
                Member::new("last"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "function function function\n");
}

#[test]
fn fresh_factory_eager_binding_split_reports_destructuring_completion_limit() {
    let source = r#"const seed = 'ready';
function factory() { return { selected: seed }; }
const { selected: first } = factory();
const { selected: middle } = factory();
const { selected: last } = factory();
console.log(first, middle, last);
export { first, middle, last };
"#;
    let split = FixtureOpts::new(
        source,
        vec![
            logical_module("outer", &[Member::new("first"), Member::new("last")]),
            logical_module("inner", &[Member::new("seed"), Member::new("middle")]),
        ],
    );
    let rejected = assert_destructuring_completion_refusal(split, "ready ready ready\n");
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/outer.js",
        &["first", "last"],
        &["seed", "middle"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/inner.js",
        &["seed", "middle"],
        &["first", "last"],
    );
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module(
            "records/all",
            &[
                Member::new("seed"),
                Member::new("factory"),
                Member::new("first"),
                Member::new("middle"),
                Member::new("last"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "ready ready ready\n");
}

#[test]
fn fresh_literal_split_reports_destructuring_completion_limit() {
    let source = r#"const { selected: first } = { selected: () => 1 };
const { selected: middle } = { selected: () => 2 };
const { selected: last } = { selected: () => 3 };
console.log(first(), middle(), last());
export { first, middle, last };
"#;
    let rejected = assert_destructuring_completion_refusal(
        FixtureOpts::new(
            source,
            vec![
                logical_module("outer", &[Member::new("first"), Member::new("last")]),
                logical_module("inner", &[Member::new("middle")]),
            ],
        ),
        "1 2 3\n",
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/outer.js",
        &["first", "last"],
        &["middle"],
    );
    assert_module_exports(
        &rejected.app_root(),
        "static/app/modules/inner.js",
        &["middle"],
        &["first", "last"],
    );
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module(
            "records/all",
            &[
                Member::new("first"),
                Member::new("middle"),
                Member::new("last"),
            ],
        )],
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
    // A direct `eval` is refused at admission (A1); the override keeps
    // the analysis running.
    let source = declarations("function factory() { eval(''); return { selected: () => 1 }; }");
    assert!(
        first_reasons_of(split(&source).with_admission_overrides(&["a1_eval"]))
            .contains(&analysis::PurityRule::DestructuringPattern)
    );
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
