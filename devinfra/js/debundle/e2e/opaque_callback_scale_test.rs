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
fn shared_and_equivalent_capture_dags_preserve_output() {
    let mut source = String::from(
        "globalThis.host = () => 7;\n\
         const state = 7;\n\
         function factory(left, right) {\n\
           return () => { host(left, right); return state; };\n\
         }\n\
         const callback = () => state;\n\
         const left0 = callback;\n\
         const right0 = callback;\n",
    );
    for index in 1..=32 {
        // Each DAG shares captures internally. The two DAGs are structurally
        // equal but were allocated separately, so pointer equality alone does
        // not prevent exponential comparisons when their values meet.
        for name in ["left", "right"] {
            writeln!(
                source,
                "const {name}{index} = factory({name}{}, {name}{});",
                index - 1,
                index - 1,
            )
            .unwrap();
        }
    }
    source.push_str(
        "const result = host({ left32, right32 });\nconsole.log(result);\nexport { result };\n",
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn repeated_opaque_calls_do_not_grow_callback_capture_cross_products() {
    let mut source = String::from(
        "globalThis.host = () => 7;\n\
         const state = 7;\n\
         function retain(callback) { return () => callback(); }\n",
    );
    for index in 0..256 {
        writeln!(
            source,
            "function factory{index}(callback) {{ host(retain(callback)); return state; }}"
        )
        .unwrap();
    }
    source.push_str("const payload = {");
    for index in 0..256 {
        write!(source, "factory{index},").unwrap();
    }
    source.push_str("};\n");
    for index in 0..512 {
        writeln!(source, "const result{index} = host(payload);").unwrap();
    }
    source.push_str("console.log(result511);\nexport { result511 };\n");
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}
