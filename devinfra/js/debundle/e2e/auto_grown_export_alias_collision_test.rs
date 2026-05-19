//! RED test: when the chunk's source `export { … }` aliases a
//! local binding to a public name `P`, and `auto_grown_residual_exports`
//! would grow an export for a different local binding whose symbol
//! is *also* `P`, the auto-grow pass produces a duplicate public
//! export name and the chunk link-fails.
//!
//! Background. `lowering/exports.rs::auto_grown_residual_exports`
//! gates each candidate against `pre_existing_entry_exports`, but
//! that set stores LOCAL bindings (the `orig` side of `export {
//! orig as exported }`). It never inspects the PUBLIC side — the
//! `exported` names that are already taken in the chunk's emitted
//! `export { … }` block. When a peeled module's body happens to
//! reference a residual binding whose symbol happens to coincide
//! with an alias used in the chunk's source export, the auto-grow
//! pass blindly emits a fresh `export { <local> as <name> }`
//! whose `<name>` collides with an existing public name.
//!
//! `validate_emitted_exports` (which runs late in the pipeline)
//! catches the resulting duplicate-export module — surfacing the
//! bug as a build-time error rather than a silent blank page in
//! Chromium — but the fix belongs upstream: the auto-grow pass
//! should consult the set of public names already exported (the
//! `exported` side of named export specifiers and `ExportDecl`
//! bindings) and either rename the new export to a non-colliding
//! public name or skip it entirely.
//!
//! ## Generalized pattern
//!
//! Anonymized from a real failure observed in a large bundle
//! (`static/index-DI2GynTv`), where the chunk's source export
//! contained `BackgroundPattern as av` and the residual body
//! also had a top-level `const av = …` binding referenced by a
//! peeled module. Four public names (`a6`, `aI`, `av`, `bu`) all
//! collided this way in a single rebuild.
//!
//! ## Fixture
//!
//! - `X` is a local binding the chunk exports under public name `av`.
//! - `av` is a *different* top-level binding (a residual one,
//!   never explicitly exported by the source).
//! - `usesAv` is a function that reads `av`; the spec moves it to
//!   `mod_b`, so `mod_b`'s body needs `import { av } from
//!   '../entry'`.
//! - The materializer's auto-grow pass sees `av` is referenced,
//!   declared in the chunk, and not in
//!   `pre_existing_entry_exports` (which holds only `{X, usesAv}`),
//!   so it grows `export { av }` — colliding with the source's
//!   `X as av`.
//!
//! ## Expected outcomes
//!
//! - **Today (RED)**: `validate_emitted_exports` rejects with a
//!   duplicate-export error naming `av` in `entry.js`. The
//!   fixture build fails at pipeline time.
//! - **After the fix**: `auto_grown_residual_exports` either
//!   renames the new export to a non-colliding public name
//!   (e.g. `av$1`) or skips it (forcing the peeled module to
//!   reach `av` by some other path the upstream invariant
//!   allows). The fixture build succeeds and the entry runs.

use debundle_e2e_support::*;

#[test]
fn auto_grown_residual_exports_avoid_alias_collision() {
    let mut opts = FixtureOpts::new(
        r#"const X = "x-impl";
const av = "av-impl";
function usesAv() { return av; }
console.log(X, av, usesAv());
export { X as av, usesAv };
"#,
        vec![logical_module("mod_b", &[Member::new("usesAv")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    // Today the build never reaches this point — validate_emitted_exports
    // rejects on duplicate public name `av`. After the fix to
    // `auto_grown_residual_exports`, the entry runs and emits this
    // line.
    assert_entry_output(&fixture, "x-impl av-impl av-impl\n");
}
