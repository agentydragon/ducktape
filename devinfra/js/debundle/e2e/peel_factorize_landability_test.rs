//! End-to-end pinning of the peel proposer's correctness against
//! the materializer's actual gates.
//!
//! `debundle run` emits stable owner and atomic-DAG facts. The peel
//! proposer computes heuristic module proposals from that emitted
//! graph on demand. Blocked or size-capped frontier states are
//! diagnostics, not proposals.

use debundle_e2e_support::*;
use peel::propose::{PeelCandidateStatus, ProposalsReport, propose};
use spec::{MemberEffect, ModulePath};
use std::collections::BTreeMap;

/// Empty active-claims map: these fixtures all start from a fully
/// residual graph (no pre-existing spec modules).
fn no_claims() -> BTreeMap<String, ModulePath> {
    BTreeMap::new()
}

/// Run `debundle` on `source` with `modules` claimed and every other
/// binding inlined in the residual entry.
fn residual_fixture(source: &str, modules: Vec<LogicalModuleEntry>) -> Fixture {
    let mut opts = FixtureOpts::new(source, modules);
    opts.unassigned_mode = unassigned_mode_inline();
    run_fixture(opts)
}

fn factorize_residual(source: &str, modules: Vec<LogicalModuleEntry>) -> ProposalsReport {
    propose(
        &residual_fixture(source, modules).owner_graph(),
        &no_claims(),
        10_000,
    )
    .unwrap()
}

fn proposal_has_bindings(proposal: &peel::propose::ModuleProposal, bindings: &[&str]) -> bool {
    bindings
        .iter()
        .all(|binding| proposal.binding_ids.contains(&(*binding).to_string()))
}

fn annotated_effect_module(
    path: &str,
    binding: &'static str,
    effect: MemberEffect,
) -> debundle_e2e_support::LogicalModuleEntry {
    logical_module(path, &[Member::new(binding).with_effect(effect)])
}

#[test]
fn proposer_proposes_lazy_consumer_alone_via_emit_auto_grown_exports() {
    // `dep` is residual and NOT in entry's `export { ... }` list;
    // `consumer` lazily reads it inside its body. Emit grows entry's
    // export list on demand (docs/design.md "Valid peels and atomic
    // modules", importability clause), so `consumer` is peelable on its
    // own and `dep` stays in the residual entry.
    //
    // `anchor` exists so the chunk has at least one active logical
    // module (the spec rejects all-residual chunks).
    let chunk_source = r#"const anchor = "anchor";
const dep = "secret";
function consumer() { return dep; }
export { anchor, consumer };
"#;

    let report = factorize_residual(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );

    assert!(
        report.proposals.iter().all(
            |proposal| proposal.status == PeelCandidateStatus::PeelableNow
                && (proposal.landable_today
                    || (!proposal.unaddressable_anonymous_owner_ids.is_empty()
                        && !proposal.landability_notes.is_empty()))
        ),
        "propose proposals must be landable or explicitly advisory: {report:#?}",
    );
    assert!(
        report.proposals.iter().any(|proposal| proposal.binding_ids
            == vec!["consumer".to_string()]
            && proposal.landable_today),
        "lazy consumer should be peelable on its own: {report:#?}",
    );
}

#[test]
fn analyzer_proposer_keeps_importable_lazy_consumers_as_singletons() {
    let chunk_source = r#"const anchor = "anchor";
function dep() { return "dep"; }
function consumer() { return dep(); }
export { anchor, dep, consumer };
"#;

    let report = factorize_residual(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );

    assert!(
        report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["consumer".to_string()]
                && proposal.landable_today
                && proposal.status == PeelCandidateStatus::PeelableNow
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
fn proposer_orders_chain_cells_by_dependency_and_materializer_accepts_promotion() {
    // Source: three `const` initializers chained by at-init reads
    // (b reads a, c reads b). Only `a` is logical-module-claimed;
    // {b, c} sit residual.
    //
    // The closure-based analyzer treats `c → b` as a dependency
    // (c needs b first). The CLI proposer should surface the
    // useful peel shape directly: {b, c}, since b is small and the
    // combined closure is landable.
    let chunk_source = r#"const a = 1;
const b = a + 1;
const c = b + 2;
export { a, b, c };
"#;

    let report = factorize_residual(
        chunk_source,
        vec![logical_module("anchors/a", &[Member::new("a")])],
    );

    let chain_cell = report
        .proposals
        .iter()
        .find(|p| {
            p.binding_ids.contains(&"b".to_string()) && p.binding_ids.contains(&"c".to_string())
        })
        .expect("proposer should propose a combined cell for `b` and `c`");
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
fn proposer_proposes_lazy_consumer_alone_when_multiple_residual_deps_are_unexported() {
    // `dep_a` and `dep_b` are residual and unexported; `consumer` reads
    // both lazily. As in the single-`dep` test above, all three are
    // independently peelable: three singleton proposals, not one closure.
    let chunk_source = r#"const anchor = "anchor";
const dep_a = "left";
const dep_b = "right";
function consumer() { return dep_a + dep_b; }
export { anchor, consumer };
"#;

    let report = factorize_residual(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );

    for binding in ["dep_a", "dep_b", "consumer"] {
        let singleton = report
            .proposals
            .iter()
            .find(|p| p.binding_ids == vec![binding.to_string()])
            .unwrap_or_else(|| panic!("proposer should propose `{{{binding}}}` as a singleton"));
        assert!(
            singleton.landable_today,
            "singleton `{binding}` cell must be landable; got {singleton:?}",
        );
    }
}

#[test]
fn proposer_combines_multiple_consumers_with_shared_prerequisite_when_under_cap() {
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

    let report = factorize_residual(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );

    let combined = report
        .proposals
        .iter()
        .find(|p| proposal_has_bindings(p, &["shared", "consumer_a", "consumer_b"]))
        .expect("proposer should combine both consumers with their shared prerequisite");
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
fn blocked_residual_dependency_proposals_are_not_landable_today() {
    // `consumer` reads `dep` at init (constraining edge). Under a size
    // cap that admits each singleton but refuses the combined closure,
    // the quotient keeps them as separate residual cells, so
    // `consumer`'s cell retains an outgoing constraining edge into
    // `dep`'s cell. Promoting `consumer` alone would route the read
    // through `residual_entry` and trip the realizability gate —
    // status is `blocked_residual_dependency` AND `landable_today`
    // must be false (one predicate, not two): `bindings assign
    // --batch` consumers must not burn a run on it. `dep` itself has
    // no cross-residual edges and stays landable.
    let chunk_source = r#"const anchor = "anchor";
const dep = "secret";
const consumer = dep + "/x";
export { anchor, dep, consumer };
"#;

    let graph = residual_fixture(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    )
    .owner_graph();
    let report = propose(&graph, &no_claims(), 1).unwrap();

    let blocked = report
        .proposals
        .iter()
        .find(|p| p.binding_ids == vec!["consumer".to_string()])
        .expect("proposer should keep `{consumer}` as its own cell under the tight cap");
    assert_eq!(
        blocked.status,
        PeelCandidateStatus::BlockedResidualDependency,
        "{blocked:?}",
    );
    assert!(
        !blocked.landable_today,
        "cross-residual proposal must not claim landability; got {blocked:?}",
    );

    let landable = report
        .proposals
        .iter()
        .find(|p| p.binding_ids == vec!["dep".to_string()])
        .expect("proposer should keep `{dep}` as its own cell under the tight cap");
    assert_eq!(
        landable.status,
        PeelCandidateStatus::PeelableNow,
        "{landable:?}",
    );
    assert!(
        landable.landable_today,
        "edge-free residual cell must stay landable; got {landable:?}",
    );
}

#[test]
fn proposer_splits_pure_symbol_declarator_from_impure_sibling() {
    let chunk_source = r#"const anchor = "anchor";
class Something {}
const impure = new Something(), pureBrand = Symbol("Brand");
export { anchor, impure, pureBrand };
"#;

    let report = factorize_residual(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    assert!(
        report.proposals.iter().any(|proposal| {
            proposal.binding_ids == vec!["pureBrand".to_string()]
                && proposal.owner_ids.len() == 1
                && proposal.anonymous_statement_owner_ids.is_empty()
                && proposal.landable_today
        }),
        "CLI proposer should preserve the analyzer's singleton pureBrand proposal: {report:#?}",
    );
    assert!(
        !report.proposals.iter().any(|proposal| {
            proposal.binding_ids.contains(&"pureBrand".to_string())
                && proposal.binding_ids.contains(&"impure".to_string())
        }),
        "impure sibling must not poison pureBrand's propose proposal: {report:#?}",
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
fn proposer_does_not_emit_binding_only_proposal_for_rebound_split_let() {
    let chunk_source = r#"const anchor = "anchor";
let mutable = 1, peer = Symbol("Peer");
mutable = mutable + 1;
export { anchor, mutable, peer };
"#;

    let report = factorize_residual(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
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
        "CLI proposer must not surface a binding-only mutable proposal: {report:#?}",
    );

    let mut rejected_opts = FixtureOpts::new(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module("state/mutable", &[Member::new("mutable")]),
        ],
    );
    rejected_opts.unassigned_mode = unassigned_mode_inline();
    expect_atomic_conflict_rejection(rejected_opts, &["state/mutable"], &["eager_rebind"]);
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

    let report = factorize_residual(
        chunk_source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            annotated_effect_module(
                "infra/decorators/ts_decorate",
                "Ro",
                MemberEffect::TypescriptDecorateHelper,
            ),
            logical_module("infra/decorators/observable", &[Member::new("Z")]),
        ],
    );

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
                MemberEffect::TypescriptDecorateHelper,
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
                MemberEffect::TypescriptDecorateHelper,
            ),
            logical_module("infra/decorators/observable", &[Member::new("Z")]),
            logical_module(
                "features/search/popover_state",
                &[Member::new("SearchPopoverState")],
            ),
        ],
    );

    expect_atomic_conflict_rejection(opts, &["features/search/popover_state"], &["local_effect"]);
}
