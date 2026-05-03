//! Closure-pass sibling-declarator overwrite bug.
//!
//! When a logical-module plan's dependency closure pulls in a binding
//! whose declaration is part of a multi-binding `const a = 1, b = 2;`
//! comma-list, the closure pass currently OVERWRITES the binding
//! assignment for sibling declarators in the same comma-list — even
//! when the siblings were explicitly claimed by another plan's spec.
//! Result: the explicit-claim plan ends up with `export { ... }` but
//! no declarations to back the export.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;

#[test]
fn closure_does_not_steal_sibling_declarators_from_explicit_plan() {
    // Comma-list `const a = 1, b = 2;` — `a` is explicitly claimed by
    // mod_x; `b` is referenced by `c`, which mod_y owns. The closure
    // on mod_y should pull `b` into mod_y without also claiming `a`.
    let opts = FixtureOpts::new(
        r#"const a = 1, b = 2;
function c() { return b; }
console.log(a, b);
export { a, b, c };
"#,
        vec![
            json!({
                "id": "logical__mod_x",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_x" },
                "members": [{
                    "id": "m_a",
                    "name": "readableA",
                    "selector": { "binding": { "name": "a" } },
                }],
            }),
            json!({
                "id": "logical__mod_y",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_y" },
                "members": [{
                    "id": "m_c",
                    "name": "readableC",
                    "selector": { "binding": { "name": "c" } },
                }],
            }),
        ],
    );
    let fixture = run_logical_modules_e2e_fixture(opts);

    // mod_x must own `a`'s decl + its export.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_x.js",
        &["readableA"],
        &[],
    );
    let mod_x = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_x.js"))
        .expect("read mod_x.js");
    assert!(
        mod_x.contains("readableA = 1"),
        "mod_x.js must declare readableA = 1; got:\n{mod_x}",
    );

    // mod_y owns `c` + the closure-pulled `b`. It must NOT also have `a`.
    let mod_y = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_y.js"))
        .expect("read mod_y.js");
    assert!(
        !mod_y.contains("const a = 1") && !mod_y.contains("a = 1;"),
        "mod_y.js must not steal `a` from mod_x; got:\n{mod_y}",
    );
}
