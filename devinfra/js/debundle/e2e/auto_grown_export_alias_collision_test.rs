//! Regression test (originally RED): when the chunk's source
//! `export { … }` aliases a local binding to a public name `P`,
//! and `auto_grown_residual_exports` would grow an export for a
//! different local binding whose symbol is *also* `P`, the
//! auto-grow pass used to produce a duplicate public export name
//! and the chunk link-failed.
//!
//! Background. `lowering/exports.rs::auto_grown_residual_exports`
//! gates each candidate against `pre_existing_entry_exports`, but
//! that set stores LOCAL bindings (the `orig` side of `export {
//! orig as exported }`). It used to never inspect the PUBLIC side
//! — the `exported` names already taken in the chunk's emitted
//! `export { … }` block. When a peeled module's body referenced a
//! residual binding whose symbol coincided with an alias used in
//! the chunk's source export, the auto-grow pass blindly emitted
//! a fresh `export { <local> as <name> }` whose `<name>` collided
//! with an existing public name.
//!
//! `validate_emitted_exports` (which runs late in the pipeline)
//! caught the resulting duplicate-export module — surfacing the
//! bug as a build-time error rather than a silent blank page in
//! Chromium — but the fix landed upstream: the auto-grow pass now
//! consults the set of public names already exported
//! (`pre_existing_public_export_names`) and mints a non-colliding
//! suffixed name instead.
//!
//! ## Generalized pattern
//!
//! Anonymized from a real failure observed in a large bundle
//! (`static/index-EXAMPLE`), where the chunk's source export
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
//! ## Pinned behavior
//!
//! `validate_emitted_exports` used to reject with a
//! duplicate-export error naming `av` in `entry.js` (the fixture
//! build failed at pipeline time). Now
//! `auto_grown_residual_exports` renames the new export to a
//! non-colliding public name, so the fixture build succeeds and
//! the entry runs.

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
    // Before the fix the build never reached this point —
    // validate_emitted_exports rejected on duplicate public name
    // `av`. With the collision-aware auto-grow pass, the entry
    // runs and emits this line.
    assert_entry_output(&fixture, "x-impl av-impl av-impl\n");
}
