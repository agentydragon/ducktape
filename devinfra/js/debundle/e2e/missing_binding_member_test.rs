//! Spec member whose source binding does not exist as a top-level decl.
//!
//! When a `define_logical_module` member claims a source binding that
//! is not a top-level declaration in the chunk, `materialize_logical_modules`
//! has nothing to move. Without filtering, the destination still
//! emits `export { <renamed> };` and Node bails at module load with
//! `SyntaxError: Export '<renamed>' is not defined in module`.
//!
//! Pin the contract: the destination module must only export bindings
//! that were actually moved, and must remain loadable.

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn missing_binding_member_does_not_leak_undefined_export() {
    let opts = FixtureOpts::new(
        r#"function a() { return 1; }
console.log(a());
export { a };
"#,
        vec![(
            "mod_x".to_string(),
            json!({
                "id": "logical__mod_x",
                "members": [
                    { "name": "Foo", "selector": { "binding": { "name": "a" } } },
                    { "name": "Bar", "selector": { "binding": { "name": "b" } } },
                ],
            }),
        )],
    );
    let fixture = run_logical_modules_e2e_fixture(opts);
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_x.js",
        &["Foo"],
        &["Bar"],
    );
}
