//! End-to-end coverage for the **emit auto-grow** redesign: the
//! materializer is now responsible for ensuring every cross-module
//! read into the residual entry resolves to an exported binding, by
//! growing entry's `export {...}` list on demand. The peelability
//! proposer never refuses a peel for "binding isn't exported by
//! entry" — that responsibility moved to emit.
//!
//! DESIGN.md "Valid peels and atomic modules" / "Emit-side
//! responsibilities" describe the contract this test asserts.
//!
//! Pre-redesign behavior (now removed): the proposer flagged any
//! peel whose moved body read an unexported residual binding as
//! `blocked_emit_resolvability` and refused to propose it.
//! Post-redesign behavior: the proposer proposes freely; emit
//! auto-grows entry's export list; the bundle runs.

use analysis::OwnerGraphReport;
use debundle_e2e_support::*;
use serde::de::DeserializeOwned;
use std::{fs, path::Path};

fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

/// Positive case: the residual hub `Se` has a body that lazily
/// reads another residual binding `Fx` not on entry's source export
/// list. Before the redesign this was the dominant
/// `blocked_emit_resolvability` shape on gaffer's `78d928dca7`
/// chunk; the proposer refused to peel `Se`. Post-redesign, peeling
/// `Se` is valid: emit grows entry's export list to include `Fx`,
/// `Se`'s new module imports `Fx` from entry, and the bundle runs.
#[test]
fn lazy_read_of_unexported_residual_binding_peels_via_auto_grown_entry_export() {
    // `Se` and `Fx` mirror the gaffer minified names from PR #1614's
    // smoke notes (`Se` was the residual hub whose body lazily read
    // `Fx`, the canonical case the proposer used to refuse). Keep
    // the names so a future bisector can grep the report.
    let mut opts = FixtureOpts::new(
        r#"function Se() { return Fx + "!"; }
const Fx = "fixture";
const Existing = "existing";
console.log(Existing);
console.log(Se());
export { Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    // The emitted bundle runs end-to-end. `Se()` returns "fixture!"
    // via its lazy read of `Fx` resolved through entry's auto-grown
    // export.
    assert_entry_output(&fixture, "existing\nfixture!\n");

    // The peelability projection now reports {Se} as PeelableNow on
    // the singleton horizon, and the candidate appears in
    // minimal_peel_sets. (The cell-promotion path is exercised
    // separately by `peel_factorize_landability_test`.)
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    let se_candidate = peelability
        .evaluated_owner_sets
        .iter()
        .find(|c| c.members.len() == 1 && c.members[0].binding == "Se")
        .unwrap_or_else(|| panic!("evaluated_owner_sets should include {{Se}}: {peelability:#?}"));
    assert_eq!(
        se_candidate.status,
        analysis::PeelCandidateStatus::PeelableNow,
        "{{Se}} should be PeelableNow — auto-grown entry exports make \
         the lazy read of Fx resolvable: {peelability:#?}",
    );

    assert!(
        peelability
            .minimal_peel_sets
            .iter()
            .any(|c| c.members.len() == 1 && c.members[0].binding == "Se"),
        "minimal_peel_sets should include singleton {{Se}}: {peelability:#?}",
    );
}

/// Verify the emit step actually grows entry's exports and the moved
/// module imports the binding from entry. Acts as an emit-shape pin
/// so a future regression that silently drops the auto-grow is caught
/// here, not just by the runtime smoke above.
///
/// `callHelper` is exported from entry so the moved module is
/// reachable via a `await import(...)` probe; entry itself doesn't
/// invoke `callHelper`, which would create a residual→callers
/// at-init read + callers→entry import cycle. The probe script
/// exercises the runtime path post-emit.
#[test]
fn auto_grown_export_is_visible_in_entry_and_imported_by_moved_module() {
    // `callHelper` is exported from entry so the moved module is
    // reachable via a `await import(...)` probe; entry itself
    // doesn't invoke `callHelper`, which would create a residual→
    // callers at-init read + callers→entry import cycle. The probe
    // script exercises the runtime path post-emit.
    //
    // Entry has no top-level side effects — `assert_module_exports`
    // imports entry once to list its exports; any `console.log` in
    // entry would prepend to the probe's stdout and break the JSON
    // parser.
    let mut opts = FixtureOpts::new(
        r#"function callHelper() { return helper; }
const helper = "h";
const Anchor = "anchor";
export { Anchor, callHelper };
"#,
        vec![logical_module(
            "callers/call_helper",
            &[Member::new("callHelper")],
        )],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    // Entry now exports `helper` (auto-grown), `Anchor`, and
    // `callHelper` (the source-level exports).
    assert_module_exports(
        &fixture.out_root,
        "static/app/entry.js",
        &["helper", "Anchor", "callHelper"],
        &[],
    );

    // The moved module imports `helper` from entry.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callers/call_helper.js",
        &[
            r#"import { helper } from "../../entry.js";"#,
            "return helper",
        ],
        &[],
    );

    // Runtime smoke: pull `callHelper` from the peeled module and
    // verify its body reads `helper` through the auto-grown import.
    assert_generated_module_after_entry_script(
        &fixture.out_root,
        r#"const { callHelper } = await import("./static/app/modules/callers/call_helper.js");
console.log(callHelper());
"#,
        "h\n",
    );
}

/// Negative: auto-grow only emits each name once. When the same
/// residual binding is read by multiple moved modules, entry must
/// not duplicate-export it (Node bails at load with
/// `SyntaxError: Duplicate export of 'helper'`).
#[test]
fn auto_grow_dedupes_when_multiple_moved_modules_read_same_residual_binding() {
    let mut opts = FixtureOpts::new(
        r#"function callerA() { return helper + "/a"; }
function callerB() { return helper + "/b"; }
const helper = "h";
const Anchor = "anchor";
export { Anchor, callerA, callerB };
"#,
        vec![
            logical_module("callers/a", &[Member::new("callerA")]),
            logical_module("callers/b", &[Member::new("callerB")]),
        ],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    // Entry exports `helper` exactly once across both consumers'
    // needs. Reaching this assertion at all is half the proof: a
    // duplicate `export { helper }` would have load-time failed
    // with `SyntaxError: Duplicate export of 'helper'`.
    assert_module_exports(
        &fixture.out_root,
        "static/app/entry.js",
        &["helper", "Anchor", "callerA", "callerB"],
        &[],
    );
}

/// Auto-grow must NOT shadow a pre-existing source-level
/// `export { name }` — emitting a second `export { name }` would be
/// a `SyntaxError: Duplicate export of 'name'` at load time. The
/// fixture's `helper` is already source-exported; the moved module
/// reads it; the materializer must not re-emit the export.
#[test]
fn auto_grow_skips_bindings_already_in_source_export_list() {
    let mut opts = FixtureOpts::new(
        r#"function callHelper() { return helper; }
const helper = "h";
const Anchor = "anchor";
export { Anchor, helper, callHelper };
"#,
        vec![logical_module(
            "callers/call_helper",
            &[Member::new("callHelper")],
        )],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    // The probe imports entry; a duplicate `export { helper }`
    // would have load-time failed with `SyntaxError: Duplicate
    // export of 'helper'`. Reaching this assertion proves
    // dedupe works.
    assert_module_exports(
        &fixture.out_root,
        "static/app/entry.js",
        &["helper", "Anchor", "callHelper"],
        &[],
    );
}
