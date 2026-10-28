use super::*;
use swc_common::SyntaxContext;

fn id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
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

    let mut candidates = BTreeSet::from([ValueOrigin::Unknown]);
    for name in ["alpha", "beta", "unknown_box", "rooted"] {
        candidates.extend(
            flow.values_of_binding(&id(name))
                .iter()
                .filter(|origin| matches!(origin, ValueOrigin::Object(_))),
        );
    }
    candidates.extend(
        flow.values_of_binding(&id("cycle"))
            .iter()
            .filter(|origin| matches!(origin, ValueOrigin::Function(_))),
    );

    // This candidate comes from a second parsed source with a much larger
    // source span, so it is outside the first flow's collected origin table.
    // The old BTreeMap index accepted it as a seed; the dense index must retain
    // that behavior without relying on an unchecked origin-table lookup.
    let foreign_source = format!("/*{}*/ const foreign = {{}};", "padding".repeat(512));
    let foreign_module = raw_js_test_support::parse(&foreign_source);
    let foreign_body = top_level_item_views(&foreign_module.body);
    let foreign_flow = ChunkValueFlow::analyze(&foreign_body);
    let [ValueOrigin::Object(foreign_span)] = foreign_flow
        .values_of_binding(&id("foreign"))
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("foreign binding should have one object identity");
    };
    assert!(foreign_span > source.len() as u32);
    let foreign = ValueOrigin::Object(foreign_span);
    assert!(candidates.insert(foreign));

    let flow = ChunkValueFlow::analyze_with_candidates(&body, &candidates);
    let actual = flow.candidate_object_index_maps_for_test(&candidates);
    let expected = flow.candidate_object_index_reference_for_test(&candidates);
    assert_eq!(actual, expected);

    let ValueOrigin::Object(alpha_span) = *candidates
        .iter()
        .find(|origin| {
            matches!(origin, ValueOrigin::Object(_))
                && flow.values_of_binding(&id("alpha")).contains(origin)
        })
        .expect("alpha should be a candidate object")
    else {
        panic!("alpha should have an object identity");
    };
    let alpha = ValueOrigin::Object(alpha_span);
    let alpha_reachable = &actual.0[&alpha];
    let [ValueOrigin::Function(cycle_span)] = flow
        .values_of_property(alpha_span, "callback")
        .expect("cycle property should have a value")
        .iter()
        .collect::<Vec<_>>()[..]
    else {
        panic!("cycle property should contain one function identity");
    };
    assert!(alpha_reachable.contains(&ValueOrigin::Function(cycle_span)));
    assert!(
        alpha_reachable.contains(&ValueOrigin::Object(
            flow.values_of_property(alpha_span, "peer")
                .expect("peer property should have a value")
                .iter()
                .find_map(|origin| match origin {
                    ValueOrigin::Object(span) => Some(span),
                    _ => None,
                })
                .expect("peer property should point to beta"),
        ))
    );
    let unknown_reachable = &actual.0[&ValueOrigin::Unknown];
    assert!(unknown_reachable.contains(&ValueOrigin::Unknown));
    assert_eq!(actual.0[&foreign], BTreeSet::from([foreign]));
    assert!(actual.1.contains_key(&id("rooted")));
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
    let module = raw_js_test_support::parse(source);
    let body = top_level_item_views(&module.body);
    let (actual, stats) = ChunkValueFlow::analyze_with_stats(&body);
    assert_eq!(actual, ChunkValueFlow::analyze_reference(&body));
    stats.argument_evaluations
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
    expected.insert(ValueOrigin::Unknown);

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
    assert!(callback.contains(&ValueOrigin::Unknown));
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
        let source = return_call_chain(depth, false);
        let module = raw_js_test_support::parse(&source);
        let body = top_level_item_views(&module.body);
        let (actual, stats) = ChunkValueFlow::analyze_with_stats(&body);
        assert_eq!(actual, ChunkValueFlow::analyze_reference(&body));
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
    assert!(callback_values.contains(&ValueOrigin::Unknown));
    assert!(
        callback_values
            .iter()
            .any(|value| matches!(value, ValueOrigin::Function(_)))
    );
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
        assert!(values.contains(&ValueOrigin::Unknown));
        assert!(
            values
                .iter()
                .any(|value| matches!(value, ValueOrigin::Function(_)))
        );
    }
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
            .filter(|value| matches!(value, ValueOrigin::Function(_)))
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
                ValueOrigin::Object(object) => Some(object),
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
        assert!(unknown.contains(&ValueOrigin::Unknown));
        let changing = flow
            .values_of_property(object, "changing")
            .expect("the changing property should be written");
        for expected in expected_functions {
            assert!(changing.contains(expected));
        }
    }

    for binding in ["early_left", "late_left"] {
        let values = flow.values_of_binding(&id(binding));
        assert!(
            values.contains(&ValueOrigin::Unknown),
            "{binding} lost Unknown"
        );
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
        assert!(
            values.contains(&ValueOrigin::Unknown),
            "{binding} lost Unknown"
        );
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
            .filter(|value| matches!(value, ValueOrigin::Function(_)))
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
            .filter(|value| matches!(value, ValueOrigin::Function(_)))
            .count(),
        8,
    );
    assert!(
        flow.values_of_binding(&id("overlap"))
            .contains(&ValueOrigin::Unknown)
    );

    for name in ["first", "alias", "second"] {
        let values = flow.values_of_binding(&id(name));
        assert!(
            values.contains(&ValueOrigin::Unknown),
            "{name} lost Unknown"
        );
        assert!(
            values
                .iter()
                .filter(|value| matches!(value, ValueOrigin::Function(_)))
                .count()
                >= 8,
            "{name} lost part of the wide function set",
        );
    }

    let [ValueOrigin::Object(box_origin)] = flow
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
        assert!(values.contains(&ValueOrigin::Unknown) || property == "primary");
        assert!(
            values
                .iter()
                .filter(|value| matches!(value, ValueOrigin::Function(_)))
                .count()
                >= 8,
            "property {property} lost part of the wide function set",
        );
    }
}
