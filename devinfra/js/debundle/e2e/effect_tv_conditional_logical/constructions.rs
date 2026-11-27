//! `new` of plain functions, `this` stores and copied getters reach their effects.

use super::{assert_reader_split_diverges_with, with_reader_split};
use debundle_e2e_support::*;

/// `new F()` of a plain function that stores its `this` in a module binding
/// and stores `value` through `this`; `run` reaches the stored value
/// through the binding.
const THIS_STORE_SHAPES: &[(&str, &str)] = &[
    (
        "function F() { {guard}saved = this; this.fn = {value}; }",
        "function flag() { new F(); return saved; }\nfunction run() { return flag().fn(); }",
    ),
    (
        "function F() { this.fn = {value}; saved = this; }",
        "function flag() { {guard}new F(); return saved.fn; }\nfunction run() { return flag()(); }",
    ),
];

fn this_store_definitions(constructor: &str, rest: &str, guard: &str, value: &str) -> String {
    format!(
        "let saved = null;\n{}\n{}",
        constructor
            .replace("{guard}", guard)
            .replace("{value}", value),
        rest.replace("{guard}", guard)
    )
}

/// Opposing control: `new` of a plain function runs its body, so what the
/// body stores through `this` (here `second`) is published and reachable
/// through the binding that saved `this`.
#[test]
fn new_of_a_plain_function_runs_its_body() {
    for (constructor, rest) in THIS_STORE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            assert_reader_split_diverges_with(
                &this_store_definitions(constructor, rest, guard, "second"),
                &["saved", "F"],
            );
        }
    }
}

/// Positive anchor for the control above: the same constructors storing
/// the effect-free `first` keep the split.
#[test]
fn new_of_a_plain_function_without_effects_keeps_the_split() {
    for (constructor, rest) in THIS_STORE_SHAPES {
        for guard in ["", "if (first()) {} "] {
            let fixture = with_reader_split(
                &this_store_definitions(constructor, rest, guard, "first"),
                &["saved", "F"],
                run_fixture,
            );
            assert_preserved_entry_output(&fixture, "0 0\n");
        }
    }
}

/// Opposing control: `new` of a guarded plain function with a direct effect
/// (a call, a store, a parameter default); `this` saved by a top-level
/// construction and its stored callback called from another statement; and
/// a getter that reaches `second` only through a capture, copied by
/// `Object.defineProperties`. Each reaches `second`.
#[test]
fn constructions_and_copied_getters_reach_their_effects() {
    for (definitions, extra) in [
        (
            "function F() { if (first()) {} second(); }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function F() { if (first()) {} counter = 1; }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function F(x = second()) { if (first()) {} }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function F() { if (first()) {} second(); }\nfunction flag() { new F(); return noop; }\nfunction run() { return flag()(); }",
            &["F"][..],
        ),
        (
            "let saved = null;\nfunction F() { saved = this; this.fn = second; }\nconst inst = new F();\nfunction flag() { return saved; }\nfunction run() { return flag().fn(); }",
            &["saved", "F", "inst"][..],
        ),
        (
            "function mk(h) { return { get x() { return h; } }; }\nconst src = mk(second);\nfunction flag() { const o = Object.defineProperties({}, Object.getOwnPropertyDescriptors(src)); return o.x; }\nfunction run() { return flag()(); }",
            &["mk", "src"][..],
        ),
        (
            "function mk(h) { return { get x() { h(); return noop; } }; }\nconst src = mk(second);\nfunction flag() { const o = Object.defineProperties({}, Object.getOwnPropertyDescriptors(src)); o.x; return noop; }\nfunction run() { flag(); return 0; }",
            &["mk", "src"][..],
        ),
    ] {
        assert_reader_split_diverges_with(definitions, extra);
    }
}

/// Positive anchor for the control above: the same shapes over the
/// effect-free `first` keep the split.
#[test]
fn constructions_and_copied_getters_without_effects_keep_the_split() {
    for (definitions, extra) in [
        (
            "let saved = null;\nfunction F() { saved = this; this.fn = first; }\nconst inst = new F();\nfunction flag() { return saved; }\nfunction run() { return flag().fn(); }",
            &["saved", "F", "inst"][..],
        ),
        (
            "function F() { if (first()) {} first(); }\nfunction flag() { new F(); return noop; }\nfunction run() { flag(); return 0; }",
            &["F"][..],
        ),
        (
            "function mk(h) { return { get x() { return h; } }; }\nconst src = mk(first);\nfunction flag() { const o = Object.defineProperties({}, Object.getOwnPropertyDescriptors(src)); return o.x; }\nfunction run() { return flag()(); }",
            &["mk", "src"][..],
        ),
    ] {
        let fixture = with_reader_split(definitions, extra, run_fixture);
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}

/// A refused construction whose class body stores only a literal keeps the
/// split, although another module binding holds an uncalled effect.
#[test]
fn a_refused_construction_storing_literals_keeps_the_split() {
    for flag in [
        "function flag() { new K(); return noop; }\nfunction run() { flag(); return 0; }",
        "function flag() { if (first()) {} new K(); return noop; }\nfunction run() { if (first()) {} flag(); return 0; }",
        "function flag() { new K(); return noop; }\nfunction run() { return flag()(); }",
    ] {
        let fixture = with_reader_split(
            &format!(
                "const holder = {{ h: second }};\nclass Base {{}}\nclass K extends Base {{ constructor() {{ super(); this.n = 1; }} }}\n{flag}"
            ),
            &["holder", "Base", "K"],
            run_fixture,
        );
        assert_preserved_entry_output(&fixture, "0 0\n");
    }
}
