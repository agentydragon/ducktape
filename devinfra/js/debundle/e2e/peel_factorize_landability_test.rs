//! End-to-end pinning of `peel_factorize`'s `landable_today` contract
//! against the materializer's actual gates.
//!
//! The factorizer's `landable_today: true` verdict is supposed to mean
//! "mechanically promoting this cell to an active YAML would pass the
//! materializer's cycle + emit-resolvability gates without spec-level
//! surgery." Earlier rounds of the factorizer marked cells landable
//! that the materializer then rejected — that drift is exactly what
//! this test pins against.
//!
//! Two paired fixtures:
//!
//! 1. **Clean cell** — bindings whose body references only entry-
//!    exported targets. Factorizer says landable; the corresponding
//!    spec edit DOES land green.
//! 2. **Emit-blocked cell** — a residual binding's body references
//!    another residual binding that isn't on entry's export list.
//!    Factorizer says NOT landable with a specific
//!    `emit_blocked_residual_bindings` entry; the corresponding
//!    spec edit DOES get rejected by the materializer with the
//!    matching error message.

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
fn factorizer_marks_clean_cell_landable_and_materializer_accepts_the_promotion() {
    // Source: three `const` initializers chained by at-init reads
    // (b reads a, c reads b). Only `a` is logical-module-claimed;
    // {b, c} sits residual and the factorizer should agglomerate
    // them into one cell whose only outgoing constraining edge
    // targets the active module `anchors/a`. Both gates pass:
    //
    // * No outgoing edge to another residual cell → cycle gate.
    // * `b → a` references `a`, which is auto-exported by entry
    //   because its owner currently lives in an active module →
    //   emit-resolvability gate.
    //
    // The mechanically-applied promotion spec then materializes
    // green.
    let chunk_source = r#"const a = 1;
const b = a + 1;
const c = b + 2;
export { a, b, c };
"#;

    let mut opts = FixtureOpts::new(
        chunk_source,
        vec![logical_module("anchors/a", &[Member::new("a")])],
    );
    // The factorizer's residual scope is `ModuleId::ResidualEntry`
    // exclusively (matches the analyzer's SSOT predicate). The
    // fixture's default `include_residual: true` would emit a
    // `Logical(R)` residual catch-all instead, putting `b` and `c`
    // in a logical module rather than the residual entry.
    opts.include_residual = false;
    let fixture = run_fixture(opts);
    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let report = factorize(&graph, &BTreeMap::new(), &BTreeMap::new(), 2000);

    let cell = report
        .proposals
        .iter()
        .find(|p| {
            p.binding_ids.contains(&"b".to_string()) && p.binding_ids.contains(&"c".to_string())
        })
        .expect("factorizer should agglomerate b and c into one cell");
    assert!(
        cell.landable_today,
        "clean cell whose only outgoing edges target active modules \
         must be marked landable_today; got cell={cell:?}",
    );
    assert!(
        cell.emit_blocked_residual_bindings.is_empty(),
        "clean cell must have empty emit_blocked_residual_bindings; \
         got {:?}",
        cell.emit_blocked_residual_bindings,
    );
    // `a` is in an active module → not counted as a residual edge.
    assert_eq!(cell.edges_to_other_residual_cells, 0);

    // Now mechanically apply the cell as an active module and verify
    // the materializer accepts it.
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
fn factorizer_auto_grows_cell_to_absorb_emit_blocked_residual_binding() {
    // `dep` is residual and NOT in entry's `export { ... }` list.
    // `consumer` lazily reads `dep` (inside its body). Promoting
    // `consumer` alone to an active module would require entry to
    // emit `import { dep } from "entry"` — but entry doesn't
    // export `dep`. The analyzer's auto-grow loop absorbs `dep`
    // into `consumer`'s cell, turning it into a single landable
    // proposal {consumer, dep} that moves both bindings together.
    //
    // `anchor` exists so the chunk has at least one active
    // logical module (the spec rejects all-residual chunks);
    // `dep` and `consumer` stay in the residual entry via
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

    let cell = report
        .proposals
        .iter()
        .find(|p| p.binding_ids.contains(&"consumer".to_string()))
        .expect("factorizer should propose a cell containing `consumer`");
    assert!(
        cell.binding_ids.contains(&"dep".to_string()),
        "auto-grow should absorb `dep` (consumer's only free reference) \
         into consumer's cell; got cell={cell:?}",
    );
    assert!(
        cell.landable_today,
        "the absorbed {{consumer, dep}} cell has no remaining \
         non-exported free references; must be marked landable_today; \
         got cell={cell:?}",
    );
    assert!(
        cell.emit_blocked_residual_bindings.is_empty(),
        "after auto-grow `dep` is internal to the cell so \
         emit_blocked_residual_bindings must be empty; got {:?}",
        cell.emit_blocked_residual_bindings,
    );
}
