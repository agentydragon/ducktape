//! Regression: a scope-local heuristic rename whose SOURCE name
//! coincides with a top-level exported binding must not remap the
//! module's export specifier. The heuristic (here `t` -> `readable`,
//! derived from `f`'s destructured param) renames only inside `f`'s
//! subtree; the top-level declaration `let t` keeps its name. Mapping
//! the export local through the merged (heuristic-inclusive) rename
//! map emitted `export { readable as t }` with no `readable`
//! declaration — a SyntaxError at module load.

use debundle_e2e_support::*;

#[test]
fn heuristic_rename_source_matching_exported_binding_does_not_remap_export() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let t = "T";
function f({ readable: t }) {
  return t;
}
console.log(f({ readable: "R" }) + t);
export { t, f };
"#,
        vec![logical_module("x", &[Member::new("t"), Member::new("f")])],
    ));
    assert_entry_output(&fixture, "RT\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["t", "f"],
        &[],
    );
}
