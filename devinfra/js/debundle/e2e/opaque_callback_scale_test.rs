//! An opaque host can re-enter a pool of callbacks. Its abstract call tree
//! must be bounded even when each callback calls the host again.

use debundle_e2e_support::*;
use std::fmt::Write as _;

fn callback_pool() -> String {
    let mut source = String::from("globalThis.host = () => 7;\n");
    for index in 0..1024 {
        writeln!(
            source,
            "function factory{index}(callback) {{ host(callback); return state; }}"
        )
        .unwrap();
    }
    source.push_str("const result = host({");
    for index in 0..1024 {
        write!(source, "factory{index},").unwrap();
    }
    source.push_str("});\n");
    source
}

#[test]
fn reentrant_callback_pool_finishes_and_preserves_output() {
    let source = format!(
        "const state = 7;\n{}console.log(result);\nexport {{ result }};\n",
        callback_pool()
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn bounded_callback_summary_preserves_tdz_constraints() {
    let source = format!(
        "{}const state = 7;\nexport {{ result, state }};\n",
        callback_pool()
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("callbacks/invocation", &[Member::new("result")]),
                logical_module("values/state", &[Member::new("state")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["callbacks/invocation", "values/state"],
        &[],
    );
}

#[test]
fn shared_capture_diamond_is_walked_once_and_preserves_output() {
    let mut source = String::from(
        "globalThis.host = () => 7;\n\
         const state = 7;\n\
         function factory(left, right) {\n\
           return () => { host(left, right); return state; };\n\
         }\n\
         const callback0 = () => state;\n",
    );
    for index in 1..=32 {
        writeln!(
            source,
            "const callback{index} = factory(callback{}, callback{});",
            index - 1,
            index - 1,
        )
        .unwrap();
    }
    source.push_str("const result = host(callback32);\nconsole.log(result);\nexport { result };\n");
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}
