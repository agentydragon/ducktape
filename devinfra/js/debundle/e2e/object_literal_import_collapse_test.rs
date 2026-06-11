//! Regression: lowerer emits a canonical relative import path when a
//! peeled module references imports from a sibling source-chunk module.
//!
//! Before the fix in `source_chunk_imports_for_moved_body`
//! (<devinfra/js/debundle/logical_modules.rs:3821>), the fallback branch
//! that runs when the source-chunk resolver can't locate the import
//! built the rewritten specifier by raw string concatenation —
//! `format!("{}{}", "../".repeat(depth), info.src)` — and `info.src`
//! itself often starts with `./`. The result was a non-canonical
//! `".././provider_module.js"` spelling. Node tolerates it for
//! resolution, but it's a smell, and the synthetic fixture below pins
//! it. The fix normalizes the constructed path through
//! `normalize_relative_module_specifier` so the emitted import path
//! is canonical.
//!
//! ## Companion bug (not pinned here)
//!
//! While peeling the upstream `someObjectLiteralExport` (an object-literal
//! `const` mapping readable property names to imports from a sibling
//! `sync_timing` module), a related but separate failure was observed
//! where the lowerer's import-planning pass walks object-literal
//! property values AFTER `naturalize_object_literal_shorthand`
//! (<devinfra/js/debundle/logical_modules.rs:3259>) has already
//! collapsed `{ key: value }` to `{ key }`. The collapsed form hides
//! the cross-module identifier reference, and the missing import is
//! dropped entirely — Node throws `ReferenceError` at module-load
//! time. The synthetic two-line source below does NOT repro that
//! shape; it only exercises the path-normalization layer. See PR
//! #1625's analyzer-side companion bug for the proposer/gate side of
//! the same session of the upstream bundle.

use debundle_e2e_support::*;
use std::fs;

/// Generic-naming chunk source: an entry that imports three minified
/// bindings (`sA`, `sB`, `sC`) from a sibling provider module and
/// builds a `targetConst` object literal whose property values are
/// exactly those imports, but under readable property keys
/// (`propKeyA`, `propKeyB`, `propKeyC`). This is the exact post-minifier
/// shape the bug surfaces on:
///
/// ```js
/// import { sA, sB, sC } from "./provider_module.js";
/// const targetConst = { propKeyA: sA, propKeyB: sB, propKeyC: sC };
/// ```
///
/// The naturalizer wants to rename the value-side scrambled idents
/// (`sA → propKeyA`, etc.) to match the surrounding readable keys, then
/// the shorthand collapse fires. The bug is that the rewriter does this
/// inside the peeled module body without also rewriting (or preserving)
/// the cross-module import that bound those original identifiers.
const CHUNK_SOURCE: &str = r#"import { sA, sB, sC } from "./provider_module.js";
const targetConst = {
  propKeyA: sA,
  propKeyB: sB,
  propKeyC: sC,
};
console.log(targetConst.propKeyA + ":" + targetConst.propKeyB + ":" + targetConst.propKeyC);
export { targetConst };
"#;

const PROVIDER_SOURCE: &str = r#"export const sA = "a";
export const sB = "b";
export const sC = "c";
"#;

/// The peeled `target_module.js` must emit a single
/// `import { sA, sB, sC } from "../provider_module.js"` directive whose
/// path resolves correctly from the moved module's location — otherwise
/// the object-literal initializer references three free variables and
/// Node refuses to load the module. The path must be the canonical
/// `"../provider_module.js"` spelling, not the non-canonical
/// `".././provider_module.js"` that the pre-fix lowerer emitted by
/// concatenating `"../"` with `info.src = "./provider_module.js"`.
#[test]
fn peeled_object_literal_emits_well_formed_import_for_value_identifiers() {
    let mut opts = FixtureOpts::new(
        CHUNK_SOURCE,
        vec![logical_module(
            "target_module",
            &[Member::new("targetConst")],
        )],
    );
    opts.extra_files = &[("static/app/provider_module.js", PROVIDER_SOURCE)];
    let fixture = run_fixture(opts);

    let target_path = fixture.out_root.join("static/app/modules/target_module.js");
    let target_src =
        fs::read_to_string(&target_path).unwrap_or_else(|e| panic!("read target_module.js: {e}"));

    // The peeled module body still contains the object literal
    // referencing the three imported bindings; the lowerer must have
    // emitted an import directive that brings them into scope.
    assert!(
        target_src.contains("sA") && target_src.contains("sB") && target_src.contains("sC"),
        "target_module.js must still reference all three provider \
         imports in its object literal body; got:\n{target_src}",
    );
    assert!(
        target_src.contains("import") && target_src.contains("provider_module"),
        "target_module.js must import from provider_module when its \
         body references provider exports via object-literal value \
         positions; got:\n{target_src}",
    );

    // The rebased import path must be the canonical
    // `../provider_module.js`, not the non-canonical
    // `.././provider_module.js` the pre-fix lowerer emitted.
    assert!(
        target_src.contains(r#"from "../provider_module.js""#),
        "target_module.js must emit a normalized relative import \
         path; got:\n{target_src}",
    );

    // The emitted module must actually load under Node and produce the
    // chunk's original stdout. `assert_entry_output` surfaces a
    // `ReferenceError` (missing import) or any other module-load
    // failure as a non-zero node exit. If the lowerer's import-emission
    // ever regresses to dropping the cross-module values (per the the upstream
    // `someObjectLiteralExport` companion bug noted above), this
    // assertion catches it.
    assert_entry_output(&fixture, "a:b:c\n");
}
