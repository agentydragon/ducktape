//! Receiver, alias and capture precision keep residual obligations and publications.

use super::{assert_init_order_edge, assert_reason, execution, execution_from_graph};
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;

#[test]
fn local_method_receiver_precision_excludes_implicit_captures() {
    for body in [
        "this.name = 'changed';",
        "const change = () => { this.name = 'changed'; }; return change;",
    ] {
        let source = format!(
            "const record = {{ name: 'original', change() {{ {body} }} }};\nconst returned = record.change();\nif (returned) returned();\nconst selected = record.name;\nconsole.log(selected);\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "changed\n");
        assert_reason(
            &execution(&fixture, "selected"),
            serde_json::json!({"code": "unsupported_operation", "operation": "member_read"}),
        );
    }
}

#[test]
fn external_root_getter_keeps_published_source_callbacks_at_the_read() {
    for source in [
        "globalThis.syntheticHost = { get read() { return state; } };\nconst observed = syntheticHost.read;\nvar state = 'late';\nconsole.log(observed);\ndelete globalThis.syntheticHost;\n",
        "globalThis.syntheticHost = {};\nObject.defineProperty(globalThis.syntheticHost, 'read', { get() { return state; } });\nconst observed = syntheticHost.read;\nvar state = 'late';\nconsole.log(observed);\ndelete globalThis.syntheticHost;\n",
    ] {
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "undefined\n");
        let graph = fixture.dependency_graph();
        assert_reason(
            &execution_from_graph(&graph, "observed"),
            serde_json::json!({"code": "unsupported_operation", "operation": "member_read"}),
        );
        assert_init_order_edge(&graph, "observed", "state");
    }
}

#[test]
fn external_binding_alias_proof_excludes_reassignment_private_and_skipped_values() {
    for setup in [
        "let alias = syntheticHost; alias = { get read() { return state; } };",
        "const privateRecord = { get read() { return state; } }; const alias = privateRecord;",
        "const alias = syntheticHost; function install(value) { Object.defineProperty(value, 'read', { get() { return state; } }); } install(...[alias]);",
    ] {
        let source = format!(
            "globalThis.syntheticHost = {{ read: 'original' }};\n{setup}\nconst observed = alias.read;\nvar state = 'late';\nconsole.log(observed);\ndelete globalThis.syntheticHost;\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "undefined\n");
        let graph = fixture.dependency_graph();
        let observed = source_unit_for_binding(&graph, "observed");
        let state = source_unit_for_binding(&graph, "state");
        assert!(
            graph.edges.iter().any(|edge| {
                edge.source == observed && edge.target == state && edge.constrains_init_order
            }),
            "the actual later getter read must retain state: {setup}"
        );
    }
}

#[test]
fn skipped_reach_keeps_distinct_captures_and_later_prefix_references() {
    for (source, readers, expected) in [
        (
            "var shared = { label: 'shared' };\nfunction capture(record) { return () => [record, shared]; }\nconst first = { name: 'first' };\nconst second = { name: 'second' };\nconst callbacks = [capture(first), capture(second)];\nfunction install(callbacks) { for (const callback of callbacks) { const [record] = callback(); Object.defineProperty(record, 'name', { get() { return state; } }); } }\ninstall(...[callbacks]);\nconst left = first.name;\nconst right = second.name;\nvar state = 'late';\nconsole.log(left, right);\n",
            vec!["left", "right"],
            "undefined undefined\n",
        ),
        (
            "let current = { name: 'first' };\nconst reader = () => current;\nfunction install(callback) { Object.defineProperty(callback(), 'name', { get() { return state; } }); }\ninstall(...[reader]);\nconst second = { name: 'second' };\nconst before = second.name;\ncurrent = second;\ninstall(...[reader]);\nconst observed = second.name;\nvar state = 'late';\nconsole.log(before, observed);\n",
            vec!["observed"],
            "second undefined\n",
        ),
    ] {
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, expected);
        let graph = fixture.dependency_graph();
        if readers == ["observed"] {
            assert_eq!(execution_from_graph(&graph, "before")["complete"], true);
        }
        for reader in readers {
            assert_reason(
                &execution_from_graph(&graph, reader),
                serde_json::json!({"code": "unsupported_operation", "operation": "member_read"}),
            );
            assert_init_order_edge(&graph, reader, "state");
        }
    }
}

#[test]
fn completed_purity_retains_residual_evaluation_obligations() {
    for (source, output, complete) in [
        (
            "function change() { globalThis.flag = 1; return 1; }\nconst value = change();\nconsole.log(value, globalThis.flag);\n",
            "1 1\n",
            Some(true),
        ),
        (
            "const record = {};\nfunction fill(input) { input.flag = 1; return 1; }\nconst value = fill(record);\nconsole.log(value, record.flag);\n",
            "1 1\n",
            Some(true),
        ),
        (
            "const value = ({ valueOf() { return 1; } }) + 1;\nconsole.log(value);\n",
            "2\n",
            None,
        ),
        (
            "function fail() { throw 'sentinel'; }\nconst value = (() => { try { fail(); return 'wrong'; } catch (error) { return error; } })();\nconsole.log(value);\n",
            "sentinel\n",
            None,
        ),
    ] {
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, output);
        let graph = fixture.dependency_graph();
        let value = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "value")
            })
            .unwrap();
        assert!(!value.purity.is_pure(), "{source}: {:?}", value.purity);
        let execution = value.synchronous_execution.as_ref().unwrap();
        if let Some(complete) = complete {
            assert_eq!(execution.complete, complete);
        }
        assert!(execution.purity_disagreement.is_none());
    }
    // The migrated host row can prove timing through its native slot census,
    // while its purity requires the separately audited exact-site input.
    for (constructor, pure, complete) in
        [("TextEncoder", false, true), ("TextDecoder", true, false)]
    {
        let source = format!(
            "const value = new {constructor}();\nconsole.log(value instanceof {constructor});\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "true\n");
        let graph = fixture.dependency_graph();
        let value = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "value")
            })
            .unwrap();
        assert_eq!(
            value.purity.is_pure(),
            pure,
            "{constructor}: {:?}",
            value.purity
        );
        let execution = value.synchronous_execution.as_ref().unwrap();
        assert_eq!(execution.complete, complete, "{constructor}: {execution:?}");
        assert!(execution.purity_disagreement.is_none());
    }
}

#[test]
fn completed_owner_publication_retains_final_value_and_export_capabilities() {
    for declaration in [
        "const initialized = install();",
        "export default install();",
    ] {
        let source = format!(
            "const record = {{ value: () => 0 }};\nfunction install() {{ record.value = () => state; return 1; }}\n{declaration}\nconst observed = record.value();\nvar state = 'late';\nconsole.log(observed);\n"
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(&source, vec![]).with_import_order(&["residual"]),
        );
        assert_preserved_entry_output(&fixture, "undefined\n");
        let graph = fixture.dependency_graph();
        assert_init_order_edge(&graph, "observed", "state");
        assert_eq!(
            graph
                .nodes
                .iter()
                .filter(|node| {
                    node.synchronous_execution
                        .as_ref()
                        .is_some_and(|execution| execution.purity_disagreement.is_some())
                })
                .count(),
            0
        );
    }
}
