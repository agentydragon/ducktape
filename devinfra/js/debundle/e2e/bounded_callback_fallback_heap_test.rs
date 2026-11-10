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
