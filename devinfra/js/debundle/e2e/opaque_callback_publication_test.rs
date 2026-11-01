//! Source-visible and opaque-provider publication both keep later callbacks.

use debundle_e2e_support::*;

fn assert_later_callback_read(publication: &str) {
    let peer = r#"export function publish(callback) { globalThis.saved = callback; }
export function invoke() { return globalThis.saved(); }
"#;
    let peers = [("static/peer", peer)];
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
