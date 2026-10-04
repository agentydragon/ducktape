//! A mint base / disambiguation target that is a reserved JS word must not
//! surface verbatim into an emitted import clause.
//!
//! `lowering/rename_ledger.rs::RenameLedger::mint` is the single name minter
//! behind `disambiguate_import_locals`,
//! `disambiguate_residual_entry_import_locals`, and
//! `auto_grown_residual_exports`; it must consult `is_valid_js_identifier`
//! before returning a base. `disambiguate_import_locals` prefers a binding's
//! PUBLIC export name as the consumer-side local. A reserved public name is
//! legal JS (`export { impl as default }`), but the entry that still
//! references the binding would emit `import { default } from "./mod"` —
//! `default as default`, which is NOT valid JS (a reserved word cannot be an
//! import local). Emitted modules are ESM (always strict mode), so the
//! reserved word is a hard parse error with no debundler-side diagnostic.
//!
//! ## Fixture
//!
//! - `impl` is a top-level binding the entry references via `show()`.
//! - The spec moves `impl` to `mod_x`, re-exporting it under the public
//!   name `default`.
//! - The entry keeps calling `show()`, which reads `impl`, so the entry
//!   must re-import the binding from `mod_x`. The disambiguator mints the
//!   import local from the preferred (public) name `default`.
//!
//! ## Expected outcome
//!
//! `RenameLedger::mint` rejects the reserved base via
//! `is_valid_js_identifier` and suffixes it to `default$1`, emitting
//! `import { default$1 as default } from "..."` — a valid identifier
//! that parses and runs.

use debundle_e2e_support::*;

#[test]
fn reserved_public_name_does_not_mint_reserved_import_local() {
    let mut opts = FixtureOpts::new(
        r#"const impl = "impl-value";
function show() { return impl; }
console.log(show());
export { show };
"#,
        vec![logical_module(
            "mod_x",
            &[Member::renamed("default", "impl")],
        )],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    // Node rejects a reserved-word import local with a SyntaxError, and the
    // entry only prints `impl-value` if it re-imports `impl` from mod_x.
    assert_entry_output(&fixture, "impl-value\n");
}
