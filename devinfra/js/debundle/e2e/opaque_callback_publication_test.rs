//! Source-visible and opaque-provider publication both keep later callbacks.

use debundle_e2e_support::*;

fn assert_later_callback_read(publication: &str) {
    assert_published_read(publication, "globalThis.saved()");
}

fn assert_published_read(publication: &str, invocation: &str) {
    let peer = format!(
        "export function publish(callback) {{ globalThis.saved = callback; }}\nexport function invoke() {{ return {invocation}; }}\n"
    );
    let peers = [("static/peer", peer.as_str())];
    let source = format!(
        r#"import {{ publish, invoke }} from './peer.js';
function read() {{ try {{ return later; }} catch (error) {{ return error.name; }} }}
{publication}
const result = invoke();
const later = 'value';
console.log(result);
"#
    );
    let fixture = run_fixture(
        FixtureOpts::new(&source, Vec::new())
            .with_extra_chunks(&peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
    let graph = fixture.dependency_graph();
    let invocation = source_unit_for_binding(&graph, "result");
    let value = source_unit_for_binding(&graph, "later");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == invocation
                && edge.target == value
                && edge.binding.as_deref() == Some("later")
                && edge.constrains_init_order
        }),
        "the later opaque call must retain the published callback's read"
    );
}

#[test]
fn source_explicit_global_store_stays_persistent() {
    assert_later_callback_read("globalThis.saved = read;");
}

#[test]
fn invisible_provider_global_store_stays_persistent() {
    assert_later_callback_read("publish(read);");
}

#[test]
fn later_property_inserted_into_published_object_stays_persistent() {
    assert_published_read(
        "const holder = {}; globalThis.holder = holder; holder.read = read;",
        "globalThis.holder.read()",
    );
}

#[test]
fn later_element_inserted_into_published_array_stays_persistent() {
    assert_published_read(
        "const holder = []; globalThis.holder = holder; holder[0] = read;",
        "globalThis.holder[0]()",
    );
}

#[test]
fn later_nested_container_insertion_stays_persistent() {
    assert_published_read(
        "const holder = {}; globalThis.holder = holder; const nested = []; holder.nested = nested; nested[0] = read;",
        "globalThis.holder.nested[0]()",
    );
}

#[test]
fn later_member_chain_store_stays_persistent() {
    assert_published_read(
        "const nested = {}; const holder = { nested }; globalThis.holder = holder; holder.nested.read = read;",
        "globalThis.holder.nested.read()",
    );
}

#[test]
fn untracked_member_chain_store_stays_persistent() {
    assert_published_read(
        "const holder = JSON.parse('{\"nested\":{}}'); globalThis.holder = holder; holder.nested.read = read;",
        "globalThis.holder.nested.read()",
    );
}

#[test]
fn skipped_local_publication_keeps_later_container_insertions() {
    assert_published_read(
        "function expose(value) { globalThis.holder = value; } const holder = {}; expose(...[holder]); holder.read = read;",
        "globalThis.holder.read()",
    );
}

#[test]
fn later_map_key_stays_persistent() {
    assert_published_read(
        "const holder = new Map(); globalThis.holder = holder; holder.set(read, 1);",
        "[...globalThis.holder.keys()][0]()",
    );
}

#[test]
fn later_map_value_stays_persistent() {
    assert_published_read(
        "const holder = new Map(); globalThis.holder = holder; holder.set('read', read);",
        "globalThis.holder.get('read')()",
    );
}

#[test]
fn later_set_member_stays_persistent() {
    assert_published_read(
        "const holder = new Set(); globalThis.holder = holder; holder.add(read);",
        "[...globalThis.holder][0]()",
    );
}
