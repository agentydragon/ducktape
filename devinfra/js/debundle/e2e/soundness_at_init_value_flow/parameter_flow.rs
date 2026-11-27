//! Functions passed through parameters, constructors and class bodies read initialized values.

use super::{in_case, members};
use debundle_e2e_support::*;
use std::fmt::Write as _;

/// `mod_a` and `mod_b` with the given members, default unassigned mode.
fn ab_split<'a>(
    source: &'a str,
    mod_a: &[&'static str],
    mod_b: &[&'static str],
) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module("mod_a", &members(mod_a)),
            logical_module("mod_b", &members(mod_b)),
        ],
    )
}

const CALLBACK_PARAMETER_SOURCE: &str = r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function bar(callback) { return callback(); }
const result = bar(foo);
console.log(result);
export { a, b, foo, bar, result };
"#;

const CONSTRUCTOR_BODY_SOURCE: &str = r#"const a = 1;
const b = a + 1;
class Foo { constructor() { this.value = b; } }
const result = new Foo().value;
console.log(result);
export { a, b, Foo, result };
"#;

/// `result` calls into a read of `b`, which needs `a` from `mod_a`: a cycle
/// whichever way a function value reaches the call.
#[test]
fn function_values_reading_initialized_value_reject_split() {
    for (name, source, mod_a) in [
        (
            "function passed through a parameter",
            CALLBACK_PARAMETER_SOURCE,
            &["a", "foo", "bar", "result"][..],
        ),
        (
            "constructor body",
            CONSTRUCTOR_BODY_SOURCE,
            &["a", "Foo", "result"],
        ),
        (
            "callback returned through a parameter",
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
function identity(callback) { return callback; }
const result = identity(foo)();
console.log(result);
export { a, b, foo, identity, result };
"#,
            &["a", "foo", "identity", "result"],
        ),
        (
            "callback held in a static object property",
            r#"const a = 1;
const b = a + 1;
function foo() { return b; }
const box = { invoke: foo };
const result = box.invoke();
console.log(result);
export { a, b, foo, box, result };
"#,
            &["a", "foo", "box", "result"],
        ),
    ] {
        in_case(name, || {
            expect_cycle_rejection(ab_split(source, mod_a, &["b"]), &["mod_a", "mod_b"]);
        });
    }
}

/// Anchors for the rejections above: moving `a` with `b` breaks the cycle.
#[test]
fn function_values_reading_initialized_value_run_when_dependency_moves_with_it() {
    for (name, source, mod_a) in [
        (
            "callback assignment",
            CALLBACK_PARAMETER_SOURCE,
            &["foo", "bar", "result"][..],
        ),
        (
            "constructor assignment",
            CONSTRUCTOR_BODY_SOURCE,
            &["Foo", "result"],
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(ab_split(source, mod_a, &["a", "b"]));
            assert_preserved_entry_output(&fixture, "2\n");
        });
    }
}

#[test]
fn uninstantiated_class_bodies_do_not_create_an_eager_cycle() {
    for (name, source) in [
        (
            "constructor",
            r#"class Foo { constructor() { bar(); } }
function bar() { return b; }
const b = Foo.name;
console.log(b);
export { Foo, bar, b };
"#,
        ),
        (
            "instance field",
            r#"class Foo { value = bar(); }
function bar() { return b; }
const b = Foo.name;
console.log(b);
export { Foo, bar, b };
"#,
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(
                ab_split(source, &["Foo", "bar"], &["b"]).with_import_order(&["mod_b", "mod_a"]),
            );
            assert_preserved_entry_output(&fixture, "Foo\n");
        });
    }
}

#[test]
fn class_definition_time_calls_still_run() {
    for (name, source, expected) in [
        (
            "static field",
            r#"const b = 3;
function bar() { return b; }
class Foo { static value = bar(); }
console.log(Foo.value);
export { b, bar, Foo };
"#,
            "3\n",
        ),
        (
            "computed method key",
            r#"const b = "value";
function bar() { return b; }
class Foo { [bar()]() {} }
console.log(typeof Foo.prototype.value);
export { b, bar, Foo };
"#,
            "function\n",
        ),
    ] {
        in_case(name, || {
            let fixture = run_fixture(ab_split(source, &["bar", "Foo"], &["b"]));
            assert_preserved_entry_output(&fixture, expected);
        });
    }
}

#[test]
fn return_call_chain_through_parameters_recursion_and_property_fanout_preserves_output() {
    const RETURN_DEPTH: usize = 64;
    const PROPERTY_FANOUT: usize = 24;

    let mut source = String::from("function base() { return 7; }\n");
    for index in 0..RETURN_DEPTH {
        writeln!(source, "function f{index}() {{ return f{}(); }}", index + 1).unwrap();
    }
    writeln!(source, "function f{RETURN_DEPTH}() {{ return base; }}").unwrap();
    source.push_str(
        "function apply(callback, depth) {\n\
           if (depth > 0) return apply(callback, depth - 1);\n\
           return callback();\n\
         }\n\
         const source = { run: f0 };\n",
    );
    for index in 0..PROPERTY_FANOUT {
        writeln!(source, "const branch{index} = {{ run: source.run }};").unwrap();
    }
    writeln!(
        source,
        "const result = apply(branch{}.run, 2)();",
        PROPERTY_FANOUT - 1
    )
    .unwrap();
    source.push_str("console.log(result);\nexport { result };\n");

    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

// ─── property writes and setters ────────────────────────────────────────
