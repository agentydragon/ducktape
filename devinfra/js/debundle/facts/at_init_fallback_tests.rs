//! The at-init fallback's plain-array admission against element state the
//! interpreter settles at each statement (plan § Step 8).

use super::*;
use raw_js_test_support::parse;
use swc_common::SyntaxContext;

const TOOLS: &str = "const later = \"ready\";\n\
const tools = [\n  { name: \"tool\", systemNodeId: \"tool-id\", run: () => later },\n  { name: \"other\", systemNodeId: \"other-id\", run: () => later },\n];\n";

fn tools_id() -> Id {
    ("tools".into(), SyntaxContext::empty())
}

/// What the last statement of `{TOOLS}{prefix}{statement}` settles for
/// `tools`.
fn settled(prefix: &str, statement: &str) -> Option<SettledElements> {
    let module = parse(&format!("{TOOLS}{prefix}{statement}\n"));
    let facts = analyze_chunk(&module, &AnalysisInputs::default(), None, |_| None).facts;
    facts
        .last()
        .and_then(|fact| fact.synchronous_calls.as_ref())
        .and_then(|calls| calls.settled_array_elements.get(&tools_id()).cloned())
}

const READER: &str = "tools.forEach((n) => { registry[n.systemNodeId] = n.name; });";

#[test]
fn untainted_records_settle_their_own_primitive_keys() {
    assert_eq!(
        settled("const registry = {};\n", READER),
        Some(SettledElements::Records {
            shared_keys: BTreeSet::from(["name".to_string(), "systemNodeId".to_string()]),
        })
    );
}

#[test]
fn an_element_reaching_other_code_before_the_statement_is_not_settled() {
    for prefix in [
        // Escapes to an unknown call.
        "globalThis.consume?.(tools[0]);\n",
        // Stored into a container that escapes.
        "const holder = { first: tools[0] };\nglobalThis.consume?.(holder);\n",
        // Returned by a function, then escapes.
        "function pick() { return tools[0]; }\nglobalThis.consume?.(pick());\n",
        // Captured by a closure that unknown code may run.
        "const grab = () => tools[0];\nglobalThis.consume?.(grab);\n",
        // Written through an alias.
        "const first = tools[0];\nfirst.name = { toString() { return later; } };\n",
        // Reflection installs an accessor, replaces the prototype or writes.
        "Object.defineProperty(tools[0], \"name\", { get() { return later; } });\n",
        "Object.setPrototypeOf(tools[0], { get name() { return later; } });\n",
        "Reflect.set(tools[0], \"name\", later);\n",
        "globalThis.wrapped = new Proxy(tools[0], {});\n",
        // Stored into a collection that escapes.
        "globalThis.held = new Set([tools[0]]);\n",
        "globalThis.held = new Map([[\"first\", tools[0]]]);\n",
        // An incomplete statement that reads the array.
        "globalThis.consume?.(tools);\n",
        // An incomplete statement whose summarized callee reads the array.
        "function peek() { return tools; }\nglobalThis.consume?.(peek);\n",
    ] {
        assert_eq!(
            settled(&format!("const registry = {{}};\n{prefix}"), READER),
            None,
            "{prefix}"
        );
    }
}

#[test]
fn another_binding_the_statement_reads_holding_an_element_is_not_settled() {
    assert_eq!(
        settled(
            "const registry = {};\nconst first = tools[0];\n",
            "(globalThis.consume?.(first), tools.forEach((n) => { registry[n.name] = n; }));"
        ),
        None
    );
}

/// The roots `statement` admits under the given element state of `tools`.
fn admitted(statement: &str, elements: Option<SettledElements>) -> bool {
    let module = parse(&format!("{TOOLS}const registry = {{}};\n{statement}\n"));
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let graph = ChunkCodeGraph::build(&body, &shadowed, &BTreeSet::new());
    let settled: BTreeMap<_, _> = elements.into_iter().map(|e| (tools_id(), e)).collect();
    safe_plain_array_at_init_fallback_sources(
        body.last().expect("statement").as_module_item(),
        &shadowed,
        &graph,
        &settled,
    )
    .contains(&tools_id())
}

fn records() -> Option<SettledElements> {
    Some(SettledElements::Records {
        shared_keys: BTreeSet::from(["name".to_string(), "systemNodeId".to_string()]),
    })
}

#[test]
fn own_primitive_field_reads_of_settled_records_are_silent() {
    for statement in [
        // The `lV` shape: tool objects read through two own String fields.
        "const labels = tools.map((n) => `${n.name}:${n.systemNodeId}`);",
        // The `eet` shape: command objects registered by an own key.
        "tools.forEach((command) => { registry[command.systemNodeId] = command; });",
        "const ids = [...tools.filter((n) => n.name).map((n) => n.systemNodeId)];",
    ] {
        assert!(admitted(statement, records()), "{statement}");
        assert!(
            !admitted(statement, None),
            "{statement} without settled records"
        );
    }
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
        assert!(!admitted(statement, records()), "{statement}");
    }
}
