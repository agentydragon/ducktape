//! Peelability analysis tests: lazy-only residual edges, empty-declared closure
//! overshoot, 3-vertex SCC recovery, and emit-resolvability blocking.

use analysis::{OwnerGraphReport, PeelCandidateStatus, ResidualOwnerPeelStatus};
use debundle_e2e_support::*;
use serde::de::DeserializeOwned;
use std::{collections::BTreeSet, fs, path::Path};

fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

// Regression-style failing fixture for the
// `candidate_has_residual_dependency` over-conservativeness
// at <peelability.rs:730>.
//
// Today, that rule marks a peel candidate
// `BlockedResidualDependency` when ANY outgoing owner edge
// targets an owner whose destination is residual — including
// `LazyUse` edges, which DO NOT constrain realizability per
// <graph.rs> (`EdgeReason::constrains_init_order`). Lazy
// reads against a residual neighbor are runtime-safe: the
// function body only fires after both the peeled module and
// the residual entry have finished evaluating, so there is no
// top-level cycle and no TDZ.
//
// The fixture below is the canonical synthetic shape:
// `Leaf` is a function whose body lazily reads `Dep`; `Dep`
// stays in the residual entry. `Leaf`'s only cross-destination
// owner edge is the lazy read of `Dep`. There is no at-init
// read, no write, and no side-effect-order edge between them.
// Singleton-`{Leaf}` is structurally fine to peel — leaving
// `Dep` in residual is allowed under realizability — yet
// today the rule blocks the candidate and forces the closure
// to absorb `Dep` (covered by
// `owner_graph_report_blocks_residual_entry_dependency_peel_candidate`
// in `realizability_test.rs`, which pins the current —
// over-conservative — behavior).
//
// Real-world manifestation: Tana's `FocusService` (eF) +
// `NativeFocusService` (jde) pair, with 17 lazy-only residual
// neighbors that block the peel even though the realizability
// cycle would unwind fine. See
// `gaffer-private/tana/x/modules/compiler/blockers.md`,
// section "candidate_has_residual_dependency blocks lazy-only
// edges", for the full diagnosis.
//
// Expected behavior (post-fix): the singleton `{Leaf}`
// candidate is `PeelableNow`, the residual horizon classifies
// `Leaf` as `Direct`, and `minimal_peel_sets` contains a
// `SingleOwner` entry for `Leaf` alone. This test asserts
// that — and consequently fails on current `devel`, where the
// rule still considers lazy edges.
#[test]
fn singleton_with_lazy_only_residual_edge_should_be_peelable_now() {
    // `Leaf` reads `Dep` only inside a function body — a lazy
    // read. `Dep` stays in residual (no logical_module covers
    // it). `Existing` exists so the chunk has at least one
    // already-extracted module, exercising the residual peel
    // pipeline.
    let mut opts = FixtureOpts::new(
        r#"function Leaf() { return Dep; }
const Dep = "dep";
const Existing = "existing";
console.log(Existing);
export { Leaf, Dep, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.include_residual = false;
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // Post-fix: singleton {Leaf} is `Direct` on the horizon —
    // peeling it alone is safe because the only cross-edge to
    // residual is a lazy read, which doesn't constrain
    // realizability.
    assert!(
        peelability.residual_owner_horizon.iter().any(|owner| {
            owner.members.len() == 1
                && owner.members[0].binding == "Leaf"
                && owner.status == ResidualOwnerPeelStatus::Direct
        }),
        "Leaf should be Direct-peelable (lazy-only edge to residual): {graph:#?}",
    );

    // Post-fix: minimal_peel_sets contains a `SingleOwner`
    // candidate covering `Leaf` alone — no companion needed.
    assert!(
        peelability.minimal_peel_sets.iter().any(|candidate| {
            candidate.owner_ids.len() == 1
                && candidate.members.len() == 1
                && candidate.members[0].binding == "Leaf"
        }),
        "minimal_peel_sets should include singleton {{Leaf}}: {graph:#?}",
    );
}

// Repro for the empty-declared-closure-overshoot blocker that
// prevents most blocked owners from getting peel candidates.
//
// Empirical observation against the live gaffer Tana graph
// (May 2026): of 5289 owners on the residual horizon, 4016 are
// reported `blocked` with empty `peel_set_ids` — i.e., the
// algorithm couldn't propose any peel candidate, not even a
// many-owner closure. Tracing one of them (owner:23, binding
// `TA` / `envConfig`) revealed:
//
// - Singleton {TA} is `BlockedResidualDependency` because TA has
//   a `sequenced` edge to owner:7 in residual.
// - Forward closure of TA's component is 3 owners: TA itself,
//   plus 2 side-effect-only statements with empty
//   `declared_bindings`.
// - `residual_dependency_closure_candidates` rejects the closure
//   at the representability check (every closure owner must have
//   a non-empty declared binding to put in `members[]`).
// - Net effect: TA reports `peel_set_ids: []` even though the
//   3-owner closure {TA, side_effect_a, side_effect_b} would be
//   structurally peelable as one module.
//
// This minimal fixture reproduces the failure shape: a top-level
// `var` declaration with a side-effectful initializer, sandwiched
// between two side-effect-only `console.log` statements. The
// analyzer emits side-effect-order edges that make the var's
// singleton candidate `BlockedResidualDependency`. Closure
// expansion includes the bracketing `console.log` statements,
// which have empty `declared_bindings`, so the closure is
// rejected and the var is reported with empty `peel_set_ids`.
//
// Expected behavior (post-fix): the algorithm should be able to
// propose a peel that moves the var together with the side-effect
// statements as one closure (probably as anonymous statements in
// the new module), or — alternatively — recognize that the
// side-effect-order constraint can be satisfied by the eventual
// ESM import order without co-moving and propose the singleton
// peel of the var alone. Either way, `peel_set_ids` should be
// non-empty for the var.
#[test]
fn singleton_blocked_only_by_side_effect_order_to_anonymous_owner_should_be_peelable() {
    // Source-order layout:
    //   1. console.log("a")          - side-effect statement, empty declared
    //   2. var X = (() => "x")();    - var_decl with side-effectful initializer
    //                                  → declares X, has_side_effect=True
    //   3. console.log("c")          - side-effect statement, empty declared
    //
    // The analyzer emits side-effect-order edges between the three
    // consecutive side-effect statements. X's singleton candidate
    // is BlockedResidualDependency because of those edges. The
    // closure expansion pulls in the bracketing console.log
    // statements, both empty-declared → closure rejected →
    // peel_set_ids empty.
    let mut opts = FixtureOpts::new(
        r#"console.log("a");
var X = (() => "x")();
const Existing = "existing";
console.log(Existing);
export { X, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.include_residual = false;
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // After the fix, X should NOT be `Blocked` with empty
    // peel_set_ids. It should either be `Direct` (singleton-peel
    // works because the s-edges can be satisfied by ESM load
    // order) or `WithCompanions` (the closure is proposed and
    // includes the side-effect statements as anonymous members).
    let x_horizon = peelability
        .residual_owner_horizon
        .iter()
        .find(|owner| owner.members.iter().any(|m| m.binding == "X"))
        .expect("X should appear on the residual horizon");
    assert!(
        x_horizon.status != ResidualOwnerPeelStatus::Blocked || !x_horizon.peel_set_ids.is_empty(),
        "X should have a peel candidate proposed, not be blocked with empty peel_set_ids: {x_horizon:#?}",
    );
}

// Regression fixture for N-owner SCC recovery via the
// residual-dependency-closure path in `peelability.rs`.
//
// Earlier analysis claimed the algorithm only handled 2-owner pair
// peels for cyclic blockers (see
// `gaffer-private/tana/x/research/hub_class_peel_blockers.md`).
// That was wrong: `residual_dependency_closure_candidates` runs
// Tarjan-SCC over the residual subgraph and proposes the SCC's
// closure as a multi-owner peel, including for 3+ vertex cases.
//
// This test pins the working behavior so future refactors don't
// regress it: a 3-vertex at-init read cycle is correctly proposed
// as a 3-owner peel candidate.
//
// Fixture: three top-level vars A, B, C such that
//     var A = B   (A reads B at-init)
//     var B = C   (B reads C at-init)
//     var C = A   (C reads A at-init)
// `var` hoisting avoids TDZ; the assignments resolve to `undefined`
// at runtime. The SCC is structurally clean (no out-edges to
// residual side-effect statements that would force the closure to
// grow), so the proposed peel is exactly {A, B, C}.
#[test]
fn three_vertex_constraining_scc_should_be_peelable_as_one_owner_closure() {
    // Three vars forming a 3-vertex at-init read cycle:
    //   A reads B   (var A = B)
    //   B reads C   (var B = C)
    //   C reads A   (var C = A)
    // `var` hoisting avoids TDZ; assignments run in order with
    // the still-uninitialized targets resolving to `undefined`,
    // so the bundle runs without crashing.
    //
    // The SCC has no out-edges to residual (no helper called
    // from inside, no console.log reading A/B/C from residual),
    // so the closure for the SCC is exactly {A, B, C}. The
    // algorithm correctly proposes this as a 3-owner peel
    // candidate in `minimal_peel_sets`.
    //
    // `Existing` is an already-extracted module so the residual
    // peel pipeline has work to do. `include_residual = false`
    // keeps the pipeline strict about what the chunk emits.
    let mut opts = FixtureOpts::new(
        r#"var A = B;
var B = C;
var C = A;
const Existing = "existing";
console.log(Existing);
export { A, B, C, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.include_residual = false;
    let fixture = run_fixture(opts);

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    // `minimal_peel_sets` contains an owner closure of size 3
    // covering exactly {A, B, C}: the whole constraining SCC is
    // proposed as one peel module by the closure path.
    let three_owner_scc_closures: Vec<_> = peelability
        .minimal_peel_sets
        .iter()
        .filter(|candidate| {
            let names: BTreeSet<_> = candidate
                .members
                .iter()
                .map(|m| m.binding.as_str())
                .collect();
            names == BTreeSet::from(["A", "B", "C"]) && candidate.owner_ids.len() == 3
        })
        .collect();
    assert!(
        !three_owner_scc_closures.is_empty(),
        "minimal_peel_sets should include the 3-owner SCC {{A, B, C}}: {graph:#?}",
    );

    // Each of A, B, C is on the residual horizon with a non-empty
    // `peel_set_ids` (status `WithCompanions`) referencing the
    // 3-owner closure.
    for binding in ["A", "B", "C"] {
        assert!(
            peelability.residual_owner_horizon.iter().any(|owner| {
                owner.members.len() == 1
                    && owner.members[0].binding == binding
                    && owner.status == ResidualOwnerPeelStatus::WithCompanions
                    && !owner.peel_set_ids.is_empty()
            }),
            "{binding} should be WithCompanions with non-empty peel_set_ids: {graph:#?}",
        );
    }
}

// E2E fixture for the post-peelability emit-resolvability
// projection added on top of `peelability.rs`.
//
// `materialize_logical_modules` rejects a peel that moves a body
// whose reads target residual entry binding(s) that aren't on
// entry's export list — see the bail at
// "moved module references residual entry binding(s) … not exported
// by entry". Before this filter, peelability didn't surface that
// constraint: a candidate could pass cycle/realizability checks and
// still get rejected when materialization actually ran.
//
// The fixture below is the canonical synthetic shape. `Helper` is a
// `function` whose body lazily reads the residual `Internal` const.
// `Internal` is NOT in the source's `export {}` set, so peeling
// `Helper` out of entry would produce a moved module that imports
// `Internal` from entry — but entry doesn't export it. The
// `evaluated_owner_sets[]` entry for the singleton {Helper}
// candidate must therefore have `status ==
// blocked_emit_resolvability` with `emit_blocked_residual_bindings`
// listing `Internal`, and `Helper` must NOT appear in
// `minimal_peel_sets[]`.
#[test]
fn unexported_residual_read_marks_candidate_blocked_emit_resolvability() {
    // `Helper` lazy-reads `Internal`, which stays in residual but is
    // not in the source-level `export { … }` list. `Existing` exists
    // so the chunk has at least one already-extracted module — the
    // pipeline expects to do *some* peel work.
    let mut opts = FixtureOpts::new(
        r#"function Helper() { return Internal; }
const Internal = "internal";
const Existing = "existing";
console.log(Existing);
export { Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.include_residual = false;
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;

    let helper_candidate = peelability
        .evaluated_owner_sets
        .iter()
        .find(|candidate| candidate.members.len() == 1 && candidate.members[0].binding == "Helper")
        .unwrap_or_else(|| {
            panic!("evaluated_owner_sets should include singleton {{Helper}}: {peelability:#?}")
        });

    assert_eq!(
        helper_candidate.status,
        PeelCandidateStatus::BlockedEmitResolvability,
        "{{Helper}} should be flagged blocked_emit_resolvability \
         (lazy read of unexported residual binding Internal): {peelability:#?}",
    );
    assert_eq!(
        helper_candidate.emit_blocked_residual_bindings,
        vec!["Internal".to_string()],
        "emit_blocked_residual_bindings should pinpoint Internal: {helper_candidate:#?}",
    );

    // The materializer would reject a {Helper} peel — make sure the
    // peelability projection mirrors that by NOT advertising it as a
    // minimal peel set.
    assert!(
        !peelability.minimal_peel_sets.iter().any(|candidate| {
            candidate.members.len() == 1 && candidate.members[0].binding == "Helper"
        }),
        "minimal_peel_sets must omit {{Helper}} when emit-resolvability blocks it: {peelability:#?}",
    );
}
