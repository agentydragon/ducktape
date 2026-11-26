use super::*;
use crate::facts::value_flow::Values;
use analysis_vocabulary::value::{Kind, Value};
use swc_common::SyntaxContext;

fn id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
}

fn has_function(values: &Values) -> bool {
    values
        .iter()
        .any(|value| matches!(value, Value::Alloc(Kind::Function, _)))
}

fn assert_matches_reference(source: &str) -> ChunkValueFlow {
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    let actual = ChunkValueFlow::analyze(&body);
    let reference = ChunkValueFlow::analyze_reference(&body);
    assert_eq!(
        actual, reference,
        "value-flow mismatch for source:\n{source}"
    );

    // The raw parse above preserves empty SyntaxContexts for the individual
    // binding assertions below. Also compare after the production resolver:
    // its fresh Marks distinguish same-named bindings in separate scopes,
    // which the value-flow worklist and argument cache must handle too.
    let resolved_module = js_ast::with_swc_globals(|| {
        js_ast::parse_js_module_ast("<value-flow-test>", source)
            .expect("value-flow test source should parse and resolve")
    });
    let resolved_body = top_level_item_views(&resolved_module.body);
    let resolved_actual = ChunkValueFlow::analyze(&resolved_body);
    let resolved_reference = ChunkValueFlow::analyze_reference(&resolved_body);
    assert_eq!(
        resolved_actual, resolved_reference,
        "resolved value-flow mismatch for source:\n{source}"
    );
    actual
}

/// Analyzes `source`, checking the result against the reference analysis.
fn analyze_checked(source: &str) -> (ChunkValueFlow, super::value_flow::FlowWorkStats) {
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    let (flow, stats) = ChunkValueFlow::analyze_with_stats(&body);
    assert_eq!(flow, ChunkValueFlow::analyze_reference(&body));
    (flow, stats)
}

#[test]
fn independent_identity_projections_have_linear_property_reads() {
    use std::fmt::Write as _;

    for count in [8, 32, 128] {
        let mut source = String::from("function relay(value) { return value; }\n");
        for index in 0..count {
            writeln!(source, "function callback{index}() {{ return {index}; }}").unwrap();
            writeln!(
                source,
                "const selected{index} = relay({{ value: callback{index} }}).value;"
            )
            .unwrap();
        }
        let module = raw_js_test_support::parse(&source);
        let body = top_level_item_views(&module.body);
        let (flow, stats) = ChunkValueFlow::analyze_with_stats(&body);
        for index in 0..count {
            let actual = flow.values_of_binding(&id(&format!("selected{index}")));
            let callback = flow.values_of_binding(&id(&format!("callback{index}")));
            assert_eq!(
                actual
                    .iter()
                    .filter(|origin| *origin != Value::Top)
                    .collect::<Vec<_>>(),
                callback.iter().collect::<Vec<_>>(),
                "independent callback origins were pooled at width {count}"
            );
        }
        assert!(
            stats.property_possible_reads_tracked <= 4 * count,
            "independent projections replayed a pooled receiver set: {stats:?}"
        );
    }
}

#[test]
fn candidate_object_index_matches_ordered_reference_for_cycles_and_external_candidates() {
    let source = r#"
const alpha = {};
const beta = {};
const unknown_box = { value: missing_global };
alpha.peer = beta;
beta.peer = alpha;
function cycle() { return alpha; }
alpha.callback = cycle;
function return_cycle() { return cycle(); }
const rooted = return_cycle();
"#;
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    let flow = ChunkValueFlow::analyze(&body);

    let mut candidates = BTreeSet::from([Value::Top]);
    for name in ["alpha", "beta", "unknown_box", "rooted"] {
        candidates.extend(
            flow.values_of_binding(&id(name))
                .iter()
                .filter(|origin| matches!(origin, Value::Alloc(Kind::Object, _))),
        );
    }
    candidates.extend(
        flow.values_of_binding(&id("cycle"))
            .iter()
            .filter(|origin| matches!(origin, Value::Alloc(Kind::Function, _))),
    );

    // This candidate comes from a second parsed source with a much larger
    // source span, so it is outside the first flow's collected origin table.
    // The old BTreeMap index accepted it as a seed; the dense index must retain
    // that behavior without relying on an unchecked origin-table lookup.
    let foreign_source = format!("/*{}*/ const foreign = {{}};", "padding".repeat(512));
    let foreign_module = raw_js_test_support::parse(&foreign_source);
    let foreign_body = top_level_item_views(&foreign_module.body);
    let foreign_flow = ChunkValueFlow::analyze(&foreign_body);
    let [Value::Alloc(Kind::Object, foreign_span)] = foreign_flow
        .values_of_binding(&id("foreign"))
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("foreign binding should have one object identity");
    };
    assert!(foreign_span > source.len() as u32);
    let foreign = Value::Alloc(Kind::Object, foreign_span);
    assert!(candidates.insert(foreign));

    let flow = ChunkValueFlow::analyze_with_candidates(&body, &candidates);
    let actual = flow.candidate_object_index_maps_for_test(&candidates);
    let expected = flow.candidate_object_index_reference_for_test(&candidates);
    assert_eq!(actual, expected);

    let Value::Alloc(Kind::Object, alpha_span) = *candidates
        .iter()
        .find(|origin| {
            matches!(origin, Value::Alloc(Kind::Object, _))
                && flow.values_of_binding(&id("alpha")).contains(origin)
        })
        .expect("alpha should be a candidate object")
    else {
        panic!("alpha should have an object identity");
    };
    let alpha = Value::Alloc(Kind::Object, alpha_span);
    let alpha_reachable = &actual.0[&alpha];
    let [Value::Alloc(Kind::Function, cycle_span)] = flow
        .values_of_property(alpha_span, "callback")
        .expect("cycle property should have a value")
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("cycle property should contain one function identity");
    };
    assert!(alpha_reachable.contains(&Value::Alloc(Kind::Function, cycle_span)));
    assert!(
        alpha_reachable.contains(&Value::Alloc(
            Kind::Object,
            flow.values_of_property(alpha_span, "peer")
                .expect("peer property should have a value")
                .iter()
                .find_map(|origin| match origin {
                    Value::Alloc(Kind::Object, span) => Some(span),
                    _ => None,
                })
                .expect("peer property should point to beta"),
        ))
    );
    let unknown_reachable = &actual.0[&Value::Top];
    assert!(unknown_reachable.contains(&Value::Top));
    assert_eq!(actual.0[&foreign], BTreeSet::from([foreign]));
    assert!(actual.1.contains_key(&id("rooted")));
}

#[test]
fn independent_candidate_seeds_use_storage_proportional_to_reached_roots() {
    const CANDIDATE_COUNT: usize = 512;
    let mut source = String::new();
    for index in 0..CANDIDATE_COUNT {
        source.push_str(&format!("const array{index} = [];\n"));
    }
    let module = raw_js_test_support::parse(&source);
    let body = top_level_item_views(&module.body);
    let initial = ChunkValueFlow::analyze(&body);
    let candidates: BTreeSet<_> = (0..CANDIDATE_COUNT)
        .flat_map(|index| initial.values_of_binding(&id(&format!("array{index}"))))
        .collect();
    assert_eq!(candidates.len(), CANDIDATE_COUNT);

    let flow = ChunkValueFlow::analyze_with_candidates(&body, &candidates);
    let (set_count, cardinality, payload_bytes) = flow.candidate_reachability_storage_for_test();
    assert_eq!(set_count, CANDIDATE_COUNT);
    assert_eq!(cardinality, CANDIDATE_COUNT as u64);
    assert!(
        payload_bytes <= (CANDIDATE_COUNT * 16) as u64,
        "one-root seeds should use bounded payload per reached root: {payload_bytes} bytes"
    );
}

#[test]
fn candidate_value_union_visits_each_requested_root_once() {
    let mut source = String::new();
    for index in 0..129 {
        source.push_str(&format!("const array{index} = [];\n"));
    }
    source.push_str("const unused = [];\n");
    for (name, range) in [("left", 0..97), ("right", 32..129)] {
        source.push_str(&format!("const {name} = {{"));
        for index in range {
            source.push_str(&format!("field{index}: array{index},"));
        }
        source.push_str("};\n");
    }
    source.push_str("const escaped = condition ? left : right;\n");
    let module = raw_js_test_support::parse(&source);
    let body = top_level_item_views(&module.body);
    let initial = ChunkValueFlow::analyze(&body);
    let candidates: BTreeSet<_> = (0..129)
        .map(|index| format!("array{index}"))
        .chain(std::iter::once("unused".to_owned()))
        .flat_map(|name| initial.values_of_binding(&id(&name)))
        .collect();
    let flow = ChunkValueFlow::analyze_with_candidates(&body, &candidates);
    let values = flow.values_of_binding(&id("escaped"));
    assert_eq!(values.iter().count(), 2);

    // Exercise dense, sparse, disjoint, and empty masks against the independent
    // ordered-map oracle. The sparse mask spans multiple bitmap words.
    let requested_masks = [
        candidates.clone(),
        candidates.iter().step_by(3).copied().collect(),
        BTreeSet::from([initial
            .values_of_binding(&id("unused"))
            .iter()
            .next()
            .unwrap()]),
        BTreeSet::new(),
    ];
    for requested in requested_masks {
        let reference = flow.candidate_object_index_reference_for_test(&requested).0;
        let expected: BTreeSet<_> = values
            .iter()
            .flat_map(|value| reference.get(&value).into_iter().flatten().copied())
            .collect();
        let mut visited = Vec::new();
        flow.candidate_object_index(&requested)
            .for_values(&values, |root| visited.push(root));
        assert_eq!(visited, expected.into_iter().collect::<Vec<_>>());
    }

    // A mask reached completely by the first value saturates immediately; the
    // result must still match the map oracle exactly.
    let left_values = flow.values_of_binding(&id("left"));
    let all_roots = flow
        .candidate_object_index_reference_for_test(&candidates)
        .0;
    let saturated_mask: BTreeSet<_> = left_values
        .iter()
        .flat_map(|value| all_roots.get(&value).into_iter().flatten().copied())
        .collect();
    assert!(!saturated_mask.is_empty());
    let reference = flow
        .candidate_object_index_reference_for_test(&saturated_mask)
        .0;
    let expected: BTreeSet<_> = left_values
        .iter()
        .flat_map(|value| reference.get(&value).into_iter().flatten().copied())
        .collect();
    let mut visited = Vec::new();
    flow.candidate_object_index(&saturated_mask)
        .for_values(&left_values, |root| visited.push(root));
    assert_eq!(visited, expected.into_iter().collect::<Vec<_>>());
}

#[test]
fn candidate_value_queries_intersect_each_shared_summary_once() {
    let source = r#"
const alpha = {};
const beta = {};
const unrelated = {};
alpha.peer = beta;
beta.peer = alpha;
const either = true ? alpha : beta;
const primitive = 1;
const unknown = missing_global;
"#;
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    let initial = ChunkValueFlow::analyze(&body);
    let allocation = |name: &str| {
        initial
            .values_of_binding(&id(name))
            .iter()
            .find(|value| matches!(value, Value::Alloc(Kind::Object, _)))
            .unwrap_or_else(|| panic!("{name} should have an object identity"))
    };
    let alpha = allocation("alpha");
    let beta = allocation("beta");
    let unrelated = allocation("unrelated");
    let candidates = BTreeSet::from([alpha, beta, unrelated, Value::Top]);
    let flow = ChunkValueFlow::analyze_with_candidates(&body, &candidates);

    assert!(flow.candidate_summaries_share_for_test(alpha, beta));
    let either = flow.values_of_binding(&id("either"));
    assert_eq!(
        either.iter().collect::<BTreeSet<_>>(),
        BTreeSet::from([alpha, beta])
    );

    let expected = |mask: &BTreeSet<Value>, values: &Values| {
        let reference = flow.candidate_object_index_reference_for_test(mask).0;
        values
            .iter()
            .flat_map(|value| reference.get(&value).into_iter().flatten().copied())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>()
    };
    let query = |mask: &BTreeSet<Value>, values: &Values| {
        let index = flow.candidate_object_index(mask);
        let mut visited = Vec::new();
        index.for_values(values, |root| visited.push(root));
        (visited, index.summary_intersections_for_test())
    };

    // Two origins share one retained SCC bitmap. The exact union is unchanged,
    // while the work count is one intersection rather than one per origin.
    let (all_roots, work) = query(&candidates, &either);
    assert_eq!(all_roots, expected(&candidates, &either));
    assert_eq!(all_roots, vec![alpha, beta]);
    assert_eq!(work, 1);

    // A sparse partial mask keeps only alpha; a disjoint mask gives no roots.
    let sparse = BTreeSet::from([alpha, unrelated]);
    let (sparse_roots, sparse_work) = query(&sparse, &either);
    assert_eq!(sparse_roots, expected(&sparse, &either));
    assert_eq!(sparse_roots, vec![alpha]);
    assert_eq!(sparse_work, 1);

    let disjoint = BTreeSet::from([unrelated]);
    let (disjoint_roots, disjoint_work) = query(&disjoint, &either);
    assert_eq!(disjoint_roots, expected(&disjoint, &either));
    assert!(disjoint_roots.is_empty());
    assert_eq!(disjoint_work, 1);

    // Primitive origins have no completed reachability row; Top retains its
    // own exact seed and still produces the ordered reference result.
    let primitive = flow.values_of_binding(&id("primitive"));
    let (primitive_roots, primitive_work) = query(&candidates, &primitive);
    assert_eq!(primitive_roots, expected(&candidates, &primitive));
    assert!(primitive_roots.is_empty());
    assert_eq!(primitive_work, 0);

    let top_mask = BTreeSet::from([Value::Top]);
    let top = flow.values_of_binding(&id("unknown"));
    assert_eq!(top.iter().collect::<Vec<_>>(), vec![Value::Top]);
    let (top_roots, top_work) = query(&top_mask, &top);
    assert_eq!(top_roots, expected(&top_mask, &top));
    assert_eq!(top_roots, vec![Value::Top]);
    assert_eq!(top_work, 1);
}

#[test]
fn candidate_reachability_tracks_late_values_and_call_aliases() {
    let source = r#"
const leaf = {};
const outer = {};
let delayed;
outer.child = delayed;
delayed = leaf;
function identity(value) { return value; }
function relay(argument) { return identity(argument); }
const alias = relay(outer);
function make() { return alias; }
const returned = make();
"#;
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    let initial = ChunkValueFlow::analyze(&body);

    let mut candidates = BTreeSet::new();
    for name in ["leaf", "outer"] {
        candidates.extend(
            initial
                .values_of_binding(&id(name))
                .iter()
                .filter(|origin| matches!(origin, Value::Alloc(Kind::Object, _))),
        );
    }
    for name in ["identity", "relay", "make"] {
        candidates.extend(
            initial
                .values_of_binding(&id(name))
                .iter()
                .filter(|origin| matches!(origin, Value::Alloc(Kind::Function, _))),
        );
    }

    let (_, without_candidates) = ChunkValueFlow::analyze_with_stats(&body);
    let (flow, with_candidates) =
        ChunkValueFlow::analyze_with_candidates_and_stats(&body, &candidates);
    assert_eq!(
        without_candidates.event_executions, with_candidates.event_executions,
        "candidate growth must not reevaluate AST events"
    );
    assert_eq!(
        without_candidates.argument_evaluations, with_candidates.argument_evaluations,
        "candidate growth must not reevaluate call arguments"
    );
    let actual = flow.candidate_object_index_maps_for_test(&candidates);
    assert_eq!(
        actual,
        flow.candidate_object_index_reference_for_test(&candidates)
    );

    let binding_origin = |name: &str, is_object: fn(&Value) -> bool| {
        flow.values_of_binding(&id(name))
            .iter()
            .find(is_object)
            .unwrap_or_else(|| panic!("{name} should have a matching value origin"))
    };
    let Value::Alloc(Kind::Object, leaf) = binding_origin("leaf", |origin| {
        matches!(origin, Value::Alloc(Kind::Object, _))
    }) else {
        unreachable!();
    };
    let Value::Alloc(Kind::Object, outer) = binding_origin("outer", |origin| {
        matches!(origin, Value::Alloc(Kind::Object, _))
    }) else {
        unreachable!();
    };
    let Value::Alloc(Kind::Function, relay) = binding_origin("relay", |origin| {
        matches!(origin, Value::Alloc(Kind::Function, _))
    }) else {
        unreachable!();
    };
    let Value::Alloc(Kind::Function, make) = binding_origin("make", |origin| {
        matches!(origin, Value::Alloc(Kind::Function, _))
    }) else {
        unreachable!();
    };

    assert!(
        actual.0[&Value::Alloc(Kind::Object, outer)].contains(&Value::Alloc(Kind::Object, leaf))
    );
    assert!(
        actual.0[&Value::Alloc(Kind::Function, relay)].contains(&Value::Alloc(Kind::Object, leaf))
    );
    assert!(
        actual.0[&Value::Alloc(Kind::Function, make)].contains(&Value::Alloc(Kind::Object, leaf))
    );
    for name in ["alias", "returned"] {
        assert!(actual.1[&id(name)].contains(&Value::Alloc(Kind::Object, leaf)));
    }
}

fn return_call_chain(depth: usize, reverse_declarations: bool) -> String {
    let mut source = String::from("function base() {}\n");
    if reverse_declarations {
        source.push_str(&format!("function f{depth}() {{ return base; }}\n"));
        for index in (0..depth).rev() {
            source.push_str(&format!(
                "function f{index}() {{ return f{}(); }}\n",
                index + 1
            ));
        }
    } else {
        for index in 0..depth {
            source.push_str(&format!(
                "function f{index}() {{ return f{}(); }}\n",
                index + 1
            ));
        }
        source.push_str(&format!("function f{depth}() {{ return base; }}\n"));
    }
    source.push_str("const result = f0();\n");
    source
}

fn reverse_identity_alias_chain(depth: usize) -> String {
    let mut source = String::from("function base() {}\n");
    for index in 0..depth {
        source.push_str(&format!(
            "function f{index}() {{ return f{}; }}\n",
            index + 1
        ));
    }
    source.push_str(&format!(
        "function f{depth}() {{ return base; }}\nconst result = f0();\n"
    ));
    source
}

fn invoke_return_set_fanout(target_count: usize, feedback: bool) -> String {
    let mut source = String::from("function f0(callback) { poly(callback()); }\n");
    for index in 1..target_count {
        source.push_str(&format!("function f{index}(p{index}) {{}}\n"));
    }
    source.push_str(
        "function target() { return leaf; }\n\
         function leaf() { return extra; }\n\
         function extra() {}\n",
    );
    source.push_str("const poly = f0");
    for index in 1..target_count {
        source.push_str(&format!(" || f{index}"));
    }
    source.push_str(";\n");
    if feedback {
        source.push_str("const result = f0(target);\n");
    } else {
        source.push_str("const result = f0(target || leaf || extra);\n");
    }
    source
}

fn argument_evaluations(source: &str) -> usize {
    analyze_checked(source).1.argument_evaluations
}

fn expression_dependency_stats(source: &str) -> super::value_flow::FlowWorkStats {
    assert_matches_reference(source);
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    ChunkValueFlow::analyze_with_stats(&body).1
}

#[test]
fn cached_expression_dependency_work_tracks_distinct_reads() {
    use std::fmt::Write as _;

    const READS: usize = 24;
    let source_for = |duplicate: bool| {
        let mut source =
            String::from("function invoke(callback) { return callback(); }\nfunction leaf() {}\n");
        for index in 0..READS {
            writeln!(source, "const box{index} = {{ run: leaf }};").unwrap();
        }
        source.push_str("const result = invoke(");
        for index in 0..READS {
            if index != 0 {
                source.push_str(" || ");
            }
            let box_index = if duplicate { 0 } else { index };
            write!(source, "box{box_index}.run").unwrap();
        }
        source.push_str(");\n");
        source
    };

    let repeated = expression_dependency_stats(&source_for(true));
    let distinct = expression_dependency_stats(&source_for(false));
    let repeated_savings = repeated
        .expression_dependency_read_occurrences
        .saturating_sub(repeated.expression_dependency_unique_keys);
    let distinct_savings = distinct
        .expression_dependency_read_occurrences
        .saturating_sub(distinct.expression_dependency_unique_keys);

    assert!(
        repeated_savings > 0,
        "duplicate reads were not observed: {repeated:?}"
    );
    assert!(
        repeated_savings > distinct_savings,
        "duplicate-heavy input should have more repeated reads: {repeated:?} vs {distinct:?}"
    );
    assert!(
        distinct.expression_dependency_unique_keys > repeated.expression_dependency_unique_keys,
        "distinct input should retain more dependency keys: {repeated:?} vs {distinct:?}"
    );
}

#[test]
fn worklist_matches_reference_across_reverse_calls_and_source_orders() {
    for source in [
        return_call_chain(48, false),
        return_call_chain(48, true),
        reverse_identity_alias_chain(48),
        r#"
const result = relay(target);
function relay(callback) { return callback(); }
function target() { return base; }
function base() {}
"#
        .to_string(),
        r#"
function apply(callback, depth) {
  if (depth > 0) return apply(callback, depth - 1);
  return callback();
}
function left() { return right(); }
function right() { return base; }
function base() { return object; }
function object() {}
const source = { run: left };
const branch0 = { run: source.run };
const branch1 = { run: branch0.run };
const branch2 = { run: branch1.run };
const result = apply(branch2.run, 2);
"#
        .to_string(),
    ] {
        assert_matches_reference(&source);
    }
}

#[test]
fn worklist_matches_reference_across_expression_and_binding_shapes() {
    let cases = [
        // Arrow expression bodies and block bodies both contribute return facts.
        r#"
function leaf() { return base; }
function base() {}
const expression = callback => callback();
const block = callback => { return callback(); };
const first = expression(leaf);
const second = block(leaf);
"#,
        // Canonical callable identities flow through classes and object methods.
        r#"
function helper() {}
class Target { static run(callback = helper) { return callback; } }
const { run } = Target;
const holder = {
  run,
  invoke(callback = run) { return callback(); },
};
const result = holder.invoke();
"#,
        // Array holes and spreads preserve the existing element-value behavior.
        r#"
function first() {}
function second() {}
const source = [first, , ...[second]];
const copy = [...source];
const result = copy[0] || copy[1] || copy[2];
"#,
        // Dynamic member writes and later static reads must agree with the sweep.
        r#"
function target() {}
const key = "run";
const box = {};
box[key] = target;
const dynamic_read = box[key];
const static_read = box.run;
const result = dynamic_read || static_read;
"#,
        // Destructured/default/rest parameters include declared ids with no value.
        r#"
function fallback() {}
function target() {}
let empty;
function select({ run = fallback } = {}, ...callbacks) {
  const callback = callbacks[0] || run || empty;
  return callback();
}
const result = select({ run: target }, target, fallback);
"#,
        // Nested conditional/logical/sequence/assignment expressions grow callees.
        r#"
function invoke(callback) { return callback(); }
function first() { return second(); }
function second() { return leaf; }
function leaf() {}
function fallback() {}
let chosen;
const box = { run: first };
const result = invoke((chosen = true ? box.run : second, chosen || fallback));
"#,
    ];

    for source in cases {
        assert_matches_reference(source);
    }
}

#[test]
fn worklist_matches_reference_when_only_function_metadata_is_discovered() {
    for source in [
        "((callback) => callback)(globalThis);",
        "((callback) => callback)(class {});",
        "((Constructor) => new Constructor())(class {});",
        // A separate return fact makes the legacy solver perform another sweep.
        "((callback) => callback)(globalThis); (() => ({}))();",
    ] {
        assert_matches_reference(source);
    }
}

#[test]
fn polymorphic_invokes_reuse_complex_arguments_without_param_dependencies() {
    let flow = assert_matches_reference(
        r#"
function primary_leaf() {}
function returned_leaf() {}
function factory() { return returned_leaf; }
function target0(value0) {}
function target1(value1) {}
function target2(value2) {}
function target3(value3) {}
function target4(value4) {}
function target5(value5) {}
function target6(value6) {}
function target7(value7) {}

const box = { primary: primary_leaf, factory };
const poly = target0 || target1 || target2 || target3 || target4 || target5 || target6 || target7;
const before = poly(box.missing || box.primary || box.factory());
box.missing = returned_leaf;
const after = poly(box.missing || box.primary || box.factory());
"#,
    );

    let primary = flow.values_of_binding(&id("primary_leaf"));
    let returned = flow.values_of_binding(&id("returned_leaf"));
    let mut expected = primary.iter().collect::<std::collections::BTreeSet<_>>();
    expected.extend(returned.iter());
    expected.insert(Value::Top);

    for parameter in [
        "value0", "value1", "value2", "value3", "value4", "value5", "value6", "value7",
    ] {
        assert_eq!(
            flow.values_of_binding(&id(parameter))
                .iter()
                .collect::<std::collections::BTreeSet<_>>(),
            expected,
            "wrong argument facts for {parameter}",
        );
    }
}

#[test]
fn arguments_reading_a_later_callees_params_see_earlier_param_writes() {
    let flow = assert_matches_reference(
        r#"
function earlier(left, right) {}
function feedback(seed, carry) {
  (earlier || feedback)(carry, seed);
}
function initial_value() {}
function introduced_value() {}
feedback(initial_value, introduced_value);
"#,
    );

    let initial = flow.values_of_binding(&id("initial_value"));
    let introduced = flow.values_of_binding(&id("introduced_value"));
    let mut expected = initial.iter().collect::<std::collections::BTreeSet<_>>();
    expected.extend(introduced.iter());

    // `seed` is first read for `earlier`, before the later `feedback` target
    // receives `carry` into `seed`. Its value must be evaluated again for the
    // second target after that parameter write.
    for parameter in ["seed", "carry", "left", "right"] {
        assert_eq!(
            flow.values_of_binding(&id(parameter))
                .iter()
                .collect::<std::collections::BTreeSet<_>>(),
            expected,
            "wrong feedback facts for {parameter}",
        );
    }
}

#[test]
fn polymorphic_invokes_zip_only_to_declared_identifier_parameters() {
    let flow = assert_matches_reference(
        r#"
function no_params() {}
function one(callback) {}
function destructured({ ignored }, secondary) {}
function two(left, right) {}
function leaf() {}
function extra() {}
const box = {};
const poly = no_params || one || destructured || two;
poly(box.missing || leaf, extra);
"#,
    );

    let extra = flow.values_of_binding(&id("extra"));
    let extra_values = extra.iter().collect::<Vec<_>>();
    assert_eq!(
        extra_values.len(),
        1,
        "extra should have one function identity"
    );
    let extra_origin = extra_values[0];
    let callback = flow.values_of_binding(&id("callback"));
    assert!(callback.contains(&Value::Top));
    let leaf = flow.values_of_binding(&id("leaf"));
    let leaf_values = leaf.iter().collect::<Vec<_>>();
    assert_eq!(
        leaf_values.len(),
        1,
        "leaf should have one function identity"
    );
    assert!(callback.contains(&leaf_values[0]));
    assert!(!callback.contains(&extra_origin));

    assert_eq!(
        flow.values_of_binding(&id("secondary"))
            .iter()
            .collect::<Vec<_>>(),
        [extra_origin],
    );
    assert_eq!(
        flow.values_of_binding(&id("right"))
            .iter()
            .collect::<Vec<_>>(),
        [extra_origin],
    );
    assert!(
        flow.values_of_binding(&id("ignored"))
            .iter()
            .next()
            .is_none(),
        "a destructured parameter should not receive a binding fact",
    );
}

#[test]
fn reverse_return_call_chain_has_bounded_worklist_growth() {
    let mut previous_executions = None;
    for depth in [64, 128, 256, 512] {
        let (_, stats) = analyze_checked(&return_call_chain(depth, false));
        if let Some(previous) = previous_executions {
            assert!(
                stats.event_executions <= previous * 3,
                "doubling return-call depth grew work from {previous} to {} events",
                stats.event_executions,
            );
        }
        previous_executions = Some(stats.event_executions);
    }
}

#[test]
fn invoke_arguments_reuse_return_sets_until_a_read_binding_changes() {
    let mut stable_evaluations = Vec::new();
    let mut feedback_evaluations = Vec::new();
    for target_count in [8, 64] {
        let stable = invoke_return_set_fanout(target_count, false);
        assert_matches_reference(&stable);
        stable_evaluations.push(argument_evaluations(&stable));

        let feedback = invoke_return_set_fanout(target_count, true);
        assert_matches_reference(&feedback);
        feedback_evaluations.push(argument_evaluations(&feedback));
    }

    assert!(
        stable_evaluations[1] <= stable_evaluations[0] + 2,
        "stable return-set argument evaluation grew with target fanout: {stable_evaluations:?}",
    );
    assert!(
        feedback_evaluations[1] <= feedback_evaluations[0] + 2,
        "feedback return-set argument evaluation grew with target fanout: {feedback_evaluations:?}",
    );
    assert!(
        feedback_evaluations[0] > stable_evaluations[0],
        "feedback must force reevaluation after its read parameter gains a return origin: stable={stable_evaluations:?}, feedback={feedback_evaluations:?}",
    );
}

#[test]
fn invoke_does_not_evaluate_arguments_without_identifier_parameters() {
    let source = r#"
function noParams() {}
function leaf() {}
const box = {};
const result = noParams(box.missing || leaf);
"#;
    assert_matches_reference(source);
    assert_eq!(argument_evaluations(source), 0);
}

#[test]
fn unknown_from_missing_property_survives_its_later_static_write() {
    let flow = assert_matches_reference(
        r#"
function invoke(callback) { return callback(); }
function target() { return leaf; }
function leaf() {}
const box = {};
const early = invoke(box.run);
box.run = target;
const later = invoke(box.run);
"#,
    );

    let callback_values = flow.values_of_binding(&id("callback"));
    assert!(callback_values.contains(&Value::Top));
    assert!(has_function(&callback_values));
}

#[test]
fn dynamic_property_read_wakes_when_a_new_static_property_is_added() {
    for source in [
        r#"
function target() {}
const key = "run";
const box = {};
const early = box[key];
box.run = target;
"#,
        // The member event must first track its base binding, then its property.
        r#"
let box;
function target() {}
const early = box.run;
box.run = target;
box = {};
"#,
    ] {
        let flow = assert_matches_reference(source);

        let values = flow.values_of_binding(&id("early"));
        assert!(values.contains(&Value::Top));
        assert!(has_function(&values));
    }
}

#[test]
fn a_possible_property_read_keeps_its_watcher_for_a_late_fixed_writer() {
    let source = r#"
const box = { run: target };
const early = box.run;
function target() {}
"#;
    let (flow, stats) = analyze_checked(source);
    let values = flow.values_of_binding(&id("early"));
    assert!(values.contains(&Value::Top));
    assert!(has_function(&values));
    assert_eq!(stats.property_impossible_reads_skipped, 0);
    assert!(stats.property_possible_reads_tracked > 0);
}

#[test]
fn impossible_property_absence_keeps_top_without_a_property_watcher() {
    let source = "const box = {};\nconst early = box.neverWritten;\n";
    let (flow, stats) = analyze_checked(source);
    assert!(flow.values_of_binding(&id("early")).contains(&Value::Top));
    assert_eq!(stats.property_impossible_reads_skipped, 1);
    assert_eq!(stats.property_possible_reads_tracked, 0);
}

#[test]
fn a_possible_property_read_keeps_its_watcher_for_a_late_dynamic_receiver() {
    let source = r#"
const box = {};
let receiver;
function target() {}
const early = box.run;
receiver = box;
receiver.run = target;
"#;
    let (flow, stats) = analyze_checked(source);
    let values = flow.values_of_binding(&id("early"));
    assert!(values.contains(&Value::Top));
    assert!(has_function(&values));
    assert_eq!(stats.property_impossible_reads_skipped, 0);
    assert!(stats.property_possible_reads_tracked > 0);
}

#[test]
fn same_slot_dynamic_writer_on_an_unrelated_object_is_conservative() {
    let source = r#"
const box = {};
const other = {};
function target() {}
const early = box.run;
other.run = target;
"#;
    let (flow, stats) = analyze_checked(source);
    let values = flow.values_of_binding(&id("early"));
    assert!(values.contains(&Value::Top));
    assert!(!has_function(&values));
    assert_eq!(stats.property_impossible_reads_skipped, 0);
    assert!(stats.property_possible_reads_tracked > 0);
}

#[test]
fn empty_rhs_property_events_remain_in_the_producer_census() {
    let source = r#"
const box = {};
const early = box.run;
box.run = 1;
"#;
    let (flow, stats) = analyze_checked(source);
    assert!(flow.values_of_binding(&id("early")).contains(&Value::Top));
    assert_eq!(stats.property_impossible_reads_skipped, 0);
    assert!(stats.property_possible_reads_tracked > 0);
}

#[test]
fn dynamic_property_unions_track_late_writes_per_object_alias() {
    let flow = assert_matches_reference(
        r#"
function left_first() {}
function left_second() {}
function left_third() {}
function right_first() {}
function right_second() {}
function right_third() {}

const left = {};
const right = {};
function make_left() { return left; }
function make_right() { return right; }
const key = globalThis;

const early_left = make_left()[key];
const early_right = make_right()[key];
make_left().unknown_value = make_left().missing;
make_right().unknown_value = make_right().missing;

let left_progress;
left_progress = left_first;
make_left().changing = left_progress;
left_progress = left_second;
make_left().changing = left_progress;
make_left().another_property = left_third;

let right_progress;
right_progress = right_first;
make_right().changing = right_progress;
right_progress = right_second;
make_right().changing = right_progress;
make_right().another_property = right_third;

const late_left = make_left()[key];
const late_right = make_right()[key];
"#,
    );

    let function_origin = |name: &str| {
        let values = flow.values_of_binding(&id(name));
        let functions = values
            .iter()
            .filter(|value| matches!(value, Value::Alloc(Kind::Function, _)))
            .collect::<Vec<_>>();
        assert_eq!(
            functions.len(),
            1,
            "{name} should have one function identity"
        );
        functions[0]
    };
    let left_functions = [
        function_origin("left_first"),
        function_origin("left_second"),
        function_origin("left_third"),
    ];
    let right_functions = [
        function_origin("right_first"),
        function_origin("right_second"),
        function_origin("right_third"),
    ];
    let object_origin = |name: &str| {
        let values = flow.values_of_binding(&id(name));
        let objects = values
            .iter()
            .filter_map(|value| match value {
                Value::Alloc(Kind::Object, object) => Some(object),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(objects.len(), 1, "{name} should have one object identity");
        objects[0]
    };

    for (binding, expected_functions) in [
        ("left", &left_functions[..2]),
        ("right", &right_functions[..2]),
    ] {
        let object = object_origin(binding);
        let unknown = flow
            .values_of_property(object, "unknown_value")
            .expect("the missing-property write should create a property");
        assert!(unknown.contains(&Value::Top));
        let changing = flow
            .values_of_property(object, "changing")
            .expect("the changing property should be written");
        for expected in expected_functions {
            assert!(changing.contains(expected));
        }
    }

    for binding in ["early_left", "late_left"] {
        let values = flow.values_of_binding(&id(binding));
        assert!(values.contains(&Value::Top), "{binding} lost Unknown");
        for expected in left_functions {
            assert!(values.contains(&expected), "{binding} lost {expected:?}");
        }
        for other_object in right_functions {
            assert!(
                !values.contains(&other_object),
                "{binding} included another object's value {other_object:?}",
            );
        }
    }
    for binding in ["early_right", "late_right"] {
        let values = flow.values_of_binding(&id(binding));
        assert!(values.contains(&Value::Top), "{binding} lost Unknown");
        for expected in right_functions {
            assert!(values.contains(&expected), "{binding} lost {expected:?}");
        }
        for other_object in left_functions {
            assert!(
                !values.contains(&other_object),
                "{binding} included another object's value {other_object:?}",
            );
        }
    }
}

#[test]
fn later_alias_values_grow_recursive_callee_sets() {
    let depth = 40;
    let mut source = String::from(
        "function invoke(callback, recursive) {\n\
           if (recursive) return invoke(callback, false);\n\
           return callback();\n\
         }\n\
         function target_a() { return leaf_a; }\n\
         function target_b() { return leaf_b; }\n\
         function leaf_a() {}\n\
         function leaf_b() {}\n\
         var alias0 = alias1;\n",
    );
    for index in 1..depth {
        source.push_str(&format!("var alias{index} = alias{};\n", index + 1));
    }
    source.push_str(&format!(
        "var alias{depth} = target_a;\nconst result = invoke(alias0, true);\nalias{depth} = target_b;\n"
    ));

    let flow = assert_matches_reference(&source);
    let callback_values = flow.values_of_binding(&id("callback"));
    assert!(
        callback_values
            .iter()
            .filter(|value| matches!(value, Value::Alloc(Kind::Function, _)))
            .count()
            >= 2
    );
}

#[test]
fn borrowed_value_merges_match_reference_for_overlapping_call_and_property_sets() {
    let flow = assert_matches_reference(
        r#"
function relay(callback) { return callback || callback || globalThis; }
function leaf0() {}
function leaf1() {}
function leaf2() {}
function leaf3() {}
function leaf4() {}
function leaf5() {}
function leaf6() {}
function leaf7() {}

const wide = leaf0 || leaf1 || leaf2 || leaf3 || leaf4 || leaf5 || leaf6 || leaf7;
const overlap = wide || leaf0 || leaf3 || globalThis;
const box = { primary: wide, secondary: overlap };
const before_write = box.later;
box.later = overlap;
const first = relay(before_write || box.primary || globalThis);
const alias = first || box.secondary || box.later;
const second = relay(alias || box.primary || globalThis);
"#,
    );

    let wide_values = flow.values_of_binding(&id("wide"));
    assert_eq!(
        wide_values
            .iter()
            .filter(|value| matches!(value, Value::Alloc(Kind::Function, _)))
            .count(),
        8,
    );
    assert!(flow.values_of_binding(&id("overlap")).contains(&Value::Top));

    for name in ["first", "alias", "second"] {
        let values = flow.values_of_binding(&id(name));
        assert!(values.contains(&Value::Top), "{name} lost Unknown");
        assert!(
            values
                .iter()
                .filter(|value| matches!(value, Value::Alloc(Kind::Function, _)))
                .count()
                >= 8,
            "{name} lost part of the wide function set",
        );
    }

    let [Value::Alloc(Kind::Object, box_origin)] = flow
        .values_of_binding(&id("box"))
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("box should have exactly one object identity");
    };
    for property in ["primary", "secondary", "later"] {
        let values = flow
            .values_of_property(box_origin, property)
            .unwrap_or_else(|| panic!("missing static property {property}"));
        assert!(values.contains(&Value::Top) || property == "primary");
        assert!(
            values
                .iter()
                .filter(|value| matches!(value, Value::Alloc(Kind::Function, _)))
                .count()
                >= 8,
            "property {property} lost part of the wide function set",
        );
    }
}

fn growing_invocation(
    target_count: usize,
    growing_argument: bool,
    stable_argument: bool,
) -> String {
    let mut source = String::from("function payload() {}\n");
    for index in 0..target_count {
        source.push_str(&format!(
            "function target{index}(value{index}, stable{index}) {{}}\n"
        ));
    }
    source.push_str("const selected = target0 || next0;\n");
    let argument = if growing_argument { "next0" } else { "payload" };
    source.push_str(&format!(
        "selected({argument}{});\n",
        if stable_argument { ", payload" } else { "" },
    ));
    for index in 0..target_count - 1 {
        source.push_str(&format!("const next{index} = target{}", index + 1));
        if index + 2 < target_count {
            source.push_str(&format!(" || next{}", index + 1));
        }
        source.push_str(";\n");
    }
    source
}

#[test]
fn unchanged_argument_is_evaluated_once_while_call_targets_grow() {
    for targets in [4, 16, 64] {
        let source = growing_invocation(targets, false, false);
        assert_eq!(argument_evaluations(&source), 1, "targets={targets}");
    }
}

#[test]
fn growing_argument_does_not_revisit_an_unchanged_sibling() {
    for targets in [4, 16, 64] {
        let changing = argument_evaluations(&growing_invocation(targets, true, false));
        let with_stable = argument_evaluations(&growing_invocation(targets, true, true));
        assert!(changing > 1, "the argument must consume later origins");
        assert_eq!(with_stable, changing + 1, "targets={targets}");
    }
}

#[test]
fn argument_reuse_watches_properties_introduced_by_a_later_receiver() {
    let flow = assert_matches_reference(
        r#"
function relay(value) {}
function target() {}
const box = {};
const middle = {};
relay(box.peer.run);
box.peer = middle;
middle.run = delayed;
const delayed = next;
const next = target;
"#,
    );
    let values = flow.values_of_binding(&id("value"));
    assert!(values.contains(&Value::Top));
    for origin in flow.values_of_binding(&id("target")) {
        assert!(values.contains(&origin));
    }
}

fn invocation_work(source: &str) -> super::value_flow::FlowWorkStats {
    analyze_checked(source).1
}

#[test]
fn late_targets_activate_edges_once_without_replaying_existing_edges() {
    for targets in [4, 16, 64] {
        let stats = invocation_work(&growing_invocation(targets, false, false));
        assert_eq!(stats.target_discoveries, targets);
        assert_eq!(stats.activated_edges, targets);
        assert_eq!(stats.edge_visits, targets);
        assert_eq!(stats.origin_deliveries, targets);
        assert_eq!(stats.argument_evaluations, 1);
    }
}

#[test]
fn invocation_origin_deliveries_are_bounded_by_edges_times_distinct_origins() {
    // Independent product oracle: every source call reaches every target, and
    // each formal eventually receives precisely the explicit origin functions.
    // Alias cycles and shared source arguments add no origins or call contexts.
    for targets in [1, 8] {
        for origins in [1, 8] {
            for consumers in [1, 5] {
                for cycle in [false, true] {
                    let mut source = String::new();
                    for target in 0..targets {
                        source.push_str(&format!("function target{target}(formal{target}) {{}}\n"));
                    }
                    for origin in 0..origins {
                        source.push_str(&format!("function origin{origin}() {{}}\n"));
                    }
                    let target_expr = (0..targets)
                        .map(|target| format!("target{target}"))
                        .collect::<Vec<_>>()
                        .join(" || ");
                    source.push_str(&format!("const selected = {target_expr};\n"));
                    for _ in 0..consumers {
                        source.push_str("selected(payload0);\n");
                    }
                    for origin in 0..origins {
                        let next = if origin + 1 < origins {
                            format!(" || payload{}", origin + 1)
                        } else if cycle {
                            " || payload0".into()
                        } else {
                            String::new()
                        };
                        source
                            .push_str(&format!("const payload{origin} = origin{origin}{next};\n"));
                    }
                    let stats = invocation_work(&source);
                    let edges = targets * consumers;
                    assert_eq!(stats.activated_edges, edges, "{source}");
                    assert_eq!(stats.target_discoveries, edges);
                    assert_eq!(stats.origin_deliveries, edges * origins);
                    assert_eq!(
                        stats.callee_evaluations, consumers,
                        "argument growth must not walk callees"
                    );
                    assert!(stats.difference_inputs >= stats.origin_deliveries);
                    assert!(stats.edge_visits <= edges * (origins + 1));
                    if origins > 1 {
                        assert!(
                            stats.difference_inputs > stats.origin_deliveries,
                            "exact-result subtraction must charge recurring origins separately"
                        );
                    }
                }
            }
        }
    }
}

#[test]
fn a_late_edge_receives_current_exact_argument_without_historical_unknown() {
    let flow = assert_matches_reference(
        r#"
function first(early) {}
function second(late) {}
function callback() {}
const box = {};
const selected = first || delayed;
selected(box.peer.run);
box.peer = { run: callback };
var delayed = next;
var next = second;
"#,
    );
    assert!(flow.values_of_binding(&id("early")).contains(&Value::Top));
    let late = flow.values_of_binding(&id("late"));
    assert!(!late.contains(&Value::Top));
    assert_eq!(late, flow.values_of_binding(&id("callback")));
}

#[test]
fn late_property_receivers_get_current_rhs_without_historical_unknown() {
    let flow = assert_matches_reference(
        r#"
function callback() {}
const first = {};
const second = {};
const box = {};
const selected = first || delayed;
selected.run = box.peer.run;
box.peer = { run: callback };
var delayed = next;
var next = second;
"#,
    );
    let receiver = |name| match flow.values_of_binding(&id(name)).iter().next().unwrap() {
        Value::Alloc(Kind::Object, site) => site,
        _ => panic!("object receiver"),
    };
    assert!(
        flow.values_of_property(receiver("first"), "run")
            .unwrap()
            .contains(&Value::Top)
    );
    let late = flow.values_of_property(receiver("second"), "run").unwrap();
    assert!(!late.contains(&Value::Top));
    assert_eq!(late, &flow.values_of_binding(&id("callback")));
}

#[test]
fn property_feedback_refreshes_receiver_and_rhs_reads() {
    for source in [
        "const first = {}; const second = {}; first.next = second; second.next = first; const state = { slot: first }; state.slot.next = state.slot; state.slot = state.slot.next;",
        "function callback() {} const first = {}; const second = {}; const state = { slot: first }; state.slot.run = state.slot.next; first.next = second; second.next = callback; state.slot = state.slot.next;",
        "function callback() {} const box = {}; box.next = box; box.next.run = box.next.next.run || callback;",
    ] {
        assert_matches_reference(source);
    }
}

fn property_fanout(
    receivers: usize,
    origins: usize,
    consumers: usize,
    late_receivers: bool,
) -> String {
    let mut source = String::new();
    for receiver in 0..receivers {
        source.push_str(&format!("const receiver{receiver} = {{}};\n"));
    }
    for origin in 0..origins {
        source.push_str(&format!("function origin{origin}() {{}}\n"));
    }
    let payload = (0..origins)
        .map(|i| format!("origin{i}"))
        .collect::<Vec<_>>()
        .join(" || ");
    source.push_str(&format!("const payload = {payload};\n"));
    if !late_receivers {
        let selected = (0..receivers)
            .map(|i| format!("receiver{i}"))
            .collect::<Vec<_>>()
            .join(" || ");
        source.push_str(&format!("const selected = {selected};\n"));
    }
    for consumer in 0..consumers {
        source.push_str(&format!("selected.field{consumer} = payload;\n"));
    }
    if late_receivers {
        source.push_str("const selected = receiver0 || alias0;\n");
        for receiver in 1..receivers {
            let next = if receiver + 1 < receivers {
                format!(" || alias{receiver}")
            } else {
                String::new()
            };
            source.push_str(&format!(
                "const alias{} = receiver{receiver}{next};\n",
                receiver - 1
            ));
        }
    }
    source
}

#[test]
#[ignore = "run //devinfra/js/debundle/e2e/bench:analysis_work_bounds_test"]
fn work_bounds_property_receivers_do_not_replay_old_receivers_or_stable_rhs() {
    for receivers in [2, 8, 32] {
        for origins in [1, 8] {
            for consumers in [1, 5] {
                let stats = invocation_work(&property_fanout(receivers, origins, consumers, true));
                let edges = receivers * consumers;
                assert_eq!(stats.property_rhs_evaluations, consumers);
                assert_eq!(stats.property_receiver_activations, edges);
                assert_eq!(stats.property_receiver_visits, edges);
                assert_eq!(stats.property_proposals, 2 * edges);
                assert_eq!(stats.property_join_inputs, 2 * edges * origins);
                assert_eq!(stats.property_retained_receivers, edges);
                assert_eq!(
                    stats.property_retained_origins,
                    consumers * (receivers + origins)
                );
                assert_eq!(stats.property_retained_reads, 2 * consumers);
                assert_eq!(stats.property_receiver_scans, edges);
                assert!(
                    stats.property_comparison_inputs
                        <= consumers * (origins + receivers * (receivers + 1))
                );
            }
        }
    }
}

#[test]
#[ignore = "run //devinfra/js/debundle/e2e/bench:analysis_work_bounds_test"]
fn work_bounds_property_rhs_preserves_shared_full_result() {
    for receivers in [1, 8] {
        for origins in [1, 8, 32] {
            for consumers in [1, 5] {
                let mut source = String::new();
                for receiver in 0..receivers {
                    source.push_str(&format!("const receiver{receiver} = {{}};\n"));
                }
                for origin in 0..origins {
                    source.push_str(&format!("function origin{origin}() {{}}\n"));
                }
                let selected = (0..receivers)
                    .map(|i| format!("receiver{i}"))
                    .collect::<Vec<_>>()
                    .join(" || ");
                source.push_str(&format!("const selected = {selected};\n"));
                for consumer in 0..consumers {
                    source.push_str(&format!("selected.field{consumer} = payload0;\n"));
                }
                for origin in 0..origins {
                    let next = if origin + 1 < origins {
                        format!(" || payload{}", origin + 1)
                    } else {
                        String::new()
                    };
                    source.push_str(&format!("const payload{origin} = origin{origin}{next};\n"));
                }
                let module = raw_js_test_support::parse(&source);
                let body = top_level_item_views(&module.body);
                let (actual, stats) = ChunkValueFlow::analyze_with_stats(&body);
                let reference = ChunkValueFlow::analyze_reference(&body);
                assert_eq!(actual, reference);
                let payloads = |flow: &ChunkValueFlow| {
                    let mut unique = Vec::new();
                    for receiver in 0..receivers {
                        let Value::Alloc(Kind::Object, object) = flow
                            .values_of_binding(&id(&format!("receiver{receiver}")))
                            .iter()
                            .next()
                            .unwrap()
                        else {
                            panic!("object receiver")
                        };
                        for consumer in 0..consumers {
                            let values = flow
                                .values_of_property(object, &format!("field{consumer}"))
                                .unwrap();
                            if !unique.iter().any(|previous: &&super::value_flow::Values| {
                                values.shares_bitmap(previous)
                            }) {
                                unique.push(values);
                            }
                        }
                    }
                    (
                        unique.len(),
                        unique
                            .iter()
                            .map(|values| values.bitmap_payload_bytes())
                            .sum::<u64>(),
                    )
                };
                let actual_payloads = payloads(&actual);
                let reference_payloads = payloads(&reference);
                assert_eq!(reference_payloads.0, 1);
                assert_eq!(
                    actual_payloads.0, reference_payloads.0,
                    "receivers={receivers}, origins={origins}, consumers={consumers}; actual={actual_payloads:?}, reference={reference_payloads:?}"
                );
                assert!(
                    actual_payloads.1 <= reference_payloads.1,
                    "receivers={receivers}, origins={origins}, consumers={consumers}; actual={actual_payloads:?}, reference={reference_payloads:?}"
                );
                let edges = receivers * consumers;
                assert_eq!(stats.property_receiver_evaluations, consumers);
                assert_eq!(stats.property_receiver_scans, edges);
                assert!(stats.property_join_inputs >= 2 * edges * origins);
                assert!(stats.property_join_inputs <= edges * origins * (origins + 1));
                assert!(stats.property_receiver_visits <= edges * origins);
                assert!(stats.property_subset_checks <= consumers * origins);
                assert_eq!(stats.property_dynamic_records, consumers);
                assert_eq!(stats.property_fixed_records, 0);
                assert_eq!(
                    stats.property_retained_origins,
                    consumers * (receivers + origins)
                );
                assert!(
                    stats.property_comparison_inputs
                        <= consumers * (receivers + origins * (origins + 1))
                );
            }
        }
    }
}

#[test]
fn literal_fields_retain_only_caches_with_growing_inputs() {
    let stats = invocation_work(
        "function callback() {} const record = { fixed: function () {}, empty: 1, callback, read: delayed }; const delayed = callback; record.count = 1;",
    );
    assert_eq!(stats.property_dynamic_records, 0);
    assert_eq!(stats.property_fixed_records, 2);
    assert_eq!(stats.property_direct_evaluations, 1);
    assert_eq!(stats.property_retained_receivers, 0);
    assert_eq!(stats.property_retained_origins, 2);
    assert_eq!(stats.property_retained_reads, 2);
    assert_eq!(stats.property_receiver_evaluations, 0);
}

#[test]
fn numeric_computed_members_use_only_the_numeric_property_aggregate() {
    let source = r#"
function numeric_zero() {}
function numeric_one() {}
function nan_value() {}
function infinity_value() {}
function named_value() {}
const items = {
  0: numeric_zero,
  1: numeric_one,
  NaN: nan_value,
  Infinity: infinity_value,
  ref: named_value,
  props: named_value,
  type: named_value,
};
const numeric = items[items.length - 1];
const bigint = items[1n];
const variable_key = items[index];
const mixed_key = items[flag ? 0 : "ref"];
const string_key = items["0"];
"#;
    let flow = assert_matches_reference(source);
    let zero = flow.values_of_binding(&id("numeric_zero"));
    let one = flow.values_of_binding(&id("numeric_one"));
    let nan = flow.values_of_binding(&id("nan_value"));
    let infinity = flow.values_of_binding(&id("infinity_value"));
    let named = flow.values_of_binding(&id("named_value"));

    for name in ["numeric", "bigint"] {
        let values = flow.values_of_binding(&id(name));
        assert!(values.contains(&Value::Top), "{name}: {values:?}");
        assert!(
            zero.iter().all(|value| values.contains(&value)),
            "{name}: {values:?}"
        );
        assert!(
            one.iter().all(|value| values.contains(&value)),
            "{name}: {values:?}"
        );
        assert!(
            nan.iter().all(|value| values.contains(&value)),
            "{name}: {values:?}"
        );
        assert!(
            infinity.iter().all(|value| values.contains(&value)),
            "{name}: {values:?}"
        );
        assert!(
            !named.iter().any(|value| values.contains(&value)),
            "{name}: {values:?}"
        );
    }
    for name in ["variable_key", "mixed_key"] {
        let values = flow.values_of_binding(&id(name));
        assert!(values.contains(&Value::Top), "{name}: {values:?}");
        assert!(
            named.iter().all(|value| values.contains(&value)),
            "{name}: {values:?}"
        );
    }
    let string_key = flow.values_of_binding(&id("string_key"));
    assert!(zero.iter().all(|value| string_key.contains(&value)));
    assert!(!one.iter().any(|value| string_key.contains(&value)));
    assert!(!named.iter().any(|value| string_key.contains(&value)));
}

#[test]
fn numeric_property_reads_keep_constant_work_as_named_fields_grow() {
    use std::fmt::Write as _;

    for named_count in [8, 64, 256] {
        let mut source = String::from("function numeric_zero() {}\nfunction numeric_one() {}\n");
        for index in 0..named_count {
            writeln!(source, "function named{index}() {{}}").unwrap();
        }
        source.push_str("const items = { 0: numeric_zero, 1: numeric_one,\n");
        for index in 0..named_count {
            writeln!(source, "named_{index}: named{index},").unwrap();
        }
        source.push_str("};\nconst numeric = items[items.length - 1];\n");

        let (flow, stats) = analyze_checked(&source);
        assert_eq!(
            stats.numeric_property_reads, 1,
            "width {named_count}: {stats:?}"
        );
        assert_eq!(
            stats.numeric_property_proposals, 2,
            "named fields must not enter the numeric aggregate at width {named_count}: {stats:?}"
        );
        let numeric = flow.values_of_binding(&id("numeric"));
        assert!(numeric.contains(&Value::Top));
        for name in ["numeric_zero", "numeric_one"] {
            for value in flow.values_of_binding(&id(name)) {
                assert!(numeric.contains(&value));
            }
        }
        for index in 0..named_count {
            let name = id(&format!("named{index}"));
            for value in flow.values_of_binding(&name) {
                assert!(!numeric.contains(&value), "named{index}: {numeric:?}");
            }
        }
    }
}

#[test]
fn cached_numeric_member_rhs_wakes_for_a_late_numeric_property_write() {
    let source = r#"
const items = {};
const target = {};
function late_value() {}
target.run = items[items.length - 1];
const early = target.run;
items["2"] = late_value;
"#;
    let (flow, stats) = analyze_checked(source);
    let early = flow.values_of_binding(&id("early"));
    assert!(early.contains(&Value::Top), "{early:?}");
    for value in flow.values_of_binding(&id("late_value")) {
        assert!(early.contains(&value), "{early:?}");
    }
    assert_eq!(stats.numeric_property_reads, 2, "{stats:?}");
    assert_eq!(stats.numeric_property_proposals, 1, "{stats:?}");
}

#[test]
fn numeric_property_key_names_are_a_conservative_superset() {
    for name in ["0", "17", "-1", "+1", ".5", "1e+21", "NaN", "Infinity"] {
        assert!(
            super::value_flow::may_be_numeric_property_key(name),
            "{name}"
        );
    }
    for name in ["props", "ref", "type", "length", "constructor"] {
        assert!(
            !super::value_flow::may_be_numeric_property_key(name),
            "{name}"
        );
    }
}

#[derive(Default)]
struct MemberExpressionCollector(Vec<MemberExpr>);

impl Visit for MemberExpressionCollector {
    fn visit_member_expr(&mut self, node: &MemberExpr) {
        self.0.push(node.clone());
        node.visit_children_with(self);
    }
}

#[test]
fn counted_loop_numeric_certificates_survive_event_and_local_query_lowering() {
    let source = r#"
function numeric_value() {}
function named_value() {}
const items = { 0: numeric_value, ref: named_value };
function read() {
  var selected;
  for (var index = 0; index < 1; index++) {
    selected = items[index];
  }
  index = "ref";
  var later = items[index];
  var exact = items["ref"];
}
"#;
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    let flow = ChunkValueFlow::analyze(&body);
    let reference = ChunkValueFlow::analyze_reference(&body);
    assert_eq!(flow, reference);

    let numeric = flow.values_of_binding(&id("numeric_value"));
    let named = flow.values_of_binding(&id("named_value"));
    let selected = flow.values_of_binding(&id("selected"));
    assert!(selected.contains(&Value::Top), "{selected:?}");
    assert!(numeric.iter().all(|value| selected.contains(&value)));
    assert!(!named.iter().any(|value| selected.contains(&value)));

    // The var binding is assigned a string after the loop. That later reuse
    // stays an ordinary dynamic read and does not widen the certified site.
    let later = flow.values_of_binding(&id("later"));
    assert!(later.contains(&Value::Top), "{later:?}");
    assert!(numeric.iter().all(|value| later.contains(&value)));
    assert!(named.iter().all(|value| later.contains(&value)));

    let mut members = MemberExpressionCollector::default();
    module.visit_with(&mut members);
    assert_eq!(members.0.len(), 3);
    let certified = flow.values_of_expr(&Expr::Member(members.0[0].clone()));
    assert!(certified.contains(&Value::Top), "{certified:?}");
    assert!(numeric.iter().all(|value| certified.contains(&value)));
    assert!(!named.iter().any(|value| certified.contains(&value)));
    assert_eq!(
        certified,
        reference.values_of_expr(&Expr::Member(members.0[0].clone()))
    );
    let later_read = flow.values_of_expr(&Expr::Member(members.0[1].clone()));
    assert!(numeric.iter().all(|value| later_read.contains(&value)));
    assert!(named.iter().all(|value| later_read.contains(&value)));
    let exact = flow.values_of_expr(&Expr::Member(members.0[2].clone()));
    assert!(named.iter().all(|value| exact.contains(&value)));
    assert!(!numeric.iter().any(|value| exact.contains(&value)));
}
