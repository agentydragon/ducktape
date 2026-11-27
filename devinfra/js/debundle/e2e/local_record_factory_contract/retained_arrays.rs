//! Retained arrays and argument mutation keep only real constructor heap edges.

use super::{execution, members};
use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, node_declaring};
use debundle_e2e_support::*;

/// Two factory results whose retained callbacks capture `shared`; `after_second`
/// runs before the final log.
fn captured_array_source(after_second: &str, log_first: &str, exports: &str) -> String {
    format!(
        r#"class Empty {{}}
class Holder {{
  constructor(record, checker) {{ this.record = record; this.checker = checker; this.size = record.size ?? 0; }}
  size;
  run() {{ return this.record.callback(); }}
}}
function create(record) {{ return new Holder(record, new Empty()); }}
const shared = [0];
const first = create({{ size: 1, items: [1], callback: () => shared.push(1) }});
const second = create({{ size: 2, items: [2], callback: () => shared.push(2) }});
{after_second}console.log({log_first}, second.size, first.run(), second.run(), shared.join(','));
export {{ {exports} }};
"#
    )
}

fn captured_array_modules(shared: &[&'static str]) -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "framework",
            &members("create", &["Empty", "Holder", "create"]),
        ),
        logical_module("first", &[Member::new("first")]),
        logical_module("second", &[Member::new("second")]),
        logical_module("shared", &members("create", shared)),
    ]
}

#[test]
fn independent_retained_arrays_do_not_create_a_constructor_heap_frontier() {
    let source = captured_array_source("", "first.size", "first, second, shared");
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(&source, captured_array_modules(&["shared"]))
            .with_dataflow_aware_s_chain()
            .with_import_order(&["framework", "shared", "first", "second"]),
    );
    assert_preserved_entry_output(&fixture, "1 2 2 3 0,1,2\n");
    let graph = fixture.dependency_graph();
    let shared = node_declaring(&graph, "shared");
    for result in ["first", "second"] {
        let heap = execution(&graph, result)
            .heap_frontier
            .as_ref()
            .expect("literal allocations keep heap evidence");
        assert!(!heap.own_at_init.opaque, "{result}: {heap:#?}");
        assert_eq!(heap.own_at_init.reads.count, 0, "{result}: {heap:#?}");
        assert_eq!(heap.own_at_init.writes.count, 0, "{result}: {heap:#?}");
        assert!(
            heap.own_at_init.allocations.count > 0,
            "{result}: {heap:#?}"
        );
        assert!(
            !heap.composed_synchronous_calls.opaque,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.composed_synchronous_calls.reads.count, 0,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.composed_synchronous_calls.writes.count, 0,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.non_assumed_heap_accesses.reads.count, 0,
            "{result}: {heap:#?}"
        );
        assert_eq!(
            heap.non_assumed_heap_accesses.writes.count, 0,
            "{result}: {heap:#?}"
        );
        let result_id = &node_declaring(&graph, result).id;
        assert!(
            !graph
                .edges
                .iter()
                .any(|edge| edge.edge_kind == DepKind::Sequenced
                    && &edge.source == result_id
                    && edge.target == shared.id),
            "retaining a callback is not a read or write of the captured array: {:#?}",
            graph.edges
        );
    }
}

#[test]
fn later_reader_can_share_module_with_a_captured_array() {
    let source = captured_array_source(
        "const after = first.size;\n",
        "after",
        "first, second, shared, after",
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, captured_array_modules(&["shared", "after"]))
            .with_dataflow_aware_s_chain()
            .with_import_order(&["shared", "first", "second", "framework"]),
    );
    assert_preserved_entry_output(&fixture, "1 2 2 3 0,1,2\n");
    assert_eq!(fixture.translation_validation()["accepts"], true);
    for module in ["first", "second", "shared"] {
        assert!(
            fixture
                .out_root
                .join(format!("static/app/modules/{module}.js"))
                .is_file(),
            "missing emitted {module} module"
        );
    }
}

#[test]
fn argument_mutation_keeps_its_heap_edge_beside_a_checked_call() {
    let source = r#"class Holder {
  constructor(record) { this.items = record.items; }
}
function create(record) { return new Holder(record); }
const shared = [0];
function mutateSharedArray() { shared.push(1); return [1]; }
const first = create({ items: mutateSharedArray() });
const second = create({ items: [2] });
console.log(shared.join(','), first.items[0], second.items[0]);
"#;
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &members(
                "create",
                &[
                    "Holder",
                    "create",
                    "shared",
                    "mutateSharedArray",
                    "first",
                    "second",
                ],
            ),
            &["console.log(shared.join(','), first.items[0], second.items[0]);"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "0,1 1 2\n");
    let graph = fixture.dependency_graph();
    let node = |binding: &str| node_declaring(&graph, binding);
    let first_heap = execution(&graph, "first")
        .heap_frontier
        .as_ref()
        .expect("the nested call retains its heap effects");
    assert!(first_heap.own_at_init.opaque, "{first_heap:#?}");
    assert!(
        first_heap.composed_synchronous_calls.writes.count > 0,
        "{first_heap:#?}"
    );
    assert!(
        first_heap.non_assumed_heap_accesses.writes.count > 0,
        "{first_heap:#?}"
    );
    let sequenced_after_shared = |binding: &str| {
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && edge.source == node(binding).id
                && edge.target == node("shared").id
        })
    };
    assert!(
        sequenced_after_shared("first"),
        "the nested mutation must remain ordered after the shared array: {:#?}",
        graph.edges
    );
    assert!(
        !sequenced_after_shared("second"),
        "the independent checked call must not inherit the first call's heap effects: {:#?}",
        graph.edges
    );
}
