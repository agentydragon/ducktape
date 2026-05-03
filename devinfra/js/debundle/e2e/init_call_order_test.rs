//! Init-call order across modules.
//!
//! When `materialize_logical_modules` routes two plans through the
//! init-wrapper pattern and one plan's init body references the
//! other's binding, the order in which the init functions are
//! called from the residual entry must follow the dependency
//! direction: the dependency target (`modB`'s init) must run
//! before the dependent (`modA`'s init).
//!
//! Today the entry calls inits in source-ordinal-of-first-triggering-
//! item order. When `modA`'s first binding precedes `modB`'s in
//! source but `modA`'s later body references `modB`'s binding,
//! `modA`'s init runs first, reads `modB`'s binding (still
//! `undefined` under the var-placeholder shape), and any property
//! access on it throws `TypeError: Cannot read properties of
//! undefined`. This mirrors the Tana smoke's
//! `m.dataTypeNumberId` failure where `m` belongs to
//! `runtime_vendor_symbols` (init at line 381) but the init body
//! that reads it lives in `ai_mcp_prompting_runtime` (init at line
//! 282).

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn init_call_order_respects_cross_module_dependency() {
    // - `modA` owns `x1` (source item 1, unsafe init) and `x2`
    //   (source item 3; init reads `y.id`).
    // - `modB` owns `y` (source item 2, unsafe init).
    //
    // Source order has `x1` first, so `modA`'s init is called by
    // the entry first. `modA`'s init body has `x2 = { [y.id]: "v" }`;
    // if `modB`'s init hasn't run yet, `y` is `undefined` and the
    // computed-key access throws TypeError on module load.
    let opts = FixtureOpts::new(
        r#"function f() { return { id: "k" }; }
const x1 = f();
const y = f();
const x2 = { [y.id]: "v" };
console.log(x1.id, y.id, x2.k);
export { x1, y, x2 };
"#,
        vec![
            json!({
                "id": "logical__mod_a",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_a" },
                "members": [
                    {
                        "id": "m_x1",
                        "name": "x1",
                        "selector": { "binding": { "name": "x1" } },
                    },
                    {
                        "id": "m_x2",
                        "name": "x2",
                        "selector": { "binding": { "name": "x2" } },
                    },
                ],
            }),
            json!({
                "id": "logical__mod_b",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_b" },
                "members": [{
                    "id": "m_y",
                    "name": "y",
                    "selector": { "binding": { "name": "y" } },
                }],
            }),
        ],
    );
    let fixture = run_logical_modules_e2e_fixture(opts);

    // Behaviour preservation under correct init-call ordering:
    // entry should print "k k v". Today (without the init-order
    // fix) entry crashes at module load with `TypeError: Cannot
    // read properties of undefined (reading 'id')`.
    assert_entry_output(&fixture, "k k v\n");
}
