//! Soundness holes in the dataflow-aware S-chain
//! (`graph.rs::emit_s_chain`, opt-in via
//! `chunk_analysis_options.<chunk>.dataflow_aware_s_chain`).
//!
//! Each test pins one hole by asserting the owner graph carries the
//! `Sequenced` edge the bundle's observable semantics require (red
//! before the fix: edge absent), plus a runtime check that the
//! emitted bundle preserves the original output once the edge exists.
//!
//! Holes covered:
//!
//! - **Write-after-read (WAR)**: last-writer-only tracking missed the
//!   edge from a writer to readers since the last write — a later
//!   writer could be scheduled before an earlier reader.
//! - **Opaque calls**: a call/new the purity classifier can't prove
//!   pure may touch any cell (I/O, globals via callee bodies,
//!   indirect `eval`); the statement must fall back to the
//!   conservative chain.
//! - **Member/update writes**: `obj.x = 1` recorded only a read of
//!   `obj`; `globalThis.count++` recorded no write at all; aliasing
//!   `globalThis` into a binding escaped the cell tracking entirely.
//! - **window/self**: only the literal `globalThis` was treated as
//!   the global object, despite README claiming otherwise.

use analysis::{DepKind, OwnerGraphReport};
use debundle_e2e_support::*;

fn dataflow_opts<'a>(source: &'a str, logical_modules: Vec<LogicalModuleEntry>) -> FixtureOpts<'a> {
    FixtureOpts::new(source, logical_modules).with_dataflow_aware_s_chain()
}

fn owner_for_ordinal(graph: &OwnerGraphReport, ordinal: usize) -> &str {
    let node = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == ordinal)
        .unwrap_or_else(|| panic!("no owner-graph node with ordinal {ordinal}"));
    node.id.as_str()
}

/// Assert a `Sequenced` edge `later → earlier` exists (the
/// "earlier must evaluate first" constraint).
fn assert_sequenced_edge(graph: &OwnerGraphReport, later: &str, earlier: &str) {
    let found = graph
        .edges
        .iter()
        .any(|e| e.edge_kind == DepKind::Sequenced && e.source == later && e.target == earlier);
    assert!(
        found,
        "expected Sequenced edge {later} -> {earlier}; edges: {:#?}",
        graph.edges,
    );
}

fn read_owner_graph(fixture: &Fixture) -> OwnerGraphReport {
    read_json(&fixture.report_root.join("static/app/owner_graph.json"))
}

/// WAR: `limit`'s write of `globalThis.flag` must be sequenced after
/// `snapshot`'s read of it, even though the last *writer* of the cell
/// is the earlier statement.
#[test]
fn write_after_read_emits_sequenced_edge() {
    let fixture = run_fixture(dataflow_opts(
        r#"globalThis.flag = "first";
const snapshot = globalThis.flag;
const limit = (globalThis.flag = "second", "L");
console.log(snapshot + limit);
export { snapshot, limit };
"#,
        vec![
            logical_module_with_anon("mod_w", &[], &[r#"globalThis.flag = "first";"#]),
            logical_module("mod_read", &[Member::new("snapshot")]),
            logical_module("mod_w2", &[Member::new("limit")]),
        ],
    ));
    assert_entry_output(&fixture, "firstL\n");
    let graph = read_owner_graph(&fixture);
    assert_sequenced_edge(
        &graph,
        owner_for_binding(&graph, "limit"),
        owner_for_binding(&graph, "snapshot"),
    );
}

/// Opaque calls: two `console.log` statements touch no common cell
/// the analyzer can see, but I/O is not a cell — their order is
/// observable and must be preserved via the conservative fallback.
#[test]
fn console_log_pair_keeps_conservative_order() {
    let fixture = run_fixture(dataflow_opts(
        r#"const seed = "s";
console.log("one");
const base = "two" + seed;
console.log(base);
export { seed, base };
"#,
        vec![
            logical_module("mod_seed", &[Member::new("seed")]),
            logical_module_with_anon("mod_one", &[], &[r#"console.log("one");"#]),
            logical_module_with_anon("mod_two", &[Member::new("base")], &["console.log(base);"]),
        ],
    ));
    assert_entry_output(&fixture, "one\ntwos\n");
    let graph = read_owner_graph(&fixture);
    // ordinals: 0 = seed, 1 = log("one"), 2 = base, 3 = log(base)
    assert_sequenced_edge(
        &graph,
        owner_for_ordinal(&graph, 3),
        owner_for_ordinal(&graph, 1),
    );
}

/// Opaque calls through chunk functions: `setup()` writes a global
/// prop from inside its body (invisible to the depth-0-gated cell
/// recorder); the reader of that prop must still be ordered after
/// the call statement.
#[test]
fn at_init_call_writing_global_in_body_is_ordered_before_reader() {
    let fixture = run_fixture(dataflow_opts(
        r#"function setup() { globalThis.mode = "ready"; }
setup();
const got = globalThis.mode;
console.log(got);
export { got };
"#,
        vec![
            logical_module_with_anon("mod_boot", &[], &["setup();"]),
            logical_module("mod_read", &[Member::new("got")]),
        ],
    ));
    assert_entry_output(&fixture, "ready\n");
    let graph = read_owner_graph(&fixture);
    // ordinals: 0 = setup decl, 1 = setup(), 2 = got, 3 = log
    assert_sequenced_edge(
        &graph,
        owner_for_binding(&graph, "got"),
        owner_for_ordinal(&graph, 1),
    );
}

/// Member writes: `registry.mode = "on"` mutates state reachable
/// from `registry`; the reader of `registry.mode` must be ordered
/// after it.
#[test]
fn member_write_through_binding_is_ordered_before_reader() {
    let fixture = run_fixture(dataflow_opts(
        r#"const registry = {};
registry.mode = "on";
const snap = registry.mode;
console.log(snap);
export { registry, snap };
"#,
        vec![
            logical_module_with_anon(
                "mod_w",
                &[Member::new("registry")],
                &[r#"registry.mode = "on";"#],
            ),
            logical_module("mod_read", &[Member::new("snap")]),
        ],
    ));
    assert_entry_output(&fixture, "on\n");
    let graph = read_owner_graph(&fixture);
    // ordinals: 0 = registry, 1 = member write, 2 = snap, 3 = log
    assert_sequenced_edge(
        &graph,
        owner_for_binding(&graph, "snap"),
        owner_for_ordinal(&graph, 1),
    );
}

/// Update expressions: `globalThis.count++` writes the cell; the
/// reader must be ordered after the increment, not just after the
/// initial assignment.
#[test]
fn global_prop_update_expr_records_write() {
    let fixture = run_fixture(dataflow_opts(
        r#"globalThis.count = 1;
globalThis.count++;
const got = globalThis.count;
console.log(got);
export { got };
"#,
        vec![
            logical_module_with_anon("mod_a", &[], &["globalThis.count = 1;"]),
            logical_module_with_anon("mod_b", &[], &["globalThis.count++;"]),
            logical_module("mod_read", &[Member::new("got")]),
        ],
    ));
    assert_entry_output(&fixture, "2\n");
    let graph = read_owner_graph(&fixture);
    // ordinals: 0 = init, 1 = increment, 2 = got, 3 = log
    assert_sequenced_edge(
        &graph,
        owner_for_binding(&graph, "got"),
        owner_for_ordinal(&graph, 1),
    );
}

/// Aliasing: `const g = globalThis; const got = g.tag;` reads a
/// global prop through an alias the cell tracker can't see —
/// statements touching the alias must bail to the conservative
/// chain so they stay ordered after the direct writer.
#[test]
fn global_this_alias_read_is_ordered_after_direct_writer() {
    let fixture = run_fixture(dataflow_opts(
        r#"const g = globalThis;
globalThis.tag = "x";
const got = g.tag;
console.log(got);
export { g, got };
"#,
        vec![
            logical_module_with_anon("mod_w", &[], &[r#"globalThis.tag = "x";"#]),
            logical_module("mod_read", &[Member::new("got")]),
        ],
    ));
    assert_entry_output(&fixture, "x\n");
    let graph = read_owner_graph(&fixture);
    // ordinals: 0 = g, 1 = tag write, 2 = got, 3 = log
    assert_sequenced_edge(
        &graph,
        owner_for_binding(&graph, "got"),
        owner_for_ordinal(&graph, 1),
    );
}

/// window/self: an unshadowed `window` is the same object as
/// `globalThis`; writes through it must hit the same cells.
#[test]
fn window_alias_prop_write_orders_reader() {
    let fixture = run_fixture(dataflow_opts(
        r#"globalThis.window = globalThis;
window.tag = "w";
const got = window.tag;
console.log(got);
export { got };
"#,
        vec![
            logical_module_with_anon("mod_boot", &[], &["globalThis.window = globalThis;"]),
            logical_module_with_anon("mod_w", &[], &[r#"window.tag = "w";"#]),
            logical_module("mod_read", &[Member::new("got")]),
        ],
    ));
    assert_entry_output(&fixture, "w\n");
    let graph = read_owner_graph(&fixture);
    // ordinals: 0 = boot, 1 = window.tag write, 2 = got, 3 = log
    assert_sequenced_edge(
        &graph,
        owner_for_binding(&graph, "got"),
        owner_for_ordinal(&graph, 1),
    );
}

/// Indirect eval: `(0, eval)(...)` executes in global scope and can
/// touch any cell — the statement must fall back to the conservative
/// chain (subsumed by the opaque-call rule).
#[test]
fn indirect_eval_is_a_barrier() {
    // Top-level indirect eval also violates the A1 admission check
    // (chunk_admission_test pins that rejection); this fixture opts
    // out via the spec override so the per-statement dataflow
    // bail-out stays exercised on its own.
    let fixture = run_fixture(
        dataflow_opts(
            r#"globalThis.alpha = "a";
const tagB = ((0, eval)("globalThis.beta = globalThis.alpha + 'b'"), "t");
const got = globalThis.beta;
console.log(got, tagB);
export { tagB, got };
"#,
            vec![
                logical_module_with_anon("mod_a", &[], &[r#"globalThis.alpha = "a";"#]),
                logical_module("mod_b", &[Member::new("tagB")]),
                logical_module("mod_read", &[Member::new("got")]),
            ],
        )
        .with_admission_overrides(&["a1_eval"]),
    );
    assert_entry_output(&fixture, "ab t\n");
    let graph = read_owner_graph(&fixture);
    assert_sequenced_edge(
        &graph,
        owner_for_binding(&graph, "got"),
        owner_for_binding(&graph, "tagB"),
    );
}
