//! Graph topology, graph-export parity, and multi-run initialization controls.
//! Plain runtime cases live in `cases/realizability/initialization_order/`.

use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, program_graph_path, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn hoisted_factory_import_retains_runtime_and_graph_topology() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"function factory() { return { value: 1 }; }
const command = factory();
const list = [command.value];
function readList() { return list; }
console.log(list[0]);
export { factory, command, list, readList };
"#,
            vec![
                logical_module("factory", &binding_members(["factory", "readList"])),
                logical_module("command", &[Member::new("command")]),
                logical_module("list", &[Member::new("list")]),
            ],
        )
        .with_import_order(&["list", "command", "factory"])
        .with_graphs(),
    );
    assert_preserved_entry_output(&fixture, "1\n");
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const factory = await import("./static/app/modules/factory.js");
const command = await import("./static/app/modules/command.js");
const list = await import("./static/app/modules/list.js");
console.log(JSON.stringify({
  factory: Object.keys(factory).sort(),
  command: Object.keys(command).sort(),
  list: Object.keys(list).sort(),
}));
"#,
        "{\"factory\":[\"factory\",\"readList\"],\"command\":[\"command\"],\"list\":[\"list\"]}\n",
    );
    // This is the graph-report contract: a hoisted factory reference is a
    // visible HoistedUse, but the edge does not constrain initialization
    // order (docs/design.md § G_atomic).
    let graph = fixture.dependency_graph();
    let command_source_unit = source_unit_for_binding(&graph, "command");
    let factory_source_unit = source_unit_for_binding(&graph, "factory");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == command_source_unit
            && edge.target == factory_source_unit
            && edge.edge_kind == DepKind::HoistedUse
            && !edge.constrains_init_order
    }));
}

#[test]
fn flat_fixture_without_graph_export_keeps_output_and_gate_verdict() {
    let accepted = || {
        FixtureOpts::new(
            "const value = 7; console.log(value); export { value };\n",
            vec![logical_module("value", &[Member::new("value")])],
        )
    };
    let ordinary = run_fixture(accepted());
    let with_graphs = run_fixture(accepted().with_graphs());
    assert_same_tree(&ordinary.out_root, &with_graphs.out_root);
    assert_entry_output(&ordinary, "7\n");
    assert_entry_output(&with_graphs, "7\n");

    let ordinary_assessment = ordinary
        .report_root
        .join(&ordinary.chunk_id)
        .join("candidate_relocation_assessment.json");
    let graph_assessment = with_graphs
        .report_root
        .join(&with_graphs.chunk_id)
        .join("candidate_relocation_assessment.json");
    let ordinary_assessment: serde_json::Value = read_json(&ordinary_assessment);
    let graph_assessment: serde_json::Value = read_json(&graph_assessment);
    assert_eq!(ordinary_assessment["legacy_gate_accepts"], true);
    assert_eq!(ordinary_assessment["status"], graph_assessment["status"]);
    assert_eq!(
        ordinary_assessment["legacy_gate_accepts"],
        graph_assessment["legacy_gate_accepts"],
    );

    let ordinary_graph = ordinary
        .report_root
        .join(&ordinary.chunk_id)
        .join("dependency_graph.json.gz");
    let graph_graph = with_graphs
        .report_root
        .join(&with_graphs.chunk_id)
        .join("dependency_graph.json.gz");
    assert!(
        !ordinary_graph.exists(),
        "ordinary fixture exported a dependency graph"
    );
    assert!(
        !read_tree(&ordinary.report_root).keys().any(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name == "program_graph.json.gz")
        }),
        "ordinary fixture exported a program graph"
    );
    assert!(
        graph_graph.exists(),
        "explicit graph fixture omitted its graph"
    );
    assert!(program_graph_path(&graph_graph).exists());

    let refused = || {
        FixtureOpts::new(
            "import { next, peek } from './state.js';\n\
             const first = next();\n\
             const second = peek();\n\
             console.log(first, second);\n",
            vec![logical_module("second", &[Member::new("second")])],
        )
        .with_extra_chunks(&[(
            "static/state",
            "let count = 0;\n\
             export function next() { return ++count; }\n\
             export function peek() { return count; }\n",
        )])
        .with_unassigned_mode(unassigned_mode_inline())
    };
    let ordinary_refusal = run_rejection_fixture(refused().with_emit_on_rejection());
    let graph_refusal = run_rejection_fixture(refused().with_graphs().with_emit_on_rejection());
    assert_eq!(ordinary_refusal.cycles(), graph_refusal.cycles());
    let ordinary_refusal_assessment = ordinary_refusal.candidate_relocation_assessment();
    let graph_refusal_assessment = graph_refusal.candidate_relocation_assessment();
    assert_eq!(ordinary_refusal_assessment["legacy_gate_accepts"], false);
    assert_eq!(
        ordinary_refusal_assessment["status"],
        graph_refusal_assessment["status"],
    );
    assert_eq!(
        ordinary_refusal_assessment["legacy_gate_accepts"],
        graph_refusal_assessment["legacy_gate_accepts"],
    );
    let ordinary_refusal_graph = ordinary_refusal.report_path("dependency_graph.json.gz");
    let graph_refusal_graph = graph_refusal.report_path("dependency_graph.json.gz");
    assert!(
        !ordinary_refusal_graph.exists(),
        "ordinary refused fixture exported a dependency graph"
    );
    assert!(
        !read_tree(&ordinary_refusal.report_root).keys().any(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name == "program_graph.json.gz")
        }),
        "ordinary refused fixture exported a program graph"
    );
    assert!(graph_refusal_graph.exists());
    assert!(program_graph_path(&graph_refusal_graph).exists());
}

#[test]
fn method_rebinding_requires_explicit_colocation_and_preserves_runtime() {
    // A peeled class whose method assigns a top-level `let`. Split from the
    // `let`, the method would assign through a read-only ESM import; the gate
    // rejects that split. Claiming the `let` with the class co-locates them.
    let source = r#"let counter = 0;
class Counter {
  bump(b) { counter = b; }
  read() { return counter; }
}
const c = new Counter();
c.bump(1);
console.log(c.read());
export { counter, Counter, c };
"#;
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "mod_counter_class",
                &binding_members(["Counter", "c"]),
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_emit_on_rejection(),
    );
    let conflicts = rejected.atomic_group_conflicts();
    assert_eq!(conflicts.len(), 1, "{conflicts:#?}");
    assert!(
        conflicts[0]["causes"]
            .as_array()
            .expect("atomic group causes")
            .iter()
            .any(|cause| cause == "lazy_rebind"),
        "{conflicts:#?}"
    );
    assert_node_error(&rejected.entry_path(), "Assignment to constant variable");

    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "mod_counter_class",
                &binding_members(["counter", "Counter", "c"]),
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_graphs(),
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_counter_class.js",
        &["let counter = 0;", "counter = b;"],
        &[],
    );
    assert_preserved_entry_output(&fixture, "1\n");
}
