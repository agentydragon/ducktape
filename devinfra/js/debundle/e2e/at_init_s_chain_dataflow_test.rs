//! Dataflow-aware S-chain (`dataflow_aware_s_chain`, docs/design.md
//! "Emission modes"): a `Sequenced` owner edge joins two consecutive impure
//! top-level statements only when the earlier one writes a cell the later
//! one reads or writes.
//!
//! Each test builds a fixture, inspects `owner_graph.json::edges`, and
//! asserts that no `Sequenced` edge connects the two non-interacting
//! statements in either direction. Patterns: disjoint global property
//! writes; a fresh-local allocation after a global write; independent
//! cross-module inits of a local class whose constructor only touches
//! `this`.

use analysis::{DepKind, OwnerGraphReport};
use debundle_e2e_support::*;

fn trusted_dataflow_opts<'a>(
    source: &'a str,
    logical_modules: Vec<LogicalModuleEntry>,
) -> FixtureOpts<'a> {
    FixtureOpts::new(source, logical_modules)
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries()
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
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const tagA = (globalThis.alpha = "alpha-val", "tag-a");
const tagB = (globalThis.beta = "beta-val", "tag-b");
console.log(tagA, tagB, globalThis.alpha, globalThis.beta);
export { tagA, tagB };
"#,
            vec![
                logical_module("mod_a", &[Member::new("tagA")]),
                logical_module("mod_b", &[Member::new("tagB")]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph = fixture.owner_graph();
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
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const tagA = (globalThis.tag = "first", "tag-a");
const boxedB = Object.freeze({ kind: "fresh" });
console.log(tagA, boxedB.kind, globalThis.tag);
export { tagA, boxedB };
"#,
            vec![
                logical_module("mod_a", &[Member::new("tagA")]),
                logical_module("mod_b", &[Member::new("boxedB")]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_entry_output(&fixture, "tag-a fresh first\n");

    let graph = fixture.owner_graph();
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
fn unproven_constructor_calls_keep_conservative_s_edge() {
    // Two modules each call a constructor the purity classifier
    // can't prove pure (`new <chunk class>` is `UnknownNew` without
    // a `pure_new` annotation). An unproven call/new may touch any
    // cell — the opaque-call rule keeps the conservative S edge
    // between the two statements even though their syntactic cell
    // sets are disjoint. This deliberately pins the conservative
    // behavior: relaxing it requires proving the constructor
    // cell-confined (e.g. a `pure_new` spec annotation), not
    // assuming it.
    let fixture = run_fixture(
        FixtureOpts::new(
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
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_entry_output(&fixture, "h1 h2\n");

    let graph = fixture.owner_graph();
    let owner_a = owner_for_binding(&graph, "instA");
    let owner_b = owner_for_binding(&graph, "instB");
    assert_kept_sequenced_between(&graph, owner_a, owner_b);
}

#[test]
fn trusted_constructor_calls_use_dataflow_summary() {
    // The default above remains conservative because an unproven
    // `new` may have arbitrary observable effects. Some real bundle
    // specs are separately audited and accept the old dataflow
    // assumption for conservative-but-present summaries: calls/news
    // stay impure, but they do not become barriers against every
    // prior impure owner. In that trusted mode these two constructor
    // calls touch disjoint binding cells, so there is no S-edge
    // between them.
    let fixture = run_fixture(trusted_dataflow_opts(
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

    let graph = fixture.owner_graph();
    let owner_a = owner_for_binding(&graph, "instA");
    let owner_b = owner_for_binding(&graph, "instB");
    let offending = sequenced_edges_between(&graph, owner_a, owner_b);
    assert!(
        offending.is_empty(),
        "trusted dataflow summarization should not add an unrelated S-edge \
         between constructor calls whose syntactic summaries touch disjoint \
         binding cells. Offending edges: {offending:#?}\n\nFull graph: {graph:#?}",
    );
}

#[test]
fn s_chain_keeps_edge_when_writes_overlap() {
    // Sanity guard: the relaxation must NOT remove S-edges
    // between statements that genuinely interact via dataflow.
    // Both statements write `globalThis.shared` (the LAST one
    // wins, and any reader of `globalThis.shared` observes the
    // ordering). The edge must remain.
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const tagA = (globalThis.shared = "from-a", "tag-a");
const tagB = (globalThis.shared = "from-b", "tag-b");
console.log(tagA, tagB, globalThis.shared);
export { tagA, tagB };
"#,
            vec![
                logical_module("mod_a", &[Member::new("tagA")]),
                logical_module("mod_b", &[Member::new("tagB")]),
            ],
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_entry_output(&fixture, "tag-a tag-b from-b\n");

    let graph = fixture.owner_graph();
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
    //
    // Top-level eval also violates the A1 admission check
    // (chunk_admission_test pins that rejection); this fixture opts
    // out via the spec override so the per-statement dataflow bail-out
    // stays exercised on its own.
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const tagA = (globalThis.alpha = "alpha-val", "tag-a");
const tagB = (eval("globalThis.beta = 'beta-val'"), "tag-b");
console.log(tagA, tagB, globalThis.alpha, globalThis.beta);
export { tagA, tagB };
"#,
            vec![
                logical_module("mod_a", &[Member::new("tagA")]),
                logical_module("mod_b", &[Member::new("tagB")]),
            ],
        )
        .with_dataflow_aware_s_chain()
        .with_admission_overrides(&["a1_eval"]),
    );
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph = fixture.owner_graph();
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
    let fixture = run_fixture(
        FixtureOpts::new(
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
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph = fixture.owner_graph();
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
    let fixture = run_fixture(
        FixtureOpts::new(
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
        )
        .with_dataflow_aware_s_chain(),
    );
    assert_entry_output(&fixture, "tag-a tag-b alpha-val beta-val\n");

    let graph = fixture.owner_graph();
    assert_kept_sequenced_between(
        &graph,
        owner_for_binding(&graph, "tagA"),
        owner_for_binding(&graph, "fnB"),
    );
}
