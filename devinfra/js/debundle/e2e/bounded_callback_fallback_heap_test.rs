//! A callback pool past `MAX_SYNCHRONOUS_CALLBACK_POOL` takes the bounded
//! fallback summary. A member can still reach a callable through the heap
//! rather than a lexical read: a stored closure it calls or returns, or an
//! accessor its property access runs. That callable's TDZ observation keeps
//! the opaque call before the observed declaration.

use debundle_e2e_support::saved_analysis::{assert_same_invocation_trees, path, staged};
use debundle_e2e_support::*;

const PEER: [(&str, &str); 1] = [(
    "static/peer",
    "export function host(...callbacks) {\n  for (const callback of callbacks) {\n    const result = callback();\n    if (typeof result === 'function') result();\n  }\n}\n",
)];

const CAPTURE_PEER: [(&str, &str); 1] = [(
    "static/peer",
    "export function host(...callbacks) {\n  for (const callback of callbacks) {\n    try { console.log(callback()); } catch (error) { console.log(error.name); }\n  }\n}\n",
)];

const OBSERVE: &str = "try { seen = later; } catch (error) { seen = error.name; }";

/// More pool members than the synchronous pool limit.
const POOL: usize = 70;

fn source(setup: &str, member: &str) -> String {
    source_after_call(setup, member, "")
}

fn source_after_call(setup: &str, member: &str, after_call: &str) -> String {
    let setup = setup.replace("OBSERVE", OBSERVE);
    let mut source = format!(
        "import {{ host }} from './peer.js';\nlet seen = 'unset';\nconst holder = {{}};\n{setup}\nfunction reader() {{ {member} }}\n"
    );
    let mut pool = vec!["reader".to_string()];
    for index in 0..POOL {
        source.push_str(&format!("function filler{index}() {{ return {index}; }}\n"));
        pool.push(format!("filler{index}"));
    }
    source.push_str(&format!(
        "host({});\n{after_call}\nconst later = 'value';\nconsole.log(seen);\n",
        pool.join(", ")
    ));
    source
}

fn assert_observation_stays_before_declaration(setup: &str, member: &str) {
    let source = source(setup, member);
    let colocated = run_fixture(
        FixtureOpts::new(&source, Vec::new())
            .with_extra_chunks(&PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![logical_module("value", &[Member::new("later")])],
        )
        .with_extra_chunks(&PEER)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value"],
        &[],
    );
}

fn captured_reader_pool() -> (String, String) {
    let mut source = String::from(
        "import { host } from './peer.js';\n\
         function factory(holder) { return () => holder.read(); }\n",
    );
    let mut callbacks = Vec::new();
    let mut expected = String::new();
    for index in 0..POOL - 1 {
        source.push_str(&format!(
            "const value{index} = 'value{index}';\n\
             const callback{index} = factory({{ read: () => value{index} }});\n"
        ));
        callbacks.push(format!("callback{index}"));
        expected.push_str(&format!("value{index}\n"));
    }
    source.push_str(
        "const finalCallback = factory({ read: () => later });\n\
         host(",
    );
    callbacks.push("finalCallback".to_string());
    source.push_str(&callbacks.join(", "));
    source.push_str(
        ");\n\
         const later = 'ready';\n\
         console.log('done');\n",
    );
    expected.push_str("ReferenceError\ndone\n");
    (source, expected)
}

#[test]
fn shared_callable_keeps_each_captured_holder_in_the_bounded_pool() {
    let (source, expected) = captured_reader_pool();
    let peers = &CAPTURE_PEER;

    let accepted = run_fixture(
        FixtureOpts::new(
            &source,
            vec![logical_module("values/first", &[Member::new("value0")])],
        )
        .with_extra_chunks(peers)
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&accepted, &expected);
    assert!(
        accepted
            .out_root
            .join("static/app/modules/values/first.js")
            .is_file(),
        "accepted split did not emit the values/first module"
    );

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![logical_module("values/later", &[Member::new("later")])],
        )
        .with_extra_chunks(peers)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["values/later"],
        &["eager_use"],
    );
}

#[test]
fn later_opaque_call_sees_callback_installed_by_an_earlier_call() {
    let source = "import { host } from './peer.js';\n\
        let callback = () => 'initial';\n\
        function install() { callback = () => later; }\n\
        host(install);\n\
        host(callback);\n\
        const later = 'ready';\n\
        console.log('done');\n";
    let peers = &CAPTURE_PEER;
    let colocated = run_fixture(
        FixtureOpts::new(source, Vec::new())
            .with_extra_chunks(peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "undefined\nReferenceError\ndone\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![logical_module("values/later", &[Member::new("later")])],
        )
        .with_extra_chunks(peers)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["values/later"],
        &["eager_use"],
    );
}

#[test]
fn stored_closure_called_by_member() {
    assert_observation_stays_before_declaration(
        "holder.callback = () => { OBSERVE };",
        "holder.callback();",
    );
}

#[test]
fn stored_closure_returned_by_member() {
    assert_observation_stays_before_declaration(
        "holder.callback = () => { OBSERVE };",
        "return holder.callback;",
    );
}

#[test]
fn stored_setter_run_by_member() {
    assert_observation_stays_before_declaration(
        "holder.target = { set value(next) { OBSERVE } };",
        "holder.target.value = 1;",
    );
}

#[test]
fn stored_getter_run_by_member() {
    assert_observation_stays_before_declaration(
        "holder.target = { get value() { OBSERVE; return 1; } };",
        "return holder.target.value;",
    );
}

#[test]
fn captured_holder_fallback_constraints_survive_saved_analysis() {
    let (source, expected) = captured_reader_pool();
    for (binding, accepted) in [("value0", true), ("later", false)] {
        let (staged, _fixture) = staged(
            FixtureOpts::new(
                &source,
                vec![logical_module("value", &[Member::new(binding)])],
            )
            .with_extra_chunks(&CAPTURE_PEER)
            .with_unassigned_mode(unassigned_mode_inline()),
        );
        let direct = staged.run(&["--emit-on-rejection"]);
        let saved = staged.run(&["--emit-on-rejection", "--analysis", path(&staged.artifact)]);
        for invocation in [&direct, &saved] {
            assert_eq!(
                invocation.result.status.code(),
                Some(if accepted { 0 } else { 3 }),
                "{binding}: {}",
                invocation.result.stderr,
            );
            if accepted {
                assert_node_output(
                    &invocation.output_root().join("app/static/app/entry.js"),
                    &expected,
                    "",
                );
            }
        }
        assert_same_invocation_trees(&direct, &saved);
    }
}

#[test]
fn repeated_foreign_fallback_sees_changed_retained_callback_edges() {
    let peer = [(
        "static/peer",
        "export function host(...callbacks) { for (const callback of callbacks) { const result = callback(); if (typeof result === 'function') { if (result.prototype) new result(); else result(); } } }\n",
    )];
    let cases = [
        (
            "holder.callback = () => 0;",
            "holder.callback = () => { OBSERVE };",
            "holder.callback();",
        ),
        // The root adjacency stays identical: only a descendant inside a
        // strongly connected retained graph changes between the two queries.
        (
            "holder.child = { callback: () => 0 }; holder.child.parent = holder;",
            "holder.child.callback = () => { OBSERVE };",
            "holder.child.callback();",
        ),
        (
            "function make() { let callback = () => 0; return { read: () => callback(), install: () => { callback = () => { OBSERVE }; } }; } const pair = make(); holder.callback = pair.read;",
            "pair.install();",
            "holder.callback();",
        ),
        (
            "function Base() { holder.callback = () => { OBSERVE }; } class Derived extends Base {} holder.callback = () => 0;",
            "host(() => Derived);",
            "holder.callback();",
        ),
        (
            "globalThis.saved = () => 0;",
            "globalThis.saved = () => { OBSERVE };",
            "return globalThis.saved;",
        ),
    ];
    for (setup, update, member) in cases {
        let mut after_call = update.replace("OBSERVE", OBSERVE);
        after_call.push_str("\nhost(reader");
        for index in 0..POOL {
            after_call.push_str(&format!(", filler{index}"));
        }
        after_call.push_str(");");
        let source = source_after_call(setup, member, &after_call);
        let colocated = run_fixture(
            FixtureOpts::new(&source, Vec::new())
                .with_extra_chunks(&peer)
                .with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&colocated, "ReferenceError\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![logical_module("values/later", &[Member::new("later")])],
            )
            .with_extra_chunks(&peer)
            .with_unassigned_mode(unassigned_mode_inline()),
            &["values/later"],
            &[],
        );
    }
}

#[test]
fn cyclic_retained_native_object_preserves_the_observed_callback() {
    assert_observation_stays_before_declaration(
        "holder.record = /pattern/; holder.record.self = holder.record; holder.record.callback = () => { OBSERVE };",
        "holder.record.callback();",
    );
}
