//! A callback pool past `MAX_SYNCHRONOUS_CALLBACK_POOL` takes the bounded
//! fallback summary. A member can still reach a callable through the heap
//! rather than a lexical read: a stored closure it calls or returns, or an
//! accessor its property access runs. That callable's TDZ observation keeps
//! the opaque call before the observed declaration.

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
        "host({});\nconst later = 'value';\nconsole.log(seen);\n",
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
