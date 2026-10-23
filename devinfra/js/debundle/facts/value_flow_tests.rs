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
    actual
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
