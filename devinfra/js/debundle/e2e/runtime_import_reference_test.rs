//! Moved code references a source-chunk ImportSpecifier-bound local.
//!
//! When `materialize_logical_modules` moves a top-level decl whose
//! body references a name that was an `import { foo as gge }` in the
//! source chunk, the destination module must carry along a re-import
//! for `gge`. Without it the moved code references a free variable
//! and Node throws `ReferenceError: gge is not defined` at runtime.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;

#[test]
fn moved_body_re_imports_runtime_specifier_local() {
    let mut opts = FixtureOpts::new(
        r#"import { mu as gge } from "./vendor.js";
function bridge() {
  return gge.decode;
}
console.log(bridge()());
export { bridge };
"#,
        vec![json!({
            "id": "logical__mod_x",
            "operation": "define_logical_module",
            "selector": { "chunkId": "static/app" },
            "target": { "path": "mod_x" },
            "members": [{
                "id": "m_bridge",
                "name": "bridge",
                "selector": { "binding": { "name": "bridge" } },
            }],
        })],
    );
    opts.extra_files = &[(
        "static/vendor.js",
        "export const mu = { decode: () => \"ok\" };\n",
    )];
    let fixture = run_logical_modules_e2e_fixture(opts);

    let mod_x = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_x.js"))
        .expect("read mod_x.js");
    assert!(
        mod_x.contains("gge") && mod_x.contains("import"),
        "mod_x.js must re-import the source-chunk specifier; got:\n{mod_x}",
    );
    // The destination still references `gge` — confirms the moved body
    // wasn't rewritten away.
    assert!(
        mod_x.contains("gge.decode"),
        "mod_x.js body must still reference `gge.decode`; got:\n{mod_x}",
    );
}
