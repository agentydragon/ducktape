//! Soundness: at-init calls whose callee can't be resolved to a
//! chunk-declared function must not be skipped by at-init call promotion
//! (`graph/build/at_init.rs::promote_at_init_calls`). An at-init call routed through a
//! single-assignment alias (`const g = readB; g();`) or an object-literal
//! method (`api.read()`) fires its body's TDZ-locked cross-module reads at
//! runtime, so such a statement conservatively depends eagerly on the
//! transitive lazy closures of every chunk binding it reads at-init
//! (following initializer read chains); the gate-accepted-but-TDZ shapes
//! below close a constraining cycle and are rejected.

use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use graph::EdgeRoleReport;

/// `const g = readB; const r = g();` — the alias `g` is a VarDecl, not a
/// function declaration. `readB`'s body reads `B` (mod_b), and mod_b
/// eagerly reads `A` (mod_a): an asymmetric cycle that TDZs on `g()` at
/// runtime.
#[test]
fn aliased_at_init_call_closing_cross_module_cycle_is_rejected() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const A = 1;
const B = A + 1;
function readB() { return B; }
const g = readB;
const r = g();
console.log(r);
export { A, B, g, r, readB };
"#,
            vec![
                logical_module("mod_a", &binding_members(["A", "readB", "g", "r"])),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

/// Method-call variant: `const api = { read: () => B }; api.read();`
/// — the callee is a member expression, not a bare identifier.
#[test]
fn object_literal_method_at_init_call_closing_cycle_is_rejected() {
    expect_cycle_rejection(
        FixtureOpts::new(
            r#"const A = 1;
const B = A + 1;
const api = { read: () => B };
const r = api.read();
console.log(r);
export { A, B, api, r };
"#,
            vec![
                logical_module("mod_a", &binding_members(["A", "api", "r"])),
                logical_module("mod_b", &[Member::new("B")]),
            ],
        ),
        &["mod_a", "mod_b"],
    );
}

/// Green companion: the fallback emits a *correct* constraint (not a
/// blanket rejection) when the aliased call's transitive reads are
/// acyclic — the emitted bundle runs and the cross-module read is
/// ordered.
#[test]
fn aliased_at_init_call_with_acyclic_closure_is_accepted_and_runs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const VALUE = "v";
const get = () => VALUE;
const indirect = get;
const r = indirect();
console.log(r);
export { VALUE, get, indirect, r };
"#,
        vec![
            logical_module("mod_val", &[Member::new("VALUE")]),
            logical_module("mod_use", &binding_members(["get", "indirect", "r"])),
        ],
    ));
    assert_entry_output(&fixture, "v\n");
}

#[test]
fn plain_array_collection_methods_do_not_promote_element_function_bodies() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const tools = [{ name: "tool", systemNodeId: "tool-id", run: () => later }];
const toolCopies = tools.map((tool) => ({ ...tool }));
const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]));
const ids = [...tools.filter((tool) => tool.systemNodeId).map((tool) => tool.systemNodeId)];
const copiedIds = [...toolCopies.map((tool) => tool.systemNodeId)];
const later = byName.tool.name + "-ready";
console.log(byName.tool.name, ids[0], copiedIds[0], tools[0].run(), toolCopies[0].run());
export { later, tools, toolCopies, byName, ids, copiedIds };
"#,
            vec![
                logical_module(
                    "collections",
                    &binding_members(["tools", "toolCopies", "byName", "ids", "copiedIds"]),
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_import_order(&["values/later", "collections"]),
    );

    assert_preserved_entry_output(&fixture, "tool tool-id tool-id tool-ready tool-ready\n");
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const collections = await import("./static/app/modules/collections.js");
const values = await import("./static/app/modules/values/later.js");
console.log(JSON.stringify({
  collections: Object.keys(collections).sort(),
  values: Object.keys(values).sort(),
}));
"#,
        "{\"collections\":[\"byName\",\"copiedIds\",\"ids\",\"toolCopies\",\"tools\"],\"values\":[\"later\"]}\n",
    );
}

#[test]
fn plain_array_callback_mutation_preserves_runtime_behavior() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const later = "ready";
const tools = [{ name: "tool", id: "tool-id", run: () => later }];
const byId = {};
tools.forEach((tool) => {
  tool.id && (byId[tool.id] = tool.name);
});
console.log(byId["tool-id"], tools[0].run());
export { later, tools, byId };
"#,
            vec![logical_module("collections", &[Member::new("byId")])],
        )
        .with_import_order(&["collections", "residual"]),
    );
    assert_preserved_entry_output(&fixture, "tool ready\n");
}

#[test]
fn fresh_descriptor_for_each_does_not_invoke_stored_executor() {
    let source = r#"let later = 0;
const nodeIds = { tool: "tool-id" };
const descriptors = [{ id: nodeIds.tool, name: "tool", executor: () => later }];
const lookup = {};
descriptors.forEach((data) => { lookup[data.id] = data.name; });
later = 42;
console.log(lookup["tool-id"], descriptors[0].executor());
export { lookup };
"#;
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &binding_members(["later", "nodeIds", "descriptors", "lookup"]),
            &[
                "descriptors.forEach((data) => { lookup[data.id] = data.name; });",
                "later = 42;",
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "tool 42\n");

    let split = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &binding_members(["nodeIds", "descriptors", "lookup"]),
                    &["descriptors.forEach((data) => { lookup[data.id] = data.name; });"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/later", "registry"]),
    );
    assert_preserved_entry_output(&split, "tool 42\n");
    assert_module_exports(
        &split.out_root,
        "static/app/modules/registry.js",
        &["descriptors", "lookup"],
        &["later", "nodeIds"],
    );
    assert_module_exports(
        &split.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &["nodeIds", "descriptors", "lookup"],
    );
}

#[test]
fn fresh_descriptor_filter_map_spread_does_not_invoke_stored_executor() {
    let source = r#"let later = 0;
const descriptors = [{ id: "tool-id", name: "tool", executor: () => later }];
globalThis.savedExecutor = descriptors[0].executor;
const ids = [...descriptors.filter((data) => data.id).map((data) => data.id)];
later = 42;
console.log(ids[0], descriptors[0].executor());
export { ids };
"#;
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "all",
            &binding_members(["later", "descriptors", "ids"]),
            &[
                "globalThis.savedExecutor = descriptors[0].executor;",
                "later = 42;",
            ],
        )],
    ));
    assert_preserved_entry_output(&colocated, "tool-id 42\n");

    let split = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &binding_members(["descriptors", "ids"]),
                    &["globalThis.savedExecutor = descriptors[0].executor;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/later", "registry"]),
    );
    assert_preserved_entry_output(&split, "tool-id 42\n");
    assert_module_exports(
        &split.out_root,
        "static/app/modules/registry.js",
        &["descriptors", "ids"],
        &["later"],
    );
    assert_module_exports(
        &split.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &["descriptors", "ids"],
    );
}

#[test]
fn global_event_listener_callback_is_not_promoted_as_at_init() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        r#"const later = "ready";
typeof window < "u" &&
  window.addEventListener("beforeunload", () => {
    console.log(later);
  });
export { later };
"#,
        vec![logical_module("later_mod", &[Member::new("later")])],
    ));

    // `window` is absent under Node, so the runtime branch is intentionally
    // inert; this graph assertion retains the documented addEventListener
    // callback-timing contract (docs/design.md § Builtin member rows).
    let graph = fixture.dependency_graph();
    let later_source_unit = source_unit_for_binding(&graph, "later");
    let promoted_to_later: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.target == later_source_unit
                && edge.edge_kind == DepKind::EagerUse
                && matches!(edge.role, Some(EdgeRoleReport::PromotedAtInit { .. }))
        })
        .collect();
    assert!(
        promoted_to_later.is_empty(),
        "global addEventListener registration must not promote lazy callback reads; \
         got {promoted_to_later:#?}\n\nFull dependency graph: {graph:#?}",
    );
    assert_entry_output(&fixture, "");
}

#[test]
fn dom_event_listener_callback_is_not_promoted_as_at_init() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"const later = "ready";
const document = {
  querySelector: () => ({ addEventListener() {} }),
};
const button = document.querySelector("button");
button?.addEventListener("click", () => {
  console.log(later);
});
export { later, document, button };
"#,
            vec![logical_module("later_mod", &[Member::new("later")])],
        )
        .with_import_order(&["later_mod", "residual"]),
    );

    // This keeps the documented callback-timing graph contract explicit:
    // the builtin member row treats addEventListener as non-invoking at call
    // time, regardless of receiver identity (docs/design.md § Builtin member
    // rows). This user-written Node stub makes the call execute, but does not
    // model a browser host implementation.
    let graph = fixture.dependency_graph();
    let later_source_unit = source_unit_for_binding(&graph, "later");
    let promoted_to_later: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.target == later_source_unit
                && edge.edge_kind == DepKind::EagerUse
                && matches!(edge.role, Some(EdgeRoleReport::PromotedAtInit { .. }))
        })
        .collect();
    assert!(
        promoted_to_later.is_empty(),
        "DOM addEventListener registration must not promote lazy callback reads; \
         got {promoted_to_later:#?}\n\nFull dependency graph: {graph:#?}",
    );
    assert_entry_output(&fixture, "");
}

#[test]
fn record_array_iteration_preserves_escaped_and_mutated_elements() {
    for (prefix, first) in [
        ("", "tool"),
        ("globalThis.consume?.(tools[0]);", "tool"),
        (
            "const holder = { first: tools[0] }; globalThis.consume?.(holder);",
            "tool",
        ),
        (
            "function pick() { return tools[0]; } globalThis.consume?.(pick());",
            "tool",
        ),
        (
            "const grab = () => tools[0]; globalThis.consume?.(grab);",
            "tool",
        ),
        (
            "const first = tools[0]; first.name = { toString() { return later; } };",
            "ready",
        ),
        (
            "Object.defineProperty(tools[0], 'name', { get() { return later; } });",
            "ready",
        ),
        (
            "Object.setPrototypeOf(tools[0], { get name() { return later; } });",
            "tool",
        ),
        ("Reflect.set(tools[0], 'name', later);", "ready"),
        ("globalThis.wrapped = new Proxy(tools[0], {});", "tool"),
        ("globalThis.held = new Set([tools[0]]);", "tool"),
        ("globalThis.held = new Map([['first', tools[0]]]);", "tool"),
        ("globalThis.consume?.(tools);", "tool"),
        (
            "function peek() { return tools; } globalThis.consume?.(peek);",
            "tool",
        ),
        (
            "function rename(t) { t.name = later; } rename(...[tools[0]]);",
            "ready",
        ),
        (
            "function tag(_, t) { t.name = later; } tag`${tools[0]}`;",
            "ready",
        ),
        (
            "function rec(t, n) { if (n > 0) rec(t, n - 1); t.name = later; } rec(tools[0], 3);",
            "ready",
        ),
    ] {
        let source = format!(
            "const later = 'ready';\nconst tools = [{{ name: 'tool', systemNodeId: 'tool-id', run: () => later }}, {{ name: 'other', systemNodeId: 'other-id', run: () => later }}];\nconst registry = {{}};\n{prefix}\ntools.forEach(n => {{ registry[n.systemNodeId] = n.name; }});\nconsole.log(String(registry['tool-id']), registry['other-id'], tools[0].run());\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module("values/later", &[Member::new("later")])],
            )
            .with_unassigned_mode(unassigned_mode_inline())
            .with_import_order(&["values/later"]),
        );
        assert_preserved_entry_output(&fixture, &format!("{first} other ready\n"));
    }
}

#[test]
fn record_array_field_and_identity_consumers_keep_their_split() {
    for (expression, output, expected) in [
        (
            "tools.map(n => `${n.name}:${n.systemNodeId}`)",
            "JSON.stringify(result)",
            "[\"tool:tool-id\",\"other:other-id\"] 42\n",
        ),
        (
            "[...tools.filter(n => n.name).map(n => n.systemNodeId)]",
            "JSON.stringify(result)",
            "[\"tool-id\",\"other-id\"] 42\n",
        ),
    ] {
        let source = format!(
            "let later = 0;\nconst tools = [{{ name: 'tool', systemNodeId: 'tool-id', run: () => later }}, {{ name: 'other', systemNodeId: 'other-id', run: () => later }}];\nconst registry = {{}};\nconst result = {expression};\nlater = 42;\nconsole.log({output}, tools[0].run());\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "registry",
                        &binding_members(["tools", "registry", "result"]),
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_import_order(&["values/later", "registry"]),
        );
        assert_preserved_entry_output(&fixture, expected);
    }
}

#[test]
fn same_statement_element_alias_keeps_identity_and_runtime_effects() {
    let source = "const later = 'ready';\nconst tools = [{ name: 'tool', run: () => later }];\nconst first = tools[0];\nconst registry = {};\n(globalThis.consume?.(first), tools.forEach(n => { registry[n.name] = n; }));\nconsole.log(registry.tool === first, first.run());\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("values/later", &[Member::new("later")])],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["values/later"]),
    );
    assert_preserved_entry_output(&fixture, "true ready\n");
}

#[test]
fn a_real_host_mutator_through_the_same_statement_alias_keeps_ordering() {
    let source = "import './peer.js';\nlet later = 0;\nconst tools = [{ name: 'tool', run: () => later }];\nconst first = tools[0];\nconst registry = {};\n(globalThis.consume?.(first), tools.forEach(n => { registry[n.name] = n; }));\nlater = 42;\nconsole.log(Object.keys(registry).join(','), tools[0].run());\n";
    let peers = [(
        "static/peer",
        "globalThis.consume = entry => { entry.name = { toString() { return entry.run(); } }; };\nexport {};\n",
    )];
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_import_order(&["residual"])
            .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &binding_members(["tools", "first", "registry"]),
                    &["(globalThis.consume?.(first), tools.forEach(n => { registry[n.name] = n; }));"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/later", "registry"])
        .with_extra_chunks(&peers),
        &["registry", "values/later"],
        &[],
    );
}

#[test]
fn for_each_stores_the_original_record_identity() {
    let source = "const later = 'ready';\nconst tools = [{ name: 'tool', systemNodeId: 'tool-id', run: () => later }];\nconst registry = {};\ntools.forEach(command => { registry[command.systemNodeId] = command; });\nconsole.log(registry['tool-id'] === tools[0], registry['tool-id'].run());\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("values/later", &[Member::new("later")])],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_import_order(&["values/later"]),
    );
    assert_preserved_entry_output(&fixture, "true ready\n");
}
