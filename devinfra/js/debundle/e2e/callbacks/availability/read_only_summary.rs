//! Opaque hosts may skip, repeat and reorder a pool. Read-only lexical
//! observers need no execution replay, including above invocation limits.

use super::{assert_unmoved_output, expect_single_binding_rejection};
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged};
use debundle_e2e_support::*;
use std::fmt::Write as _;

const HOST: [(&str, &str); 1] = [(
    "static/peer",
    "export function host(...callbacks) {\n\
       const values = [];\n\
       for (const callback of callbacks.reverse()) {\n\
         try { values.push(callback(), callback()); }\n\
         catch (error) { values.push(error.name); }\n\
       }\n\
       return values.join(',');\n\
     }\n",
)];

fn pool(setup: &str, body: &str, count: usize, tail: &str) -> String {
    let mut source = format!("import {{ host }} from './peer.js';\n{setup}\n");
    for index in 0..count {
        writeln!(source, "function reader{index}() {{ {body} }}").unwrap();
    }
    let arguments = (0..count)
        .map(|index| format!("reader{index}"))
        .collect::<Vec<_>>()
        .join(",");
    writeln!(source, "const result = host({arguments});\n{tail}").unwrap();
    source
}

fn assert_no_replay_exhaustion(fixture: &Fixture) {
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    for node in graph["nodes"].as_array().unwrap() {
        if let Some(losses) = node["synchronous_execution"]["precision_losses"].as_array() {
            for loss in losses {
                assert!(
                    !matches!(
                        loss["reason"]["code"].as_str(),
                        Some(
                            "callback_pool_limit"
                                | "invocation_expansion_limit"
                                | "invocation_depth_limit"
                        )
                    ),
                    "read-only pool lost precision: {loss}"
                );
            }
        }
    }
}

#[test]
fn wide_read_only_pools_preserve_execution_without_replay_exhaustion() {
    for count in [1, 64, 65, 256] {
        let source = pool(
            "const state = 7;",
            "return state;",
            count,
            "console.log(result);",
        );
        let fixture = run_fixture_with_graphs(
            FixtureOpts::new(
                &source,
                vec![logical_module("state", &[Member::new("state")])],
            )
            .with_extra_chunks(&HOST)
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, &format!("{}\n", vec!["7"; count * 2].join(",")));
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/state.js",
            &["state"],
            &[],
        );
        assert_no_replay_exhaustion(&fixture);
    }
}

#[test]
fn prefix_values_and_saved_analysis_are_rechecked_between_host_calls() {
    let source = "import { host } from './peer.js';\n\
        let state = 1;\n\
        const reader = () => state;\n\
        const first = host(reader);\n\
        state = 2;\n\
        const second = host(reader);\n\
        console.log(first, second);\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&HOST)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_node_output(
        &staged.root().join("snapshot/static/app.js"),
        "1,1 2,2\n",
        "",
    );
    let fresh = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_same_invocation_trees(&fresh, &saved);
    for invocation in [&fresh, &saved] {
        assert!(
            invocation.result.status.success(),
            "{}",
            invocation.result.stderr
        );
        assert_node_output(
            &invocation.output_root().join("app/static/app/entry.js"),
            "1,1 2,2\n",
            "",
        );
    }
}

#[test]
fn a_later_callable_return_forfeits_the_primitive_summary() {
    let source = "import { host } from './peer.js';\n\
        let value = 7;\n\
        function reader() { return value; }\n\
        function next() { return later; }\n\
        const first = host(reader);\n\
        value = next;\n\
        const second = host(reader);\n\
        const later = 'ready';\n\
        console.log(first, second);\n";
    let peer = [(
        "static/peer",
        "export function host(callback) { try { const value = callback(); return typeof value === 'function' ? value() : value; } catch (error) { return error.name; } }\n",
    )];
    assert_unmoved_output(source, &peer, "7 ReferenceError\n");
    expect_single_binding_rejection(source, &peer, "later", "later");
}

#[test]
fn wide_read_only_pool_keeps_future_lexical_observations() {
    let source = pool(
        "",
        "return state;",
        65,
        "const state = 7;\nconsole.log(result);",
    );
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(&source, vec![])
            .with_extra_chunks(&HOST)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(
        &fixture,
        &format!("{}\n", vec!["ReferenceError"; 65].join(",")),
    );
    assert_no_replay_exhaustion(&fixture);
    expect_single_binding_rejection(&source, &HOST, "state", "state");
}

#[test]
fn returned_callable_and_accessor_neighbors_keep_their_tdz_constraints() {
    for (setup, body) in [
        ("function next() { return later; }", "return next;"),
        (
            "const holder = { get value() { return later; } };",
            "return holder.value;",
        ),
        ("", "return (() => later)();"),
    ] {
        let source = pool(setup, body, 65, "const later = 'ready';");
        // A separate host invokes returned callbacks too, exposing the TDZ.
        let peer = [(
            "static/peer",
            "export function host(...callbacks) { for (const callback of callbacks) { try { const value = callback(); if (typeof value === 'function') value(); } catch (error) { console.log(error.name); } } }\n",
        )];
        assert_unmoved_output(&source, &peer, &"ReferenceError\n".repeat(65));
        expect_single_binding_rejection(&source, &peer, "later", "later");
    }
}
