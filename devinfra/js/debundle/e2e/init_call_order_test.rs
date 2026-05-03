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
    // - `modA` owns `x1` (source item 0) and `x2` (source item 2;
    //   init reads `y.id`).
    // - `modB` owns `y` (source item 1).
    //
    // The pure-object initializers don't trigger `S` edges — the
    // only constraints are the `R`/`I` edge from `mod_a → mod_b`
    // (x2 reads y at-init) and the entry's reads of x1/x2/y. The
    // ESM linker evaluates `mod_b` before `mod_a` because of the
    // cross-module import.
    let opts = FixtureOpts::new(
        r#"const x1 = { id: "x1" };
const y = { id: "k" };
const x2 = { [y.id]: "v" };
console.log(x1.id, y.id, x2[y.id]);
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
    // entry should print "x1 k v". Today (without the init-order
    // fix) entry crashes at module load with `TypeError: Cannot
    // read properties of undefined (reading 'id')`.
    assert_entry_output(&fixture, "x1 k v\n");
}
