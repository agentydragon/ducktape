//! RED tests: the S-chain should consult per-statement dataflow
//! before emitting a `Sequenced` owner edge between two
//! consecutive impure top-level statements.
//!
//! Background. `graph.rs` (S-chain emission around lines
//! 469-478) walks every impure top-level statement in source
//! order and unconditionally emits
//!
//! ```ignore
//! raw_edges.push((curr, prev, EdgeReason::sequenced(curr.ord)));
//! ```
//!
//! This is the transitive reduction of the total order over
//! impure statements. It is sound (every realizable schedule
//! satisfies it), but it is maximally conservative — it
//! manufactures an init-order constraint between every adjacent
//! pair of impure statements regardless of whether they
//! actually interact via dataflow.
//!
//! ESM evaluates modules to completion in some linker-chosen
//! order. Two impure statements that touch disjoint pieces of
//! observable state are commutative across that boundary: the
//! relative source-order between them is irrelevant to anyone
//! else (no reader observes both an intermediate "after A,
//! before B" and a "after B, before A" state because there is
//! no shared cell to observe).
//!
//! Proposed refinement (the "full dataflow" S-chain): for each
//! impure top-level statement, compute its write set and read
//! set (rebinds + global property mutations + property writes;
//! reads from bindings + global properties); emit
//! `Sequenced(prev → curr)` only when
//!
//! ```text
//! prev.writes ∩ (curr.reads ∪ curr.writes) ≠ ∅
//! ```
//!
//! This is strictly a relaxation of the existing chain (we
//! never add an edge that wasn't there before), so soundness is
//! straightforward: any realizable schedule of the relaxed
//! graph is also realizable for the strict graph, and any
//! schedule violated by the strict graph but not the relaxed
//! one corresponds to a swap of two disjoint impure statements
//! — observably indistinguishable.
//!
//! ## Patterns covered
//!
//! Anonymized from a real over-conservative S-chain observed
//! in a large bundle's quotient cycle: 4 of 10 residual cut
//! edges were S-chain edges between owners that touched
//! disjoint state.
//!
//! 1. **Disjoint global property writes.** Two impure
//!    statements that each `globalThis.X = ...` distinct,
//!    independent property keys. Today: S-edge chains them.
//!    After fix: writes are `{globalThis.alpha}` vs
//!    `{globalThis.beta}` — disjoint — no edge.
//!
//! 2. **Fresh-local-only allocation after a global write.** A
//!    `globalThis.tag = ...` followed by a `new LocalClass()`
//!    or `Object.freeze({...})` call whose effect is confined
//!    to a fresh local value. Today: both are impure, S-edge
//!    chains them. After fix: the fresh-alloc statement's
//!    write set is just its own binding and its read set
//!    doesn't intersect `globalThis.tag` — no edge.
//!
//! 3. **Independent cross-module inits.** Two modules each
//!    construct a one-off instance of a local class whose
//!    constructor only touches `this`. Today: S-edge chains
//!    them. After fix: each statement writes only its own
//!    binding (plus the fresh instance); read sets are
//!    disjoint — no edge.
//!
//! ## Test shape
//!
//! Each test builds a fixture, inspects
//! `owner_graph.json::edges`, and asserts that no
//! `Sequenced` owner edge connects the two non-interacting
//! statements (in either direction). The fixtures themselves
//! run correctly under the materializer — the S-edge is a
//! spurious extra constraint, not a cycle-closer in
//! isolation. Real impact (the gaffer case) is when the
//! spurious edge happens to land inside an SCC of other
//! constraining edges and becomes a member of the cut.

use analysis::{DepKind, OwnerGraphReport};
use debundle_e2e_support::*;

/// Build a `FixtureOpts` with the dataflow-aware S-chain emission
/// enabled. Every test in this file opts in — the assertions are
/// against the relaxed S-edge set, which only appears when the
/// chunk's `dataflow_aware_s_chain` flag is `true`.
fn dataflow_opts<'a>(source: &'a str, logical_modules: Vec<LogicalModuleEntry>) -> FixtureOpts<'a> {
    FixtureOpts::new(source, logical_modules).with_dataflow_aware_s_chain()
}

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

fn sequenced_edges_between<'a>(
    graph: &'a OwnerGraphReport,
    a: &str,
    b: &str,
) -> Vec<&'a analysis::OwnerGraphEdgeReport> {
    graph
        .edges
        .iter()
        .filter(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && ((edge.source == a && edge.target == b)
                    || (edge.source == b && edge.target == a))
        })
        .collect()
}

#[test]
fn s_chain_skips_disjoint_global_property_writes() {
    // Two top-level impure statements, each writing a distinct
    // `globalThis.<key>` property. Today's S-chain links the
    // second statement to the first; with dataflow, the write
    // sets `{globalThis.alpha}` and `{globalThis.beta}` are
    // disjoint and the read sets are empty, so no S-edge is
    // warranted.
    let fixture = run_fixture(dataflow_opts(
        r#"const tagA = (globalThis.alpha = "alpha-val", "tag-a");
const tagB = (globalThis.beta = "beta-val", "tag-b");
console.log(tagA, tagB, globalThis.alpha, globalThis.beta);
export { tagA, tagB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("tagA")]),
            logical_module("mod_b", &[Member::new("tagB")]),
        ],
    ));
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let owner_a = owner_for_binding(&graph, "tagA");
    let owner_b = owner_for_binding(&graph, "tagB");
    let offending = sequenced_edges_between(&graph, owner_a, owner_b);
    assert!(
        offending.is_empty(),
        "no Sequenced edge should link `tagA`'s owner to `tagB`'s \
         owner: the two statements write disjoint globalThis \
         properties (alpha vs beta) and read no shared state, so \
         their relative order is unobservable to any third party. \
         Offending edges: {offending:#?}\n\nFull graph: {graph:#?}",
    );
}

#[test]
fn s_chain_skips_fresh_local_alloc_after_global_write() {
    // `tagA` writes a globalThis property; `boxedB` allocates a
    // fresh frozen object literal. The freeze is impure as a
    // call (Object.freeze can throw on a non-object), but its
    // observable effect is confined to the fresh local — no
    // outside cell is touched. Dataflow:
    //   tagA.writes  = {tagA, globalThis.tag}
    //   tagA.reads   = {}
    //   boxedB.writes = {boxedB}
    //   boxedB.reads  = {Object}
    // No intersection — no S-edge.
    let fixture = run_fixture(dataflow_opts(
        r#"const tagA = (globalThis.tag = "first", "tag-a");
const boxedB = Object.freeze({ kind: "fresh" });
console.log(tagA, boxedB.kind, globalThis.tag);
export { tagA, boxedB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("tagA")]),
            logical_module("mod_b", &[Member::new("boxedB")]),
        ],
    ));
    assert_entry_output(&fixture, "tag-a fresh first\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let owner_a = owner_for_binding(&graph, "tagA");
    let owner_b = owner_for_binding(&graph, "boxedB");
    let offending = sequenced_edges_between(&graph, owner_a, owner_b);
    assert!(
        offending.is_empty(),
        "no Sequenced edge should link `tagA` to `boxedB`: the \
         Object.freeze of a fresh literal touches no outside \
         state, so swapping the two statements is unobservable. \
         Offending edges: {offending:#?}\n\nFull graph: {graph:#?}",
    );
}

#[test]
fn s_chain_skips_independent_cross_module_constructor_calls() {
    // Two modules each call a constructor whose only effect is
    // writing to its own freshly-allocated `this`. The
    // constructors touch no shared cell. Today's S-chain links
    // them; with dataflow, the write sets are each
    // `{instance_X}` and the read sets are each `{ClassX}` —
    // disjoint pairwise.
    let fixture = run_fixture(dataflow_opts(
        r#"class Holder1 { constructor() { this.kind = "h1"; } }
class Holder2 { constructor() { this.kind = "h2"; } }
const instA = new Holder1();
const instB = new Holder2();
console.log(instA.kind, instB.kind);
export { Holder1, Holder2, instA, instB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("Holder1"), Member::new("instA")]),
            logical_module("mod_b", &[Member::new("Holder2"), Member::new("instB")]),
        ],
    ));
    assert_entry_output(&fixture, "h1 h2\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let owner_a = owner_for_binding(&graph, "instA");
    let owner_b = owner_for_binding(&graph, "instB");
    let offending = sequenced_edges_between(&graph, owner_a, owner_b);
    assert!(
        offending.is_empty(),
        "no Sequenced edge should link `instA`'s owner to \
         `instB`'s owner: each `new HolderN()` only mutates its \
         own `this`, so their cross-module init order is \
         unobservable. Offending edges: {offending:#?}\n\nFull \
         graph: {graph:#?}",
    );
}

#[test]
fn s_chain_keeps_edge_when_writes_overlap() {
    // Sanity guard: the relaxation must NOT remove S-edges
    // between statements that genuinely interact via dataflow.
    // Both statements write `globalThis.shared` (the LAST one
    // wins, and any reader of `globalThis.shared` observes the
    // ordering). The edge must remain.
    let fixture = run_fixture(dataflow_opts(
        r#"const tagA = (globalThis.shared = "from-a", "tag-a");
const tagB = (globalThis.shared = "from-b", "tag-b");
console.log(tagA, tagB, globalThis.shared);
export { tagA, tagB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("tagA")]),
            logical_module("mod_b", &[Member::new("tagB")]),
        ],
    ));
    assert_entry_output(&fixture, "tag-a tag-b from-b\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let owner_a = owner_for_binding(&graph, "tagA");
    let owner_b = owner_for_binding(&graph, "tagB");
    let kept = sequenced_edges_between(&graph, owner_a, owner_b);
    assert!(
        !kept.is_empty(),
        "Sequenced edge between `tagA` and `tagB` must be kept: \
         both statements write `globalThis.shared`, so any \
         reader observes their relative order. Graph: {graph:#?}",
    );
}

/// Helper: assert that the strict adjacent-impure S-edge between
/// `owner_a` and `owner_b` remains (i.e. the dataflow relaxation
/// declined to drop it). Used by the bail-out tests below.
fn assert_kept_sequenced_between(graph: &OwnerGraphReport, owner_a: &str, owner_b: &str) {
    let edges = sequenced_edges_between(graph, owner_a, owner_b);
    assert!(
        !edges.is_empty(),
        "expected the strict Sequenced edge between {owner_a} and {owner_b} to be kept — \
         the second statement contains a shape that should disable dataflow summarization. \
         Graph: {graph:#?}",
    );
}

#[test]
fn bail_out_keeps_s_edge_when_statement_uses_direct_eval() {
    // Direct `eval(...)` can read or write any cell in scope; the
    // statement must fall back to the strict S-edge regardless of
    // whether its syntactic write set is otherwise disjoint from the
    // prior statement's writes. See `README.md` →
    // "Conditionally-correct optimizations" for the full bail-out list.
    let fixture = run_fixture(dataflow_opts(
        r#"const tagA = (globalThis.alpha = "alpha-val", "tag-a");
const tagB = (eval("globalThis.beta = 'beta-val'"), "tag-b");
console.log(tagA, tagB, globalThis.alpha, globalThis.beta);
export { tagA, tagB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("tagA")]),
            logical_module("mod_b", &[Member::new("tagB")]),
        ],
    ));
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    assert_kept_sequenced_between(
        &graph,
        owner_for_binding(&graph, "tagA"),
        owner_for_binding(&graph, "tagB"),
    );
}

#[test]
fn bail_out_keeps_s_edge_when_statement_writes_global_this_with_dynamic_key() {
    // `globalThis[<expr>] = ...` can't be reduced to a statically
    // known property cell — the statement must fall back to the
    // strict S-edge.
    let fixture = run_fixture(dataflow_opts(
        r#"const tagA = (globalThis.alpha = "alpha-val", "tag-a");
const keyB = "beta";
const tagB = (globalThis[keyB] = "beta-val", "tag-b");
console.log(tagA, tagB, globalThis.alpha, globalThis.beta);
export { tagA, tagB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("tagA"), Member::new("keyB")]),
            logical_module("mod_b", &[Member::new("tagB")]),
        ],
    ));
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    assert_kept_sequenced_between(
        &graph,
        owner_for_binding(&graph, "tagA"),
        owner_for_binding(&graph, "tagB"),
    );
}

#[test]
fn bail_out_keeps_s_edge_when_statement_uses_function_constructor() {
    // `Function(...)` (and `new Function(...)`) compile a string to
    // executable code in the global scope — same risk class as
    // direct `eval`.
    let fixture = run_fixture(dataflow_opts(
        r#"const tagA = (globalThis.alpha = "alpha-val", "tag-a");
const fnB = new Function("globalThis.beta = 'beta-val'");
const tagB = (fnB(), "tag-b");
console.log(tagA, tagB, globalThis.alpha, globalThis.beta);
export { tagA, tagB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("tagA")]),
            logical_module("mod_b", &[Member::new("fnB"), Member::new("tagB")]),
        ],
    ));
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    assert_kept_sequenced_between(
        &graph,
        owner_for_binding(&graph, "tagA"),
        owner_for_binding(&graph, "fnB"),
    );
}
