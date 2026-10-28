//! Math.round only avoids synchronous callbacks after primitive argument evaluation.

use debundle_e2e_support::*;

fn round_opts<'a>(source: &'a str, members: &[Member], anonymous: &[&str]) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon("values/rounded", members, anonymous),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
}

#[test]
fn primitive_arithmetic_argument_cannot_reenter_published_callback() {
    let source = r#"globalThis.saved = () => later;
const seconds = 138240;
const rounded = Math.round(seconds / 86400);
const later = 42;
console.log(rounded, later);
"#;
    let fixture = run_fixture(round_opts(
        source,
        &[Member::new("seconds"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    ));
    assert_preserved_entry_output(&fixture, "2 42\n");
    assert_all_emitted_js_checks(&fixture);
}

fn assert_round_reentry(source: &str, members: &[Member], anonymous: &[&str]) {
    expect_atomic_conflict_rejection(
        round_opts(source, members, anonymous),
        &["values/rounded", "values/later"],
        &[],
    );
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError 2\n");
}

#[test]
fn object_argument_coercion_can_reenter() {
    assert_round_reentry(
        r#"globalThis.saved = () => later;
const argument = { valueOf() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 1.6;
} };
const rounded = Math.round(argument);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("argument"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    );
}

#[test]
fn primitive_argument_evaluation_can_reenter() {
    assert_round_reentry(
        r#"globalThis.saved = () => later;
function argument() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 1.6;
}
const rounded = Math.round((argument(), 1.6));
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("argument"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    );
}

#[test]
fn shadowed_math_can_reenter() {
    assert_round_reentry(
        r#"globalThis.saved = () => later;
const Math = { round(value) {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 2;
} };
const rounded = Math.round(1.6);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("Math"), Member::new("rounded")],
        &["globalThis.saved = () => later;"],
    );
}

#[test]
fn aliased_math_method_mutation_can_reenter() {
    assert_round_reentry(
        r#"globalThis.saved = () => later;
const math = Math;
math.round = () => {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return 2;
};
const rounded = Math.round(1.6);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("math"), Member::new("rounded")],
        &[
            "globalThis.saved = () => later;",
            "math.round = () => { try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; } return 2; };",
        ],
    );
}

#[test]
fn math_method_getter_can_reenter() {
    assert_round_reentry(
        r#"globalThis.saved = () => later;
const originalRound = Math.round;
Object.defineProperty(Math, "round", { get() {
  try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; }
  return originalRound;
} });
const rounded = Math.round(1.6);
const later = 42;
console.log(globalThis.observed, rounded);
"#,
        &[Member::new("originalRound"), Member::new("rounded")],
        &[
            "globalThis.saved = () => later;",
            "Object.defineProperty(Math, 'round', { get() { try { globalThis.saved(); } catch (error) { globalThis.observed = error.name; } return originalRound; } });",
        ],
    );
}
