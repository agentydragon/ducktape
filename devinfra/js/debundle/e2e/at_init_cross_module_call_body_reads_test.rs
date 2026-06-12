//! Tests for the cross-module at-init body-read promotion rule.
//!
//! Background: `graph.rs::promote_at_init_calls` makes a top-level
//! eager call `caller()` inherit its callee's transitive lazy reads
//! (the callee's function body, plus any chunk-declared functions
//! the body calls). Without further gating, the propagation crosses
//! module boundaries: if the caller lives in module R and the callee
//! `gR` lives in module M, then any binding `crossModBinding` that
//! `gR`'s body reads — even one declared in a third module M2 — is
//! recorded as an eager read of R's, manufacturing an
//! `R --EagerUse--> M2` cross-module constraint.
//!
//! ## Why that's wrong (ESM semantics)
//!
//! By the time R evaluates `gR(...)`:
//! - R imports M, so M evaluated before R (ESM DFS post-order).
//! - M's evaluation already pulled in M's imports (M2), because the
//!   body of `gR` lexically reads `crossModBinding` and the linker
//!   sees that read at instantiation time.
//! - `gR`'s body's read of `crossModBinding` fires synchronously
//!   inside the call from R's top-level — but at that point M2 is
//!   fully evaluated. No TDZ.
//!
//! The constraint M -> M2 is the one ESM honors; the spurious R -> M2
//! constraint the analyzer adds is redundant at best and a cycle
//! closer at worst.
//!
//! ## Three-module fixture
//!
//! - `R` (residual): `const triggerInit = gR(iA);` — eager call into
//!   `M.gR`. Owner of `triggerInit` and the seed value `iA`.
//! - `M`: `function gR(x) { return crossModBinding + x; }` — declares
//!   `gR`, body reads `crossModBinding` LAZILY (inside the function
//!   body, fires only at call time).
//! - `M2`: `const crossModBinding = 42;` — declares the binding the
//!   body reads.
//!
//! ## Expected analyzer behavior
//!
//! At the *module-quotient* level (what the realizability gate
//! consults): no `R -> M2` cross-module edge. R -> M survives because
//! the call itself is an eager use of `gR`'s binding (and the `iA`
//! argument is read from R-owned state).
//!
//! At the *owner* level the promoted edge is still recorded with
//! `at_init_callee_owner = owner(gR)`, but the quotient + gate filter
//! it out under cross-module assignment. The intra-module variant
//! (callee and caller in the same module) keeps promoting body reads
//! — that case is unaffected.

use analysis::{DepKind, OwnerGraphReport};
use debundle_e2e_support::*;

const CROSS_MODULE_SOURCE: &str = r#"const iA = 7;
const triggerInit = gR(iA);
function gR(x) { return crossModBinding + x; }
const crossModBinding = 42;
console.log(triggerInit);
export { iA, triggerInit, gR, crossModBinding };
"#;

fn owner_for_binding<'a>(graph: &'a OwnerGraphReport, binding: &str) -> &'a str {
    let node = graph
        .nodes
        .iter()
        .find(|node| node.declared_bindings.iter().any(|b| b.binding == binding))
        .unwrap_or_else(|| {
            panic!(
                "no owner-graph node declares binding `{binding}`; \
                 nodes: {:#?}",
                graph.nodes,
            )
        });
    node.id.as_str()
}

fn module_id_for<'a>(graph: &'a OwnerGraphReport, binding: &str) -> &'a str {
    let owner_id = owner_for_binding(graph, binding);
    let node = graph
        .nodes
        .iter()
        .find(|node| node.id == owner_id)
        .unwrap_or_else(|| panic!("no node with id {owner_id} found"));
    node.destination.as_str()
}

fn quotient_edges_between<'a>(
    graph: &'a OwnerGraphReport,
    from_module: &str,
    to_module: &str,
) -> Vec<&'a analysis::QuotientEdgeReport> {
    graph
        .quotient
        .edges
        .iter()
        .filter(|edge| edge.source.as_str() == from_module && edge.target.as_str() == to_module)
        .collect()
}

#[test]
fn at_init_call_to_cross_module_function_does_not_promote_body_reads() {
    // R (residual) hosts `iA` + `triggerInit`. M owns the function
    // declaration `gR`. M2 owns `crossModBinding`. The top-level
    // `gR(iA)` is an at-init call; without the fix, the analyzer
    // propagates `gR`'s body's read of `crossModBinding` as R's
    // eager read, emitting an R -> M2 cross-module `EagerUse`.
    let fixture = run_fixture(FixtureOpts::new(
        CROSS_MODULE_SOURCE,
        vec![
            logical_module("mod_m", &[Member::new("gR")]),
            logical_module("mod_m2", &[Member::new("crossModBinding")]),
        ],
    ));

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));

    let r_module = module_id_for(&graph, "triggerInit");
    let m2_module = module_id_for(&graph, "crossModBinding");

    let offending = quotient_edges_between(&graph, r_module, m2_module);
    assert!(
        offending.is_empty(),
        "no cross-module quotient edge from R ({r_module}) to M2 \
         ({m2_module}) should exist: the call `gR(iA)` crosses module \
         boundaries (caller in residual, callee `gR` in mod_m), and \
         `gR`'s body's read of `crossModBinding` fires inside the \
         call after mod_m2 has already evaluated. The R -> M2 \
         constraint the unfixed analyzer manufactures is spurious. \
         Offending edges: {offending:#?}\n\nFull quotient: \
         {:#?}",
        graph.quotient,
    );
}

#[test]
fn at_init_call_to_cross_module_setter_does_not_colocate_caller_with_state() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let state = "initial";
function setState(value) { state = value; }
setState("updated");
console.log(state);
export { state, setState };
"#,
        vec![logical_module(
            "mod_state",
            &[Member::new("state"), Member::new("setState")],
        )],
    ));
    assert_entry_output(&fixture, "updated\n");
}

#[test]
fn at_init_call_keeps_owner_edge_marked_with_callee() {
    // The promoted owner edge is retained in the owner graph (the
    // analyzer's IR keeps it as evidence of the promotion), but with
    // the `at_init_callee_owner` field set so the gate can filter it
    // out at quotient time. This guards against an accidental
    // regression where the owner edge gets dropped entirely (which
    // would break any consumer that audits the owner-level promotion
    // shape).
    let fixture = run_fixture(FixtureOpts::new(
        CROSS_MODULE_SOURCE,
        vec![
            logical_module("mod_m", &[Member::new("gR")]),
            logical_module("mod_m2", &[Member::new("crossModBinding")]),
        ],
    ));

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let trigger_owner = owner_for_binding(&graph, "triggerInit");
    let target_owner = owner_for_binding(&graph, "crossModBinding");
    let gr_owner = owner_for_binding(&graph, "gR");

    let promoted: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.source == trigger_owner
                && edge.target == target_owner
                && edge.edge_kind == DepKind::EagerUse
        })
        .collect();

    assert_eq!(
        promoted.len(),
        1,
        "expected exactly one owner-level promoted EagerUse edge \
         from `triggerInit` ({trigger_owner}) to `crossModBinding` \
         ({target_owner}); got {promoted:#?}\n\nFull owner graph: {graph:#?}",
    );
    let callee = match promoted[0].role.as_ref() {
        Some(analysis::EdgeRoleReport::PromotedAtInit { callee_owner }) => callee_owner.as_str(),
        _ => panic!(
            "promoted owner edge must carry an `EdgeRole::PromotedAtInit` role \
             (callee_owner = {gr_owner}); got {:#?}",
            promoted[0],
        ),
    };
    assert_eq!(
        callee, gr_owner,
        "promoted owner edge's at-init callee owner must point at gR \
         ({gr_owner}) so the quotient and realizability gates can \
         drop it under cross-module assignment; got {:#?}",
        promoted[0],
    );
}

#[test]
fn cross_module_at_init_call_runs_under_node() {
    // Runtime verification: emit the spec under the fixed analyzer
    // and assert Node executes the chunk without a TDZ. The fixture
    // is the same as the analyzer test above, so a regression in
    // either direction (false positive cycle that rejects the spec,
    // or a real cycle the analyzer missed) gets caught here.
    let fixture = run_fixture(FixtureOpts::new(
        CROSS_MODULE_SOURCE,
        vec![
            logical_module("mod_m", &[Member::new("gR")]),
            logical_module("mod_m2", &[Member::new("crossModBinding")]),
        ],
    ));
    assert_entry_output(&fixture, "49\n");
}

#[test]
fn intra_module_at_init_call_still_promotes_body_reads() {
    // Negative regression guard: when the callee lives in the SAME
    // module as the caller, the call's body reads ARE genuinely the
    // caller module's eager reads (same evaluation context). The
    // analyzer must keep promoting them all the way through to the
    // module-level quotient. The fixture co-locates `gR` with the
    // top-level call in residual; `crossModBinding` lives in
    // mod_m2. Expectation: R -> M2 quotient edge survives.
    let fixture = run_fixture(FixtureOpts::new(
        CROSS_MODULE_SOURCE,
        // gR stays in residual alongside the top-level call.
        vec![logical_module("mod_m2", &[Member::new("crossModBinding")])],
    ));

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));

    let r_module = module_id_for(&graph, "triggerInit");
    let m2_module = module_id_for(&graph, "crossModBinding");

    let surviving = quotient_edges_between(&graph, r_module, m2_module);
    assert!(
        !surviving.is_empty(),
        "expected a cross-module quotient edge from R ({r_module}) \
         to M2 ({m2_module}): the call `gR(iA)` is intra-module \
         (caller and callee both in residual), so `gR`'s body's read \
         of `crossModBinding` is the same eval-time concern as \
         residual's own top-level reads. Dropping this edge would \
         lose a legitimate init-order constraint. Full quotient: \
         {:#?}",
        graph.quotient,
    );
    assert!(
        surviving.iter().any(|edge| edge.constrains_init_order),
        "surviving R -> M2 edge must constrain init order (EagerUse \
         or Sequenced); got {surviving:#?}",
    );
}
