//! `unassigned_mode: mini_factors` synthesizes one logical module
//! per unclaimed atomic factor unit instead of funneling everything
//! into the residual catch-all. Pin the contract:
//!
//! * Default mode (`catchall`): unclaimed bindings live together in
//!   the residual module.
//! * `mini_factors`: each unclaimed atomic unit becomes its own
//!   synthetic `__auto/mini/{idx:04}` module; the residual catch-all
//!   collapses to whatever truly couldn't be peeled (here: nothing).

use debundle_e2e_support::*;
use std::path::Path;

const FIXTURE_SOURCE: &str = r#"export const a = 1;
export const b = 2;
"#;

#[test]
fn catchall_keeps_unclaimed_bindings_in_residual() {
    let opts = FixtureOpts {
        source: FIXTURE_SOURCE,
        logical_modules: vec![],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: Some(unassigned_mode_catchall_file(None)),
        extra_files: &[],
    };
    let fixture = run_fixture(opts);
    // Default catchall: both `a` and `b` go into the residual
    // module; no synthetic `__auto/mini/...` files exist.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["a", "b"],
        &[],
    );
    assert!(
        !mini_factor_dir_exists(&fixture.out_root),
        "expected no __auto/mini/ tree under catchall mode",
    );
}

#[test]
fn mini_factors_synthesizes_one_module_per_unclaimed_unit() {
    let opts = FixtureOpts {
        source: FIXTURE_SOURCE,
        logical_modules: vec![],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: Some(unassigned_mode_mini_factors()),
        extra_files: &[],
    };
    let fixture = run_fixture(opts);
    // Two top-level const bindings with no constraining edges
    // between them form two separate atomic units. Under
    // `mini_factors` each unit becomes its own synthetic module.
    let mini_dir = fixture.out_root.join("static/app/modules/__auto/mini");
    assert!(
        mini_dir.exists(),
        "expected synthetic mini-factor directory at {}",
        mini_dir.display(),
    );
    // Collect synthesized files and union their exports.
    let mut entries: Vec<String> = std::fs::read_dir(&mini_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".js"))
        .collect();
    entries.sort();
    assert!(
        entries.len() >= 2,
        "expected at least two synthesized mini-factor modules; got {entries:?}",
    );
    let mut all_exports = std::collections::BTreeSet::<String>::new();
    for entry in &entries {
        let exports = list_module_exports(
            &fixture.out_root,
            &format!("static/app/modules/__auto/mini/{entry}"),
        );
        all_exports.extend(exports);
    }
    assert!(
        all_exports.contains("a") && all_exports.contains("b"),
        "expected synthesized modules to collectively export `a` and `b`; got {all_exports:?}",
    );
    // The residual catch-all collapses: nothing left to put there,
    // so the empty residual is dropped entirely.
    let residual = fixture
        .out_root
        .join("static/app/modules/residual/unhandled.js");
    if residual.exists() {
        let residual_exports = list_module_exports(
            &fixture.out_root,
            "static/app/modules/residual/unhandled.js",
        );
        assert!(
            !residual_exports.iter().any(|e| e == "a" || e == "b"),
            "expected residual to no longer carry `a`/`b` under mini_factors; got {residual_exports:?}",
        );
    }
}

fn mini_factor_dir_exists(out_root: &Path) -> bool {
    out_root.join("static/app/modules/__auto/mini").exists()
}
