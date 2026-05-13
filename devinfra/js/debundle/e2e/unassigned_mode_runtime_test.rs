//! Runtime matrix: `unassigned_mode` variants emit a bundle that
//! actually executes under `node` and prints the expected output.
//!
//! Every test in this file shares the same source shape:
//!
//! * Two top-level decls (`claimed_const`, `claimed_fn`) extracted
//!   into a named logical module `domains/claimed.js`.
//! * Two unclaimed top-level decls (`unclaimed_const`, `unclaimed_fn`)
//!   left to the `unassigned_mode` policy.
//! * A single anonymous side-effect statement (`console.log(...)`)
//!   that reads one binding from each side, so successful execution
//!   end-to-end proves both the extracted module and the unclaimed
//!   half are wired correctly.
//!
//! The three tests differ only in the spec's `unassigned_mode` for
//! the chunk: `inline_in_entry`, `catchall_file`, `mini_factors`.
//! Each asserts (a) the bundle runs and prints `"claimed unclaimed\n"`,
//! and (b) the emitted file tree matches the mode's contract.
//!
//! TODO (next PR's responsibility — `claude/drop-residual-entry-variant`):
//! extend this matrix with corner cases that don't work today, e.g.
//! anonymous-statement bridging atomic units under `catchall_file`,
//! mutable rebind reachable from an extracted module probing the
//! unrealizable-spec rejection, and multi-atom mini-factor synthesis
//! with edges between mini-factors.

use debundle_e2e_support::*;

const FIXTURE_SOURCE: &str = r#"const claimed_const = "claimed";
function claimed_fn() { return claimed_const; }
const unclaimed_const = "unclaimed";
function unclaimed_fn() { return unclaimed_const; }
console.log(claimed_fn(), unclaimed_fn());
export { claimed_const, claimed_fn };
"#;

const EXPECTED_STDOUT: &str = "claimed unclaimed\n";

fn claimed_module() -> LogicalModuleEntry {
    logical_module(
        "domains/claimed",
        &[Member::new("claimed_const"), Member::new("claimed_fn")],
    )
}

#[test]
fn unassigned_mode_inline_in_entry_executes_correctly() {
    let opts = FixtureOpts {
        source: FIXTURE_SOURCE,
        logical_modules: vec![claimed_module()],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_inline(),
        extra_files: &[],
    };
    let fixture = run_fixture(opts);

    // Extracted module is emitted; no residual catch-all file
    // exists under `inline_in_entry` (unclaimed decls stay inline
    // in the entry).
    assert!(
        fixture
            .out_root
            .join("static/app/modules/domains/claimed.js")
            .exists(),
        "expected extracted module at static/app/modules/domains/claimed.js",
    );
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/residual/unhandled.js")
            .exists(),
        "expected no residual/unhandled.js under inline_in_entry",
    );
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/__auto/mini")
            .exists(),
        "expected no __auto/mini/ tree under inline_in_entry",
    );

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/domains/claimed.js",
        &["claimed_const", "claimed_fn"],
        &[],
    );
    assert_entry_output(&fixture, EXPECTED_STDOUT);
}

#[test]
fn unassigned_mode_catchall_file_executes_correctly() {
    let opts = FixtureOpts {
        source: FIXTURE_SOURCE,
        logical_modules: vec![claimed_module()],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_catchall_file(Some("residual/unhandled")),
        extra_files: &[],
    };
    let fixture = run_fixture(opts);

    // Extracted module + residual catch-all both emitted.
    assert!(
        fixture
            .out_root
            .join("static/app/modules/domains/claimed.js")
            .exists(),
        "expected extracted module at static/app/modules/domains/claimed.js",
    );
    assert!(
        fixture
            .out_root
            .join("static/app/modules/residual/unhandled.js")
            .exists(),
        "expected residual catch-all at static/app/modules/residual/unhandled.js",
    );

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/domains/claimed.js",
        &["claimed_const", "claimed_fn"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["unclaimed_const", "unclaimed_fn"],
        &[],
    );
    assert_entry_output(&fixture, EXPECTED_STDOUT);
}

#[test]
fn unassigned_mode_mini_factors_executes_correctly() {
    let opts = FixtureOpts {
        source: FIXTURE_SOURCE,
        logical_modules: vec![claimed_module()],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_mini_factors(),
        extra_files: &[],
    };
    let fixture = run_fixture(opts);

    // Extracted module is emitted; synthetic mini-factor files
    // collectively carry the unclaimed bindings (each unclaimed
    // owner is its own singleton atomic unit, so the mini-factors
    // are 1:1 with the bindings).
    assert!(
        fixture
            .out_root
            .join("static/app/modules/domains/claimed.js")
            .exists(),
        "expected extracted module at static/app/modules/domains/claimed.js",
    );
    let mini_dir = fixture.out_root.join("static/app/modules/__auto/mini");
    assert!(
        mini_dir.exists(),
        "expected synthetic mini-factor directory at {}",
        mini_dir.display(),
    );

    let mut entries: Vec<String> = std::fs::read_dir(&mini_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".js"))
        .collect();
    entries.sort();
    assert!(
        !entries.is_empty(),
        "expected at least one synthesized mini-factor module; got {entries:?}",
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
        all_exports.contains("unclaimed_const") && all_exports.contains("unclaimed_fn"),
        "expected synthesized mini-factor modules to collectively export \
         `unclaimed_const` and `unclaimed_fn`; got {all_exports:?}",
    );

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/domains/claimed.js",
        &["claimed_const", "claimed_fn"],
        &[],
    );
    assert_entry_output(&fixture, EXPECTED_STDOUT);
}
