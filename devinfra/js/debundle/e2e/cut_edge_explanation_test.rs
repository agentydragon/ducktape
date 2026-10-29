//! Cut edges carry a typed `explanation` of why the gate treats them as
//! constraining (`CycleEdge::explanation`). This fixture exercises the
//! dataflow-aware S-chain, where a `sequenced` edge's cause names the
//! cell timeline relation and both endpoints' effect classes: a
//! write-after-write on one `globalThis` property between two
//! summarized statements, and an opaque barrier raised by an unknown
//! call.
//!
//! Source order: `alpha` and `beta` both write `globalThis.tag`
//! (`beta → alpha`, write-after-write), `probe` calls an unknown
//! function (not dataflow-summarizable: it depends on every prior
//! impure statement and becomes the barrier), and `gamma` writes the
//! same cell again (`gamma → beta` write-after-write, `gamma → probe`
//! after-barrier). The gate keeps one sequenced edge per module pair,
//! so `probe` gets its own module: the module graph is then
//! `b→a`, `c→a`, `c→b`, `a→b`, `a→c`, where every arc from or into
//! `mod_c` is a barrier edge and every `a`/`b` arc is the
//! write-after-write. Any feedback arc set that breaks the SCC cuts at
//! least one arc of each shape.

use debundle_e2e_support::*;
use serde_json::{Value, json};

fn dataflow_cycle_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"const alpha = (globalThis.tag = "first", "a");
const beta = (globalThis.tag = "second", "b");
const probe = probeHost(alpha);
const gamma = (globalThis.tag = "third", "g");
console.log(alpha, beta, probe, gamma, globalThis.tag);
export { alpha, beta, probe, gamma };
"#,
        vec![
            logical_module("mod_a", &[Member::new("alpha"), Member::new("gamma")]),
            logical_module("mod_b", &[Member::new("beta")]),
            logical_module("mod_c", &[Member::new("probe")]),
        ],
    )
    .with_dataflow_aware_s_chain()
}

fn cut_edges(cycles: &[Value]) -> Vec<&Value> {
    cycles
        .iter()
        .flat_map(|scc| scc["cut"].as_array().expect("SCC cut"))
        .collect()
}

fn sequenced_cause(edge: &Value) -> &Value {
    assert_eq!(edge["kind"], "sequenced", "{edge}");
    assert_eq!(edge["explanation"]["kind"], "sequenced", "{edge}");
    &edge["explanation"]["cause"]
}

fn conflicts(cause: &Value) -> &Vec<Value> {
    cause["conflicts"].as_array().expect("cell-order conflicts")
}

#[test]
fn cut_names_the_written_cell_and_both_summarized_endpoints() {
    let rejected = run_rejection_fixture(dataflow_cycle_fixture());
    let cycles = rejected.cycles();
    let tag_cell = json!({ "kind": "global_prop", "key": "tag" });
    let edge = cut_edges(&cycles)
        .into_iter()
        .find(|edge| {
            edge["kind"] == "sequenced"
                && conflicts(sequenced_cause(edge)).contains(&json!({
                    "kind": "write_after_write",
                    "cell": tag_cell,
                }))
        })
        .unwrap_or_else(|| panic!("no write-after-write cut edge on `tag`: {cycles:#?}"));
    let cause = sequenced_cause(edge);
    assert_eq!(cause["rule"], "cell_order", "{edge}");
    assert_eq!(cause["from"], json!({ "kind": "summarized" }), "{edge}");
    assert_eq!(cause["to"], json!({ "kind": "summarized" }), "{edge}");
    // Both endpoints are impure (`assign_or_update` on the global), and
    // the report names the target side too.
    assert_eq!(
        edge["sequenced_source_unit"]["purity"]["reasons"][0]["rule"], "assign_or_update",
        "{edge}"
    );
    assert_eq!(
        edge["explanation"]["target_source_unit"]["purity"]["reasons"][0]["rule"],
        "assign_or_update",
        "{edge}"
    );
}

#[test]
fn cut_names_the_opaque_barrier_and_its_unknown_call() {
    let rejected = run_rejection_fixture(dataflow_cycle_fixture());
    let cycles = rejected.cycles();
    let opaque = json!({
        "kind": "opaque_barrier",
        "causes": ["opaque_at_init_call"],
    });
    let edge = cut_edges(&cycles)
        .into_iter()
        .find(|edge| {
            edge["kind"] == "sequenced" && {
                let cause = sequenced_cause(edge);
                cause["from"] == opaque || cause["to"] == opaque
            }
        })
        .unwrap_or_else(|| panic!("no opaque-barrier cut edge: {cycles:#?}"));
    let cause = sequenced_cause(edge);
    assert_eq!(cause["rule"], "cell_order", "{edge}");
    // The barrier is `probe`; its purity reason is the unknown call that
    // made it non-summarizable, and the same unresolved call left its
    // synchronous execution incomplete. Which side it sits on depends on
    // the module pair the feedback arc set picked.
    let (barrier_cause, conflict) = if cause["from"] == opaque {
        (
            &edge["sequenced_source_unit"],
            json!({ "kind": "barrier_after_prior" }),
        )
    } else {
        (
            &edge["explanation"]["target_source_unit"],
            json!({ "kind": "after_barrier" }),
        )
    };
    assert!(conflicts(cause).contains(&conflict), "{edge}");
    assert_eq!(barrier_cause["binding_names"], json!(["probe"]), "{edge}");
    assert_eq!(
        barrier_cause["synchronous_execution_complete"], false,
        "{edge}"
    );
    let reason = &barrier_cause["purity"]["reasons"][0];
    assert_eq!(reason["rule"], "unknown_call", "{edge}");
    assert_eq!(reason["detail"], "probeHost", "{edge}");
}

/// Array-content order (emitted in both S-chain modes; this fixture uses
/// the strict default): `count` observes the contents of `items` after
/// its allocation (`count → items`), and `back`'s `push` is an opaque
/// heap barrier over the live frontier, which includes the `marks`
/// allocation (`back → marks`). Each module pair's retained sequenced
/// edge therefore targets a pure array allocation that carries no
/// impure-initializer evidence; the edge still explains itself through
/// the retained heap-content cause.
#[test]
fn heap_content_cut_edges_explain_pure_allocation_targets() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        r#"const items = [];
const marks = [];
const count = items.length;
const back = (marks.push(count), "b");
console.log(items, marks, count, back);
export { items, marks, count, back };
"#,
        vec![
            logical_module("mod_a", &[Member::new("items"), Member::new("back")]),
            logical_module("mod_b", &[Member::new("marks"), Member::new("count")]),
        ],
    ));
    let cycles = rejected.cycles();
    let edges = cut_edges(&cycles);
    let allocation_use = edges
        .iter()
        .find(|edge| {
            edge["kind"] == "sequenced"
                && sequenced_cause(edge)["to"] == json!({ "kind": "heap_allocation" })
        })
        .unwrap_or_else(|| panic!("no cut edge targeting an allocation: {cycles:#?}"));
    let cause = sequenced_cause(allocation_use);
    assert_eq!(cause["rule"], "cell_order", "{allocation_use}");
    assert!(
        conflicts(cause).iter().any(|conflict| {
            (conflict["kind"] == "use_after_allocation"
                || conflict["kind"] == "frontier_after_cell")
                && conflict["cell"]["kind"] == "array_contents"
        }),
        "{allocation_use}"
    );
    // An empty array literal is pure: no impure-initializer evidence on
    // the target side, yet the explanation is present.
    assert!(
        allocation_use["explanation"]["target_source_unit"].is_null(),
        "{allocation_use}"
    );
    assert_eq!(cycles.len(), 1, "{cycles:#?}");
    assert!(
        edges.iter().all(|edge| !edge["explanation"].is_null()),
        "every cut edge explains itself: {cycles:#?}"
    );
}

#[test]
fn gate_cut_query_carries_the_same_explanations() {
    let rejected = run_rejection_fixture(dataflow_cycle_fixture());
    let graph = rejected
        .report_root
        .join("static/app/dependency_graph.json");
    let out = run_debundle(&[
        "gate",
        "cut",
        "0",
        "--graph",
        graph.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let report: Value = serde_json::from_slice(&out.stdout).expect("gate cut JSON");
    let cycles = rejected.cycles();
    let stored = cut_edges(&cycles);
    let queried = report["cut"].as_array().expect("cut");
    assert_eq!(
        queried
            .iter()
            .map(|e| &e["explanation"])
            .collect::<Vec<_>>(),
        stored.iter().map(|e| &e["explanation"]).collect::<Vec<_>>(),
        "{report}"
    );
    // Every constraining edge in a dataflow-mode cut explains itself:
    // sequenced edges through the cell timeline, the `probe → alpha`
    // argument read as a direct at-init read.
    assert!(
        queried.iter().all(|edge| match edge["kind"].as_str() {
            Some("sequenced") => edge["explanation"]["cause"]["rule"] == "cell_order",
            Some("eager_use") =>
                edge["explanation"]
                    == json!({ "kind": "at_init_read", "position": { "kind": "direct" } }),
            _ => false,
        }),
        "{report}"
    );
}
