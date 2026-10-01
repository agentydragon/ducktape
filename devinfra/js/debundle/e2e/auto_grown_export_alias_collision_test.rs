//! `auto_grown_residual_exports` must not grow an export whose public name
//! collides with one the chunk's source `export { … }` already exposes:
//! when the source exports a local binding under an alias `P` and a
//! different local binding is itself named `P`, the grown export takes a
//! non-colliding suffixed name (`pre_existing_public_export_names` holds the
//! taken public names) instead of duplicating `P`.
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
//!   so it grows an export of `av`, which must not collide with the
//!   source's `X as av`.

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
    // Reaching the entry means `validate_emitted_exports` saw no duplicate
    // public name `av`.
    assert_entry_output(&fixture, "x-impl av-impl av-impl\n");
}
