//! Regression coverage for the peelability proposer's "pure hub"
//! blocker.
//!
//! Before the fix, the peel-set proposer in `peelability.rs` computed
//! the post-peel quotient's cycle reachability over **all** module
//! edges — including lazy reads. That meant any pure, top-level
//! `function`/`var`/`class` declaration assigned to residual was
//! flagged `BlockedCycle` whenever it had **both** (a) at least one
//! incoming eager use from another residual owner and (b) any
//! intra-residual lazy out-edge. That shape describes the vast
//! majority of pure top-level helpers in a Tana web chunk. Concrete
//! gaffer-private owners that hit the bug on `78d928dca7`:
//!
//! | binding | kind | incoming eager_use |
//! |---------|------|--------------------|
//! | Se      | fn_decl    | 172            |
//! | et      | var_decl   |  83            |
//! | nt      | var_decl   |  82            |
//! | st      | class_decl |  80            |
//! | ot      | var_decl   |  79            |
//! | ft      | fn_decl    |  67            |
//! | cn / an | var_decl, class_decl | 46  |
//!
//! Per `DESIGN.md` "Valid peels and atomic modules": "Lazy read edges
//! are non-constraining: they still contribute imports, but a cycle
//! made entirely of lazy read edges is realizable." A cycle whose
//! constraining edges form a DAG (e.g. `residual → P` eager + `P →
//! residual` lazy) is realizable: ESM evaluates the lazy/hoisted side
//! first, the eager side never observes a TDZ. The fix restricts the
//! post-peel SCC reachability to the **constraining-edge subgraph**
//! of the quotient — consistent with `compute_atomic_units`, which
//! already builds `G_atomic` from constraining edges only.

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

fn owner_graph(fixture: &Fixture) -> OwnerGraphReport {
    read_json(&fixture.report_root.join("static/app/owner_graph.json"))
}

fn assert_direct_peel(graph: &OwnerGraphReport, binding: &str) {
    let peelability = &graph.peelability;
    let candidate = peelability
        .evaluated_owner_sets
        .iter()
        .find(|candidate| candidate.members.len() == 1 && candidate.members[0].binding == binding)
        .unwrap_or_else(|| {
            panic!(
                "evaluated_owner_sets should include singleton {{{binding}}}: {:#?}",
                peelability.evaluated_owner_sets,
            )
        });
    assert_eq!(
        candidate.status,
        PeelCandidateStatus::PeelableNow,
        "{{{binding}}} singleton must be PeelableNow (pure hub, no outgoing constraining edges): \
         {candidate:#?}",
    );

    let horizon = peelability
        .residual_owner_horizon
        .iter()
        .find(|owner| owner.members.iter().any(|m| m.binding == binding))
        .unwrap_or_else(|| {
            panic!("residual_owner_horizon must include {binding}: {peelability:#?}")
        });
    assert_eq!(
        horizon.status,
        ResidualOwnerPeelStatus::Direct,
        "{binding} must be Direct on the residual horizon: {horizon:#?}",
    );

    let singleton = peelability
        .minimal_peel_sets
        .iter()
        .find(|set| {
            set.owner_ids.len() == 1 && set.members.len() == 1 && set.members[0].binding == binding
        })
        .unwrap_or_else(|| {
            panic!(
                "minimal_peel_sets must include singleton {{{binding}}}: {:#?}",
                peelability.minimal_peel_sets
            )
        });
    assert_eq!(singleton.owner_ids.len(), 1);
}

fn assert_not_peelable(graph: &OwnerGraphReport, binding: &str) {
    let peelability = &graph.peelability;
    let candidate = peelability
        .evaluated_owner_sets
        .iter()
        .find(|candidate| candidate.members.len() == 1 && candidate.members[0].binding == binding)
        .unwrap_or_else(|| {
            panic!("evaluated_owner_sets should include singleton {{{binding}}}: {peelability:#?}")
        });
    assert_ne!(
        candidate.status,
        PeelCandidateStatus::PeelableNow,
        "{{{binding}}} must NOT be PeelableNow: {candidate:#?}",
    );

    assert!(
        !peelability.minimal_peel_sets.iter().any(|set| {
            set.owner_ids.len() == 1 && set.members.len() == 1 && set.members[0].binding == binding
        }),
        "minimal_peel_sets must NOT contain singleton {{{binding}}}: {:#?}",
        peelability.minimal_peel_sets,
    );
}

/// Canonical reproduction of the gaffer bug: pure `function`
/// declaration with many eager-use consumers AND an intra-residual
/// lazy out-edge. Before the fix the post-peel cycle reachability
/// walked the lazy out-edge into residual, observed the eager
/// in-edges back from residual, and flagged the candidate
/// `BlockedCycle` with hundreds of cycle blockers. After the fix:
/// the constraining-edge subgraph forms a DAG (residual → Se eager,
/// no constraining edges out of Se), so the candidate certifies as
/// `PeelableNow`.
#[test]
fn pure_fn_decl_hub_with_eager_consumers_and_lazy_intra_residual_out_is_direct_peelable() {
    // Five top-level consumers each eagerly read `Se` at init time
    // (`var ci = Se(...)` evaluates Se synchronously). `Se`'s body
    // lazily reads `Helper`, which stays in residual — that
    // lazy out-edge is the bug trigger. `Existing` is a logical
    // module so the pipeline does real peel work.
    let mut opts = FixtureOpts::new(
        r#"function Se(n) { return Helper(n); }
function Helper(n) { return n * 2; }
const c1 = Se(1);
const c2 = Se(2);
const c3 = Se(3);
const c4 = Se(4);
const c5 = Se(5);
const Existing = "existing";
console.log(Existing, c1, c2, c3, c4, c5);
export { Se, Helper, c1, c2, c3, c4, c5, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing 2 4 6 8 10\n");

    let graph = owner_graph(&fixture);
    assert_direct_peel(&graph, "Se");

    // Sanity-check the bug shape is actually exercised: Se has at
    // least 5 incoming eager_use edges from same-residual consumers
    // AND a lazy out-edge to a same-residual `Helper`. Without that
    // mixed-edge topology the test would be silently weakened.
    let se_node = graph
        .nodes
        .iter()
        .find(|n| n.declared_bindings.iter().any(|b| b.binding == "Se"))
        .expect("Se node");
    let se_id = se_node.id.clone();
    let eager_in: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| edge.target == se_id && edge.edge_kind == analysis::DepKind::EagerUse)
        .collect();
    assert!(
        eager_in.len() >= 5,
        "Se must have >= 5 incoming eager_use edges to reproduce the bug shape; got {}",
        eager_in.len(),
    );
    let lazy_out_to_residual_present = graph.edges.iter().any(|edge| {
        edge.source == se_id
            && !edge.constrains_init_order
            && graph
                .nodes
                .iter()
                .any(|n| n.id == edge.target && n.destination.residual)
    });
    assert!(
        lazy_out_to_residual_present,
        "Se must have at least one lazy out-edge to a same-residual owner to reproduce the bug shape",
    );
}

/// Pure `var` declaration hub: object-literal with method bodies that
/// lazily reference an intra-residual binding. The lazy intra-residual
/// out-edge plus the eager-use incoming edges from consumers is the
/// exact bug shape that caused `et` / `nt` / `ot` to surface as
/// `BlockedCycle` in the gaffer report.
#[test]
fn pure_var_decl_hub_with_eager_consumers_and_lazy_intra_residual_out_is_direct_peelable() {
    // `Et`'s method body lazily reads `Helper` (same residual), and
    // 5 consumers eagerly read `Et` at init.
    let mut opts = FixtureOpts::new(
        r#"const Et = { greet: (n) => Helper(n) };
function Helper(n) { return "hi " + n; }
const u1 = Et.greet("a");
const u2 = Et.greet("b");
const u3 = Et.greet("c");
const u4 = Et.greet("d");
const u5 = Et.greet("e");
const Existing = "existing";
console.log(Existing, u1, u2, u3, u4, u5);
export { Et, Helper, u1, u2, u3, u4, u5, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing hi a hi b hi c hi d hi e\n");

    let graph = owner_graph(&fixture);
    assert_direct_peel(&graph, "Et");
}

/// Pure `class` declaration hub: many eager `new` consumers, plus a
/// lazy method-body read into residual. Matches the gaffer
/// `st` / `an` case.
#[test]
fn pure_class_decl_hub_with_eager_consumers_and_lazy_intra_residual_out_is_direct_peelable() {
    let mut opts = FixtureOpts::new(
        r#"class St { hello() { return Helper(); } }
function Helper() { return "hello"; }
const i1 = new St().hello();
const i2 = new St().hello();
const i3 = new St().hello();
const i4 = new St().hello();
const i5 = new St().hello();
const Existing = "existing";
console.log(Existing, i1, i2, i3, i4, i5);
export { St, Helper, i1, i2, i3, i4, i5, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing hello hello hello hello hello\n");

    let graph = owner_graph(&fixture);
    assert_direct_peel(&graph, "St");
}

/// Negative: a pure top-level `var` declaration that nevertheless
/// has an OUTGOING constraining edge to a same-residual dependency
/// must NOT be reported as direct-peelable — the dep is the real
/// blocker. Verifies the fix doesn't over-correct by ignoring
/// genuine constraining out-edges.
#[test]
fn pure_var_decl_with_outgoing_constraining_to_residual_dep_is_not_direct_peelable() {
    // `Dep` is the prerequisite owner (stays in residual). `Hub` is
    // a `const` whose initializer eagerly reads `Dep` at module-init
    // time — that read is a constraining out-edge from Hub to a
    // same-residual owner, so {Hub} alone cannot peel (it would
    // leave a back-pointer from the new module to residual).
    let mut opts = FixtureOpts::new(
        r#"const Dep = { v: 7 };
const Hub = { base: Dep.v };
const c1 = Hub.base + 1;
const c2 = Hub.base + 2;
const c3 = Hub.base + 3;
const Existing = "existing";
console.log(Existing, c1, c2, c3);
export { Hub, Dep, c1, c2, c3, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing 8 9 10\n");

    let graph = owner_graph(&fixture);
    // `Hub`'s initializer reads `Dep` at init time → constraining
    // out-edge to a same-residual owner. Singleton {Hub} is
    // BlockedResidualDependency. It must not appear as a direct
    // singleton peel.
    assert_not_peelable(&graph, "Hub");
}

/// Negative: an impure top-level statement (calls an unknown function
/// with observable side-effects) must NOT be direct-peelable — the
/// impurity (side-effect-order edges) is the blocker. Verifies the
/// fix doesn't accidentally promote impure owners that have no
/// outgoing constraining read edge.
#[test]
fn impure_var_decl_is_not_direct_peelable() {
    // `console.log()` returns undefined and is a known side-effect.
    // The analyzer classifies `var Side = ...console.log(...)` as
    // `has_side_effect`, which seeds source-order sequenced edges
    // to bracketing top-level statements.
    let mut opts = FixtureOpts::new(
        r#"console.log("first");
var Side = (console.log("mid"), 42);
console.log("last");
const c1 = Side + 1;
const c2 = Side + 2;
const c3 = Side + 3;
const Existing = "existing";
console.log(Existing, c1, c2, c3);
export { Side, c1, c2, c3, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    let graph = owner_graph(&fixture);
    assert_not_peelable(&graph, "Side");
}

/// Companion-set integrity: a constraining 2-vertex cycle still
/// surfaces as a companion-set peel. Verifies the fix doesn't
/// displace the existing 2-owner closure path (the gaffer report
/// must still surface 2-owner sets where they're actually needed).
#[test]
fn constraining_two_vertex_cycle_still_emits_two_owner_companion_set() {
    // Two top-level `var`s with mutual eager reads form a real
    // constraining cycle. Neither is direct-peelable alone (cycle
    // would remain in the post-peel quotient between {A} or {B} and
    // residual), but the pair {A, B} together IS peelable.
    let mut opts = FixtureOpts::new(
        r#"var A = (() => B || 0)();
var B = (() => A || 0)();
const Existing = "existing";
console.log(Existing, A, B);
export { A, B, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);

    let graph = owner_graph(&fixture);
    let peelability = &graph.peelability;
    let pair_set = peelability.minimal_peel_sets.iter().find(|set| {
        set.owner_ids.len() == 2
            && set.members.len() == 2
            && set.members.iter().any(|m| m.binding == "A")
            && set.members.iter().any(|m| m.binding == "B")
    });
    assert!(
        pair_set.is_some(),
        "minimal_peel_sets should still surface the 2-owner {{A, B}} companion set: {:#?}",
        peelability.minimal_peel_sets,
    );
}
