//! End-to-end pinning for the `Object.{entries, keys, values,
//! freeze, fromEntries}` plain-data admission rule (their builtin
//! summary rows).
//!
//! These built-ins are not pure on any argument because
//! on a general argument they call `[[Get]]` on own keys (firing
//! user accessors) or mutate descriptors. They become Pure only
//! when the argument is structurally a fresh plain-data
//! object/array literal (no accessors / methods / `__proto__` /
//! computed keys / spread of non-plain-data sources). The negative
//! case — an opaque ident or function-call argument — stays
//! Unknown.
//!
//! Companion unit tests in `purity/classifier_tests.rs` cover the
//! bare classifier verdicts; these end-to-end tests pin the
//! cycle-breaking behaviour the rule enables in real specs.

use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

fn assert_plain_data_call_schedule_limit(source: &str, call: &str, expected_stdout: &str) {
    let opts = || {
        FixtureOpts::new(
            source,
            vec![logical_module("b_module", &[Member::new("b")])],
        )
    };
    let rejected = assert_conservative_schedule_refusal(
        opts(),
        "evaluation_schedule_unknown_reordered",
        expected_stdout,
    );
    let assessment = rejected.candidate_relocation_assessment();
    assert_eq!(assessment["legacy_gate_accepts"], true, "{assessment:#}");
    assert_module_source(
        &rejected.app_root(),
        "static/app/modules/b_module.js",
        &[call],
        &["const a"],
    );
    let graph = rejected.dependency_graph();
    assert!(
        node_declaring(&graph, "b").purity.is_pure(),
        "the plain-data native call remains pure in the split candidate"
    );

    let control = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[Member::new("a"), Member::new("b"), Member::new("c")],
        )],
    ));
    assert_preserved_entry_output(&control, expected_stdout);
    assert!(
        node_declaring(&control.dependency_graph(), "b")
            .purity
            .is_pure()
    );
}

/// Cycle-forcing layout for the positive case:
///   1. `const a = (() => 1)();` — SE; stays in residual
///   2. `const b = Object.freeze({ x: 1 });` — would be SE
///      without the rule; peeled to b_module
///   3. `const c = b.x + a;` — reads b at init
///   4. `console.log(c);`
///
/// Without the rule: `Object.freeze({…})` classifies Unknown
/// (on a general argument), `b` is
/// SE → `b → a` s-edge → cycle after peeling `b` to b_module.
///
/// With the rule: the call is Pure on a fresh plain-data literal,
/// `b` is not SE, the cycle disappears, validator accepts.
#[test]
fn object_freeze_on_plain_object_literal_admits() {
    assert_pure_cycle_break(
        r#"const a = (() => 1)();
const b = Object.freeze({ x: 1 });
const c = b.x + a;
console.log(c);
export { a, b, c };
"#,
        vec![logical_module("b_module", &[Member::new("b")])],
        "b_module",
        &["const b = Object.freeze("],
        &["const a"],
        "2\n",
    );
}

#[test]
fn object_entries_on_plain_object_literal_admits() {
    assert_plain_data_call_schedule_limit(
        r#"const a = (() => 1)();
const b = Object.entries({ x: 1, y: 2 });
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
        "const b = Object.entries(",
        "3\n",
    );
}

#[test]
fn object_keys_on_plain_array_literal_admits() {
    // Array literal is also an ordinary plain-data shape — its own
    // properties are integer-indexed data slots, no accessors.
    assert_plain_data_call_schedule_limit(
        r#"const a = (() => 1)();
const b = Object.keys([10, 20, 30]);
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
        "const b = Object.keys(",
        "4\n",
    );
}

#[test]
fn object_from_entries_on_array_of_pair_literals_admits() {
    assert_plain_data_call_schedule_limit(
        r#"const a = (() => 1)();
const b = Object.fromEntries([["x", 1], ["y", 2]]);
const c = b.x + a;
console.log(c);
export { a, b, c };
"#,
        "const b = Object.fromEntries(",
        "2\n",
    );
}

/// Negative: a non-literal argument (an IIFE call here) leaves
/// `Object.entries(...)` classified Unknown. The side-effect cycle
/// closes and the validator rejects.
#[test]
fn object_entries_on_non_literal_arg_does_not_admit() {
    expect_pure_cycle_rejection(
        FixtureOpts::new(
            r#"const a = (() => 1)();
const b = Object.entries((() => ({ x: 1 }))());
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
            vec![logical_module("b_module", &[Member::new("b")])],
        ),
        "b_module",
    );
}

/// Negative: an object literal carrying a getter has accessor
/// channels — admitting would let `Object.values`/`entries` fire
/// user code. The strict shape predicate (`is_plain_data_prop`)
/// rejects getter properties, so the call stays Unknown and the
/// cycle is preserved.
#[test]
fn object_values_on_object_with_getter_does_not_admit() {
    expect_pure_cycle_rejection(
        FixtureOpts::new(
            r#"const a = (() => 1)();
const b = Object.values({ get x() { return 1; } });
const c = b.length + a;
console.log(c);
export { a, b, c };
"#,
            vec![logical_module("b_module", &[Member::new("b")])],
        ),
        "b_module",
    );
}
