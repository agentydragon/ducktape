//! Pin `chunk_renames` rename propagation into LOGICAL-MODULE-peeled
//! emissions.
//!
//! `chunk_renames` carries a `binding_name -> export_name` map that
//! the lowerer applies in-place to bindings staying in entry's body.
//! The map must ALSO follow into peeled logical-module bodies: when
//! a peeled module references a binding the spec renamed (e.g. an
//! imported alias `cx` renamed to `getMobxGlobalState`), both the
//! peeled module's import statement and its callsites must use the
//! new name. Otherwise the residual entry says `getMobxGlobalState`
//! and the peeled `b_module.js` still says `cx`, producing two
//! disagreeing local aliases for the same upstream binding.

use debundle_e2e_support::*;
use serde_json::json;

#[test]
fn chunk_rename_propagates_into_peeled_module_body() {
    // Fixture mirrors `purity_test::chunk_rename_with_purity_pure_propagates_to_call_classifier`'s shape:
    //   - vendor.js exports a function `f`.
    //   - entry imports `f as cx`.
    //   - `const a = (() => 1)();` -- pure-by-IIFE, stays in residual
    //   - `const b = cx();`         -- peel target into b_module
    //   - `const c = a + b;`        -- reads b at-init, stays in residual
    //   - chunk_renames carries `cx -> getMobxGlobalState` AND
    //     marks `cx` as `pure` (so the peel doesn't induce a cycle
    //     via cx() classified Unknown).
    //
    // The renaming MUST propagate to b_module.js: its import line
    // and its `const b = cx();` callsite both need the new alias.
    let opts = FixtureOpts {
        source: r#"import { f as cx } from "./vendor.js";
const a = (() => 1)();
const b = cx();
const c = a + b;
console.log(c);
export { a, b, c };
"#,
        logical_modules: vec![logical_module("b_module", &[Member::new("b")])],
        residual: None,
        chunk_renames: Some(json!({
            "id": "chunk_renames__static_app",
            "members": [
                {
                    "name": "getMobxGlobalState",
                    "selector": {
                        "binding": {
                            "name": "cx",
                            "kind": "import_specifier",
                        },
                    },
                    "purity": "pure",
                },
            ],
        })),
        chunk_id: "static/app",
        include_residual: true,
        unassigned_mode: None,
        extra_files: &[(
            "static/app/vendor.js",
            "export function f() { return 1; }\n",
        )],
    };
    let fixture = run_fixture(opts);

    // Behaviour preserved: c == a + b == 1 + 1 == 2.
    assert_entry_output(&fixture, "2\n");

    // The peeled module imports the renamed alias and uses it at
    // its callsite. Without rename propagation, the body keeps the
    // original `cx` and the import keeps `f as cx`.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["getMobxGlobalState", "const b = getMobxGlobalState()"],
        &[" as cx", "const b = cx("],
    );

    // The residual entry is unaffected by this test's contract,
    // but pin that it also picked up the rename for parity with
    // `chunk_renames_test`.
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["getMobxGlobalState"],
        &[" as cx", "cx()"],
    );
}
