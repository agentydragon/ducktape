//! Init order across modules.
//!
//! When `modA`'s body reads a binding owned by `modB` at-init,
//! `modB` must evaluate first. Source-order emit hands this off
//! to the ESM linker via cross-module imports: the import line
//! in `modA` declares the dep, and the linker topologically
//! sorts before evaluation. The fixture below verifies that
//! semantic via stdout — even when the modules' source bindings
//! interleave in a way the legacy init-wrapper pattern got
//! wrong.

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn init_call_order_respects_cross_module_dependency() {
    // - `modA` owns `x1` (source item 1) and `x2` (source item 3;
    //   init reads `y.id`).
    // - `modB` owns `y` (source item 2).
    //
    // Source order has `x1` first, but `modA`'s body reads `y`
    // at-init through `{ [y.id]: "v" }`. The cross-module import
    // declares the dep and the ESM linker evaluates `modB` first.
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
