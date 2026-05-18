//! RED test pinning the lowerer's object-literal shorthand-collapse +
//! import-planning bug noted in the PR #1627 / #1630 thread.
//!
//! ## Context
//!
//! PR #1630 fixed the path-normalization layer in
//! `source_chunk_imports_for_moved_body` so peeled modules emit
//! canonical `"../foo.js"` instead of `".././foo.js"`. The PR body
//! explicitly noted a companion bug it didn't address:
//!
//! > A companion bug affecting object-literal shorthand collapse and
//! > import-planning is noted but not addressed in this change.
//!
//! The synthetic fixture in `object_literal_import_collapse_test`
//! deliberately doesn't repro that shape — it pins only the path
//! normalization. This file pins the import-dropping bug.
//!
//! ## Failure mode
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
//! After this pass, `collect_module_body_facts` walks the body and
//! sees `key` referenced — but the source chunk's `runtime_imports`
//! map is keyed by the original local name `value`. The planner
//! looks up `key` in `runtime_imports`, misses, and emits no import
//! for it.
//!
//! The emitted module has `function makeConfig() { return { key }; }`
//! with `key` as a free variable. Node throws `ReferenceError: key is
//! not defined` at module-load time.
//!
//! ## Repro shape
//!
//! Generic naming — `targetFn` is the peel target, `sA`/`sB` are the
//! minified import locals, `propKeyA`/`propKeyB` are the readable
//! property keys that drive the heuristic rename. The exact shape the
//! Tana `getActionEventLimits` peel hit.
//!
//! ## Fix sketch (for whoever picks this up)
//!
//! The plan_module_reference_needs / import-planning step must learn
//! about the heuristic renames the naturalizer applied. Two options:
//!
//! - **Symmetric rename**: after collecting heuristic renames, also
//!   re-key `runtime_imports` so `runtime_imports[key] =
//!   runtime_imports[value]` (with `imported_name = value`). Then the
//!   planner finds the import under the post-rename name and emits
//!   `import { value as key } from "../provider.js"`.
//! - **Collect facts pre-naturalize**: capture
//!   `runtime_imports`-relevant references before renames apply, then
//!   plan imports off the pre-rename names. Trickier — the post-
//!   naturalize body still needs to reference `key`, so the import
//!   has to land as `import { value as key }`.
//!
//! Drop a regression assertion (canonical import path + a re-import
//! for every still-referenced original local) into
//! `object_literal_import_collapse_test` once fixed, and flip this
//! test from RED to GREEN.

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
