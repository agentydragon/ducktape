//! Values reaching a binding only as unknown are published, with their capabilities and arguments.

use super::{
    CALL_FLAG, assert_reader_split_commutes, assert_reader_split_diverges,
    assert_reader_split_diverges_with, with_reader_split,
};
use debundle_e2e_support::*;

/// The value-losing binding sources: each binds a part of `source` only as
/// unknown, so `source` is published. `{guard}` puts `flag` on the
/// source-summary lane when non-empty.
const UNKNOWN_SOURCE_SHAPES: &[(&str, &str)] = &[
    ("", "const [h] = [{value}]; return h;"),
    ("", "const [, h] = [noop, {value}]; return h;"),
    (
        "",
        "const k = 'h'; const { [k]: h } = { h: {value} }; return h;",
    ),
    (
        "function* gen() { yield {value}; }\n",
        "for (const h of gen()) { return h; } return noop;",
    ),
    (
        "function* gen() { yield {value}; }\n",
        "return gen().next().value;",
    ),
    (
        "function pick() { return arguments[0]; }\n",
        "return pick({value});",
    ),
    (
        "function pick() { for (const h of arguments) { return h; } return noop; }\n",
        "return pick({value});",
    ),
    ("", "try { throw {value}; } catch (e) { return e; }"),
    (
        "function pick({ h }) { return h; }\n",
        "return pick({ h: {value} });",
    ),
    (
        "function pick([h]) { return h; }\n",
        "return pick([{value}]);",
    ),
];

fn unknown_source_definitions(helpers: &str, body: &str, guard: &str, value: &str) -> String {
    format!(
        "{}function flag() {{ {guard}{} }}\n{CALL_FLAG}",
        helpers.replace("{value}", value),
        body.replace("{value}", value)
    )
}

/// Opposing control: an array element, a computed key, a generator's yield,
/// `arguments`, a thrown value or a pattern parameter's part reaches its
/// binding only as unknown, so the source is published and calling the
/// binding reaches `second`, on either lane.
#[test]
fn a_value_reaching_a_binding_only_as_unknown_is_published() {
    for (helpers, body) in UNKNOWN_SOURCE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            let extra = unknown_source_extra(helpers);
            assert_reader_split_diverges_with(
                &unknown_source_definitions(helpers, body, guard, "second"),
                &extra,
            );
        }
    }
}

fn unknown_source_extra(helpers: &str) -> Vec<&'static str> {
    ["gen", "pick"]
        .into_iter()
        .filter(|name| {
            helpers.contains(&format!("function {name}"))
                || helpers.contains(&format!("function* {name}"))
        })
        .collect()
}

/// Positive anchors: the same shapes carrying only the effect-free `first`
/// keep the split, with the source's output.
#[test]
fn a_value_reaching_a_binding_only_as_unknown_without_effects_keeps_the_split() {
    for (helpers, body) in UNKNOWN_SOURCE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &unknown_source_definitions(helpers, body, guard, "first"),
                &unknown_source_extra(helpers),
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// Opposing control: the same value-losing destructuring at the top level
/// publishes its source when its owner runs.
#[test]
fn a_top_level_value_reaching_a_binding_only_as_unknown_is_published() {
    for (declaration, extra) in [
        ("const [h] = [second];", &["h"][..]),
        ("const [, h] = [noop, second];", &["h"][..]),
        (
            "const k = 'h';\nconst { [k]: h } = { h: second };",
            &["k", "h"][..],
        ),
    ] {
        assert_reader_split_diverges_with(
            &format!("{declaration}\nfunction flag() {{ return h; }}\n{CALL_FLAG}"),
            extra,
        );
    }
}

/// Opposing control: a parameter default runs whenever the incoming value
/// may be `undefined`, not only when it is the literal `undefined` value: a
/// global `undefined` read, a missing property and an unknown argument may
/// each be `undefined`, so the default (`second`) is among the values.
#[test]
fn a_parameter_default_runs_for_an_argument_that_may_be_undefined() {
    for parameter in ["{ p } = { p: second }", "p = second"] {
        for argument in [
            "undefined",
            "[][0]",
            "({}).q",
            "JSON.parse('null') ?? undefined",
        ] {
            assert_reader_split_diverges(&format!(
                "function flag({parameter}) {{ if (first()) {{}} return p; }}\nfunction run() {{ return flag({argument})(); }}"
            ));
        }
    }
}

const ITERATION_RESULT_READS: &[&str] = &[
    "return [{value}].map((x) => x)[0];",
    "return [{value}].filter(() => true)[0];",
    "const items = [{value}]; return items.map((x) => x)[0];",
];

/// Opposing control: an iteration result the analysis cannot keep settled
/// is read as unknown, and unknown alone reaches no unescaped callable. The
/// read keeps the values the iteration stored, so calling the element read
/// from a `map` or `filter` result reaches `second`, on either lane.
#[test]
fn a_read_of_an_unsettled_iteration_result_keeps_its_stored_values() {
    for body in ITERATION_RESULT_READS {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges(&format!(
                "function flag() {{ {guard}{} }}\n{CALL_FLAG}",
                body.replace("{value}", "second")
            ));
        }
    }
}

/// Positive anchors: the same reads reaching only the effect-free `first`
/// keep the split.
#[test]
fn a_read_of_an_unsettled_iteration_result_without_effects_keeps_the_split() {
    for body in ITERATION_RESULT_READS {
        assert_reader_split_commutes(&format!(
            "function flag() {{ if (first()) {{}} {} }}\n{CALL_FLAG}",
            body.replace("{value}", "first")
        ));
    }
}

/// Opposing control: an arrow's `arguments` is its enclosing function's, so
/// a function whose nested arrow reads it publishes its call's arguments;
/// and a generator's `return` value reaches `next()` and `yield*` as a
/// yielded one does.
#[test]
fn arguments_read_by_an_arrow_and_a_generator_return_are_published() {
    for (definitions, extra) in [
        (
            "function pick() { const g = () => arguments[0]; return g(); }\nfunction flag() { return pick(second); }",
            &["pick"][..],
        ),
        (
            "function pick() { return () => arguments[0]; }\nfunction flag() { return pick(second)(); }",
            &["pick"][..],
        ),
        (
            "function pick() { return (() => () => arguments[0])()(); }\nfunction flag() { return pick(second); }",
            &["pick"][..],
        ),
        (
            "function pick() { return ((h = arguments[0]) => h)(); }\nfunction flag() { return pick(second); }",
            &["pick"][..],
        ),
        (
            "function* gen() { return second; }\nfunction flag() { return gen().next().value; }",
            &["gen"][..],
        ),
        (
            "function* inner() { return second; }\nfunction* gen() { const h = yield* inner(); yield h; }\nfunction flag() { return gen().next().value; }",
            &["inner", "gen"][..],
        ),
    ] {
        assert_reader_split_diverges_with(&format!("{definitions}\n{CALL_FLAG}"), extra);
    }
}

/// Opposing control: a value dropped to unknown keeps its capabilities, so
/// calling it reaches `second`: a capture past the closure budget in an owner
/// with no cells (`new Map()` keeps it off the ordered lane), and `new` of a
/// plain function that returns `second`.
#[test]
fn a_value_dropped_to_unknown_keeps_its_capabilities() {
    let locals: Vec<String> = (0..70).map(|index| format!("a{index}")).collect();
    let budget = format!(
        "function flag() {{ new Map(); let {} = 0; let zz = second; const get = () => ({}, zz); return get(); }}\n{CALL_FLAG}",
        locals.join(" = 0, "),
        locals.join(", ")
    );
    for definitions in [
        budget,
        format!(
            "function F() {{ return second; }}\nfunction flag() {{ return new F(); }}\n{CALL_FLAG}"
        ),
    ] {
        let extra: &[&'static str] = if definitions.contains("function F") {
            &["F"]
        } else {
            &[]
        };
        assert_reader_split_diverges_with(&definitions, extra);
    }
}

/// Positive anchor for the control above: `new` of a plain function that
/// returns only the effect-free `first` keeps the split.
#[test]
fn a_constructed_plain_function_without_effects_keeps_the_split() {
    let fixture = with_reader_split(
        &format!(
            "function F() {{ return first; }}\nfunction flag() {{ if (first()) {{}} return new F(); }}\n{CALL_FLAG}"
        ),
        &["F"],
        run_fixture,
    );
    assert_preserved_entry_output(&fixture, "0 0\n");
}
