//! Guarded native summaries describe both call timing and returned values.

use debundle_e2e_support::*;

fn split_opts<'a>(source: &'a str, members: &[Member], anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("native/value", members, anonymous),
            logical_module("native/later", &[Member::new("later")]),
        ],
    )
}

#[test]
fn unary_math_summaries_compose_with_later_primitive_conversion() {
    for (method, expected) in [
        ("round", "2"),
        ("floor", "1"),
        ("ceil", "2"),
        ("trunc", "1"),
        ("abs", "1.6"),
        ("sign", "1"),
    ] {
        let source = format!(
            r#"globalThis.saved = () => later;
const input = 1.6;
const normalized = Math.{method}(input);
const text = `${{normalized}}`;
const later = 42;
console.log(text, later);
"#
        );
        let fixture = run_fixture(split_opts(
            &source,
            &[
                Member::new("input"),
                Member::new("normalized"),
                Member::new("text"),
            ],
            &["globalThis.saved = () => later;"],
        ));
        assert_preserved_entry_output(&fixture, &format!("{expected} 42\n"));
    }
}

#[test]
fn primitive_pattern_and_flags_prove_call_and_construction_timing() {
    for constructor in ["RegExp", "new RegExp"] {
        let source = format!(
            r#"globalThis.saved = () => later;
const pattern = {constructor}("ok", "i");
const later = 42;
console.log(pattern.test("OK"), later);
"#
        );
        let fixture = run_fixture(split_opts(
            &source,
            &[Member::new("pattern")],
            &["globalThis.saved = () => later;"],
        ));
        assert_preserved_entry_output(&fixture, "true 42\n");
    }
}

#[test]
fn regexp_object_conversion_keeps_real_reentry() {
    let source = r#"globalThis.saved = () => later;
const argument = { toString() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return "ok";
} };
const pattern = new RegExp(argument);
const later = 42;
console.log(globalThis.observed, pattern.test("ok"));
"#;
    expect_atomic_conflict_rejection(
        split_opts(
            source,
            &[Member::new("argument"), Member::new("pattern")],
            &["globalThis.saved = () => later;"],
        ),
        &["native/value", "native/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError true\n");
}

#[test]
fn aliased_global_constructor_replacement_keeps_real_reentry() {
    let source = r#"globalThis.saved = () => later;
const host = globalThis;
const Original = RegExp;
host.RegExp = function() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return new Original("ok");
};
const pattern = new RegExp("ok");
const later = 42;
console.log(globalThis.observed, pattern.test("ok"));
"#;
    expect_atomic_conflict_rejection(
        split_opts(
            source,
            &[
                Member::new("host"),
                Member::new("Original"),
                Member::new("pattern"),
            ],
            &[
                "globalThis.saved = () => later;",
                "host.RegExp = function() { try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; } return new Original('ok'); };",
            ],
        ),
        &["native/value", "native/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError true\n");
}

#[test]
fn regexp_syntax_errors_remain_observable() {
    let source = r#"let failure;
try { new RegExp("("); } catch (error) { failure = error.name; }
console.log(failure);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "SyntaxError\n");
}

#[test]
fn symbol_primitives_do_not_reenter_published_callbacks() {
    for expression in ["Symbol()", "Symbol('marker')", "Symbol.for('marker')"] {
        let source = format!(
            r#"globalThis.saved = () => later;
const marker = {expression};
const later = 42;
console.log(typeof marker, later);
"#
        );
        let fixture = run_fixture(split_opts(
            &source,
            &[Member::new("marker")],
            &["globalThis.saved = () => later;"],
        ));
        assert_preserved_entry_output(&fixture, "symbol 42\n");
    }
}

#[test]
fn symbol_object_coercion_keeps_real_reentry() {
    let source = r#"globalThis.saved = () => later;
const argument = { toString() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return "marker";
} };
const marker = Symbol(argument);
const later = 42;
console.log(globalThis.observed, typeof marker);
"#;
    expect_atomic_conflict_rejection(
        split_opts(
            source,
            &[Member::new("argument"), Member::new("marker")],
            &["globalThis.saved = () => later;"],
        ),
        &["native/value", "native/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError symbol\n");
}

#[test]
fn symbol_member_alias_mutation_keeps_real_reentry() {
    let source = r#"globalThis.saved = () => later;
const original = Symbol;
const symbols = Symbol;
symbols.for = () => {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return original("marker");
};
const marker = Symbol.for("marker");
const later = 42;
console.log(globalThis.observed, typeof marker);
"#;
    expect_atomic_conflict_rejection(
        split_opts(
            source,
            &[
                Member::new("original"),
                Member::new("symbols"),
                Member::new("marker"),
            ],
            &[
                "globalThis.saved = () => later;",
                "symbols.for = () => { try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; } return original('marker'); };",
            ],
        ),
        &["native/value", "native/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError symbol\n");
}
