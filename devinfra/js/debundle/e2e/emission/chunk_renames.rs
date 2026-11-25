//! `TransformSpec.chunk_renames` applies in-place renames to bindings
//! staying in entry's body without creating an explicit logical module `R`
//! that would form a 2-module SCC with the entry destination.
//!
//! ## Why not a logical module `R`
//!
//! The residual-member-rename path needs a logical module `R`, which the
//! validator treats as a distinct node from the `EntryDestination`. When
//! the chunk interleaves orphan top-level statements (`console.log(x)` etc.,
//! owned by entry) with side-effecting initializers on residual-owned decls
//! (owned by `R`), the S-edge ordering loop produces a 2-module SCC. Both
//! nodes emit into files that evaluate in the chunk's same init phase — the
//! cycle is a validator artifact, not a real evaluation hazard, but the gate
//! still rejects.
//!
//! ## What `chunk_renames` does
//!
//! Carries `members[]` with rename info, like a `LogicalModule`, but
//! doesn't create a logical module. The materializer collects the
//! renames into a `binding_name -> export_name` map and the lowerer
//! applies them in-place during entry-body emission for any binding
//! *not* claimed by a logical module. Bindings claimed by a logical
//! module take their rename from the module plan; the
//! `chunk_renames` entry (if any) is dropped for those.
//!
//! With no `R` to interleave against, the orphan stmts and unowned decls
//! share entry ownership; no cycle.

use debundle_e2e_support::*;

#[test]
fn extracted_module_imports_chunk_renamed_residual_helper_for_execution() {
    let opts = FixtureOpts::new(
        r#"function helper() {
  return "ok";
}
function run() {
  return helper();
}
console.log("entry");
export { helper, run };
"#,
        vec![logical_module("mod_run", &[Member::new("run")])],
    )
    .with_chunk_renames(chunk_rename("readableHelper", "helper"))
    .with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "entry\n");
    // The rename propagates into the peeled module: the import
    // pulls `helper` from entry.js under the renamed local alias
    // `readableHelper`, and the body's `helper()` callsite is
    // rewritten to `readableHelper()` to match. Without this
    // propagation, the peeled module would carry the original
    // alias and produce two disagreeing local aliases for the same
    // upstream binding (entry's body uses `readableHelper`,
    // mod_run's body uses `helper`).
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_run.js",
        &[
            r#"import { helper as readableHelper } from "../entry.js";"#,
            "return readableHelper()",
        ],
        &["return helper()"],
    );
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const { run } = await import("./static/app/modules/mod_run.js");
console.log(run());
"#,
        "ok\n",
    );
}

#[test]
fn private_chunk_renamed_residual_helper_used_by_extracted_module_auto_grows_entry_export() {
    // Even when `helper` is NOT in the source-level `export {...}`
    // list, the materializer auto-grows entry's export surface so
    // `mod_run`'s body can `import { helper as readableHelper }` from
    // entry. The chunk_renames mapping is honored: entry's local name
    // is `readableHelper`, exported as `helper`.
    // docs/design.md "Valid peels and atomic modules": residual entry
    // bindings are importable because the emitter auto-exports them on
    // demand; "private to entry" is not a first-class spec contract.
    let opts = FixtureOpts::new(
        r#"function helper() {
  return "ok";
}
function run() {
  return helper();
}
export { run };
"#,
        vec![logical_module("mod_run", &[Member::new("run")])],
    )
    .with_chunk_renames(chunk_rename("readableHelper", "helper"))
    .with_unassigned_mode(unassigned_mode_inline());

    let fixture = run_fixture(opts);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_run.js",
        &[
            r#"import { helper as readableHelper } from "../entry.js";"#,
            "return readableHelper()",
        ],
        &["return helper()"],
    );
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const { run } = await import("./static/app/modules/mod_run.js");
console.log(run());
"#,
        "ok\n",
    );
}

/// Multiple chunk_renames violations should all surface in a single
/// rejection rather than the validator bailing on the first one. Spec
/// authors fix the spec in one round-trip instead of iterating per
/// error.
#[test]
fn surfaces_every_chunk_rename_violation_at_once() {
    let opts = FixtureOpts::new(
        r#"let alpha = "alpha-value";
let bravo = "bravo-value";
let charlie = "charlie-value";
let delta = "delta-value";
console.log(alpha, bravo, charlie, delta);
export { alpha, bravo, charlie, delta };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_renames(&[
        // Invalid JS identifier — should report a "not a valid JS identifier" error.
        ChunkRenameEntry::new("1-bad-ident", "alpha"),
        // Collides with the existing body local `delta`.
        ChunkRenameEntry::new("delta", "bravo"),
        // Duplicate target — both `charlie` and `delta` would rename to the same name.
        ChunkRenameEntry::new("shared_target", "charlie"),
        ChunkRenameEntry::new("shared_target", "delta"),
    ]))
    .with_unassigned_mode(unassigned_mode_inline());

    let rejected = run_rejection_fixture(opts);
    // Every failing entry is named, and the accepted one (`charlie`) is not.
    for name in ["1-bad-ident", "alpha", "bravo", "delta", "shared_target"] {
        assert!(
            rejected.stderr.contains(name),
            "{name}: {}",
            rejected.stderr
        );
    }
    assert!(!rejected.stderr.contains("charlie"), "{}", rejected.stderr);
}
