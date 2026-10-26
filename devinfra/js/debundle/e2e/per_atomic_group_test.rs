//! `unassigned_mode: per_atomic_group` synthesizes one logical module
//! per unclaimed atomic factor group instead of funneling everything
//! into the residual catch-all. Pin the contract:
//!
//! * Default mode (`catchall`): unclaimed bindings live together in
//!   the residual module.
//! * `per_atomic_group`: each unclaimed atomic group becomes its own
//!   synthetic `__auto/atomic_groups/{idx:04}` module; the residual catch-all
//!   collapses to whatever truly couldn't be peeled (here: nothing).

use debundle_e2e_support::*;
use std::path::Path;

const FIXTURE_SOURCE: &str = r#"export const a = 1;
export const b = 2;
"#;

#[test]
fn catchall_keeps_unclaimed_bindings_in_residual() {
    let fixture = run_fixture(FixtureOpts::new(FIXTURE_SOURCE, vec![]));
    // Default catchall: both `a` and `b` go into the residual
    // module; no synthetic `__auto/atomic_groups/...` files exist.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["a", "b"],
        &[],
    );
    assert!(
        !atomic_group_dir_exists(&fixture.out_root),
        "expected no __auto/atomic_groups/ tree under catchall mode",
    );
}

#[test]
fn per_atomic_group_synthesizes_one_module_per_unclaimed_atomic_group() {
    let fixture = run_fixture(
        FixtureOpts::new(FIXTURE_SOURCE, vec![])
            .with_unassigned_mode(unassigned_mode_per_atomic_group()),
    );
    // Two top-level const bindings with no constraining edges
    // between them form two separate atomic groups. Under
    // `per_atomic_group` each atomic group becomes its own synthetic module.
    let group_dir = fixture.out_root.join("static/app/modules/__auto/atomic_groups");
    assert!(
        group_dir.exists(),
        "expected synthetic atomic-group module directory at {}",
        group_dir.display(),
    );
    // Collect synthesized files and union their exports.
    let mut entries: Vec<String> = std::fs::read_dir(&group_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".js"))
        .collect();
    entries.sort();
    assert!(
        entries.len() >= 2,
        "expected at least two synthesized atomic-group modules; got {entries:?}",
    );
    let mut all_exports = std::collections::BTreeSet::<String>::new();
    for entry in &entries {
        let exports = list_module_exports(
            &fixture.out_root,
            &format!("static/app/modules/__auto/atomic_groups/{entry}"),
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
            "expected residual to no longer carry `a`/`b` under per_atomic_group; got {residual_exports:?}",
        );
    }
}

fn atomic_group_dir_exists(out_root: &Path) -> bool {
    out_root.join("static/app/modules/__auto/atomic_groups").exists()
}

#[test]
fn per_atomic_group_peels_entry_with_leading_import() {
    let mut opts = FixtureOpts::new("import { a } from './dep.js'; console.log(a);", vec![]);
    opts.unassigned_mode = unassigned_mode_per_atomic_group();
    opts.extra_chunks = &[("static/dep", "export const a = 1;")];
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "1\n");
}
