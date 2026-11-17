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

#[test]
fn exported_aliases_share_one_callback_discovery_traversal() {
    let mut source = String::from(
        "import './peer.js';\n\
         globalThis.host = () => 7;\n\
         const state = 7;\n",
    );
    for index in 0..1024 {
        writeln!(source, "function reader{index}() {{ return state; }}").unwrap();
    }
    source.push_str("const payload = {");
    for index in 0..1024 {
        write!(source, "reader{index},").unwrap();
    }
    source.push_str("};\n");
    for index in 0..1024 {
        writeln!(source, "export const alias{index} = payload;").unwrap();
    }
    for index in 0..256 {
        writeln!(source, "const result{index} = host();").unwrap();
    }
    source.push_str("console.log(result255);\nexport { result255 };\n");
    // An import cycle keeps export reentry possible. The provider's aliases
    // all refer to the same object/callback graph.
    let peer = [("static/peer", "import './app.js'; export {};\n")];
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_extra_chunks(&peer),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn escaped_callback_read_closure_is_reused_across_source_prefixes() {
    let mut source = String::from("globalThis.host = () => 7;\nconst state = 7;\n");
    for index in 0..512 {
        write!(source, "function reader{index}() {{ return [state,").unwrap();
        for dependency in 0..index {
            write!(source, "reader{dependency},").unwrap();
        }
        source.push_str("]; }\n");
    }
    source.push_str("const payload = {");
    for index in 0..512 {
        write!(source, "reader{index},").unwrap();
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

#[test]
fn cached_callback_summary_grows_at_a_later_tdz_read() {
    let source = format!(
        "const state = 7;\n{}\nconst second = host(() => later);\n\
         const later = 42;\nexport {{ second, later }};\n",
        callback_pool(),
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("callbacks/second", &[Member::new("second")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["callbacks/second", "values/later"],
        &[],
    );
}

#[test]
fn cached_invocations_do_not_copy_fallback_effects_through_each_call() {
    let mut source = String::from("globalThis.host = () => 7;\nconst state = 7;\n");
    for index in 0..2048 {
        writeln!(source, "function reader{index}() {{ return state; }}").unwrap();
    }
    // Keep the pool below the bulk-pool bound, including the host callback.
    // Deferred return bodies give each fallback a large read summary without
    // making those reads part of the precise invocation's eager effects.
    for index in 0..63 {
        write!(
            source,
            "function factory{index}(callback) {{ host(callback); return () => ["
        )
        .unwrap();
        for reader in 0..2048 {
            write!(source, "reader{reader},").unwrap();
        }
        source.push_str("]; }\n");
    }
    source.push_str("const payload = {");
    for index in 0..63 {
        write!(source, "factory{index},").unwrap();
    }
    source.push_str("};\nconst result = (");
    for _ in 0..4096 {
        source.push_str("host(payload),");
    }
    source.push_str("7);\nconsole.log(result);\nexport { result };\n");
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}
