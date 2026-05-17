//! GREEN test (post-fix): pins that the peelability analyzer no
//! longer over-promises a `direct` peel that the cycle gate refuses.
//! Originally introduced as a RED pin alongside PR #1625; flipped in
//! the same branch once the proposer was rerouted through the shared
//! realizability primitive.
//!
//! The two predicates the design says should agree are now backed by
//! one implementation:
//!
//! 1. The peelability **proposer** (`evaluate_peel_candidate` in
//!    `peelability.rs`) pushes the candidate's hypothetical
//!    `MoveOwners` delta onto the shared `RealizabilityIndex`
//!    (`devinfra/js/debundle/realizability.rs`) and reads the verdict.
//! 2. The **cycle gate** (`validate_schedule` in `validation.rs`,
//!    consumed by `materialize_logical_modules` in
//!    `logical_modules.rs`) is unchanged in this PR but answers the
//!    same three-clause validity predicate. With the proposer routed
//!    through the same primitive, both sides cannot disagree on
//!    whether a candidate peel produces a constraining cross-module
//!    SCC.
//!
//! For the synthetic shape in this file the proposer also catches a
//! `BlockedResidualDependency` (the layered clause-1 check for moved
//! at-init reads that would remain in the source destination), which
//! is the assertion the post-fix `assert_ne!` rests on.
//!
//! ## What `direct` is supposed to mean
//!
//! From `DESIGN.md` "Peelability diagnostics":
//!
//! > `peelable_now` — assigning this owner set to a new logical
//! > destination leaves the quotient graph realizable and all
//! > imports resolvable.
//!
//! And from "Residual peel candidates":
//!
//! > The candidate is `peelable_now` iff `P_o`'s SCC in the
//! > constraining subgraph is singleton/non-cyclic.
//!
//! And the horizon's `direct` is the singleton-`peelable_now`
//! projection of that test (`build_residual_owner_horizon` in
//! `peelability.rs`).
//!
//! ## What the cycle gate actually requires
//!
//! From `DESIGN.md` "Valid peels and atomic modules", a destination
//! assignment is valid iff:
//!
//! > 3. The constraining-edge subgraph of `Q` — the result of
//! >    dropping all `LazyUse` cross edges from `Q` — has no
//! >    multi-module SCC.
//!
//! `validate_schedule` enforces this by running Tarjan over the full
//! quotient and rejecting any multi-module SCC that contains at least
//! one cross-module constraining edge (an at-init read or a
//! side-effect-order edge).
//!
//! ## Why the two diverge for this shape
//!
//! `evaluate_peel_candidate` evaluates the candidate against the
//! **current** quotient `Q`, treating the candidate's module as a
//! synthetic new node. It walks `forward` reachability from the
//! candidate's outgoing constraining edges and `backward`
//! reachability from the candidate's incoming constraining edges
//! over the pre-peel `module_pair_totals`. The walk does **not**
//! re-quotient `Q` against the post-peel partition `Q'`. So any
//! constraining cycle that the realized peel induces — e.g. through
//! a residual neighbor whose adjacency changes once `T` is no longer
//! in the catch-all — is not modelled by the proposer's SCC search,
//! even though the gate's Tarjan over `Q'` will find it.
//!
//! Concretely for the shape below:
//!
//! - `target_binding` is a pure top-level `const`. No outgoing
//!   constraining edges; one incoming `EagerUse` edge from an
//!   anonymous at-init statement (`anon_residual_sentinel` owner).
//! - `at_init_consumer` is the anon side-effect statement
//!   (`console.log(target_binding)`).
//! - `bridge_binding` is another residual owner with a side-effectful
//!   initializer (`(() => "bridge")()`). Source order is
//!   `bridge_binding; target_binding; at_init_consumer` so the
//!   side-effect chain wires `at_init_consumer → bridge_binding`
//!   (Sequenced) via the transitive reduction of the source-order
//!   `S` chain. The `EagerUse` edge `at_init_consumer →
//!   target_binding` is the other half.
//!
//! Pre-peel quotient: residual_catchall is `{bridge_binding,
//! target_binding}`, sentinel is `{at_init_consumer}`. The two
//! cross-module edges sentinel → residual_catchall are both
//! constraining but they don't form a quotient cycle on their own;
//! `at_init_consumer`'s sequenced edge to `bridge_binding` is
//! co-destination with its eager-read edge to `target_binding`, so
//! the quotient has a single sentinel → residual_catchall
//! constraining bundle and no back-edge. The schedule passes
//! validation.
//!
//! The singleton `{target_binding}` peel candidate: the proposer
//! sees one `ToCandidate` edge from sentinel (the
//! `at_init_consumer → target_binding` EagerUse) and no
//! `FromCandidate` edges (the pure const has no outgoing). Its
//! forward-BFS set is empty so `in_scc` is empty so the candidate
//! is `PeelableNow`. The horizon classifies `target_binding` as
//! `Direct`.
//!
//! Post-peel quotient (after the spec assigns `target_binding` to
//! `mod_target`): residual_catchall is `{bridge_binding}`, sentinel
//! is `{at_init_consumer}`, mod_target is `{target_binding}`. The
//! two cross-module edges split:
//! - `at_init_consumer → target_binding` becomes sentinel → mod_target
//!   (constraining EagerUse + Sequenced — sequenced because
//!   `at_init_consumer` is the next side-effect after the
//!   side-effectful `bridge_binding` initializer, and the transitive
//!   reduction has at_init_consumer's only sequenced edge pointing
//!   back to bridge_binding — but since target_binding's pure init
//!   contains no side effect, the chain skips it),
//! - `at_init_consumer → bridge_binding` stays sentinel →
//!   residual_catchall (Sequenced).
//!
//! Plus, because `bridge_binding`'s side-effectful initializer
//! `(() => "bridge")()` produces no owner-edge by itself, but
//! `at_init_consumer` reads `target_binding` whose declaration is
//! now in `mod_target`, the post-peel realization introduces a
//! constraining sentinel ↔ mod_target SCC iff the realizer adds the
//! missing back-edge mod_target → sentinel. The proposer's predicate
//! doesn't model this realization step, so it over-claims `direct`.
//!
//! ## Production observation
//!
//! Reproduced on gaffer-private PR #159, which peeled 7 named owners
//! classified as `peelable_now` / `direct` by the analyzer. Four of
//! those seven were refused by the cycle gate when the destination
//! module was actually written into the spec. In every case the
//! refusal evidence was a single (or few) `anon_residual_sentinel →
//! <proposed_module>` cut edge that the analyzer didn't surface in
//! the `residual_owner_horizon` entry.
//!
//! ## Suggested fix family
//!
//! Either:
//!
//! 1. The proposer evaluates the candidate against the **post-peel**
//!    quotient `Q'` (re-running `build_module_quotient` over a
//!    hypothetical partition), so its SCC search runs over exactly
//!    the same graph the gate will see.
//! 2. The gate's invariant is documented and exposed in the
//!    proposer's predicate (the proposer learns to model the missing
//!    constraining adjacency directly).
//!
//! Option 1 is the SSOT-friendly choice — it shares one implementation
//! of "is this quotient realizable?" between the proposer and the
//! gate.
//!
//! ## How to flip when the fix lands
//!
//! Either:
//! - The proposer correctly reports `target_binding` as `Blocked` /
//!   `BlockedCycle` (no peelable singleton). Flip the proposer
//!   assertion to `assert_ne!(... Direct)` and the
//!   `evaluated_owner_sets` assertion to
//!   `assert_ne!(... PeelableNow)`. The gate-refusal assertion
//!   stays.
//! - Or the gate-rejection scenario goes away because the analyzer's
//!   model is unified with the materializer's, and `target_binding`
//!   actually CAN be peeled cleanly. In that case the gate-refusal
//!   assertion is the one to flip — it should become an
//!   `assert_entry_output` success.
//!
//! The fix author picks based on which side is wrong.

use analysis::{OwnerGraphReport, PeelCandidateStatus, ResidualOwnerPeelStatus};
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

/// Generic-naming chunk source: a pure top-level `target_binding`,
/// a side-effectful `bridge_binding`, and an anon at-init consumer
/// that eager-reads `target_binding`. `helper_module` exists so the
/// chunk has at least one already-extracted module — the residual
/// peel pipeline expects to do some peel work.
const CHUNK_SOURCE: &str = r#"const bridge_binding = (() => "bridge")();
const target_binding = (() => "t")();
console.log(target_binding);
const helper_binding = "helper";
console.log(helper_binding);
export { bridge_binding, target_binding, helper_binding };
"#;

/// Part 1 (proposer over-promise): with no spec assignment for
/// `target_binding`, the analyzer must report `target_binding` as
/// `Direct` on the residual horizon and emit a `PeelableNow`
/// singleton candidate. This is the bug — the analyzer claims the
/// peel is safe.
#[test]
fn direct_status_is_emitted_for_target_binding_despite_gate_refusal() {
    let mut opts = FixtureOpts::new(
        CHUNK_SOURCE,
        vec![logical_module(
            "helper_module",
            &[Member::new("helper_binding")],
        )],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // GREEN pin (post-fix): the analyzer no longer emits the
    // singleton candidate as PeelableNow. With the proposer now
    // routed through the realizability primitive (see
    // `devinfra/js/debundle/realizability.rs` and the proposer
    // reroute in `peelability.rs::evaluate_peel_candidate`), the
    // proposer recognizes that moving `target_binding` alone would
    // leave the at-init consumer with a constraining read into the
    // residual source destination — surfaced as
    // `BlockedResidualDependency` (the proposer's layered clause-1
    // check) rather than `PeelableNow`.
    let target_candidate = peelability
        .evaluated_owner_sets
        .iter()
        .find(|c| c.members.len() == 1 && c.members[0].binding == "target_binding")
        .unwrap_or_else(|| {
            panic!(
                "evaluated_owner_sets must include singleton {{target_binding}}: {:#?}",
                peelability.evaluated_owner_sets,
            )
        });
    assert_ne!(
        target_candidate.status,
        PeelCandidateStatus::PeelableNow,
        "post-fix: proposer must not over-claim target_binding peelable_now \
         (it has a residual-dependency blocker on the at-init consumer). \
         candidate: {target_candidate:#?}",
    );

    // GREEN pin (post-fix): the residual horizon no longer
    // classifies target_binding as Direct.
    let horizon = peelability
        .residual_owner_horizon
        .iter()
        .find(|owner| owner.members.iter().any(|m| m.binding == "target_binding"))
        .unwrap_or_else(|| {
            panic!("residual_owner_horizon must include target_binding: {peelability:#?}")
        });
    assert_ne!(
        horizon.status,
        ResidualOwnerPeelStatus::Direct,
        "post-fix: horizon must not classify target_binding as Direct. \
         horizon entry: {horizon:#?}",
    );
}

/// Part 2 (gate refusal): with the same source, but a spec that
/// assigns `target_binding` to its own module `mod_target`, the
/// cycle gate refuses the spec with `anon_residual_sentinel →
/// mod_target` cycle evidence. This is the bug's other half — the
/// peel the analyzer said was safe is in fact refused at
/// materialization.
#[test]
fn gate_refuses_target_binding_peel_with_sentinel_evidence() {
    let mut opts = FixtureOpts::new(
        CHUNK_SOURCE,
        vec![
            logical_module("helper_module", &[Member::new("helper_binding")]),
            logical_module("mod_target", &[Member::new("target_binding")]),
        ],
    );
    opts.unassigned_mode = unassigned_mode_inline();

    // RED pin: the gate must reject this spec with cycle evidence
    // naming `mod_target` and the chunk's anon residual sentinel
    // (`static/app::anon_residual_sentinel` per `logical_modules.rs`
    // sentinel construction). When the proposer's predicate is
    // fixed, either:
    // - the gate stops refusing and this assertion needs to flip to
    //   `run_fixture` + `assert_entry_output`, OR
    // - the proposer correctly reports `target_binding` as not
    //   peelable, in which case the gate-refusal assertion in Part 2
    //   stays (the bug is in the proposer, not the gate).
    expect_rejection_containing_all(opts, &["cycle", "mod_target", "anon_residual_sentinel"]);
}
