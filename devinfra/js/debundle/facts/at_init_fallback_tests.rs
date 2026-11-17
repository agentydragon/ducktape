//! Source-only callback admission, independent of ordered runtime facts.
use super::*;
use raw_js_test_support::parse;
use swc_common::SyntaxContext;

fn admitted(statement: &str) -> bool {
    let module = parse(&format!(
        "const later = 'ready'; const tools = [{{ name: 'tool', systemNodeId: 'tool-id', run: () => later }}]; const registry = {{}}; {statement}"
    ));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
    safe_plain_array_at_init_fallback_sources(
        body.last().expect("statement").as_module_item(),
        &shadowed,
        &graph,
    )
    .contains(&("tools".into(), SyntaxContext::empty()))
}

#[test]
fn reads_beyond_own_primitive_fields_are_not_silent() {
    for statement in [
        // `run` holds a function, not a primitive.
        "const labels = tools.map((n) => `${n.run}`);",
        // Not an own key: the read falls through to the prototype.
        "const labels = tools.map((n) => `${n.label}`);",
        "const labels = tools.map((n) => n[n.name]);",
        "const labels = tools.map((n) => ({ ...n }));",
        // The statement names the array outside the chain.
        "const labels = (globalThis.consume?.(tools[0]), tools.map((n) => `${n.name}`));",
    ] {
        assert!(!admitted(statement), "{statement}");
    }
}
