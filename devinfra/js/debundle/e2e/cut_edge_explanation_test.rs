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

use debundle_e2e_graph_support::{FixtureGraphExt, assert_cycle_edge_source_units};
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

fn heap_frontier_cycle_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"const first = beforeHost();
const captured = [0];
const composed = [0];
function touch() { composed[0] = 1; }
const probe = (captured[0] = 1, probeHost(touch()));
const last = afterHost();
console.log(first, probe, last);
export { first, probe, last };
"#,
        vec![
            logical_module("mod_a", &[Member::new("first"), Member::new("last")]),
            logical_module(
                "mod_b",
                &[
                    Member::new("captured"),
                    Member::new("composed"),
                    Member::new("touch"),
                    Member::new("probe"),
                ],
            ),
        ],
    )
    .with_dataflow_aware_s_chain()
}

fn pure_wrapper_heap_frontier_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"import { observe } from './peer.js';
const alpha = (globalThis.tag = "first", "a");
const beta = (globalThis.tag = "second", "b");
const captured = [0];
const observed = observe(() => captured[0] = 1);
const gamma = (globalThis.tag = "third", "g");
console.log(alpha, beta, observed, gamma, globalThis.tag);
export { alpha, beta, observed, gamma };
"#,
        vec![
            logical_module("mod_a", &[Member::new("alpha"), Member::new("gamma")]),
            logical_module("mod_b", &[Member::new("beta")]),
            logical_module(
                "mod_c",
                &[
                    Member::new("captured"),
                    Member::new("observe").with_purity(MemberPurity::Pure),
                    Member::new("observed"),
                ],
            ),
        ],
    )
    .with_extra_chunks(&[(
        "static/peer",
        "export function observe(callback) { return foreignObserver(callback); }\n",
    )])
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

    // Keep the original cut fixture stable: adding captured arrays changes
    // its binding timelines and therefore which edges the feedback cut picks.
    let heap_rejected = run_rejection_fixture(heap_frontier_cycle_fixture());
    let heap_graph = heap_rejected.dependency_graph();
    let probe_node = heap_graph
        .nodes
        .iter()
        .find(|node| node.declared_bindings.iter().any(|b| b.binding == "probe"))
        .expect("probe source unit");
    let execution = serde_json::to_value(&probe_node.synchronous_execution)
        .expect("synchronous execution serializes");
    let heap = &execution["heap_frontier"];
    let own = &heap["own_at_init"];
    assert_eq!(own["opaque"], true, "{heap}");
    assert_eq!(own["writes"]["count"], 1, "{heap}");
    let own_writes = own["writes"]["sample"]
        .as_array()
        .expect("own write sample");
    assert_eq!(own_writes.len(), 1, "{heap}");
    assert_eq!(own_writes[0]["kind"], "array_contents", "{heap}");
    let composed = &heap["composed_synchronous_calls"];
    assert!(composed["writes"]["count"].as_u64().unwrap() > 0, "{heap}");
    let composed_writes = composed["writes"]["sample"]
        .as_array()
        .expect("composed write sample");
    assert!(
        composed_writes
            .iter()
            .any(|cell| !own_writes.contains(cell)),
        "local synchronous callee must contribute its own exact array cell: {heap}"
    );
    let non_assumed_writes = &heap["non_assumed_heap_accesses"]["writes"];
    assert!(non_assumed_writes["count"].as_u64().unwrap() > 0, "{edge}");
    assert!(
        non_assumed_writes["sample"]
            .as_array()
            .expect("non-assumed write sample")
            .iter()
            .all(|cell| composed_writes.contains(cell)),
        "non-assumed writes must be retained in the composed summary: {edge}"
    );
    for cells in [
        &own["allocations"],
        &own["reads"],
        &own["writes"],
        &composed["allocations"],
        &composed["reads"],
        &composed["writes"],
        &heap["non_assumed_heap_accesses"]["reads"],
        non_assumed_writes,
    ] {
        assert!(
            cells["sample"].as_array().unwrap().len() <= 8,
            "heap cell samples are bounded: {edge}"
        );
    }

    let graph_path = heap_rejected
        .report_root
        .join("static/app/dependency_graph.json.gz");
    let described = run_debundle(&[
        "gate",
        "describe",
        "0",
        "--graph",
        graph_path.to_str().unwrap(),
        "--format",
        "text",
    ]);
    assert!(
        described.status.success(),
        "{}",
        String::from_utf8_lossy(&described.stderr)
    );
    let described = String::from_utf8_lossy(&described.stdout);
    assert!(
        described.contains("heap frontier: own-at-init opaque=true"),
        "{described}"
    );
    assert!(described.contains("array contents #"), "{described}");

    let pure_rejected = run_rejection_fixture(pure_wrapper_heap_frontier_fixture());
    let pure_cycles = pure_rejected.cycles();
    assert_eq!(
        pure_cycles[0]["rejection"], "mutual_constraining_cycle",
        "adding heap endpoint context must not change the gate verdict: {pure_cycles:#?}"
    );
    let pure_graph = pure_rejected.dependency_graph();
    let pure_node = pure_graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|b| b.binding == "observed")
        })
        .expect("declared-pure wrapper source unit");
    let pure_endpoint = serde_json::to_value(pure_node).expect("source unit serializes");
    assert_eq!(pure_endpoint["purity"]["kind"], "pure", "{pure_endpoint}");
    assert_eq!(
        pure_endpoint["synchronous_execution"]["heap_frontier"]["own_at_init"]["opaque"], true,
        "{pure_endpoint}"
    );
    let pure_graph_path = pure_rejected
        .report_root
        .join("static/app/dependency_graph.json.gz");
    let pure_described = run_debundle(&[
        "gate",
        "describe",
        "0",
        "--graph",
        pure_graph_path.to_str().unwrap(),
        "--format",
        "text",
    ]);
    assert!(
        pure_described.status.success(),
        "{}",
        String::from_utf8_lossy(&pure_described.stderr)
    );
    let pure_described = String::from_utf8_lossy(&pure_described.stdout);
    assert!(
        pure_described.contains("heap frontier: own-at-init opaque=true"),
        "{pure_described}"
    );
}

/// Array-content order (emitted in both S-chain modes; this fixture uses
/// the strict default): `count` observes the contents of `items` after
/// its allocation (`count → items`), and `back`'s `push` is an opaque
/// heap barrier over the live frontier, which includes the `marks`
/// allocation (`back → marks`). Each module pair's retained sequenced
/// edge therefore targets a pure array allocation. Its endpoint context
/// carries the allocation's heap evidence independently of purity.
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
    let graph = rejected.dependency_graph();
    let edges = cut_edges(&cycles);
    let allocation_use = edges
        .iter()
        .find(|edge| {
            edge["kind"] == "sequenced"
                && sequenced_cause(edge)["to"] == json!({ "kind": "heap_allocation" })
        })
        .unwrap_or_else(|| panic!("no cut edge targeting an allocation: {cycles:#?}"));
    let target_id = allocation_use["to_source_unit_id"]
        .as_str()
        .expect("cut edge target source-unit ID");
    let target = graph
        .nodes
        .iter()
        .find(|node| node.id == target_id)
        .unwrap_or_else(|| {
            panic!("target {target_id} is not a graph source unit: {allocation_use}")
        });
    assert_eq!(
        serde_json::to_value(&target.purity).expect("purity serializes")["kind"],
        "pure"
    );
    assert_cycle_edge_source_units(&graph, allocation_use);
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
    // Pure allocations retain their heap evidence on the target side.
    let target_context = &allocation_use["explanation"]["target_source_unit"];
    assert_eq!(target_context["purity"]["kind"], "pure", "{allocation_use}");
    assert!(
        target_context["heap_frontier"]["own_at_init"]["allocations"]["sample"]
            .as_array()
            .expect("allocation cell sample")
            .iter()
            .any(|cell| cell["kind"] == "array_contents"),
        "{allocation_use}"
    );
    assert!(
        allocation_use.get("binding").is_none(),
        "sequenced edge has no target binding: {allocation_use}"
    );
    let describe = run_debundle(&[
        "gate",
        "describe",
        "0",
        "--graph",
        rejected
            .report_path("dependency_graph.json.gz")
            .to_str()
            .unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        describe.status.success(),
        "{}",
        String::from_utf8_lossy(&describe.stderr)
    );
    let described: Value = serde_json::from_slice(&describe.stdout).expect("gate describe JSON");
    let evidence_row = described["evidence"]
        .as_array()
        .expect("gate describe evidence")
        .iter()
        .find(|row| {
            row["kind"] == "sequenced"
                && row["from_source_unit_id"] == allocation_use["from_source_unit_id"]
                && row["to_source_unit_id"] == allocation_use["to_source_unit_id"]
        })
        .unwrap_or_else(|| panic!("gate describe omitted the pure-allocation edge: {described:#}"));
    assert_cycle_edge_source_units(&graph, evidence_row);
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
        .join("static/app/dependency_graph.json.gz");
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
