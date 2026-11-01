//! Opaque methods receive their receiver, including its retained callbacks.

use debundle_e2e_support::*;

fn assert_receiver_read(storage: &str, invocation: &str) {
    let peer = format!("export function dispatch() {{ {invocation} }}\n");
    let peers = [("static/peer", peer.as_str())];
    let source = format!(
        r#"import {{ dispatch }} from './peer.js';
let seen = 'unset';
function read() {{ try {{ seen = later; }} catch (error) {{ seen = error.name; }} }}
const receiver = {{ dispatch, storage: {storage} }};
receiver.dispatch();
const later = 'value';
console.log(seen);
"#
    );
    let colocated = run_fixture(
        FixtureOpts::new(&source, Vec::new())
            .with_extra_chunks(&peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&colocated, "ReferenceError\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![logical_module("value", &[Member::new("later")])],
        )
        .with_extra_chunks(&peers)
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value"],
        &[],
    );
}

#[test]
fn callback_retained_by_opaque_method_receiver_keeps_its_read() {
    assert_receiver_read("read", "this.storage();");
}

#[test]
fn nested_array_receiver_retains_callbacks() {
    assert_receiver_read("[[read]]", "this.storage[0][0]();");
}

#[test]
fn map_values_in_receiver_retain_callbacks() {
    assert_receiver_read("new Map([['read', read]])", "this.storage.get('read')();");
}

#[test]
fn map_keys_in_receiver_retain_callbacks() {
    assert_receiver_read("new Map([[read, 1]])", "[...this.storage.keys()][0]();");
}

#[test]
fn set_members_in_receiver_retain_callbacks() {
    assert_receiver_read("new Set([read])", "[...this.storage][0]();");
}
