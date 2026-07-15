//! `TransformSpec.chunk_renames` applies in-place renames to bindings
//! staying in entry's body without creating an explicit `Logical(R)`
//! module that would form a 2-module SCC with `ResidualEntry`.
//!
//! ## Background — the bug this op was added to fix
//!
//! Previously, the gaffer-side non-emitting patch workflow stuffed its
//! rename ops into a residual module's `members` list so the
//! residual-member-rename path applied them. That works mechanically
//! but the residual-member-rename path needs a `Logical(R)` module,
//! which the validator treats as a distinct node from
//! `ModuleId::ResidualEntry`. When the chunk interleaves orphan
//! top-level statements (`console.log(x)` etc., owner =
//! `ResidualEntry`) with side-effecting initializers on residual-
//! owned decls (owner = `Logical(R)`), the S-edge ordering loop
//! produces a 2-module SCC. Both nodes emit into files that evaluate
//! in the chunk's same init phase — the cycle is a validator
//! artifact, not a real evaluation hazard, but the gate still
//! rejects.
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
//! With no `Logical(R)` to interleave against, the orphan stmts and
//! unowned decls share `ResidualEntry` ownership; no cycle.

use debundle_e2e_support::*;

/// Build a `chunk_renames` spec entry that renames a single residual
/// binding to a new export name. All tests in this file rename a
/// single binding; the helper hides the wire shape.
#[test]
fn chunk_renames_renames_residual_bindings_in_entry() {
    // S1 (orphan-S):       console.log("before")
    // S2 (decl with S):    let x = (..., "x-value")  -- stays in entry
    // S3 (orphan-R+S):     console.log(x)            -- reads x at-init
    //
    // Default `unassigned_mode`: `x` stays in `ResidualEntry`-land.
    // `chunk_renames` renames `x` -> `payload`. The lowerer rewrites
    // the in-entry references; the export statement carries the
    // new name.
    let opts = FixtureOpts::new(
        r#"console.log("before");
let x = (globalThis.__touched = true, "x-value");
console.log(x);
export { x };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("payload", "x"))
    .with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture(opts);
    // The chunk evaluates: orphan logs "before", x's init runs (sets
    // globalThis.__touched and binds "x-value"), orphan logs the
    // value. With the in-place rename, the entry source emits
    // `let payload = ...` and `console.log(payload)`; runtime
    // semantics are unchanged.
    assert_entry_output(&fixture, "before\nx-value\n");
    // Pin the in-entry rename: entry.js should reference the new
    // name and not the original binding.
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &[
            "let payload =",
            "console.log(payload)",
            "export { payload as x }",
        ],
        &["let x =", "console.log(x)"],
    );
}

#[test]
fn chunk_renames_preserve_named_import_exported_names() {
    let opts = FixtureOpts::new(
        r#"import { x as importedThing, y } from "../vendor/entry.js";
let x = "local";
console.log(x, importedThing, y);
export { x };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("payload", "x"))
    .with_unassigned_mode(unassigned_mode_inline())
    .with_extra_files(&[(
        "static/vendor/entry.js",
        r#"export const x = "dep-x";
export const y = "dep-y";
"#,
    )]);
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "local dep-x dep-y\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &[
            r#"import { x as importedThing, y } from "../vendor/entry.js";"#,
            "let payload =",
            "console.log(payload, importedThing, y)",
            "export { payload as x }",
        ],
        &[
            r#"import { payload as importedThing"#,
            "let x =",
            "console.log(x, importedThing, y)",
        ],
    );
}

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
    //
    // Pre-redesign behavior was to reject this spec ("not exported by
    // entry"). docs/design.md "Valid peels and atomic modules" now says
    // residual entry bindings are importable because the emitter
    // auto-exports them on demand; "private to entry" is not a
    // first-class spec contract.
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

    expect_rejection_containing_all(
        opts,
        &[
            "invalid chunk_renames spec",
            "not a valid JS identifier",
            "collides with an existing top-level local",
            "duplicates an earlier rename target",
        ],
    );
}
