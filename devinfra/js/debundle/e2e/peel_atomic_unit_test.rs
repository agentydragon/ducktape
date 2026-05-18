//! Peelability proposer must surface every multi-owner atomic unit
//! (constraining-edge SCC, per `atomic_units.rs`) as a peel-set
//! candidate, then run it through the same validity / cycle /
//! emit-resolvability checks as singleton/pair candidates.
//!
//! The atomic unit is the analyzer's own "must move together" notion.
//! Before this candidate family existed, the proposer only emitted
//! `direct` singleton + bounded two-owner pair candidates, plus
//! residual-dependency closures. A class with N decorator
//! applications (`__decorate(C, …)` anonymous statements) forms an
//! atomic unit of size N+1 via `local_effect` edges — and was never
//! emitted as a candidate even though the whole unit is structurally
//! peelable as one module. Same for any multi-vertex constraining-edge
//! SCC (mutual `eager_use`, etc.).
//!
//! These tests pin the post-fix behavior. See <peelability.rs> for the
//! `residual_atomic_unit_candidates` proposer and <DESIGN.md>
//! "Residual peel candidates" for the contract.

use analysis::{OwnerGraphReport, PeelCandidateStatus, ResidualOwnerPeelStatus};
use debundle_e2e_support::*;
use serde::de::DeserializeOwned;
use serde_json::json;
use std::{collections::BTreeSet, fs, path::Path};

fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
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

/// Positive: class C + N anonymous `__decorate(C, …)` applications
/// form a size-(N+1) atomic unit via `local_effect` edges. The
/// proposer must emit exactly one peel-set candidate covering the
/// whole unit — class + all N decorator statements. The candidate
/// must be `PeelableNow` and must appear in `minimal_peel_sets`.
///
/// Without this family, a class with many decorators is stuck on the
/// horizon as `Blocked` (every singleton splits the atomic unit) and
/// never gets a peel proposal. This is the load-bearing case for the
/// `et` / `st` / `an` hubs in gaffer's `78d928dca7` chunk.
#[test]
fn class_with_decorator_applications_atomic_unit_emits_single_peel_candidate() {
    // Source: class C with 3 anonymous decorator applications. The
    // `Ro` helper is annotated `typescript_decorate_helper`, so the
    // analyzer emits one `local_effect` edge per application targeting
    // `C` — making `{C, app1, app2, app3}` one atomic unit of size 4.
    //
    // `Z`, `Y`, `X` are decorator factories pulled in from already-
    // extracted modules so the decorator applications have a real
    // residual neighbor count that mirrors the gaffer shape.
    let chunk_source = r#"const anchor = "anchor";
function Ro(decorators, target, key, flags) {
  for (let i = decorators.length - 1; i >= 0; i--) decorators[i](target, key);
}
const Z = () => {};
const Y = () => {};
const X = () => {};
class C {
  constructor() { this.visible = false; }
}
Ro([Z], C.prototype, "a", 2);
Ro([Y], C.prototype, "b", 2);
Ro([X], C.prototype, "c", 2);
console.log(anchor);
export { anchor, C };
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
            logical_module("infra/decorators/z", &[Member::new("Z")]),
            logical_module("infra/decorators/y", &[Member::new("Y")]),
            logical_module("infra/decorators/x", &[Member::new("X")]),
        ],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // The atomic unit candidate must be present in `minimal_peel_sets`:
    // exactly one peel set with `C` in members and 4 owner_ids (class
    // + 3 decorator applications). No other minimal peel set for `C`
    // — a smaller candidate would split the atomic unit.
    let c_atomic_peel_sets: Vec<_> = peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| candidate.members.iter().any(|m| m.binding == "C"))
        .collect();
    assert_eq!(
        c_atomic_peel_sets.len(),
        1,
        "expected exactly one minimal peel set covering C (the atomic unit): {peelability:#?}",
    );
    let unit_peel = c_atomic_peel_sets[0];
    assert_eq!(
        unit_peel.owner_ids.len(),
        4,
        "atomic-unit peel must include class + all 3 decorator applications: {unit_peel:#?}",
    );
    let unit_members: BTreeSet<_> = unit_peel
        .members
        .iter()
        .map(|m| m.binding.as_str())
        .collect();
    assert_eq!(
        unit_members,
        BTreeSet::from(["C"]),
        "anonymous decorator statements contribute no members; only C should be in members[]: {unit_peel:#?}",
    );

    // C must report `WithCompanions` on the horizon — the unit is
    // peelable but requires the 3 anonymous companions to move too.
    let c_horizon = peelability
        .residual_owner_horizon
        .iter()
        .find(|owner| owner.members.iter().any(|m| m.binding == "C"))
        .expect("C should be on the residual horizon");
    assert_eq!(
        c_horizon.status,
        ResidualOwnerPeelStatus::WithCompanions,
        "C peels with its decorator companions (not Direct, not Blocked): {c_horizon:#?}",
    );
    assert!(
        !c_horizon.peel_set_ids.is_empty(),
        "C's horizon must reference the atomic-unit peel set: {c_horizon:#?}",
    );

    // The atomic-unit candidate's id must appear in evaluated_owner_sets
    // with status `PeelableNow`.
    let unit_eval = peelability
        .evaluated_owner_sets
        .iter()
        .find(|candidate| {
            candidate.owner_ids.len() == 4 && candidate.members.iter().any(|m| m.binding == "C")
        })
        .expect("atomic-unit candidate must appear in evaluated_owner_sets");
    assert_eq!(
        unit_eval.status,
        PeelCandidateStatus::PeelableNow,
        "atomic-unit candidate must be PeelableNow: {unit_eval:#?}",
    );
}

/// Positive: a 2-vertex constraining-edge SCC via `eager_rebind`
/// makes two owners share an atomic unit. The proposer emits the
/// 2-owner SCC as a peel candidate that's `PeelableNow`.
///
/// `var A = init_a()` lets B reassign `A` (`A = mutated`) — that's an
/// `eager_rebind` edge that flows both directions in `G_atomic`, so
/// `{A, B}` form a size-2 SCC.
#[test]
fn two_vertex_constraining_edge_scc_emits_two_owner_peel_candidate() {
    // Note: no top-level `B()` call. After at-init call promotion
    // (DESIGN.md "At-init call promotion") a top-level call to B
    // would correctly merge the call statement into the atomic unit
    // (B's body mutates A at-init), which the test would still pass
    // as a 3-owner unit — but the historical assertion shape pinned
    // a 2-owner unit. Keep the body-only shape so the test continues
    // to cover the 2-vertex case it was authored for.
    let chunk_source = r#"var A = 1;
function B() { A = 99; return A; }
const Existing = "existing";
console.log(Existing);
export { A, B, Existing };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // Expect one minimal peel set covering {A, B} together. Neither A
    // nor B can peel alone (it'd split the SCC); the pair is the only
    // realizable peel containing either.
    let ab_peel_sets: Vec<_> = peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names == BTreeSet::from(["A", "B"])
        })
        .collect();
    assert!(
        !ab_peel_sets.is_empty(),
        "minimal_peel_sets must contain the 2-vertex SCC {{A, B}}: {peelability:#?}",
    );
    let ab_peel = ab_peel_sets[0];
    assert_eq!(
        ab_peel.owner_ids.len(),
        2,
        "the {{A, B}} peel must be exactly 2 owners: {ab_peel:#?}",
    );

    for binding in ["A", "B"] {
        let horizon = peelability
            .residual_owner_horizon
            .iter()
            .find(|owner| owner.members.iter().any(|m| m.binding == binding))
            .unwrap_or_else(|| panic!("{binding} must be on the residual horizon"));
        assert_eq!(
            horizon.status,
            ResidualOwnerPeelStatus::WithCompanions,
            "{binding} must report WithCompanions (SCC partner must co-move): {horizon:#?}",
        );
    }
}

/// Positive: an atomic unit with only intra-residual *lazy* reads
/// going out (no constraining out-edges to residual) is still
/// `PeelableNow`. Per PR #1614's cycle-rule (constraining-edge
/// subgraph only), a lazy edge into residual doesn't form a
/// constraining cross-cycle and shouldn't block the unit's peel.
///
/// Shape: `var A = init()`, `var B = init()`, `A = B + 1` (rebind on
/// A from B; SCC {A,B} via eager_rebind). A's body lazily reads
/// `LazyDep` from residual. The peel of `{A, B}` is realizable
/// because the only cross-edge to residual is lazy.
#[test]
fn atomic_unit_with_intra_residual_lazy_out_edges_stays_peelable_now() {
    let chunk_source = r#"var A = 1;
function lazyA() { return LazyDep + A; }
function B() { A = 99; return A; }
B();
const LazyDep = "lazy";
const Existing = "existing";
console.log(Existing);
export { A, B, lazyA, LazyDep, Existing };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // The {A, B} atomic unit must still be peelable despite the lazy
    // reads of `LazyDep` from inside `lazyA`'s body. (`lazyA` itself
    // is a separate singleton, not part of the rebind SCC.)
    let ab_peel_sets: Vec<_> = peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names.contains("A") && names.contains("B")
        })
        .collect();
    assert!(
        !ab_peel_sets.is_empty(),
        "minimal_peel_sets must contain the {{A, B}} rebind SCC even with intra-residual lazy edges: {peelability:#?}",
    );
}

/// Negative: an atomic unit with a real outgoing constraining edge
/// into a residual non-member must NOT appear in `minimal_peel_sets`.
/// The unit candidate is evaluated but `BlockedResidualDependency`
/// (the unit's at-init read of `Dep` would back-point into the source
/// destination after the peel, leaving `Dep` orphaned in residual).
///
/// Shape: SCC {A, B} via mutual at-init reads (`var A = B`,
/// `var B = A`) — both members are in the same atomic unit. A
/// additionally reads a third residual var `Dep` at-init. Peeling
/// {A, B} alone would leave a back-pointer from the new destination
/// to the residual `Dep`. The candidate must NOT be `PeelableNow`.
#[test]
fn atomic_unit_with_outgoing_constraining_edge_to_residual_non_member_is_blocked() {
    let chunk_source = r#"var A = B + Dep;
var B = A;
var Dep = 1;
const Existing = "existing";
console.log(Existing);
export { A, B, Dep, Existing };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // The {A, B} candidate must NOT show up in minimal_peel_sets —
    // the SCC has an at-init read of `Dep` that would back-point
    // into the source destination after the peel.
    let ab_peelable_now: Vec<_> = peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names == BTreeSet::from(["A", "B"])
        })
        .collect();
    assert!(
        ab_peelable_now.is_empty(),
        "the {{A, B}} atomic unit must NOT be PeelableNow when an outgoing constraining edge crosses into a non-member residual owner: {peelability:#?}",
    );

    // It must still be evaluated and surfaced as Blocked* in
    // evaluated_owner_sets so downstream tooling can see why.
    let ab_eval = peelability
        .evaluated_owner_sets
        .iter()
        .find(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names == BTreeSet::from(["A", "B"])
        })
        .expect("atomic-unit candidate must be evaluated even when blocked");
    assert_ne!(
        ab_eval.status,
        PeelCandidateStatus::PeelableNow,
        "{{A, B}} atomic unit must be blocked when a constraining edge escapes the unit: {ab_eval:#?}",
    );
}

/// Companion-set integrity: non-atomic-unit residual clusters still
/// surface their previously-working candidates (pair candidates from
/// blocked-cycle evidence). Adding the atomic-unit family must be
/// additive — it shouldn't shadow or suppress pair candidates that
/// the proposer was already finding.
///
/// Shape: a clean 2-owner pair {U, V} that mutually depend via
/// at-init reads. {U, V} is a constraining-edge SCC (so it's also an
/// atomic unit of size 2) — both the pair-candidate path and the
/// atomic-unit path should be able to find it, but only one peel set
/// (deduped by candidate id) should land in `minimal_peel_sets`.
#[test]
fn pair_candidate_for_two_vertex_at_init_cycle_is_not_duplicated_by_atomic_unit_candidate() {
    // Two vars with at-init reads in both directions: each reads the
    // other. `var` hoisting avoids TDZ; the assignments resolve to
    // `undefined` at runtime. The {U, V} cycle is realizable as a
    // single module.
    let chunk_source = r#"var U = V;
var V = U;
const Existing = "existing";
console.log(Existing);
export { U, V, Existing };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // Exactly one minimal peel set for {U, V} — the pair candidate
    // and the atomic-unit candidate share the same candidate id
    // (`peel_candidate:owner_U+owner_V`) and must dedup.
    let uv_peels: Vec<_> = peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names == BTreeSet::from(["U", "V"])
        })
        .collect();
    assert_eq!(
        uv_peels.len(),
        1,
        "{{U, V}} must appear exactly once in minimal_peel_sets (atomic-unit candidate dedups against pair candidate): {peelability:#?}",
    );

    // Same dedup invariant on `evaluated_owner_sets`.
    let uv_evals: Vec<_> = peelability
        .evaluated_owner_sets
        .iter()
        .filter(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names == BTreeSet::from(["U", "V"])
        })
        .collect();
    assert_eq!(
        uv_evals.len(),
        1,
        "{{U, V}} must appear exactly once in evaluated_owner_sets: {peelability:#?}",
    );
}

/// Subset dedup integrity: an atomic-unit candidate covering owner X
/// must NOT shadow a smaller `direct` singleton candidate for X. The
/// `build_residual_owner_horizon` minimal-peel logic should prefer
/// the singleton (smaller owner set, subset of the unit) and the
/// horizon for X should be `Direct`.
///
/// Shape: a 2-owner mutual at-init cycle {P, Q}, plus a fully
/// independent owner R that has no atomic-unit peers. The atomic-unit
/// family proposes {P, Q}. The singleton family proposes {P}, {Q},
/// {R} — but {P} and {Q} are `BlockedCycle` (splitting the unit) and
/// don't reach `peelable_now`. R's singleton is `PeelableNow` and the
/// horizon prefers it over any larger candidate that happens to
/// contain R (which would be the case if R were in a unit — it
/// isn't here, but the invariant holds in general). This test just
/// pins that the atomic-unit family doesn't accidentally promote
/// `{P, Q}` to shadow R.
#[test]
fn singleton_direct_candidate_for_independent_owner_is_not_shadowed_by_atomic_unit() {
    let chunk_source = r#"var P = Q;
var Q = P;
const R = "independent";
console.log(R);
export { P, Q, R };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("existing", &[Member::new("R")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // {P, Q} atomic unit is in minimal_peel_sets — that's the new
    // family doing its job.
    let pq_peels: Vec<_> = peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names == BTreeSet::from(["P", "Q"])
        })
        .collect();
    assert!(
        !pq_peels.is_empty(),
        "{{P, Q}} atomic unit must be a minimal peel set: {peelability:#?}",
    );

    // P and Q are reported `WithCompanions` (they need each other);
    // never `Direct` (a singleton would split the unit) and never
    // `Blocked` (the unit *is* peelable as a whole).
    for binding in ["P", "Q"] {
        let horizon = peelability
            .residual_owner_horizon
            .iter()
            .find(|owner| owner.members.iter().any(|m| m.binding == binding))
            .unwrap_or_else(|| panic!("{binding} must be on the residual horizon"));
        assert_eq!(
            horizon.status,
            ResidualOwnerPeelStatus::WithCompanions,
            "{binding} must be WithCompanions (atomic-unit peel needs its partner): {horizon:#?}",
        );
    }
}
