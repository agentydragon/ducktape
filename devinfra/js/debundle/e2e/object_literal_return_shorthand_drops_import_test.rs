//! Regression test for the lowerer's object-literal shorthand-collapse +
//! import-planning bug originally pinned RED in PR #1631 alongside the
//! PR #1627 / #1630 path-normalization thread.
//!
//! ## Context
//!
//! PR #1630 fixed the path-normalization layer in
//! `source_chunk_imports_for_moved_body` so peeled modules emit
//! canonical `"../foo.js"` instead of `".././foo.js"`. The companion
//! bug — `object_literal_import_collapse_test`'s synthetic fixture
//! deliberately doesn't repro it — is the import-dropping shape
//! covered here.
//!
//! ## Failure mode (pre-fix)
//!
//! When the heuristic naturalizer (`collect_return_object_alias_renames`)
//! scans a peeled function body and finds `return { key: value }`, it
//! adds `value → key` to the rename map. The
//! `RenameAndShorthandNaturalizer` then:
//!
//! 1. Renames every `value` identifier to `key` (including the ones
//!    inside the returned object's value positions — so `{ key:
//!    value }` becomes `{ key: key }`).
//! 2. Collapses `{ key: key }` to the shorthand `{ key }`.
//!
//! After this pass, `collect_module_body_facts` walked the body and
//! saw `key` referenced — but the source chunk's `runtime_imports`
//! map is keyed by the original local name `value`. The planner
//! looked up `key` in `runtime_imports`, missed, and emitted no
//! import for it. The emitted module had
//! `function makeConfig() { return { key }; }` with `key` as a free
//! variable. Node threw `ReferenceError: key is not defined` at
//! module-load time.
//!
//! ## Fix
//!
//! `plan_module_reference_needs` now takes the heuristic-rename map
//! produced by `naturalize_module_body` and, on a miss for a
//! post-rename name, reverse-resolves to the pre-rename original and
//! looks *that* up in `runtime_imports`. The carried `RuntimeImportInfo`
//! still has `imported = "value"`, so emit produces
//! `import { value as key } from "../provider.js"`. See the
//! "Rename pipeline" entry in <devinfra/js/debundle/TODO.md> for the
//! architectural follow-up that would let this defensive bridge retire.
//!
//! ## Repro shape
//!
//! Generic naming — `targetFn` is the peel target, `sA`/`sB` are the
//! minified import locals, `propKeyA`/`propKeyB` are the readable
//! property keys that drive the heuristic rename. The exact shape the
//! the upstream `someObjectLiteralExport` peel hit.

use debundle_e2e_support::*;
use std::fs;

/// Chunk source: a top-level function whose return value is an object
/// literal mapping readable property keys to imports from a sibling
/// provider module. The naturalizer's return-object-alias heuristic
/// picks up `propKeyA: sA` and `propKeyB: sB` as candidate renames.
const CHUNK_SOURCE: &str = r#"import { sA, sB } from "./provider_module.js";
function makeConfig() {
  return { propKeyA: sA, propKeyB: sB };
}
console.log(makeConfig().propKeyA + ":" + makeConfig().propKeyB);
export { makeConfig };
"#;

const PROVIDER_SOURCE: &str = r#"export const sA = "a";
export const sB = "b";
"#;

#[test]
fn peeled_function_returning_object_literal_keeps_value_position_imports() {
    let mut opts = FixtureOpts::new(
        CHUNK_SOURCE,
        vec![logical_module(
            "target_module",
            &[Member::new("makeConfig")],
        )],
    );
    opts.extra_files = &[("static/app/provider_module.js", PROVIDER_SOURCE)];
    let fixture = run_fixture(opts);

    let target_path = fixture.out_root.join("static/app/modules/target_module.js");
    let target_src =
        fs::read_to_string(&target_path).unwrap_or_else(|e| panic!("read target_module.js: {e}"));

    // The peel target's body references the two provider imports
    // somewhere — either as the original `sA`/`sB` locals or under
    // the heuristic-renamed `propKeyA`/`propKeyB` spellings. Either
    // way, the corresponding cross-module import must land in the
    // emitted module head.
    assert!(
        target_src.contains("import") && target_src.contains("provider_module"),
        "target_module.js must import from provider_module so the \
         function's return object has its value positions defined; \
         got:\n{target_src}",
    );

    // The canonical path-rebase fix from PR #1630 must hold — we want
    // `"../provider_module.js"`, not `".././provider_module.js"`.
    assert!(
        target_src.contains(r#"from "../provider_module.js""#),
        "target_module.js must emit a normalized relative import \
         path; got:\n{target_src}",
    );

    // Critical: the runtime must actually load. If the heuristic
    // rename collapses `{ propKeyA: sA }` → `{ propKeyA }` without
    // also re-keying `runtime_imports`, the planner emits no import
    // for `propKeyA` and Node throws `ReferenceError: propKeyA is
    // not defined` at module-load time, taking the whole entry chunk
    // down. `assert_entry_output` runs the emitted bundle through
    // Node and surfaces the ReferenceError as a failed assertion.
    assert_entry_output(&fixture, "a:b\n");
}
