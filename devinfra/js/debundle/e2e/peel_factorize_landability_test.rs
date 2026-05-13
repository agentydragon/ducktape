//! End-to-end pinning of the factorize report's correctness against
//! the materializer's actual gates.
//!
//! The closure-based factorizer (`analysis::factorize`) emits a cell
//! per SCC of the residual must-co-locate graph. Each cell carries a
//! verdict from the SSOT `evaluate_residual_peel_candidate` predicate;
//! cells are valid by construction in the emit-resolvability and
//! LazyRebind senses, with cycle/dep blockers reported per cell.
//!
//! Two paired fixtures:
//!
//! 1. **Clean cell** — bindings whose body references only entry-
//!    exported targets. Each binding gets its own landable cell
//!    (no agglomeration step combines them); each is individually
//!    promotable.
//! 2. **Emit-blocked cell** — a residual binding's body references
//!    another residual binding that isn't on entry's export list.
//!    The factorizer reports the consumer's cell as not landable,
//!    with the unresolved binding listed in
//!    `emit_blocked_residual_bindings`. Promoting the consumer
//!    standalone would be rejected by the materializer; the lane
//!    worker resolves by promoting the declarer's cell first or
//!    combining both into one module.

use analysis::OwnerGraphReport;
use debundle_e2e_support::*;
use peel_factorize::factorize;
use serde::de::DeserializeOwned;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

#[test]
fn factorizer_orders_chain_cells_by_dependency_and_materializer_accepts_promotion() {
    // Source: three `const` initializers chained by at-init reads
    // (b reads a, c reads b). Only `a` is logical-module-claimed;
    // {b, c} sit residual.
    //
    // The closure-based factorizer treats `c → b` as a dependency
    // (c needs b first). The materializer's SSOT predicate flags
    // cell {c} as `BlockedResidualDependency` because c has an
    // outgoing constraining edge into residual_entry — even though
    // b's binding is in entry's pre-existing exports, the predicate
    // is conservative about S → residual_entry module edges. The
    // factorize report surfaces this honestly: cell {b} is
    // landable_today (no outgoing residual deps), cell {c} is not
    // (depends on b). A lane worker resolves by promoting both
    // into one combined module; the materializer accepts.
    let chunk_source = r#"const a = 1;
const b = a + 1;
const c = b + 2;
export { a, b, c };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/a", &[Member::new("a")])],
    );
    opts.include_residual = false;
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &BTreeMap::new(), &BTreeMap::new(), 2000);

    let cell_b = report
        .proposals
        .iter()
        .find(|p| p.binding_ids.contains(&"b".to_string()))
        .expect("factorizer should propose a cell for `b`");
    assert!(
        cell_b.landable_today,
        "b's cell only reads from the active module `anchors/a` and \
         must be marked landable_today; got cell={cell_b:?}",
    );

    let cell_c = report
        .proposals
        .iter()
        .find(|p| p.binding_ids.contains(&"c".to_string()))
        .expect("factorizer should propose a cell for `c`");
    assert!(
        !cell_c.landable_today,
        "c's cell has an outgoing constraining edge into residual_entry \
         (c reads b, b stays residual), so the predicate flags it as \
         BlockedResidualDependency. Cell should NOT be landable_today \
         on its own; got cell={cell_c:?}",
    );

    // The materializer accepts the lane-worker decision to promote
    // both into one combined module.
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
fn factorizer_flags_emit_blocked_cell_not_landable_with_blocker_binding() {
    // `dep` is residual and NOT in entry's `export { ... }` list.
    // `consumer` lazily reads `dep` (inside its body). The closure
    // graph adds `consumer → dep` (consumer reads non-exported
    // residual binding), but there's no back-edge from dep to
    // consumer — so Tarjan keeps them as separate SCCs. consumer's
    // cell has one outgoing inter-cell forcing edge in the
    // condensation DAG, and the verifier flags the cell as
    // `BlockedEmitResolvability` with `dep` listed as the blocker.
    // Promoting consumer's cell alone would be rejected; a lane
    // worker resolves by promoting dep's cell first (or co-promoting
    // both into one module).
    //
    // `anchor` exists so the chunk has at least one active logical
    // module (the spec rejects all-residual chunks); `dep` and
    // `consumer` stay in the residual entry via
    // `include_residual: false`.
    let chunk_source = r#"const anchor = "anchor";
const dep = "secret";
function consumer() { return dep; }
export { anchor, consumer };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/anchor", &[Member::new("anchor")])],
    );
    opts.include_residual = false;
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &BTreeMap::new(), &BTreeMap::new(), 2000);

    let consumer_cell = report
        .proposals
        .iter()
        .find(|p| p.binding_ids.contains(&"consumer".to_string()))
        .expect("factorizer should propose a cell for `consumer`");
    assert!(
        !consumer_cell.landable_today,
        "cell whose body references the non-exported residual binding \
         `dep` must NOT be marked landable_today; got cell={consumer_cell:?}",
    );
    assert!(
        consumer_cell
            .emit_blocked_residual_bindings
            .iter()
            .any(|b| b == "dep"),
        "emit_blocked_residual_bindings should list `dep` (the free \
         reference target). Got {:?}",
        consumer_cell.emit_blocked_residual_bindings,
    );

    let dep_cell = report
        .proposals
        .iter()
        .find(|p| p.binding_ids.contains(&"dep".to_string()))
        .expect("factorizer should propose a separate cell for `dep`");
    assert!(
        dep_cell.landable_today,
        "dep's cell has no outgoing forcing edges and must be \
         landable_today on its own; got cell={dep_cell:?}",
    );
}
