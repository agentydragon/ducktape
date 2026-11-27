//! Classes publish the enclosing locals their heritage and members capture.

use super::{assert_reader_split_diverges_with, with_reader_split};
use debundle_e2e_support::*;

const SUPER_BASES: &[&str] = &[
    "function Base(f) { f(); }",
    "class Base { constructor(f) { f(); } }",
];

fn super_capture_definitions(base: &str, guard: &str, value: &str) -> String {
    format!(
        "{base}\nfunction make(h) {{ {guard}return class extends Base {{ constructor() {{ super(h); }} }}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make({value}); return new C(); }}"
    )
}

/// Opposing control: a class value carries no captured context, so its
/// constructor's `super(h)` runs the base on unknown arguments later. The
/// class's creation publishes the enclosing locals its members read, so the
/// base calling its argument reaches `second`.
#[test]
fn a_class_publishes_the_enclosing_locals_its_members_read() {
    for base in SUPER_BASES {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &super_capture_definitions(base, guard, "second"),
                &["Base", "make"],
            );
        }
    }
}

/// Positive anchor: the same classes passing only the effect-free `first`
/// keep the split.
#[test]
fn a_class_capturing_only_effect_free_functions_keeps_the_split() {
    for base in SUPER_BASES {
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &super_capture_definitions(base, guard, "first"),
                &["Base", "make"],
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// `make` returning a class that reaches `h` through its heritage or a
/// member, and `run` reaching that member.
const CLASS_CAPTURE_SHAPES: &[(&str, &str)] = &[
    (
        "return class extends h {};",
        "const C = make({value}); return new C();",
    ),
    (
        "return class extends h { constructor() { super(); } };",
        "const C = make({value}); return new C();",
    ),
    (
        "return class { static m() { return h(); } };",
        "return make({value}).m();",
    ),
    (
        "return class { f = h(); };",
        "const C = make({value}); return new C();",
    ),
    (
        "class C { m() { return h(); } } return C;",
        "const C = make({value}); return new C().m();",
    ),
];

fn class_capture_definitions(make: &str, run: &str, guard: &str, value: &str) -> String {
    let run = run.replace("{value}", value);
    format!(
        "function make(h) {{ {guard}{make} }}\nfunction flag() {{ return 0; }}\nfunction run() {{ {run} }}"
    )
}

/// Opposing control: a class reaching a captured `h` through its heritage,
/// a static method, a field initializer, or a declared class's method
/// reaches `second` when `run` constructs or calls it.
#[test]
fn a_class_publishes_its_heritage_and_member_captures() {
    for (make, run) in CLASS_CAPTURE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &class_capture_definitions(make, run, guard, "second"),
                &["make"],
            );
        }
    }
}

/// Positive anchor for the control above: the same classes over the
/// effect-free `first` keep the split.
#[test]
fn a_class_capturing_an_effect_free_heritage_or_member_keeps_the_split() {
    for (make, run) in CLASS_CAPTURE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &class_capture_definitions(make, run, guard, "first"),
                &["make"],
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

// The analyzer's class values do not retain their creation environment. The
// superclass can be an enclosing value reached through an expression, a
// local object, or a factory; a later `new` must still reach it.
const COMPOUND_CLASS_HERITAGE: &[(&str, &str, &[&str])] = &[
    ("return class extends (0, h) {};", "", &[]),
    (
        "const o = { Base: h }; return class extends o.Base {};",
        "",
        &[],
    ),
    (
        "return class extends mixin(h) {};",
        "function mixin(v) { return v; }",
        &["mixin"],
    ),
    ("return class extends (first() ? first : h) {};", "", &[]),
    ("return class extends (h || first) {};", "", &[]),
    (
        "function factory() { return h; } return class extends factory() {};",
        "",
        &[],
    ),
    (
        "const factory = () => h; return class extends factory() {};",
        "",
        &[],
    ),
    // These were already rejected: keep the unknown-property and unknown-syntax
    // controls beside the newly covered cases.
    (
        "const o = { Base: h }; const key = 'Base'; return class extends o[key] {};",
        "",
        &[],
    ),
    (
        "const factory = () => h; return class extends factory`` {};",
        "",
        &[],
    ),
];

fn compound_heritage_definitions(make: &str, prefix: &str, guard: &str, value: &str) -> String {
    format!(
        "{prefix}\n{}",
        class_capture_definitions(
            make,
            "const C = make({value}); return new C();",
            guard,
            value
        ),
    )
}

#[test]
fn a_class_publishes_compound_heritage_captures() {
    for &(make, prefix, extra) in COMPOUND_CLASS_HERITAGE {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &compound_heritage_definitions(make, prefix, guard, "second"),
                &members,
            );
        }
    }
}

#[test]
fn a_class_with_effect_free_compound_heritage_keeps_the_split() {
    for &(make, prefix, extra) in COMPOUND_CLASS_HERITAGE {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &compound_heritage_definitions(make, prefix, guard, "first"),
                &members,
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

#[test]
fn compound_heritage_does_not_publish_an_unrelated_parameter() {
    let definitions = "function make(h, unused) { return class extends (0, h) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(first, second); return new C(); }";
    let fixture = with_reader_split(definitions, &["make"], run_fixture);
    assert_preserved_entry_output(&fixture, "0 0\n");
}

#[test]
fn a_constructor_created_in_the_heritage_keeps_its_effects() {
    for body in ["second();", "counter = 1;"] {
        let definitions = format!(
            "function make() {{ return class extends (0, function Base() {{ {body} }}) {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make(); return new C(); }}"
        );
        assert_reader_split_diverges_with(&definitions, &["make"]);
    }
}

#[test]
fn a_local_heritage_factory_can_return_a_module_callback() {
    for (value, effectful) in [("second", true), ("first", false)] {
        let definitions = format!(
            "function make() {{ const factory = () => {value}; return class extends factory() {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make(); return new C(); }}"
        );
        if effectful {
            assert_reader_split_diverges_with(&definitions, &["make"]);
        } else {
            let fixture = with_reader_split(&definitions, &["make"], run_fixture);
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

#[test]
fn a_heritage_factory_runs_at_class_creation() {
    let definitions = "function factory() { second(); return first; }\nfunction make() { return class extends factory() {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(); return new C(); }";
    assert_reader_split_diverges_with(definitions, &["factory", "make"]);
}

#[test]
fn heritage_observes_a_local_changed_by_an_earlier_operand() {
    let definitions = "function make(h) { function change() { h = second; } return class extends (change(), h) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(first); return new C(); }";
    assert_reader_split_diverges_with(definitions, &["make"]);
}

const MODULE_HERITAGE_UPDATES: &[(&str, &str, &[&str])] = &[
    ("let base = first;", "base = h, base", &["base"]),
    (
        "let base = first; function save(v) { base = v; }",
        "save(h), base",
        &["base", "save"],
    ),
];

fn module_heritage_update_definitions(
    prefix: &str,
    update: &str,
    guard: &str,
    value: &str,
) -> String {
    format!(
        "{prefix}\nfunction make(h, unused) {{ {guard}return class extends ({update}) {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make({value}, second); return new C(); }}"
    )
}

#[test]
fn class_heritage_preserves_inputs_of_module_binding_updates() {
    for &(prefix, update, extra) in MODULE_HERITAGE_UPDATES {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &module_heritage_update_definitions(prefix, update, guard, "second"),
                &members,
            );
        }
    }
}

/// A discarded call to an inert helper does not make its argument a possible
/// superclass. The unchanged `first` is the only base the class can use.
#[test]
fn discarded_inert_heritage_call_keeps_its_argument_private() {
    for (helper, args) in [
        ("function ignore(x) { return 0; }", "h"),
        ("function ignore(x) {}", "h"),
        ("function ignore(x) { return; }", "h"),
        ("function ignore(x, y) { return false; }", "0, h, first"),
    ] {
        for guard in ["", "if (first()) {} "] {
            let definitions = format!(
                "{helper}\nfunction make(h) {{ {guard}return class extends (ignore({args}), first) {{}}; }}\nfunction flag() {{ return 0; }}\nfunction run() {{ const C = make(second); return new C(); }}"
            );
            let fixture = with_reader_split(&definitions, &["ignore", "make"], run_fixture);
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// The argument remains reachable if it is also the selected base, if the
/// helper can publish it, or if the helper binding can be replaced.
#[test]
fn discarded_heritage_call_keeps_possible_superclass_capabilities() {
    for definitions in [
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), h) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), first) { constructor() { super(); h(); } }; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { first.prototype.fire = h; return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C().fire(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { first.prototype = h; return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return Object.getPrototypeOf(C.prototype)(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { const proto = first.prototype; proto.fire = h; return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C().fire(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h()), first) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), first) {}; }\nfunction flag() { return 0; }\nfunction run() { first = second; const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x) { base = arguments[0]; return 0; }\nfunction make(h) { return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { ignore = function (x) { base = x; }; const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x) { return 0; }\nfunction make(h) { base = h; return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(second); return new C(); }",
        "let base = first;\nfunction ignore(x = (base = second)) { return 0; }\nfunction make(h) { return class extends (ignore(h), base) {}; }\nfunction flag() { return 0; }\nfunction run() { const C = make(undefined); return new C(); }",
        "function ignore(x) { return 0; }\nfunction make(h) { return class extends (ignore(heritageArg), first) {}; }\nfunction flag() { return 0; }\nfunction run() { Object.defineProperty(globalThis, 'heritageArg', { get() { second(); return 0; }, configurable: true }); const C = make(first); return new C(); }",
    ] {
        let extra: &[&str] = if definitions.contains("let base") {
            &["ignore", "make", "base"]
        } else {
            &["ignore", "make"]
        };
        assert_reader_split_diverges_with(definitions, extra);
    }
}

#[test]
fn effect_free_module_heritage_updates_keep_unrelated_parameters_private() {
    for &(prefix, update, extra) in MODULE_HERITAGE_UPDATES {
        let mut members = vec!["make"];
        members.extend_from_slice(extra);
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &module_heritage_update_definitions(prefix, update, guard, "first"),
                &members,
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}
