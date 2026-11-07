//! Immediate reads of known own data fields do not run getter code.
//! The factory's eager effects and unsupported result shapes remain ordered.

use analysis::reports::schema::{
    BindingEffectAuthorityReport, LexicalBindingEffectBody, LexicalEffectRoot,
};
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

fn assert_object_binding_receipt(fixture: &Fixture, binding: &str, expected: &[(&str, &str)]) {
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap();
    let execution = node.synchronous_execution.as_ref().unwrap();
    assert!(execution.complete, "{execution:#?}");
    let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
        &execution.binding_effect_authority
    else {
        panic!("object binding must retain exact operation authority: {execution:#?}");
    };
    assert_eq!(receipt.owner, node.statement_ordinal);
    let LexicalEffectRoot::ObjectBindingInitializer {
        pattern_span,
        declarator_span,
        initializer_span,
        targets,
    } = &receipt.root
    else {
        panic!("all targets must belong to the original object-binding root: {receipt:#?}");
    };
    assert_eq!(
        targets
            .iter()
            .map(|target| (target.property.as_str(), target.binding.as_str()))
            .collect::<Vec<_>>(),
        expected
    );
    assert!(
        declarator_span.lo <= pattern_span.lo
            && pattern_span.hi <= initializer_span.lo
            && initializer_span.hi <= declarator_span.hi
    );
    for target in targets {
        assert!(pattern_span.lo <= target.span.lo && target.span.hi <= pattern_span.hi);
        assert!(
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == target.binding)
        );
    }
}

fn assert_object_binding_uncovered(
    fixture: &Fixture,
    binding: &str,
    prefix_property: Option<&str>,
) {
    let graph = fixture.dependency_graph();
    let node = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding == binding)
        })
        .unwrap();
    let execution = node.synchronous_execution.as_ref().unwrap();
    assert!(
        matches!(
            &execution.binding_effect_authority,
            Some(BindingEffectAuthorityReport::OrderedRootObjectBindingUncovered { .. })
        ),
        "{execution:#?}"
    );
    if let Some(property) = prefix_property {
        let heap = execution
            .heap_frontier
            .as_ref()
            .expect("executed prefix property read remains visible");
        assert!(heap.non_assumed_heap_accesses.reads.sample.iter().any(|cell| matches!(cell, analysis::graph::EffectCellReport::OwnProperty { property: read, .. } if read == property)), "{heap:#?}");
    }
}

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
    for binding in ["first", "middle", "last"] {
        assert_object_binding_receipt(&fixture, binding, &[("selected", binding)]);
    }
    let graph = fixture.dependency_graph();
    let first = source_unit_for_binding(&graph, "first");
    let seed = source_unit_for_binding(&graph, "seed");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == first
            && edge.target == seed
            && edge.edge_kind == analysis_model::DepKind::EagerUse
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
    for binding in ["first", "middle", "last"] {
        assert_object_binding_receipt(&fixture, binding, &[("selected", binding)]);
    }
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
fn rebound_factory_selects_the_executed_replacement() {
    let source = declarations(
        "function factory() { return { selected: () => 1 }; }\nfactory = () => ({ selected: () => 2 });",
    ).replace("console.log(typeof first, typeof middle, typeof last);", "console.log(first(), middle(), last());");
    let fixture = run_fixture(split(&source));
    assert_preserved_entry_output(&fixture, "2 2 2\n");
    for binding in ["first", "middle", "last"] {
        assert_object_binding_receipt(&fixture, binding, &[("selected", binding)]);
    }
    let graph = fixture.dependency_graph();
    for binding in ["first", "middle", "last"] {
        let unit = source_unit_for_binding(&graph, binding);
        let node = graph.nodes.iter().find(|node| node.id == unit).unwrap();
        let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) = &node
            .synchronous_execution
            .as_ref()
            .unwrap()
            .binding_effect_authority
        else {
            unreachable!()
        };
        assert_eq!(receipt.calls.len(), 1, "{receipt:#?}");
        assert!(
            matches!(&receipt.calls[0].target, LexicalBindingEffectBody::Callable { owner, .. } if owner.0 == 1),
            "receipt must execute the replacement's source owner: {receipt:#?}"
        );
    }
}

#[test]
fn effectful_replacement_factory_keeps_its_body_effects() {
    let source = declarations(
        "function factory() { return { selected: () => 1 }; }\nfactory = () => { console.log('replacement'); return { selected: () => 2 }; };",
    ).replace("console.log(typeof first, typeof middle, typeof last);", "console.log(first(), middle(), last());");
    let fixture = run_inspection_fixture(split(&source));
    assert_preserved_entry_output(&fixture, "replacement\nreplacement\nreplacement\n2 2 2\n");
    for binding in ["first", "middle", "last"] {
        assert_object_binding_uncovered(&fixture, binding, None);
    }
}

#[test]
fn unproved_module_mutability_retains_the_replacement_normal_path() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
class factory {}
factory = () => ({ selected: () => 2 });
const { selected: first } = factory();
console.log(first());
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
    assert_object_binding_uncovered(&fixture, "first", None);
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

#[test]
fn multiple_object_targets_evaluate_rhs_once_and_keep_callable_captures() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
let calls = 0;
function factory() { calls++; return { left: () => second, right: 42 }; }
const { left: first, right: second } = factory();
console.log(calls, first(), second);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "1 42 42\n");
    assert_object_binding_receipt(&fixture, "first", &[("left", "first"), ("right", "second")]);
}

#[test]
fn callable_local_object_binding_uses_the_same_ordered_operations() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
function factory() {
    const { left: first, right: second } = { left: () => 40, right: 2 };
    return first() + second;
}
const result = factory();
console.log(result);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "42\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .unwrap();
    assert!(matches!(
        &result
            .synchronous_execution
            .as_ref()
            .unwrap()
            .binding_effect_authority,
        Some(BindingEffectAuthorityReport::OperationReceipt { .. })
    ));
}

#[test]
fn later_unproved_property_retains_prefix_without_sealing_or_replaying_rhs() {
    for (property, expected) in [
        (
            "get right() { console.log(first); return 2; }",
            "1\n1 1 2\n",
        ),
        ("other: 2", "1 1 undefined\n"),
    ] {
        let source = format!(
            "let calls = 0; function factory() {{ calls++; return {{ left: 1, {property} }}; }} const {{ left: first, right: second }} = factory(); console.log(calls, first, second);"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, expected);
        assert_object_binding_uncovered(&fixture, "first", Some("left"));
    }
}

#[test]
fn abrupt_object_binding_rhs_never_seals_target_initialization() {
    let fixture = run_fixture(FixtureOpts::new(
        "function factory() { throw 'rhs failure'; } const { left: first, right: second } = factory();",
        vec![],
    ));
    assert_preserved_entry_error(&fixture, "rhs failure");
    assert_object_binding_uncovered(&fixture, "first", None);
}

#[test]
fn unproved_getter_result_keeps_the_normal_callback_continuation_reachable() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
const events = [];
const { left: first, right: callback } = { left: 1, get right() { return () => events.push('callback'); } };
callback();
console.log(first, events.join(','));
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "1 callback\n");
    assert_object_binding_uncovered(&fixture, "first", Some("left"));
    let graph = fixture.dependency_graph();
    let call = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .unwrap();
    let execution = call.synchronous_execution.as_ref().unwrap();
    assert!(
        execution.precision_losses.iter().any(|loss| loss.reason
            == analysis::execution_evidence::ExecutionPrecisionLossReason::UnknownCallTarget),
        "the normal Top alternative must reach call fallback rather than being pruned as definite TDZ: {execution:#?}"
    );
    let events = source_unit_for_binding(&graph, "events");
    assert!(
        graph.edges.iter().any(|edge| edge.source == call.id
            && edge.target == events
            && edge.edge_kind == analysis_model::DepKind::EagerUse),
        "the callback continuation must retain its events dependency: {graph:#?}"
    );
}
