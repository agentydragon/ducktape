//! `define_logical_module` member with `binding.kind: ImportSpecifier`.
//!
//! When a plan claims a member whose source is an import specifier (an
//! imported name from another chunk), the materializer must rewrite it
//! to a re-import in the destination module instead of expecting a
//! top-level decl. Without that handling, the destination has
//! `export { Readable }` but no backing decl and Node fails to load
//! it with `SyntaxError: Export 'Readable' is not defined in module`.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;

#[test]
fn import_specifier_member_emits_reimport_in_destination() {
    // The chunk imports `x` as local `a` from `./vendor.js`. The spec
    // claims that import as a member of mod_x with rename `Readable`.
    // Materialized mod_x.js must end up with a re-import like
    // `import { x as Readable } from "../vendor.js"` plus
    // `export { Readable };` so Node can resolve the export.
    let mut opts = FixtureOpts::new(
        r#"import { x as a } from "./vendor.js";
console.log(a);
export { a };
"#,
        vec![json!({
            "id": "logical__mod_x",
            "operation": "define_logical_module",
            "selector": { "chunkId": "static/app" },
            "target": { "path": "mod_x" },
            "members": [{
                "id": "m_a",
                "name": "Readable",
                "selector": {
                    "binding": { "name": "a", "kind": "ImportSpecifier" },
                    "import": { "source": "./vendor.js", "imported": "x" },
                },
            }],
        })],
    );
    opts.extra_files = &[(
        "static/vendor.js",
        "export const x = 42;\nexport default x;\n",
    )];
    let fixture = run_logical_modules_e2e_fixture(opts);

    let mod_x = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_x.js"))
        .expect("read mod_x.js");

    assert!(
        mod_x.contains("import {") && mod_x.contains("Readable") && mod_x.contains("vendor"),
        "mod_x.js must re-import the vendor binding under the readable name; got:\n{mod_x}",
    );
    assert!(
        mod_x.contains("export {") && mod_x.contains("Readable"),
        "mod_x.js must export Readable; got:\n{mod_x}",
    );
}
