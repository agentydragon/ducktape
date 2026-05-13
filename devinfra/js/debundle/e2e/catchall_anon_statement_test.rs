//! `unassigned_mode: catchall_file` must route anonymous top-level
//! side-effect statements (bare expression statements, call statements,
//! IIFEs — top-level items with empty `declared`) into the catchall
//! plan, mirroring the residual sweep already done for unclaimed named
//! bindings.
//!
//! Without this, an atomic factor unit that bridges a named binding
//! and an anonymous sibling splits: named member ends up in the
//! catchall plan, anonymous member defaults to `ModuleId::ResidualEntry`
//! (inline-in-entry). `factor_assembly::detect_unit_conflict` then
//! surfaces an `AtomicUnitConflict` and the materializer bails. Pin
//! the round-trip so the build succeeds and the anonymous statement
//! lands in the catchall file.

use debundle_e2e_support::*;

#[test]
fn anon_statement_atomically_bound_to_named_binding_lands_in_catchall_file() {
    // Source-order layout designed to form a 2-owner atomic factor
    // unit between an anonymous side-effect statement and a named
    // side-effecting var-decl:
    //
    //   1. console.log(X);          - anon side-effect; EagerUse → owner(X)
    //   2. var X = (() => "x")();   - named side-effect (IIFE init)
    //
    // Edges in the constraining-edge subgraph G_atomic:
    //   * EagerUse:  owner(console.log(X)) → owner(X)   [anon reads X]
    //   * Sequenced: owner(X) → owner(console.log(X))   [adjacent side-effect statements]
    //
    // Both directions ⇒ Tarjan's SCC collapses them into one
    // atomic unit. With no `logical_modules` entry claiming the
    // anonymous statement, the named binding's residual sweep
    // routes `X` to the catchall plan but the anonymous statement
    // (pre-fix) stays at ResidualEntry — producing an
    // `AtomicUnitConflict`. The fix sweeps unclaimed anonymous
    // statements to the same catchall plan.
    let fixture = run_fixture(FixtureOpts {
        source: r#"console.log(X);
var X = (() => "x")();
export { X };
"#,
        logical_modules: vec![],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_catchall_file(None),
        extra_files: &[],
    });

    // Both the anonymous `console.log(X)` and the `var X` decl
    // must land in the catchall file (`residual/unhandled.js`),
    // not in the entry chunk.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["console.log(X)", "var X"],
        &[],
    );

    // The entry must NOT carry the anonymous side-effect — that
    // would mean the materializer left it at ResidualEntry while
    // the named binding moved to the catchall (the bug shape).
    let entry_src = std::fs::read_to_string(&fixture.entry_path).expect("read entry.js");
    assert!(
        !entry_src.contains("console.log(X)"),
        "entry unexpectedly contained the anonymous side-effect statement\n--- entry.js ---\n{entry_src}",
    );

    // End-to-end behavior: `console.log(X)` runs after `X` is
    // bound (the IIFE init runs eagerly when the catchall module
    // is loaded), so the program prints "x".
    assert_entry_output(&fixture, "x\n");
}

#[test]
fn explicit_logical_module_pinned_at_catchall_target_also_absorbs_anon_statements() {
    // Variant of the above where an explicit `logical_modules` entry
    // is pinned at the catchall target. In this branch
    // (`logical_modules.rs:~624`), the materializer reuses the
    // pre-existing plan for residual overflow instead of synthesizing
    // a new one. The anon-statement sweep must append into the same
    // plan's `anonymous_statement_ordinals` for the named/anon SCC
    // to co-locate.
    let fixture = run_fixture(FixtureOpts {
        source: r#"console.log(X);
var X = (() => "x")();
const Pinned = "pinned";
export { X, Pinned };
"#,
        // Pin a logical_modules entry at the catchall target.
        // Members of this module are explicitly claimed; the
        // module itself becomes the chunk's catchall destination
        // (no synthesized memberless residual plan is created).
        logical_modules: vec![logical_module(
            "residual/unhandled",
            &[Member::new("Pinned")],
        )],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_catchall_file(Some("residual/unhandled")),
        extra_files: &[],
    });

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["console.log(X)", "var X", "Pinned"],
        &[],
    );

    let entry_src = std::fs::read_to_string(&fixture.entry_path).expect("read entry.js");
    assert!(
        !entry_src.contains("console.log(X)"),
        "entry unexpectedly contained the anonymous side-effect statement\n--- entry.js ---\n{entry_src}",
    );

    assert_entry_output(&fixture, "x\n");
}

#[test]
fn inline_in_entry_mode_keeps_unclaimed_anon_statements_in_entry() {
    // Contrast: `InlineInEntry` mode (the default for chunks that
    // don't request a catchall file) must NOT run the anon-statement
    // sweep — that mode intentionally keeps every unclaimed
    // top-level statement (named or anon) at ResidualEntry, inline
    // in the chunk's entry file. This pins that the sweep is gated
    // on `catchall_file` mode.
    let fixture = run_fixture(FixtureOpts {
        source: r#"console.log("inline-me");
const X = 1;
export { X };
"#,
        logical_modules: vec![],
        chunk_renames: None,
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_inline(),
        extra_files: &[],
    });

    let entry_src = std::fs::read_to_string(&fixture.entry_path).expect("read entry.js");
    assert!(
        entry_src.contains(r#"console.log("inline-me")"#),
        "InlineInEntry must keep the anon statement in entry.js; got:\n{entry_src}",
    );
    // No residual file should have been created.
    let residual = fixture
        .out_root
        .join("static/app/modules/residual/unhandled.js");
    assert!(
        !residual.exists(),
        "InlineInEntry must not synthesize a residual/unhandled.js; found {}",
        residual.display(),
    );

    assert_entry_output(&fixture, "inline-me\n");
}
