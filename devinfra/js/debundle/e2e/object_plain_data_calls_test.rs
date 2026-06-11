//! End-to-end pinning for the `Object.{entries, keys, values,
//! freeze, fromEntries}` plain-data admission rule
//! (`PURE_OBJECT_CALLS_ON_PLAIN_DATA`).
//!
//! These built-ins are excluded from `PURE_STATIC_CALLS` because
//! on a general argument they call `[[Get]]` on own keys (firing
//! user accessors) or mutate descriptors. They become Pure only
//! when the argument is structurally a fresh plain-data
//! object/array literal (no accessors / methods / `__proto__` /
//! computed keys / spread of non-plain-data sources). The negative
//! case — an opaque ident or function-call argument — stays
//! Unknown.
//!
//! Companion unit tests in `analysis_tests.rs` cover the bare
//! classifier verdicts; these end-to-end tests pin the
//! cycle-breaking behaviour the rule enables in real specs.

use debundle_e2e_support::*;

/// Cycle-forcing layout for the positive case:
///   1. `const a = (() => 1)();` — SE; stays in residual
///   2. `const b = Object.freeze({ x: 1 });` — would be SE
///      without the rule; peeled to b_module
///   3. `const c = b.x + a;` — reads b at init
///   4. `console.log(c);`
///
/// Without the rule: `Object.freeze({…})` classifies Unknown
/// (the call form is excluded from `PURE_STATIC_CALLS`), `b` is
/// SE → `b → a` s-edge → cycle after peeling `b` to b_module.
///
/// With the rule: the call is Pure on a fresh plain-data literal,
/// `b` is not SE, the cycle disappears, validator accepts.
#[test]
fn object_freeze_on_plain_object_literal_admits() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = Object.freeze({ x: 1 });
const c = b.x + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = Object.freeze("],
        &["const a"],
    );
    assert_entry_output(&fixture, "2\n");
}

#[test]
fn object_entries_on_plain_object_literal_admits() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = Object.entries({ x: 1, y: 2 });
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = Object.entries("],
        &["const a"],
    );
    assert_entry_output(&fixture, "3\n");
}

#[test]
fn object_keys_on_plain_array_literal_admits() {
    // Array literal is also an ordinary plain-data shape — its own
    // properties are integer-indexed data slots, no accessors.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = Object.keys([10, 20, 30]);
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = Object.keys("],
        &["const a"],
    );
    assert_entry_output(&fixture, "4\n");
}

#[test]
fn object_from_entries_on_array_of_pair_literals_admits() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a = (() => 1)();
const b = Object.fromEntries([["x", 1], ["y", 2]]);
const c = b.x + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = Object.fromEntries("],
        &["const a"],
    );
    assert_entry_output(&fixture, "2\n");
}

/// Negative: a non-literal argument (an IIFE call here) leaves
/// `Object.entries(...)` classified Unknown. The side-effect cycle
/// closes and the validator rejects.
#[test]
fn object_entries_on_non_literal_arg_does_not_admit() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a = (() => 1)();
const b = Object.entries((() => ({ x: 1 }))());
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
            vec![logical_module("b_module", &[Member::new("b")])],
        ),
        &["cycle", "b_module", "residual"],
    );
}

/// Negative: an object literal carrying a getter has accessor
/// channels — admitting would let `Object.values`/`entries` fire
/// user code. The strict shape predicate (`is_plain_data_prop`)
/// rejects getter properties, so the call stays Unknown and the
/// cycle is preserved.
#[test]
fn object_values_on_object_with_getter_does_not_admit() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a = (() => 1)();
const b = Object.values({ get x() { return 1; } });
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
            vec![logical_module("b_module", &[Member::new("b")])],
        ),
        &["cycle", "b_module", "residual"],
    );
}
