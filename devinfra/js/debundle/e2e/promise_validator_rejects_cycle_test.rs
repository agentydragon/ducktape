//! Phase 3 promise: a spec that produces a cycle in the at-init
//! module dep graph is rejected by `materialize_logical_modules`
//! with a clear error before any emit happens.
//!
//! Today the legacy emit silently produces an init-wrapper
//! cascade that papers over the cycle at runtime, often by
//! reading TDZ bindings or undefined values (the smoke errors
//! we hand-debugged were all symptoms of cycles the legacy
//! emitted anyway). The new design surfaces the cycle as a
//! structured error during the materialize pipeline stage,
//! before the bundle ever loads.
//!
//! The test fixture below sets up a small chunk where two
//! modules read each other's init-time bindings — mod_x reads
//! a binding owned by mod_y, mod_y reads a binding owned by
//! mod_x. The legacy emit accepts this and produces a runtime
//! crash; the new design rejects with a clear message.
//!
//! This test is `#[ignore]`'d until Phase 3 lands; running it
//! with `--include-ignored` verifies the new contract.

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn cyclic_spec_is_rejected_with_clear_error() {
    // Source: A and C are independent decls; B reads A at-init,
    // D reads C at-init. The spec puts A,D in mod_x and B,C in
    // mod_y, creating cross-module reads in both directions.
    //
    //   mod_x = { A, D }   D = wrap(C) reads C ∈ mod_y
    //   mod_y = { B, C }   B = wrap(A) reads A ∈ mod_x
    //
    // Cycle: mod_x ↔ mod_y.
    let opts = FixtureOpts::new(
        r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const C = "c";
const D = wrap(C);
console.log(B.ref, D.ref);
export { A, B, C, D };
"#,
        vec![
            json!({
                "id": "logical__mod_x",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_x" },
                "members": [
                    { "id": "m_a", "name": "A", "selector": { "binding": { "name": "A" } } },
                    { "id": "m_d", "name": "D", "selector": { "binding": { "name": "D" } } },
                ],
            }),
            json!({
                "id": "logical__mod_y",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_y" },
                "members": [
                    { "id": "m_b", "name": "B", "selector": { "binding": { "name": "B" } } },
                    { "id": "m_c", "name": "C", "selector": { "binding": { "name": "C" } } },
                ],
            }),
        ],
    );

    // The new design refuses to emit; the existing rejection
    // harness asserts the materialize stage exits non-zero with
    // a stderr message identifying both modules in the cycle.
    expect_logical_modules_e2e_rejection(opts, &["cycle", "mod_x", "mod_y"]);
}
