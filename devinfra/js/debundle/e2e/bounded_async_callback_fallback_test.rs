//! The graph's bounded opaque-call closure obeys definite async suspension.

use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::saved_analysis::assert_staged_run_matches;
use debundle_e2e_support::*;
use std::fmt::Write as _;

fn source(async_keyword: &str, callback_body: &str) -> String {
    let mut source = format!("{async_keyword} function deferred() {{ {callback_body} }}\n");
    let mut callbacks = vec!["deferred".to_string()];
    for index in 0..70 {
        writeln!(source, "function filler{index}() {{ return {index}; }}").unwrap();
        callbacks.push(format!("filler{index}"));
    }
    writeln!(
        source,
        "const registration = globalThis.register?.({});",
        callbacks.join(", ")
    )
    .unwrap();
    source.push_str(
        "const late = 'ready';\nPromise.resolve(deferred()).then(value => console.log(value));\n",
    );
    source
}

fn has_eager_read(fixture: &Fixture) -> bool {
    let graph = fixture.dependency_graph();
    let call = source_unit_for_binding(&graph, "registration");
    let late = source_unit_for_binding(&graph, "late");
    graph.edges.iter().any(|edge| {
        edge.source == call
            && edge.target == late
            && edge.binding.as_deref() == Some("late")
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    })
}

#[test]
fn post_await_read_does_not_become_an_at_init_dependency() {
    let source = source("async", "await 0; return late;");
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module("value", &[Member::new("late")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ready\n");
    assert!(
        !has_eager_read(&fixture),
        "post-await read was promoted by graph fallback"
    );
}

#[test]
fn independently_invocable_prefix_reads_still_constrain_initialization() {
    for (async_keyword, body) in [
        ("", "return late;"),
        ("async", "const first = late; await 0; return first;"),
        ("async", "if (globalThis.skip) await 0; return late;"),
        ("async", "globalThis.saved = () => late; await 0; return 0;"),
        (
            "async",
            "globalThis.saved = { get value() { return late; } }; await 0; return 0;",
        ),
        ("async", "eval('void late'); await 0; return late;"),
    ] {
        let source = source(async_keyword, body);
        let fixture = run_fixture(
            FixtureOpts::new(&source, Vec::new()).with_unassigned_mode(unassigned_mode_inline()),
        );
        assert!(
            has_eager_read(&fixture),
            "missing eager dependency for {body}"
        );
    }
}

#[test]
fn pre_await_call_to_another_owner_still_constrains_initialization() {
    let source = format!(
        "function helper() {{ return late; }}\n{}",
        source("async", "helper(); await 0; return 0;")
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, Vec::new()).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert!(
        has_eager_read(&fixture),
        "pre-await helper invocation lost its eager dependency"
    );
}

#[test]
fn immediate_pre_await_tdz_observation_is_preserved() {
    let source = format!(
        "globalThis.register = (callback) => callback().then(value => console.log(value));\n{}",
        source(
            "async",
            "let result; try { result = late; } catch (error) { result = error.name; } await 0; return result;"
        )
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, Vec::new()).with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError\nready\n");
    assert!(has_eager_read(&fixture));
}

#[test]
fn saved_analysis_keeps_the_covered_prefix() {
    let source = source("async", "await 0; return late;");
    assert_staged_run_matches(
        FixtureOpts::new(
            &source,
            vec![logical_module("value", &[Member::new("late")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &[],
    );
}
