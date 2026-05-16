//! End-to-end coverage for the two report-layer fixes in
//! `analysis::factorize::build_factorize_report`:
//!
//! 1. **Diagnostic dedup**: when several residual frontier starts grow
//!    into the same `(reason, owner-set)` closure (via blocker-driven
//!    absorption or `close_atomic_units`), only one diagnostic row is
//!    emitted instead of one per starting unit. Applies to every
//!    diagnostic reason — `ExceedsSizeCap`, `NoExactRepair`,
//!    `ActiveModuleConflict`, `RepeatedFrontier`.
//! 2. **Oversize-closure skip-walk**: once a closure has been emitted
//!    with reason `ExceedsSizeCap`, any later seed whose unrepaired set
//!    is wholly inside that closure short-circuits with `Empty` —
//!    skipping the dependency-graph walk that would re-derive a subset
//!    or duplicate row.
//!
//! These e2e tests drive the full CLI pipeline and assert the dedup
//! invariant on the emitted `owner_graph.json`. The materializer CLI
//! uses the analyzer's default `size_cap_lines = 10_000`, so
//! `ExceedsSizeCap`-flavored diagnostics aren't naturally reachable
//! from a small synthetic chunk through the CLI — the in-process unit
//! tests next to `analysis::factorize` (`analysis_tests.rs`) cover the
//! tight-cap path. Here we pin that:
//!
//! * Multi-consumer fixtures produce reports whose
//!   `factorize.diagnostics` rows obey the `(reason, sorted owner_ids)`
//!   uniqueness invariant.
//! * The dedup change does not also suppress legitimate certified
//!   proposals (shared-prerequisite closure still surfaces).
//!
//! ## Limitations of synthetic construction
//!
//! Whether any pair of frontier starts naturally produces the same
//! `(reason, owner-set)` key depends on the residual + active-module
//! landscape and on `close_atomic_units`'s absorption behaviour. The
//! invariant we assert is robust to that nondeterminism: every
//! emitted row's key must be unique, and the regression-guard test
//! checks the dedup change does not also lose certified cells.

use analysis::{FactorizeDiagnosticReason, OwnerGraphReport, PeelCandidateStatus};
use debundle_e2e_support::*;
use serde::de::DeserializeOwned;
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

/// Drive the CLI to completion and parse the emitted owner_graph.json.
fn run_cli_and_load_graph(opts: FixtureOpts<'_>) -> OwnerGraphReport {
    let fixture = run_fixture(opts);
    read_json(&fixture.report_root.join("static/app/owner_graph.json"))
}

/// Post-fix invariant: every diagnostic row has a unique
/// `(reason, sorted owner_ids)` key.
fn assert_diagnostic_keys_unique(graph: &OwnerGraphReport) {
    let mut seen: BTreeSet<(FactorizeDiagnosticReason, Vec<String>)> = BTreeSet::new();
    for diagnostic in &graph.factorize.diagnostics {
        let mut owners = diagnostic.owner_ids.clone();
        owners.sort();
        assert!(
            seen.insert((diagnostic.reason, owners.clone())),
            "duplicate factorize diagnostic key (reason={:?}, owners={:?}) in CLI report; row: {diagnostic:#?}",
            diagnostic.reason,
            owners,
        );
    }
}

#[test]
fn fan_in_multi_consumer_report_has_unique_diagnostic_keys() {
    // Three residual consumers all read the same two residual
    // prerequisites. Each consumer is its own atomic unit; each
    // produces a frontier start; growth pulls in the shared deps.
    // Pre-fix, frontiers that converge on the same closure produced
    // duplicate report rows. Post-fix, the
    // `(reason, sorted owner_ids)` dedup keeps each closure on one
    // row.
    //
    // We assert the dedup *invariant* on the actual CLI-emitted
    // `owner_graph.json`. The full pipeline must always honour it.
    let source = r#"const dep_a = "left";
const dep_b = "right";
function consumer_one() { return dep_a + dep_b; }
function consumer_two() { return dep_a + dep_b; }
function consumer_three() { return dep_a + dep_b; }
export { consumer_one, consumer_two, consumer_three };
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![logical_module("anchors/dep_a", &[Member::new("dep_a")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let graph = run_cli_and_load_graph(opts);
    assert_diagnostic_keys_unique(&graph);
}

#[test]
fn shared_prerequisite_two_consumers_report_has_unique_diagnostic_keys() {
    // Two residual consumers share one residual prerequisite. The
    // three frontier starts ({consumer_a}, {consumer_b}, {shared})
    // visit overlapping owner sets during growth. Whether any two
    // produce identical `(reason, owner-set)` keys depends on the
    // residual landscape — the invariant is what's pinned here.
    let source = r#"const shared = "shared";
const consumer_a = shared + "/a";
const consumer_b = shared + "/b";
export { consumer_a, consumer_b };
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![logical_module("anchors/shared", &[Member::new("shared")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let graph = run_cli_and_load_graph(opts);
    assert_diagnostic_keys_unique(&graph);
}

#[test]
fn cycle_megaclass_with_external_consumers_report_has_unique_diagnostic_keys() {
    // A 3-owner constraining cycle ({A, B, C}) plus two unrelated
    // consumers that lazily reference members of the cycle. Each
    // consumer's frontier closes through the cycle's blocker-driven
    // growth, so multiple starts can converge on the same megaclass
    // owner-set. The dedup invariant must hold on the report.
    //
    // `anchor` is the active logical module; everything else is
    // residual (the spec rejects all-residual chunks). The cycle
    // {A, B, C} is one atomic unit and stays residual together, which
    // matches the materializer's atomic-unit co-location rule.
    let source = r#"const anchor = "anchor";
const A = C + 1;
const B = A + 1;
const C = B + 1;
function reader_one() { return A + B; }
function reader_two() { return B + C; }
export { anchor, A, B, C, reader_one, reader_two };
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let graph = run_cli_and_load_graph(opts);
    assert_diagnostic_keys_unique(&graph);
}

#[test]
fn dedup_change_does_not_suppress_certified_shared_prerequisite_cell() {
    // Regression guard: the dedup change must not drop legitimate
    // certified proposals. A shared-prerequisite closure that is
    // already landable today should still surface as one combined
    // certified cell after the report-layer cleanup.
    let source = r#"const anchor = "anchor";
const shared = "shared";
const consumer_a = shared + "/a";
const consumer_b = shared + "/b";
export { anchor, consumer_a, consumer_b };
"#;
    let mut opts = FixtureOpts::new(
        source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let graph = run_cli_and_load_graph(opts);

    assert!(
        graph.factorize.cells.iter().any(|cell| {
            let bindings: BTreeSet<&str> = cell.binding_ids.iter().map(String::as_str).collect();
            bindings.contains("shared")
                && bindings.contains("consumer_a")
                && bindings.contains("consumer_b")
                && cell.landable_today
                && cell.status == PeelCandidateStatus::PeelableNow
        }),
        "shared-prerequisite closure should still be emitted as a certified cell after dedup change: {:#?}",
        graph.factorize,
    );

    assert_diagnostic_keys_unique(&graph);
}
