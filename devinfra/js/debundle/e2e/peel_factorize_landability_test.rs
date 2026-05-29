//! End-to-end pinning of the peel factorizer's correctness against
//! the materializer's actual gates.
//!
//! `debundle run` emits stable owner and atomic-DAG facts. The peel
//! factorizer computes heuristic module proposals from that emitted
//! graph on demand. Blocked or size-capped frontier states are
//! diagnostics, not proposals.
//!
//! Paired fixtures:
//!
//! 1. **Init-order chain** — a blocked residual cell absorbs its
//!    small at-init prerequisite when the combined closure is
//!    landable.
//! 2. **Single-prereq closure** — a residual binding's body
//!    references another residual binding that isn't on entry's
//!    export list. Promoting the consumer standalone would be
//!    rejected; the factorizer reports the consumer plus its
//!    prerequisite as one landable closure.
//! 3. **Multi-prereq closure** — same shape with two independent
//!    prerequisites, pinning that a blocked consumer can absorb all
//!    small prerequisites at once.
//! 4. **Shared-prereq closure** — two blocked consumers share the
//!    same small prerequisite; the factorizer should merge the
//!    whole closure instead of leaving the prerequisite as a
//!    singleton leaf.

use analysis::OwnerGraphReport;
use debundle_e2e_support::*;
use peel::factorize::factorize;
use serde_json::json;
use spec::ModulePath;
use std::collections::BTreeMap;

/// Empty active-claims map: these fixtures all start from a fully
/// residual graph (no pre-existing spec modules).
fn no_claims() -> BTreeMap<String, ModulePath> {
    BTreeMap::new()
}

fn proposal_has_bindings(proposal: &peel::factorize::FactorizeProposal, bindings: &[&str]) -> bool {
    bindings
        .iter()
        .all(|binding| proposal.binding_ids.contains(&(*binding).to_string()))
}

fn annotated_effect_module(
    path: &str,
    binding: &str,
    effect: &str,
) -> debundle_e2e_support::LogicalModuleEntry {
    (
        path.to_string(),
        json!({
            "members": [
                {
                    "selector": { "binding": { "name": binding } },
                    "effect": effect
                }
            ]
        }),
    )
}

#[test]
fn analyzer_factorizer_peels_lazy_consumer_alone_under_emit_auto_grow() {
    // Pre-redesign: a `function consumer() { return dep; }` body's
    // lazy read of `dep` (residual + unexported) made `consumer`
    // `blocked_emit_resolvability`, so the factorizer combined the
    // pair `{dep, consumer}` into one cell to "internalize the
    // prerequisite". docs/design.md "Emit-side responsibilities" now
    // owns that: emit auto-grows entry's exports, so `consumer` is
    // independently peelable. The factorizer proposes
    // `{consumer}` alone, and `dep` stays in residual entry.
    let chunk_source = r#"const anchor = "anchor";
const dep = "secret";
function consumer() { return dep; }
export { anchor, consumer };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);

    assert!(
        report.proposals.iter().all(|proposal| proposal.status
            == analysis::PeelCandidateStatus::PeelableNow
            && (proposal.landable_today
                || (!proposal.unaddressable_anonymous_owner_ids.is_empty()
                    && !proposal.landability_notes.is_empty()))),
        "factorize proposals must be landable or explicitly advisory: {report:#?}",
    );
    assert!(
        report.proposals.iter().any(|proposal| proposal.binding_ids
            == vec!["consumer".to_string()]
            && proposal.landable_today),
        "lazy consumer should be peelable on its own: {report:#?}",
    );
}

#[test]
fn analyzer_factorizer_coalesces_shared_prerequisite_closure_under_cap() {
    let chunk_source = r#"const anchor = "anchor";
const shared = "shared";
const consumer_a = shared + "/a";
const consumer_b = shared + "/b";
export { anchor, consumer_a, consumer_b };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);
    assert!(
        report
            .proposals
            .iter()
            .any(|proposal| proposal_has_bindings(
                proposal,
                &["shared", "consumer_a", "consumer_b"]
            ) && proposal.landable_today),
        "CLI should preserve the analyzer's shared-prerequisite proposal: {report:#?}",
    );
}

#[test]
fn analyzer_factorizer_keeps_importable_lazy_consumers_as_singletons() {
    let chunk_source = r#"const anchor = "anchor";
function dep() { return "dep"; }
function consumer() { return dep(); }
export { anchor, dep, consumer };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);

    assert!(
        report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["consumer".to_string()]
                && proposal.landable_today
                && proposal.status == analysis::PeelCandidateStatus::PeelableNow
        }),
        "entry-exported lazy provider should not be forced into consumer's proposal: {report:#?}",
    );
    assert!(
        !report
            .proposals
            .iter()
            .any(|proposal| proposal_has_bindings(proposal, &["dep", "consumer"])),
        "importable lazy edge should not create a must-colocate factor: {report:#?}",
    );
}

#[test]
fn factorizer_orders_chain_cells_by_dependency_and_materializer_accepts_promotion() {
    // Source: three `const` initializers chained by at-init reads
    // (b reads a, c reads b). Only `a` is logical-module-claimed;
    // {b, c} sit residual.
    //
    // The closure-based analyzer treats `c → b` as a dependency
    // (c needs b first). The CLI factorizer should surface the
    // useful peel shape directly: {b, c}, since b is small and the
    // combined closure is landable.
    let chunk_source = r#"const a = 1;
const b = a + 1;
const c = b + 2;
export { a, b, c };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/a", &[Member::new("a")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);

    let chain_cell = report
        .proposals
        .iter()
        .find(|p| {
            p.binding_ids.contains(&"b".to_string()) && p.binding_ids.contains(&"c".to_string())
        })
        .expect("factorizer should propose a combined cell for `b` and `c`");
    assert!(
        chain_cell.landable_today,
        "combined chain closure must be landable; got cell={chain_cell:?}",
    );

    // The materializer accepts the lane-worker decision to promote
    // both into one combined module, matching the proposal.
    let promoted_opts = FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/a", &[Member::new("a")]),
            logical_module("helpers/chain", &[Member::new("b"), Member::new("c")]),
        ],
    );
    let _ = run_fixture(promoted_opts);
}

#[test]
fn factorizer_proposes_lazy_only_consumer_alone_via_emit_auto_grown_exports() {
    // `dep` is residual and NOT in entry's `export { ... }` list.
    // `consumer` lazily reads `dep` (inside its body). Under the old
    // emit-resolvability proposer gate this peel was refused; the
    // factorizer combined `consumer` with `dep` into one cell as a
    // workaround. The new design (docs/design.md "Valid peels and atomic
    // modules", importability clause) makes the emitter grow entry's
    // export list on demand, so `consumer` is peelable on its own
    // and the factorizer proposes the smaller `{consumer}` cell.
    //
    // `anchor` exists so the chunk has at least one active logical
    // module (the spec rejects all-residual chunks); `dep` and
    // `consumer` stay in the residual entry via
    // `unassigned_mode_inline()` (`InlineInEntry`).
    let chunk_source = r#"const anchor = "anchor";
const dep = "secret";
function consumer() { return dep; }
export { anchor, consumer };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);

    let consumer_alone = report
        .proposals
        .iter()
        .find(|p| p.binding_ids == vec!["consumer".to_string()])
        .expect("factorizer should propose `{consumer}` as a singleton");
    assert!(
        consumer_alone.landable_today,
        "singleton consumer cell must be landable; got {consumer_alone:?}",
    );
}

#[test]
fn factorizer_proposes_lazy_consumer_alone_when_multiple_residual_deps_are_unexported() {
    // `dep_a` and `dep_b` are residual and unexported. `consumer`
    // reads both lazily (inside its body). Same pattern as the
    // previous test, but with two prerequisites. With the new
    // emit-resolvability design, all three of `dep_a`, `dep_b`, and
    // `consumer` are independently peelable — and the factorizer
    // proposes them as three singletons rather than one closure.
    let chunk_source = r#"const anchor = "anchor";
const dep_a = "left";
const dep_b = "right";
function consumer() { return dep_a + dep_b; }
export { anchor, consumer };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);

    let consumer_alone = report
        .proposals
        .iter()
        .find(|p| p.binding_ids == vec!["consumer".to_string()])
        .expect("factorizer should propose `{consumer}` as a singleton");
    assert!(
        consumer_alone.landable_today,
        "singleton consumer cell must be landable; got {consumer_alone:?}",
    );
}

#[test]
fn factorizer_combines_multiple_consumers_with_shared_prerequisite_when_under_cap() {
    // `shared` is a small residual prerequisite used by two residual
    // consumers. Promoting either consumer alone would leave a
    // residual dependency, and promoting only {shared, consumer_a}
    // would still leave consumer_b blocked. The useful factor is the
    // full shared-prerequisite closure.
    let chunk_source = r#"const anchor = "anchor";
const shared = "shared";
const consumer_a = shared + "/a";
const consumer_b = shared + "/b";
export { anchor, consumer_a, consumer_b };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);

    let combined = report
        .proposals
        .iter()
        .find(|p| {
            p.binding_ids.contains(&"shared".to_string())
                && p.binding_ids.contains(&"consumer_a".to_string())
                && p.binding_ids.contains(&"consumer_b".to_string())
        })
        .expect("factorizer should combine both consumers with their shared prerequisite");
    assert!(
        combined.landable_today,
        "combined shared-prerequisite closure must be landable; got {combined:?}",
    );

    let promoted_opts = FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module(
                "helpers/shared_consumer_closure",
                &[
                    Member::new("shared"),
                    Member::new("consumer_a"),
                    Member::new("consumer_b"),
                ],
            ),
        ],
    );
    let _ = run_fixture(promoted_opts);
}

#[test]
fn factorizer_splits_pure_symbol_declarator_from_impure_sibling() {
    let chunk_source = r#"const anchor = "anchor";
class Something {}
const impure = new Something(), pureBrand = Symbol("Brand");
export { anchor, impure, pureBrand };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);
    assert!(
        report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["pureBrand".to_string()]
                && proposal.owner_ids.len() == 1
                && proposal.anonymous_statement_owner_ids.is_empty()
                && proposal.landable_today
        }),
        "CLI factorizer should preserve the analyzer's singleton pureBrand proposal: {report:#?}",
    );
    assert!(
        !report.proposals.iter().any(|proposal| {
            proposal.binding_ids.contains(&"pureBrand".to_string())
                && proposal.binding_ids.contains(&"impure".to_string())
        }),
        "impure sibling must not poison pureBrand's factorize proposal: {report:#?}",
    );

    let promoted = run_fixture(FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module("brands/pure_brand", &[Member::new("pureBrand")]),
        ],
    ));
    assert_module_source(
        &promoted.out_root,
        "static/app/modules/brands/pure_brand.js",
        &["const pureBrand = Symbol(", "export {", "pureBrand"],
        &["new Something", "impure"],
    );
}

#[test]
fn factorizer_does_not_emit_binding_only_proposal_for_rebound_split_let() {
    let chunk_source = r#"const anchor = "anchor";
let mutable = 1, peer = Symbol("Peer");
mutable = mutable + 1;
export { anchor, mutable, peer };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);
    assert!(
        report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["mutable".to_string()]
                && !proposal.anonymous_statement_owner_ids.is_empty()
                && proposal.landable_today
        }),
        "mutable can only be proposed together with its rebinding statement: {report:#?}",
    );
    assert!(
        !report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["mutable".to_string()]
                && proposal.anonymous_statement_owner_ids.is_empty()
                && proposal.landable_today
        }),
        "CLI factorizer must not surface a binding-only mutable proposal: {report:#?}",
    );

    let mut rejected_opts = FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module("state/mutable", &[Member::new("mutable")]),
        ],
    );
    rejected_opts.unassigned_mode = unassigned_mode_inline();
    expect_rejection_containing_all(
        rejected_opts,
        &["assignment", "mutable", "cross-destination"],
    );
}

#[test]
fn annotated_decorate_helper_breaks_class_plus_decorator_from_side_effect_chain() {
    // Minified TypeScript-decorator shape: the class itself is small and
    // peelable only with its post-class decorator application. The unrelated
    // source-order side effects before/after the decorator should not force a
    // mega-closure once the helper is annotated as a target-local effect.
    let chunk_source = r#"const anchor = "anchor";
console.log("boot");
function Ro(decorators, target, key, flags) {
  for (let i = decorators.length - 1; i >= 0; i--) decorators[i](target, key);
}
const Z = () => {};
class SearchPopoverState {
  constructor() { this.visible = false; }
}
Ro([Z], SearchPopoverState.prototype, "visible", 2);
console.log("tail");
export { anchor, SearchPopoverState };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            annotated_effect_module(
                "infra/decorators/ts_decorate",
                "Ro",
                "typescript_decorate_helper",
            ),
            logical_module("infra/decorators/observable", &[Member::new("Z")]),
        ],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &no_claims(), 10_000);

    assert!(
        report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["SearchPopoverState".to_string()]
                && proposal.anonymous_statement_owner_ids.len() == 1
                && proposal.owner_ids.len() == 2
                && proposal.landable_today
        }),
        "decorated class should be proposed with exactly its decorator statement, not the unrelated side-effect chain: {report:#?}",
    );
    assert!(
        !report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["SearchPopoverState".to_string()]
                && proposal.anonymous_statement_owner_ids.is_empty()
        }),
        "class-only proposal would split the target-local decorator effect: {report:#?}",
    );

    let promoted_opts = FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            annotated_effect_module(
                "infra/decorators/ts_decorate",
                "Ro",
                "typescript_decorate_helper",
            ),
            logical_module("infra/decorators/observable", &[Member::new("Z")]),
            logical_module_with_anon(
                "features/search/popover_state",
                &[Member::new("SearchPopoverState")],
                &["Ro([Z], SearchPopoverState.prototype, \"visible\", 2);"],
            ),
        ],
    );
    let _ = run_fixture(promoted_opts);
}

#[test]
fn materializer_rejects_splitting_annotated_decorator_effect_from_target_class() {
    let chunk_source = r#"const anchor = "anchor";
function Ro(decorators, target, key, flags) {
  for (let i = decorators.length - 1; i >= 0; i--) decorators[i](target, key);
}
const Z = () => {};
class SearchPopoverState {}
Ro([Z], SearchPopoverState.prototype, "visible", 2);
export { anchor, SearchPopoverState };
"#;

    let opts = FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            annotated_effect_module(
                "infra/decorators/ts_decorate",
                "Ro",
                "typescript_decorate_helper",
            ),
            logical_module("infra/decorators/observable", &[Member::new("Z")]),
            logical_module(
                "features/search/popover_state",
                &[Member::new("SearchPopoverState")],
            ),
        ],
    );

    expect_rejection_containing_all(opts, &["atomic-factor-unit", "local effect"]);
}
