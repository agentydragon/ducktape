//! RED→GREEN test: a mint base / disambiguation target that is a
//! reserved JS word must not surface verbatim into an emitted import
//! clause.
//!
//! Background. `lowering/import_emit.rs::mint_unique_name` is the single
//! name minter behind `disambiguate_import_locals`,
//! `disambiguate_residual_entry_import_locals`, and
//! `auto_grown_residual_exports`. It used to return its `base` verbatim
//! whenever the claim closure accepted it, never consulting
//! `is_valid_js_identifier`. `disambiguate_import_locals` prefers a
//! binding's PUBLIC export name as the consumer-side local. So when a
//! logical module exports a binding under a reserved public name
//! (`export { impl as default }` — valid JS, `default` is a legal export
//! alias), the entry that still references the binding mints `default` as
//! the import local and emits `import { default } from "./mod"` — i.e.
//! `default as default`, which is NOT valid JS (a reserved word cannot be
//! an import local). Emitted modules are ESM (always strict mode), so the
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
//! ## Expected outcomes
//!
//! - **Today (RED)**: the debundler succeeds but emits `import { default }
//!   from "./modules/mod_x.js"` into `entry.js`. Node rejects the entry
//!   with a SyntaxError ("Unexpected reserved word"), so
//!   `assert_entry_output` fails at the node step.
//! - **After the fix**: `mint_unique_name` rejects the reserved base via
//!   `is_valid_js_identifier` and suffixes it to `default$1`, emitting
//!   `import { default$1 as default } from "..."` — a valid identifier
//!   that parses and runs.

use debundle_e2e_support::*;
use std::fs;
use swc_ecma_ast::{ImportSpecifier, ModuleDecl, ModuleItem};

/// Every import local in `source` (across all `import` statements) must
/// be a parseable, non-reserved identifier. `parse_module` already
/// rejects un-parseable input; this additionally asserts no import local
/// is a bare reserved word that slipped through.
fn assert_import_locals_are_valid(source: &str) {
    let module = parse_module(source);
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        for specifier in &import.specifiers {
            let local = match specifier {
                ImportSpecifier::Named(named) => named.local.sym.to_string(),
                ImportSpecifier::Default(default) => default.local.sym.to_string(),
                ImportSpecifier::Namespace(ns) => ns.local.sym.to_string(),
            };
            assert!(
                local != "default" && local != "class" && local != "await",
                "import local `{local}` is a reserved word; emitted import is un-parseable:\n{source}",
            );
        }
    }
}

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

    let entry = fs::read_to_string(&fixture.entry_path).expect("read entry.js");
    // `parse_module` inside this helper fails loudly if the emitted
    // import is un-parseable (the RED signal at the AST level); the
    // explicit assert pins the reserved-local case specifically.
    assert_import_locals_are_valid(&entry);

    // Behaviour preservation: the entry still resolves `impl` through
    // mod_x and prints its value.
    assert_entry_output(&fixture, "impl-value\n");
}
