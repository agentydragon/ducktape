//! Two logical modules can re-export the same imported binding
//! under different public names.
//!
//! `BindingKind::Imported { re_exported_by: BTreeMap<ModuleId,
//! BindingName> }` lets each logical module pick its own public
//! name for the same source-chunk binding. Concrete use case:
//! two parts of an app surface `vendor.j` (the JSX runtime) —
//! one as `jsxRuntime`, the other as `__jsx` — without
//! colliding.

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn imported_binding_re_exported_under_two_different_names() {
    // The chunk imports `j` from a vendor under local `a`, then
    // uses `a` once. Two logical modules each re-export `a`
    // under different public names — `jsxRuntime` and `__jsx`.
    // Both emit successfully with their own re-import paths;
    // the entries share one `BindingKind::Imported` whose
    // `re_exported_by` map carries each module's chosen public
    // name.
    let mut opts = FixtureOpts::new(
        r#"import { j as a } from "./vendor.js";
console.log(a());
export { a };
"#,
        vec![
            json!({
                "id": "logical__mod_jsx_runtime",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_jsx_runtime" },
                "members": [{
                    "id": "m_jsx_runtime",
                    "name": "jsxRuntime",
                    "selector": { "binding": { "name": "a", "kind": "ImportSpecifier" } },
                }],
            }),
            json!({
                "id": "logical__mod_dunder_jsx",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_dunder_jsx" },
                "members": [{
                    "id": "m_dunder_jsx",
                    "name": "__jsx",
                    "selector": { "binding": { "name": "a", "kind": "ImportSpecifier" } },
                }],
            }),
        ],
    );
    opts.extra_files = &[("static/vendor.js", "export const j = () => 42;\n")];
    let fixture = run_logical_modules_e2e_fixture(opts);

    // Each module exports the binding under its own chosen name.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_jsx_runtime.js",
        &["jsxRuntime"],
        &["__jsx"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_dunder_jsx.js",
        &["__jsx"],
        &["jsxRuntime"],
    );
}
