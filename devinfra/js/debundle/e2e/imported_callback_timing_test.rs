//! Imported wrappers keep callback-body reads deferred until invocation.

use analysis::DepKind;
use analysis::reports::schema::CallbackFactoryUseKind;
use debundle_e2e_support::*;

const SOURCE: &str = r#"import { factory } from './peer.js';
const wrapped = factory(() => later);
const later = 42;
console.log(wrapped());
export { wrapped, later };
"#;

#[test]
fn direct_imported_no_reentry_call_with_published_callback() {
    let source = "import { makeSlot as localMakeSlot } from './peer.js';\n\
                  let later = 0;\n\
                  const callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  const slot = localMakeSlot();\n\
                  later = 42;\n\
                  console.log(slot.kind, globalThis.saved());\n";
    let peer = [(
        "static/peer",
        "export function makeSlot() { return { kind: 'slot' }; }\n",
    )];
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(&["makeSlot"])
        .build()];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/slot",
                    &[Member::new("callback"), Member::new("slot")],
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
    );
    assert_preserved_entry_output(&fixture, "slot 42\n");
}

fn assert_direct_export_reentry_control(
    source: &str,
    peer_source: &str,
    members: &[&'static str],
    statements: &[&str],
    output: &str,
    asserted: bool,
) {
    let peer = [("static/peer", peer_source)];
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_exports(if asserted { &["makeSlot"] } else { &[] })
        .build()];
    let mut all_members = members
        .iter()
        .map(|name| Member::new(name))
        .collect::<Vec<_>>();
    all_members.push(Member::new("later"));
    let mut all_statements = statements.to_vec();
    all_statements.push("later = 42;");
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &all_members,
                &all_statements,
            )],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
    );
    assert_preserved_entry_output(&colocated, output);
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/slot",
                    &members
                        .iter()
                        .map(|name| Member::new(name))
                        .collect::<Vec<_>>(),
                    statements,
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
        &["callbacks/slot", "values/later"],
        &[],
    );
}

#[test]
fn direct_export_without_assertion_can_invoke_published_callback() {
    assert_direct_export_reentry_control(
        "import { makeSlot as localMakeSlot } from './peer.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\n\
         const slot = localMakeSlot();\n\
         later = 42;\nconsole.log(globalThis.observed, slot.kind, globalThis.saved());\n",
        "export function makeSlot() { globalThis.observed = globalThis.saved(); return { kind: 'slot' }; }\n",
        &["callback", "slot"],
        &["globalThis.saved = callback;"],
        "0 slot 42\n",
        false,
    );
}

#[test]
fn direct_export_no_reentry_keeps_uncertain_arguments_and_aliases_opaque() {
    for (setup, call, members, observed) in [
        (
            "const option = { get value() { return globalThis.saved(); } };\n",
            "localMakeSlot(option)",
            vec!["callback", "option", "slot"],
            "",
        ),
        (
            "const alias = localMakeSlot;\n",
            "alias()",
            vec!["callback", "alias", "slot"],
            "",
        ),
        (
            "const touch = () => { globalThis.observed = globalThis.saved(); };\n",
            "localMakeSlot(void touch())",
            vec!["callback", "touch", "slot"],
            "0 ",
        ),
    ] {
        let source = format!(
            "import {{ makeSlot as localMakeSlot }} from './peer.js';\n\
             let later = 0;\nconst callback = () => later;\n\
             globalThis.saved = callback;\n{setup}\
             const slot = {call};\n\
             later = 42;\nconsole.log({}slot.kind, globalThis.saved());\n",
            if observed.is_empty() {
                ""
            } else {
                "globalThis.observed, "
            }
        );
        assert_direct_export_reentry_control(
            &source,
            "export function makeSlot() { return { kind: 'slot' }; }\n",
            &members,
            &["globalThis.saved = callback;"],
            &format!("{observed}slot 42\n"),
            true,
        );
    }
}

#[test]
fn direct_export_no_reentry_does_not_defer_result_invocation_or_construction() {
    assert_direct_export_reentry_control(
        "import { makeSlot as localMakeSlot } from './peer.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\n\
         const slot = localMakeSlot();\n\
         globalThis.observed = slot.invoke();\n\
         later = 42;\nconsole.log(globalThis.observed, globalThis.saved());\n",
        "export function makeSlot() { return { invoke() { return globalThis.saved(); } }; }\n",
        &["callback", "slot"],
        &[
            "globalThis.saved = callback;",
            "globalThis.observed = slot.invoke();",
        ],
        "0 42\n",
        true,
    );
    assert_direct_export_reentry_control(
        "import { makeSlot as localMakeSlot } from './peer.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\n\
         const slot = new localMakeSlot();\n\
         later = 42;\nconsole.log(slot.kind, globalThis.saved());\n",
        "export function makeSlot() { return { kind: 'slot' }; }\n",
        &["callback", "slot"],
        &["globalThis.saved = callback;"],
        "slot 42\n",
        true,
    );
}

#[test]
fn primitive_namespace_call_does_not_reenter_published_callback_when_asserted() {
    let source = "import { api } from './peer.js';\n\
                  let later = 0;\n\
                  const callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  const slot = api.makeSlot(void 0);\n\
                  later = 42;\n\
                  console.log(slot.kind, globalThis.saved());\n";
    let peer = [(
        "static/peer",
        "export const api = { makeSlot() { return { kind: 'slot' }; } };\n",
    )];
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_members("api", &["makeSlot"])
        .with_namespace_independent_members("api", &["makeSlot"])
        .build()];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/slot",
                    &[Member::new("callback"), Member::new("slot")],
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
    );
    assert_preserved_entry_output(&fixture, "slot 42\n");
}

#[test]
fn fresh_literal_record_namespace_call_does_not_reenter_published_callback_when_asserted() {
    let source = "import { api } from './peer.js';\n\
                  let later = 0;\n\
                  const callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  const slot = api.makeSlot({ field: 0, flag: !1, nested: { ready: !0, label: 'ok' } });\n\
                  later = 42;\n\
                  console.log(slot.kind, slot.defaults.field, slot.defaults.flag, slot.defaults.nested.ready, globalThis.saved());\n";
    let peer = [(
        "static/peer",
        "export const api = { makeSlot(defaults) { return { kind: 'slot', defaults }; } };\n",
    )];
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_no_reentry_members("api", &["makeSlot"])
        .with_no_reentry_record_members("api", &["makeSlot"])
        .with_namespace_independent_members("api", &["makeSlot"])
        .build()];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/slot",
                    &[Member::new("callback"), Member::new("slot")],
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/callbacks/slot.js",
        &["slot"],
        &["later"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &["slot"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/callbacks/slot.js",
        &["makeSlot", "nested", "ready"],
        &["later = 42"],
    );
    assert_preserved_entry_output(&fixture, "slot 0 false true 42\n");
}

fn assert_no_reentry_control(
    source: &str,
    peer_source: &str,
    callback_members: &[&'static str],
    callback_statements: &[&str],
    expected_output: &str,
    independent_lookup: bool,
    record_contract: bool,
) {
    let peer = [("static/peer", peer_source)];
    let mut contract =
        ChunkExportPurityBuilder::new("static/peer").with_no_reentry_members("api", &["makeSlot"]);
    if record_contract {
        contract = contract.with_no_reentry_record_members("api", &["makeSlot"]);
    }
    if independent_lookup {
        contract = contract.with_namespace_independent_members("api", &["makeSlot"]);
    }
    let purity = [contract.build()];
    let mut all_members = callback_members
        .iter()
        .map(|name| Member::new(name))
        .collect::<Vec<_>>();
    all_members.push(Member::new("later"));
    let mut all_statements = callback_statements.to_vec();
    all_statements.push("later = 42;");
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &all_members,
                &all_statements,
            )],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
    );
    assert_preserved_entry_output(&colocated, expected_output);
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/slot",
                    &callback_members
                        .iter()
                        .map(|name| Member::new(name))
                        .collect::<Vec<_>>(),
                    callback_statements,
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
        &["callbacks/slot", "values/later"],
        &[],
    );
}

#[test]
fn namespace_no_reentry_requires_independent_member_lookup() {
    assert_no_reentry_control(
        "import { api } from './peer.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\n\
         const slot = api.makeSlot(void 0);\n\
         later = 42;\nconsole.log(slot.kind, globalThis.saved());\n",
        "export const api = { makeSlot() { return { kind: 'slot' }; } };\n",
        &["callback", "slot"],
        &["globalThis.saved = callback;"],
        "slot 42\n",
        false,
        false,
    );
}

#[test]
fn namespace_no_reentry_rejects_member_replacement() {
    assert_no_reentry_control(
        "import { api } from './peer.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\n\
         const eager = () => { globalThis.observed = globalThis.saved(); return { kind: 'slot' }; };\n\
         api.makeSlot = eager;\nconst slot = api.makeSlot(void 0);\n\
         later = 42;\nconsole.log(globalThis.observed, slot.kind, globalThis.saved());\n",
        "export const api = { makeSlot() { return { kind: 'slot' }; } };\n",
        &["callback", "eager", "slot"],
        &["globalThis.saved = callback;", "api.makeSlot = eager;"],
        "0 slot 42\n",
        true,
        false,
    );
}

#[test]
fn namespace_no_reentry_rejects_uncertain_arguments_and_keeps_argument_calls() {
    for (setup, argument, members, expected) in [
        (
            "const option = { get value() { return globalThis.saved(); } };\n",
            "option",
            vec!["callback", "option", "slot"],
            "slot 42\n",
        ),
        (
            "const touch = () => { globalThis.observed = globalThis.saved(); };\n",
            "void touch()",
            vec!["callback", "touch", "slot"],
            "0 slot 42\n",
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             let later = 0;\nconst callback = () => later;\n\
             globalThis.saved = callback;\n{setup}\
             const slot = api.makeSlot({argument});\n\
             later = 42;\n\
             console.log({}slot.kind, globalThis.saved());\n",
            if argument == "option" {
                ""
            } else {
                "globalThis.observed, "
            }
        );
        assert_no_reentry_control(
            &source,
            "export const api = { makeSlot() { return { kind: 'slot' }; } };\n",
            &members,
            &["globalThis.saved = callback;"],
            expected,
            true,
            false,
        );
    }
}

#[test]
fn namespace_no_reentry_rejects_nonliteral_record_arguments() {
    for (setup, argument, members) in [
        (
            "",
            "{ get field() { return 0; } }",
            vec!["callback", "slot"],
        ),
        ("", "{ field() { return 0; } }", vec!["callback", "slot"]),
        ("", "{ ...{ field: 0 } }", vec!["callback", "slot"]),
        ("", "{ ['field']: 0 }", vec!["callback", "slot"]),
        ("", "{ __proto__: null }", vec!["callback", "slot"]),
        ("", "{ '__proto__': null }", vec!["callback", "slot"]),
        ("", "{ field: () => 0 }", vec!["callback", "slot"]),
        ("", "new Proxy({ field: 0 }, {})", vec!["callback", "slot"]),
        ("", "+{ valueOf() { return 0; } }", vec!["callback", "slot"]),
        ("", "[0]", vec!["callback", "slot"]),
        (
            "const record = { field: 0 };\n",
            "record",
            vec!["callback", "record", "slot"],
        ),
        (
            "function makeRecord() { return { field: 0 }; }\n",
            "makeRecord()",
            vec!["callback", "makeRecord", "slot"],
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             let later = 0;\n\
             const callback = () => later;\n\
             globalThis.saved = callback;\n\
             {setup}const slot = api.makeSlot({argument});\n\
             later = 42;\n\
             console.log(slot.kind, globalThis.saved());\n"
        );
        assert_no_reentry_control(
            &source,
            "export const api = { makeSlot() { return { kind: 'slot' }; } };\n",
            &members,
            &["globalThis.saved = callback;"],
            "slot 42\n",
            true,
            true,
        );
    }
}

#[test]
fn primitive_no_reentry_assertion_does_not_admit_a_record_branch() {
    assert_no_reentry_control(
        "import { api } from './peer.js';\n\
         let later = 0;\n\
         const callback = () => later;\n\
         globalThis.saved = callback;\n\
         const slot = api.makeSlot({ field: 0 });\n\
         later = 42;\n\
         console.log(globalThis.observed, slot.kind, globalThis.saved());\n",
        "export const api = { makeSlot(defaults) { if (typeof defaults === 'object') globalThis.observed = globalThis.saved(); return { kind: 'slot' }; } };\n",
        &["callback", "slot"],
        &["globalThis.saved = callback;"],
        "0 slot 42\n",
        true,
        false,
    );
}

#[test]
fn record_no_reentry_still_requires_lookup_and_member_identity() {
    let source = "import { api } from './peer.js';\n\
                  let later = 0;\n\
                  const callback = () => later;\n\
                  globalThis.saved = callback;\n\
                  const slot = api.makeSlot({ field: 0 });\n\
                  later = 42;\n\
                  console.log(slot.kind, globalThis.saved());\n";
    assert_no_reentry_control(
        source,
        "export const api = { makeSlot() { return { kind: 'slot' }; } };\n",
        &["callback", "slot"],
        &["globalThis.saved = callback;"],
        "slot 42\n",
        false,
        true,
    );

    assert_no_reentry_control(
        "import { api } from './peer.js';\n\
         let later = 0;\n\
         const callback = () => later;\n\
         globalThis.saved = callback;\n\
         const eager = () => { globalThis.observed = globalThis.saved(); return { kind: 'slot' }; };\n\
         api.makeSlot = eager;\n\
         const slot = api.makeSlot({ field: 0 });\n\
         later = 42;\n\
         console.log(globalThis.observed, slot.kind, globalThis.saved());\n",
        "export const api = { makeSlot() { return { kind: 'slot' }; } };\n",
        &["callback", "eager", "slot"],
        &["globalThis.saved = callback;", "api.makeSlot = eager;"],
        "0 slot 42\n",
        true,
        true,
    );
}

#[test]
fn namespace_no_reentry_does_not_defer_opaque_result_invocation() {
    assert_no_reentry_control(
        "import { api } from './peer.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\n\
         const slot = api.makeSlot(void 0);\n\
         globalThis.observed = slot.invoke();\n\
         later = 42;\nconsole.log(globalThis.observed, globalThis.saved());\n",
        "export const api = { makeSlot() { return { invoke() { return globalThis.saved(); } }; } };\n",
        &["callback", "slot"],
        &[
            "globalThis.saved = callback;",
            "globalThis.observed = slot.invoke();",
        ],
        "0 42\n",
        true,
        false,
    );
}

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

#[test]
fn namespace_factory_keeps_unrelated_optional_member_values_independent() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const input = { message: 'ready' };\n\
             const message = input?.message;\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(message, wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("input"),
                        Member::new("message"),
                        Member::new("wrapped"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "ready 42\n");
}

#[test]
fn optional_namespace_member_aliases_still_disable_factory_deferral() {
    for (holder, lookup) in [
        ("{ api }", "holder?.api"),
        ("{ api }", "holder?.['api']"),
        ("{ get api() { return api; } }", "holder?.api"),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             let later = 0;\n\
             const holder = {holder};\n\
             const alias = {lookup};\n\
             const eager = callback => {{ globalThis.observed = callback(); return {{ invoke: () => callback() }}; }};\n\
             alias.make = eager;\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\n\
             console.log(globalThis.observed, wrapped.invoke());\n"
        );
        let peer = [(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )];
        let purity = [ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()];
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[
                        Member::new("later"),
                        Member::new("holder"),
                        Member::new("alias"),
                        Member::new("eager"),
                        Member::new("wrapped"),
                    ],
                    &["alias.make = eager;", "later = 42;"],
                )],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
        );
        assert_preserved_entry_output(&colocated, "0 42\n");
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &[
                            Member::new("holder"),
                            Member::new("alias"),
                            Member::new("eager"),
                            Member::new("wrapped"),
                        ],
                        &["alias.make = eager;"],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn returned_wrapper_does_not_invoke_its_callback_at_creation() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers));
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    let evidence = &report["imported_callables"]["factory"]["callback_factory_evidence"][0];
    assert_eq!(evidence["origin"], "inferred_immutable_wrapper");
    assert_eq!(evidence["defining_module"], "static/peer");
    assert_eq!(evidence["export"], "factory");
    assert_eq!(evidence["local"], "factory");
    assert_eq!(evidence["source_location"]["source_path"], "static/peer.js");
    assert_eq!(evidence["source_location"]["start_line"], 1);
    assert_eq!(evidence["source_location"]["end_line"], 1);
    assert_eq!(evidence["source_location"]["start_column"], 8);
    let graph = fixture.dependency_graph();
    assert_eq!(graph.imported_callable_summaries.len(), 1);
    assert_eq!(
        graph.imported_callable_summaries[0].imported_local,
        "factory"
    );
    assert!(matches!(
        &graph.imported_callable_summaries[0].callback_factory_evidence[0],
        analysis::CallbackFactoryEvidence::InferredImmutableWrapper { defining_module, export, local, .. }
            if defining_module == "static/peer" && export == "factory" && local == "factory"
    ));
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    let uses = &wrapped
        .synchronous_execution
        .as_ref()
        .unwrap()
        .callback_factory_uses;
    assert!(
        uses.iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::CallTiming)
    );
    assert!(
        uses.iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::ReturnShape)
    );
    assert!(uses.iter().all(|proof_use| proof_use.summary_ref
        == graph.imported_callable_summaries[0].id
        && proof_use.source_location.as_ref().unwrap().start_line == 2));
}

#[test]
fn inferred_two_parameter_module_factory_defers_initializer() {
    let peers = [(
        "static/peer",
        "export var factory = (initialize, cache) => () => {\n\
         if (!cache) { cache = { exports: {} }; initialize(cache.exports, cache); }\n\
         return cache.exports;\n\
         };\n",
    )];
    let source = "import { factory } from './peer.js';\n\
                  const wrapped = factory((exports) => { exports.value = later; });\n\
                  const later = 42;\n\
                  console.log(wrapped().value);\n\
                  export { wrapped, later };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert_eq!(
        report["imported_callables"]["factory"]["callback_factory_evidence"][0]["origin"],
        "inferred_immutable_wrapper",
    );
}

#[test]
fn inferred_factory_accepts_assignment_valued_fresh_member_argument() {
    let provider =
        "export var factory = (callback, cache) => () => callback((cache = { value: 1 }).value);\n";
    let source = "import { factory } from './peer.js';\n\
                  const wrapped = factory((value) => later + value);\n\
                  const later = 42;\n\
                  console.log(wrapped());\n\
                  export { wrapped, later };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
    );
    assert_preserved_entry_output(&fixture, "43\n");
}

#[test]
fn inferred_module_factory_rejects_extra_callback_roots_and_mutable_exports() {
    let app = "import { factory } from './peer.js';\n\
               const wrapped = factory(() => later);\n\
               const later = 42;\n\
               console.log(wrapped());\n";
    for provider in [
        // The outer call itself can invoke the supplied callback.
        "export var factory = (callback, cache) => (callback(), () => cache);\n",
        // The returned function can invoke an unrepresented host callback.
        "export var factory = (callback, cache) => () => { globalThis.saved(); return callback(); };\n",
        // A separate captured binding can read importer state when invoked.
        "const other = () => globalThis.saved(); export var factory = (callback, cache) => () => { other(); return callback(); };\n",
        // An inherited property is not part of the fresh returned graph.
        "export var factory = (callback, cache) => () => { cache = {}; return cache.constructor; };\n",
        "export var factory = (callback, cache) => () => { const other = () => globalThis.saved(); return callback(); };\n",
        "export var factory = (callback, cache) => () => { cache = { exports: {} }; return cache['exports']; };\n",
        "export var factory = (callback, cache) => () => callback((cache = { get value() { return globalThis.saved(); } }).value);\n",
        "export var factory = (callback, cache) => () => callback((cache = { value: 1 })['value']);\n",
        "export var factory = (callback, cache) => () => this;\n",
        "export var factory = (callback = () => 0, cache) => () => callback();\n",
        "export var factory = ({ callback }, cache) => () => callback();\n",
        "export var factory = (callback, cache) => () => callback(); factory = callback => callback();\n",
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                app,
                vec![
                    logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", provider)]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn inferred_module_factory_does_not_admit_a_second_supplied_capability() {
    let provider =
        "export var factory = (callback, cache) => () => { callback(); return cache(); };\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => 1, () => later);\n\
             const later = 42;\n\
             console.log(wrapped());\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", provider)]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn inferred_var_factory_keeps_a_caught_preinitialization_throw() {
    let source = "import './peer.js';\n";
    let peer = [
        (
            "static/peer",
            "import './trigger.js';\nexport var factory = (callback, cache) => () => callback();\n",
        ),
        (
            "static/trigger",
            "import { factory } from './peer.js';\n\
             let observed = 'none';\n\
             try { factory(() => 42); } catch (error) { observed = error.name; }\n\
             console.log(observed);\n",
        ),
    ];
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_extra_chunks(&peer));
    assert_preserved_entry_output(&fixture, "TypeError\n");
}

#[test]
fn audited_inline_factory_defers_its_single_callback() {
    let peers = [(
        "static/peer",
        "export function action(callback) {\n\
         function wrapped() { return callback(); }\n\
         wrapped.kind = 'action';\n\
         return wrapped;\n}\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { action as create } from './peer.js';\n\
             const wrapped = create(() => later);\n\
             const later = 42;\nconsole.log(wrapped());\n",
            modules(),
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_inline_callback_factories(&["action"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn inline_only_contract_keeps_other_call_shapes_conservative() {
    let peers = [(
        "static/peer",
        "export function action(callback) {\n\
         function wrapped() { return callback(); }\n\
         wrapped.kind = 'action';\n\
         return wrapped;\n}\n",
    )];
    for (setup, call, members) in [
        (
            "const callback = () => later;\n",
            "action(callback)",
            vec![Member::new("callback"), Member::new("wrapped")],
        ),
        ("", "action(...[() => later])", vec![Member::new("wrapped")]),
        ("", "new action(() => later)", vec![Member::new("wrapped")]),
    ] {
        let source = format!(
            "import {{ action }} from './peer.js';\n{setup}\
             const wrapped = {call};\nconst later = 42;\nconsole.log(wrapped());\n"
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/wrapped", &members),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_inline_callback_factories(&["action"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn inline_only_contract_does_not_compose_a_retained_object() {
    let peers = [(
        "static/peer",
        "export function make(callback) { return { invoke: () => callback() }; }\n\
         export function action(value) {\n\
           if (typeof value !== 'function') return value.invoke();\n\
           function wrapped() { return value(); }\n\
           wrapped.kind = 'action';\n\
           return wrapped;\n}\n",
    )];
    let source = "import { make, action } from './peer.js';\n\
                  let later = 0;\nconst result = action(make(() => later));\n\
                  later = 42;\nconsole.log(result);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("callbacks/result", &[Member::new("result")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["make"])
            .with_inline_callback_factories(&["action"])
            .build()]),
        &["callbacks/result", "values/later"],
        &[],
    );
}

#[test]
fn inline_only_contract_does_not_cross_an_unresolved_reexport() {
    let peers = [
        (
            "static/peer",
            "export function action(callback) {\n\
             function wrapped() { return callback(); }\n\
             wrapped.kind = 'action';\nreturn wrapped;\n}\n",
        ),
        (
            "static/bridge",
            "export { action as create } from './peer.js';\n",
        ),
    ];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { create } from './bridge.js';\n\
             const wrapped = create(() => later);\n\
             const later = 42;\nconsole.log(wrapped());\n",
            modules(),
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_inline_callback_factories(&["action"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn inline_only_result_invoked_before_later_write_remains_atomic() {
    let peers = [(
        "static/peer",
        "export function action(callback) {\n\
         function wrapped() { return callback(); }\n\
         wrapped.kind = 'action';\nreturn wrapped;\n}\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { action } from './peer.js';\n\
             let later = 0;\nconst wrapped = action(() => later);\n\
             const observed = wrapped();\nlater = 42;\nconsole.log(observed, later);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("observed")],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_inline_callback_factories(&["action"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn immediate_callback_invocation_preserves_the_tdz() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn successive_factories_do_not_publish_callbacks_to_the_next_factory() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const first = factory(() => later);\n\
         const second = factory(() => later);\n\
         const later = 42;\nconsole.log(first(), second());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("first"), Member::new("second")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42 42\n");
}

#[test]
fn invoking_returned_wrapper_before_initialization_is_rejected() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => later);\n\
             const outcome = wrapped();\nconst later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn escaped_wrapper_can_be_invoked_by_a_later_opaque_call() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => later);\n\
             globalThis.saved = wrapped;\nconst outcome = host();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn reassigned_provider_does_not_inherit_the_original_factory_summary() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\nfactory = callback => callback();\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn a_shadowing_parameter_does_not_inherit_the_import_contract() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             function invoke(factory) { return factory(() => later); }\n\
             const outcome = invoke(callback => callback());\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("outcome")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

const OBJECT_SOURCE: &str = "import { factory } from './peer.js';\n\
    const wrapped = factory(() => later);\n\
    const later = 42;\nconsole.log(wrapped.invoke());\n";

const OBJECT_FACTORY: &str = "let creations = 0;\n\
    export function factory(callback) { creations++; return { invoke: () => callback() }; }\n";

#[test]
fn audited_factory_keeps_callbacks_in_its_returned_object() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const wrapped = factory(() => later);\n\
         const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert_eq!(
        report["imported_callables"]["factory"]["callback_factory_evidence"],
        serde_json::json!([{ "origin": "author_assertion", "defining_module": "static/peer", "export": "factory" }]),
    );
    let graph = fixture.dependency_graph();
    assert_eq!(graph.imported_callable_summaries.len(), 1);
    assert_eq!(
        graph.imported_callable_summaries[0].callback_factory_evidence,
        vec![analysis::CallbackFactoryEvidence::AuthorAssertion {
            defining_module: "static/peer".into(),
            export: "factory".into(),
        }],
    );
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    assert!(
        wrapped
            .synchronous_execution
            .as_ref()
            .unwrap()
            .callback_factory_uses
            .iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::ReturnShape)
    );
}

#[test]
fn available_imported_factory_without_eligible_call_has_no_proof_use() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    for source in [
        "import { factory } from './peer.js';\nconsole.log(42);\n",
        "import { factory } from './peer.js';\nfactory(123);\nconsole.log(42);\n",
        "import { factory } from './peer.js';\nfunction use(factory) { return factory(() => 1); }\nconsole.log(use((callback) => callback()) + 41);\n",
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(source, vec![])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        let graph = fixture.dependency_graph();
        assert!(graph.imported_callable_summaries.is_empty());
        assert!(graph.nodes.iter().all(|node| {
            node.synchronous_execution
                .as_ref()
                .is_none_or(|execution| execution.callback_factory_uses.is_empty())
        }));
    }
}

#[test]
fn nested_repeated_factory_use_keeps_lexical_site_and_top_level_owner() {
    let source = "import { factory } from './peer.js';\n\
                  function wrap() { return factory(() => later); }\n\
                  const pair = [wrap(), wrap()];\n\
                  const later = 42;\n\
                  console.log(pair[0](), pair[1]());\n";
    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_extra_chunks(&[(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )]));
    assert_preserved_entry_output(&fixture, "42 42\n");
    let graph = fixture.dependency_graph();
    let pair = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "pair")
        })
        .unwrap();
    let uses = &pair
        .synchronous_execution
        .as_ref()
        .unwrap()
        .callback_factory_uses;
    assert_eq!(
        uses.iter()
            .filter(|proof_use| proof_use.kind == CallbackFactoryUseKind::CallTiming)
            .count(),
        1
    );
    assert_eq!(
        uses.iter()
            .filter(|proof_use| proof_use.kind == CallbackFactoryUseKind::ReturnShape)
            .count(),
        1
    );
    assert!(
        uses.iter().all(
            |proof_use| proof_use.lexical_statement_ordinal != pair.statement_ordinal
                && proof_use.source_location.as_ref().unwrap().start_line == 2
        )
    );
    let later = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "later")
        })
        .unwrap();
    assert!(
        later
            .synchronous_execution
            .as_ref()
            .unwrap()
            .callback_factory_uses
            .is_empty()
    );
    let mut old_graph = serde_json::to_value(&graph).unwrap();
    old_graph
        .as_object_mut()
        .unwrap()
        .remove("imported_callable_summaries");
    for node in old_graph["nodes"].as_array_mut().unwrap() {
        if let Some(execution) = node["synchronous_execution"].as_object_mut() {
            execution.remove("callback_factory_uses");
        }
    }
    let historical: analysis::DependencyGraphReport = serde_json::from_value(old_graph).unwrap();
    assert!(historical.imported_callable_summaries.is_empty());
}

#[test]
fn private_factory_result_records_reachability_exemption() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const wrapped = factory(() => later);\n\
         const later = 42;\n\
         console.log(wrapped());\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export function factory(callback) { return () => callback(); }\n",
        )]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let graph = fixture.dependency_graph();
    let wrapped = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "wrapped")
        })
        .unwrap();
    assert!(
        wrapped
            .synchronous_execution
            .as_ref()
            .unwrap()
            .callback_factory_uses
            .iter()
            .any(|proof_use| proof_use.kind == CallbackFactoryUseKind::ClosedResultRoot)
    );
}

#[test]
fn joined_imported_factories_preserve_behavior_without_false_direct_proof_use() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory as first } from './first.js';\n\
         import { factory as second } from './second.js';\n\
         let selected = first;\n\
         selected = second;\n\
         const wrapped = selected(() => later);\n\
         const later = 42;\n\
         console.log(wrapped());\n",
            vec![],
        )
        .with_extra_chunks(&[
            (
                "static/first",
                "export function factory(callback) { return () => callback(); }\n",
            ),
            (
                "static/second",
                "export function factory(callback) { return () => callback(); }\n",
            ),
        ]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
    let graph = fixture.dependency_graph();
    assert!(graph.imported_callable_summaries.is_empty());
}

#[test]
fn invoked_factory_result_does_not_run_its_callback_after_await_synchronously() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             import { consume } from './host.js';\n\
             const wrapped = factory(async () => { await 0; return later; });\n\
             const consumed = consume(wrapped);\n\
             const later = 42;\nconsole.log(later);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("consumed")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[
            ("static/peer", OBJECT_FACTORY),
            (
                "static/host",
                "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
            ),
        ])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factories(&["factory"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn invoked_factory_result_keeps_synchronous_callback_and_returned_callback_reads() {
    for (callback, consumer) in [
        (
            "async () => { globalThis.seen = later; await 0; }",
            "export function consume(value) { return Reflect.apply(value.invoke, value, []); }\n",
        ),
        (
            "() => () => { globalThis.seen = later; }",
            "export function consume(value) { return Reflect.apply(Reflect.apply(value.invoke, value, []), null, []); }\n",
        ),
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &format!(
                    "import {{ factory }} from './peer.js';\n\
                     import {{ consume }} from './host.js';\n\
                     let later = 0;\nconst wrapped = factory({callback});\n\
                     const consumed = consume(wrapped);\nlater = 42;\n\
                     console.log(globalThis.seen, later);\n"
                ),
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &[Member::new("wrapped"), Member::new("consumed")],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&[("static/peer", OBJECT_FACTORY), ("static/host", consumer)])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_callback_factories(&["factory"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn unknown_complex_factory_remains_conservative() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(OBJECT_SOURCE, modules()).with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn callback_factory_contract_does_not_apply_to_an_existing_callback() {
    let peers = [("static/peer", OBJECT_FACTORY)];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["factory"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const callback = () => later;\nconst wrapped = factory(callback);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("callback")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn purity_alone_does_not_defer_callbacks() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return callback(); }\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_pure_exports(&["factory"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(SOURCE, modules())
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn callbacks_written_to_the_returned_value_remain_observable() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
             const wrapped = factory(() => 0);\nwrapped.read = () => later;\n\
             const outcome = wrapped.read();\nconst later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn import_and_local_aliases_preserve_factory_identity() {
    let peers = [(
        "static/peer",
        "function build(callback) { return () => callback(); }\nexport { build as factory };\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory as create } from './peer.js';\nconst alias = create;\n\
         const wrapped = alias(() => later);\nconst later = 42;\nconsole.log(wrapped());\n",
            modules(),
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn result_mutation_cannot_hide_an_early_getter_read() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    for (property, read) in [
        ("__proto__", "wrapped.value"),
        ("options", "wrapped.options.value"),
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\n\
             const wrapped = factory(() => 0);\n\
             const configured = (wrapped.{property} = {{ get value() {{ return later; }} }});\n\
             const outcome = {read};\nconst later = 42;\nconsole.log(outcome);\n",
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &[
                            Member::new("wrapped"),
                            Member::new("configured"),
                            Member::new("outcome"),
                        ],
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&peers),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}

#[test]
fn primitive_metadata_writes_keep_the_callback_deferred() {
    let peers = [(
        "static/peer",
        "export function factory(callback) { return () => callback(); }\n",
    )];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { factory } from './peer.js';\n\
         const wrapped = factory(() => later);\n\
         const named = (wrapped.label = 'factory');\n\
         const later = 42;\nconsole.log(wrapped(), wrapped.label);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("named")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers),
    );
    assert_preserved_entry_output(&fixture, "42 factory\n");
}

const NAMESPACE_FACTORY: &str = "export const api = {\n\
    make(callback) { return { invoke: () => callback() }; },\n\
    wrap(inner) { return { invoke: () => inner.invoke() }; },\n\
};\n";

#[test]
fn namespace_factories_compose_without_eager_callback_reads() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_ignores_local_subclass_prototype_argument() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             class LocalBase {}\n\
             class LocalChild extends LocalBase {}\n\
             function observe(value) { void value; }\n\
             observe(LocalChild.prototype);\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[
                        Member::new("wrapped"),
                        Member::new("LocalBase"),
                        Member::new("LocalChild"),
                        Member::new("observe"),
                    ],
                    &["observe(LocalChild.prototype);"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_retains_inherited_static_namespace_alias() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             class LocalBase { static shared = api; }\n\
             class LocalChild extends LocalBase {}\n\
             let later = 0;\n\
             LocalChild.shared.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped);\n",
            vec![logical_module_with_anon(
                "app",
                &[
                    Member::new("LocalBase"),
                    Member::new("LocalChild"),
                    Member::new("wrapped"),
                    Member::new("later"),
                ],
                &[
                    "LocalChild.shared.make = callback => callback();",
                    "later = 42;",
                ],
            )],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "0\n");
}

#[test]
fn namespace_factory_rejects_split_inherited_static_namespace_alias() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             class LocalBase { static shared = api; }\n\
             class LocalChild extends LocalBase {}\n\
             let later = 0;\n\
             LocalChild.shared.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped);\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[
                        Member::new("LocalBase"),
                        Member::new("LocalChild"),
                        Member::new("wrapped"),
                    ],
                    &["LocalChild.shared.make = callback => callback();"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_ignores_self_initialized_unrelated_object() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             var state = state || { value: 0 };\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke(), state.value);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("state"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42 0\n");
}

#[test]
fn namespace_factory_allows_adjacent_fresh_own_field_writes() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { api } from './peer.js';
class Provider {
    limit = 1 / 0;
    candidates = [].values;
    async prepare() {}
}
const provider = new Provider();
provider.limit = 1 / 0;
provider.candidates = () => [];
const wrapped = api.make(() => later);
const later = 42;
console.log(wrapped.invoke(), provider.limit, provider.candidates().length);
"#,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[
                        Member::new("Provider"),
                        Member::new("provider"),
                        Member::new("wrapped"),
                    ],
                    &["provider.limit = 1 / 0;", "provider.candidates = () => [];"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42 Infinity 0\n");
}

#[test]
fn namespace_factory_tracks_namespace_seed_in_self_initializer() {
    assert_namespace_factory_publication_rejected(
        "var state = state || api;\n\
         globalThis.exposed = state;\n",
    );
}

#[test]
fn namespace_factory_keeps_unproven_instance_writes_conservative() {
    for prefix in [
        "class Provider { value = 0; constructor() {} }\nconst provider = new Provider();\nprovider.value = 0;\n",
        "class Base { value = 0; }\nclass Provider extends Base {}\nconst provider = new Provider();\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\ninspect(provider);\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\ndelete provider.value;\nprovider.value = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\nprovider.other = 0;\n",
        "class Provider { value = 0; }\nconst provider = new Provider();\nprovider.value = computeValue();\n",
    ] {
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &format!(
                    "import {{ api }} from './peer.js';\n\
                     function inspect(value) {{ return value; }}\n\
                     function computeValue() {{ return 0; }}\n{prefix}\
                     const wrapped = api.make(() => later);\n\
                     const later = 42;\nconsole.log(wrapped.invoke());\n"
                ),
                modules(),
            )
            .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_callback_factory_members("api", &["make"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn namespace_factory_defers_member_reads_inside_callback_result_literals() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ field: api.other, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: () => callback() }; },\n\
                 other: 'member',\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_ignores_member_callees_in_callback_result_literals() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ onEvent: api.register(() => later) }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().onEvent());\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: () => callback() }; },\n\
                 register(callback) { return callback; },\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn audited_export_factory_result_allows_an_adjacent_metadata_write() {
    for contract in [
        ChunkExportPurityBuilder::new("static/peer").with_callback_factories(&["outer"]),
        ChunkExportPurityBuilder::new("static/peer").with_inline_callback_factories(&["outer"]),
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(
                "import { api, outer } from './peer.js';\n\
             const result = outer(() => 0);\n\
             result.label = 'factory';\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(result.label, wrapped.invoke());\n",
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &[Member::new("result"), Member::new("wrapped")],
                        &["result.label = 'factory';"],
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[(
                "static/peer",
                "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
             export function outer(callback) {\n\
                 function wrapped() { return callback(); }\n\
                 wrapped.kind = 'factory';\nreturn wrapped;\n}\n",
            )])
            .with_chunk_export_purity(&[contract
                .with_callback_factory_members("api", &["make"])
                .build()]),
        );
        assert_preserved_entry_output(&fixture, "factory 42\n");
    }
}

#[test]
fn audited_factory_result_in_another_namespace_allows_an_adjacent_metadata_write() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api, other } from './peer.js';\n\
             const result = other.make(() => 0);\n\
             result.label = 'factory';\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(result.label, wrapped.invoke());\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[Member::new("result"), Member::new("wrapped")],
                    &["result.label = 'factory';"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
             export const other = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .with_callback_factory_members("other", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "factory 42\n");
}

#[test]
fn an_opaque_mutation_between_factory_result_and_metadata_write_stays_conservative() {
    const SOURCE: &str = "import { api, outer } from './peer.js';\n\
        import { install_label_setter } from './host.js';\n\
        let later = 0;\n\
        const result = outer(() => later);\n\
        install_label_setter(result);\n\
        result.label = 'factory';\n\
        const wrapped = api.make(() => later);\n\
        later = 42;\n\
        console.log(result.label, globalThis.seen, wrapped.invoke(), later);\n";
    let peers = [
        (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
             export function outer(callback) { return { invoke: () => callback() }; }\n",
        ),
        (
            "static/host",
            "export function install_label_setter(value) {\n\
                 Object.defineProperty(value, 'label', {\n\
                     set() { globalThis.seen = value.invoke(); },\n\
                     get() { return 'factory'; },\n\
                 });\n\
             }\n",
        ),
    ];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factories(&["outer"])
        .with_callback_factory_members("api", &["make"])
        .build()];
    let preserved = run_fixture(
        FixtureOpts::new(SOURCE, vec![])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&preserved, "factory 0 42 42\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[Member::new("result"), Member::new("wrapped")],
                    &["install_label_setter(result);", "result.label = 'factory';"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn a_replaced_factory_returning_another_namespace_stays_conservative() {
    const SOURCE: &str = "import { api, other } from './peer.js';\n\
        import { getNamespace } from './host.js';\n\
        other.make = getNamespace;\n\
        const result = other.make(() => 0);\n\
        result.label = 'factory';\n\
        result.make = callback => callback();\n\
        let later = 0;\n\
        const wrapped = api.make(() => later);\n\
        later = 42;\n\
        console.log(result.label, wrapped, later);\n";
    let peers = [
        (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n\
         export const other = { make(callback) { return { invoke: () => callback() }; } };\n",
        ),
        (
            "static/host",
            "import { api } from './peer.js';\nexport function getNamespace() { return api; }\n",
        ),
    ];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .with_callback_factory_members("other", &["make"])
        .build()];
    let preserved = run_fixture(
        FixtureOpts::new(SOURCE, vec![])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&preserved, "factory 0 42\n");

    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[Member::new("result"), Member::new("wrapped")],
                    &[
                        "other.make = getNamespace;",
                        "result.label = 'factory';",
                        "result.make = callback => callback();",
                    ],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_factory_ignores_unrelated_call_results_outside_mutation_receivers() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function lookup() { return {}; }\n\
             const wrapped = api.make(() => {\n\
                 const unrelated = lookup();\n\
                 return { unrelated, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_ignores_unrelated_constructor_results_outside_mutation_receivers() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const stack = new Error().stack;\n\
                 return { stack, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_keeps_unrelated_constructor_write_receivers_conservative() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const receiver = new Error();\n\
                 receiver.value = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_tracks_namespace_stored_in_local_class_field() {
    assert_namespace_factory_publication_rejected(
        "class Holder { alias = api; }\n\
         const holder = new Holder();\n\
         globalThis.exposed = holder.alias;\n",
    );
}

fn assert_namespace_factory_publication_rejected(prefix: &str) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &format!(
                "import {{ api }} from './peer.js';\n{prefix}\
                 const wrapped = api.make(() => later);\n\
                 const later = 42;\nconsole.log(wrapped.invoke());\n"
            ),
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_tracks_class_static_private_constructor_and_inherited_carriers() {
    for prefix in [
        "class Holder { static alias = api; }\n\
         globalThis.exposed = Holder.alias;\n",
        "class Holder { #alias = api; read() { return this.#alias; } }\n\
         globalThis.exposed = new Holder().read();\n",
        "class Holder { read() { return api; } }\n\
         globalThis.exposed = new Holder().read();\n",
        "class Holder { constructor() { this.alias = api; } }\n\
         globalThis.exposed = new Holder().alias;\n",
        "class Holder { constructor() { return { alias: api }; } }\n\
         globalThis.exposed = new Holder().alias;\n",
        "class Base { alias = api; }\n\
         class Holder extends Base {}\n\
         globalThis.exposed = new Holder().alias;\n",
        "class Holder { alias = api; }\n\
         class Empty {}\n\
         let Factory = Holder;\n\
         Factory = Empty;\n\
         globalThis.exposed = new Factory().alias;\n",
        "class Holder { alias = api; }\n\
         class Empty {}\n\
         const choose = true;\n\
         let Factory = choose ? Holder : Empty;\n\
         globalThis.exposed = new Factory().alias;\n",
        "class Holder { payload = { alias: api }; }\n\
         globalThis.exposed = new Holder().payload.alias;\n",
        "class Holder { static value = Holder.value; }\n\
         globalThis.exposed = Holder.value;\n",
    ] {
        assert_namespace_factory_publication_rejected(prefix);
    }
}

#[test]
fn namespace_factory_tracks_namespace_from_object_destructuring() {
    assert_namespace_factory_publication_rejected(
        "const { alias } = { alias: api };\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_tracks_default_from_object_assignment_destructuring() {
    assert_namespace_factory_publication_rejected(
        "let alias;\n\
         ({ alias = api } = {});\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_unknown_destructuring_conservative() {
    assert_namespace_factory_publication_rejected(
        "const { alias } = globalThis.untrusted?.namespace;\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_unknown_binding_conservative() {
    assert_namespace_factory_publication_rejected(
        "const alias = globalThis.untrusted?.namespace;\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_unknown_optional_call_conservative() {
    assert_namespace_factory_publication_rejected(
        "const alias = globalThis.untrusted?.();\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_keeps_array_destructuring_conservative() {
    assert_namespace_factory_publication_rejected(
        "const [alias] = [api];\n\
         globalThis.exposed = alias;\n",
    );
}

#[test]
fn namespace_factory_class_field_alias_observes_api_mutation_at_runtime() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             class Holder { alias = api; }\n\
             globalThis.exposed = new Holder().alias;\n\
             globalThis.exposed.make = callback => {\n\
                 globalThis.changed = true;\n\
                 return { invoke: callback };\n\
             };\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(globalThis.changed, wrapped.invoke());\n",
            vec![],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "true 42\n");
}

#[test]
fn namespace_factory_keeps_computed_member_callees_conservative_in_literals() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ onEvent: api[method](() => later) }));\n\
             const method = 'register';\n\
             const later = 42;\nconsole.log(wrapped.invoke().onEvent());\n",
            vec![logical_module(
                "callbacks/wrapped",
                &[Member::new("wrapped")],
            )],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: () => callback() }; },\n\
                 register(callback) { return callback; },\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn unrelated_primitive_operator_results_do_not_disable_namespace_factories() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function makeKey(name, value) {\n\
                 Symbol.for('prefix' + name);\n\
                 return 1 / 0 + (value + 1) + (value > 0) + typeof value + void value + !value + value++;\n\
             }\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn primitive_operator_operands_keep_callback_reentry_visible() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function makeKey(read) { return Symbol.for('prefix' + read()); }\n\
             const key = makeKey(() => later);\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(key.description, wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("key"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &["eager_use"],
    );
}

#[test]
fn primitive_operator_coercion_keeps_key_with_later_value() {
    let source = "function makeKey(value) { return 'prefix' + value; }\n\
                 let later = 0;\n\
                 const key = makeKey({ [Symbol.toPrimitive]() { return later; } });\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[Member::new("key"), Member::new("later")],
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
}

#[test]
fn ordinary_to_primitive_fallback_keeps_key_with_later_value() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "function makeKey(value) { return 'prefix' + value; }\n\
             let later = 0;\n\
             const key = makeKey({\n\
                 valueOf() { return {}; },\n\
                 toString() { return later; },\n\
             });\n\
             later = 42;\n\
             console.log(key, later);\n",
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
}

#[test]
fn compound_assignment_member_target_keeps_key_with_later_value() {
    let source = "let later = 0;\n\
                 const box = { value: { [Symbol.toPrimitive]() { return later; } } };\n\
                 const key = box.value += 'x';\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("box"), Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[Member::new("box"), Member::new("key"), Member::new("later")],
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "0x 42\n");
}

#[test]
fn symbol_to_primitive_getter_return_keeps_key_with_later_value() {
    let source = "function makeKey(value) { return 'prefix' + value; }\n\
                 let later = 0;\n\
                 const key = makeKey({\n\
                     get [Symbol.toPrimitive]() { return () => later; },\n\
                 });\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("values/key", &[Member::new("key")]),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[Member::new("key"), Member::new("later")],
            &["later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
}

#[test]
fn regex_literal_with_custom_to_primitive_keeps_key_with_later_value() {
    let source = "let later = 0;\n\
                 const object = /x/;\n\
                 object[Symbol.toPrimitive] = () => later;\n\
                 const key = 'prefix' + object;\n\
                 later = 42;\n\
                 console.log(key, later);\n";
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "values/key",
                    &[Member::new("object"), Member::new("key")],
                    &["object[Symbol.toPrimitive] = () => later;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        ),
        &["values/key", "values/later"],
        &["eager_use"],
    );
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module_with_anon(
            "values",
            &[
                Member::new("object"),
                Member::new("key"),
                Member::new("later"),
            ],
            &["object[Symbol.toPrimitive] = () => later;", "later = 42;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "prefix0 42\n");
    let graph = fixture.dependency_graph();
    let key = source_unit_for_binding(&graph, "key");
    let later = source_unit_for_binding(&graph, "later");
    assert!(graph.edges.iter().any(|edge| {
        edge.source == key
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
}

#[test]
fn strict_equality_and_known_primitive_operands_skip_coercion_callbacks() {
    let fixture = run_fixture(FixtureOpts::new(
        "const object = { [Symbol.toPrimitive]() { return later; } };\n\
         const same = object === null;\n\
         const key = 'prefix' + (1 + 2);\n\
         const later = 42;\n\
         console.log(same, key, later);\n",
        vec![
            logical_module("values/key", &[Member::new("same"), Member::new("key")]),
            logical_module("values/later", &[Member::new("later")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "false prefix3 42\n");
    let graph = fixture.dependency_graph();
    let comparison = source_unit_for_binding(&graph, "same");
    let later = source_unit_for_binding(&graph, "later");
    assert!(!graph.edges.iter().any(|edge| {
        edge.source == comparison
            && edge.target == later
            && edge.binding.as_deref() == Some("later")
            && edge.edge_kind == DepKind::EagerUse
            && edge.constrains_init_order
    }));
}

#[test]
fn logical_operator_results_still_preserve_namespace_alternatives() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => api || later);\n\
             const later = 42;\n\
             console.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_allows_audited_method_results_and_unrelated_callback_receivers() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(ref => {\n\
                 ref.current[dynamic] = api.createRef();\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'slot';\n\
             const later = 42;\n\
             console.log(wrapped.invoke({ current: {} }).value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = {\n\
                 make(callback) { return { invoke: value => callback(value) }; },\n\
                 createRef() { return {}; },\n\
                 other: 'member',\n\
             };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42\n");
}

#[test]
fn namespace_factory_keeps_fresh_container_identity_separate_from_contents() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const holder = { api, metadata: 0 };\n\
             holder.metadata = 1;\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             console.log(holder.metadata, wrapped.invoke());\n",
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[Member::new("holder"), Member::new("wrapped")],
                    &["holder.metadata = 1;"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "1 42\n");
}

#[test]
fn namespace_inside_fresh_container_can_still_replace_a_factory_member() {
    let source = "import { api } from './peer.js';\n\
                  let later = 0;\n\
                  const holder = { api, metadata: 0 };\n\
                  holder.api.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };\n\
                  const wrapped = api.make(() => later);\n\
                  later = 42;\n\
                  console.log(globalThis.observed, wrapped.invoke());\n";
    let peer = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let purity = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .build()];
    let write = "holder.api.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };";
    let colocated = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &[
                    Member::new("holder"),
                    Member::new("wrapped"),
                    Member::new("later"),
                ],
                &[write, "later = 42;"],
            )],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
    );
    assert_preserved_entry_output(&colocated, "0 42\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[Member::new("holder"), Member::new("wrapped")],
                    &[write],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peer)
        .with_chunk_export_purity(&purity),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_container_publication_and_uncertain_receivers_remain_conservative() {
    for (setup, side_effects, expected) in [
        (
            "const holder = { api }; globalThis.published = holder;",
            vec!["globalThis.published = holder;"],
            "undefined 42\n",
        ),
        (
            "let holder = { metadata: 0 }; holder = api; holder.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder = api;",
                "holder.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = { get member() { return api; } }; holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = { member: 0 }; Object.defineProperty(holder, 'member', { get: () => api }); holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "Object.defineProperty(holder, 'member', { get: () => api });",
                "holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = new Proxy({ member: api }, {}); holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder.member.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
        (
            "const holder = { __proto__: api }; holder.__proto__.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            vec![
                "holder.__proto__.make = callback => { globalThis.observed = callback(); return { invoke: () => callback() }; };",
            ],
            "0 42\n",
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\nlet later = 0;\n{setup}\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(globalThis.observed, wrapped.invoke());\n"
        );
        let peer = [(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )];
        let purity = [ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()];
        let colocated = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module_with_anon(
                    "all",
                    &[
                        Member::new("holder"),
                        Member::new("wrapped"),
                        Member::new("later"),
                    ],
                    &side_effects
                        .iter()
                        .copied()
                        .chain(["later = 42;"])
                        .collect::<Vec<_>>(),
                )],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
        );
        assert_preserved_entry_output(&colocated, expected);
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &[Member::new("holder"), Member::new("wrapped")],
                        &side_effects,
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peer)
            .with_chunk_export_purity(&purity),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn namespace_factory_contract_stays_conservative_when_callback_returns_namespace() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ field: api, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_stays_conservative_for_namespace_aliases() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const namespace = api;\n\
                 return { field: namespace, value: later };\n\
             });\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_computed_namespace_mutation() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 api[dynamic] = callback => callback();\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_dynamic_write_through_member_alias() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const target = api.other;\n\
                 target[dynamic] = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_rejects_unknown_call_results_as_write_receivers() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => {\n\
                 const receiver = lookup();\n\
                 receiver[dynamic] = 0;\n\
                 return { field: api.other, value: later };\n\
             });\n\
             const dynamic = 'make';\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: 'member' };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_factory_contract_stays_conservative_for_other_member_shapes() {
    for member_read in ["api['other']", "api.other.value"] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             const wrapped = api.make(() => ({{ field: {member_read}, value: later }}));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[(
                "static/peer",
                "export const api = { make(callback) { return { invoke: () => callback() }; }, other: { value: 'member' } };\n",
            )])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_callback_factory_members("api", &["make"])
                .build()]),
            &["callbacks/wrapped", "values/later"],
            &["eager_use", "sequenced"],
        );
    }
}

#[test]
fn namespace_factory_contract_rejects_prototype_setter_member_values() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => ({ __proto__: api.other, value: later }));\n\
             const later = 42;\nconsole.log(wrapped.invoke().value);\n",
            vec![
                logical_module("callbacks/wrapped", &[Member::new("wrapped")]),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[ (
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; }, other: {} };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &["eager_use", "sequenced"],
    );
}

#[test]
fn namespace_composed_result_invoked_early_is_rejected() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             const outcome = wrapped.invoke();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_factory_contract_does_not_trust_a_replaced_member() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             api.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn pure_member_assertion_alone_does_not_defer_callback_reads() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            modules(),
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_pure_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_result_published_before_initialization_is_rejected() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.wrap(api.make(() => later));\n\
             globalThis.saved = wrapped;\n\
             const outcome = host();\n\
             const later = 42;\nconsole.log(outcome);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("outcome")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn unlisted_namespace_member_stays_opaque() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const wrapped = api.other(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { other(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_alias_mutation_disables_the_factory_contract() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const alias = api;\n\
             alias.make = callback => callback();\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped);\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("alias"), Member::new("wrapped")],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn altered_retained_object_cannot_hide_an_early_accessor_read() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             const inner = api.make(() => 0);\n\
             const configured = Object.defineProperty(inner, 'invoke', { get() { return later; } });\n\
             const wrapped = api.wrap(inner);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n",
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[
                        Member::new("inner"),
                        Member::new("configured"),
                        Member::new("wrapped"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", NAMESPACE_FACTORY)])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make", "wrap"])
            .build()]),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
}

#[test]
fn namespace_factory_ignores_adjacent_fresh_error_metadata_writes() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { api } from './peer.js';\n\
             function helper(result, message, first, second) {\n\
               globalThis.fixtureValue = 1;\n\
               Object.keys(globalThis);\n\
               Object.getOwnPropertyDescriptor(globalThis, 'Error');\n\
               return (result = Error(message), result.name = 'factory', result.error = first, result.suppressed = second, result);\n\
             }\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\n\
             const error = helper(null, 'message', 1, 2);\n\
             console.log(wrapped.invoke(), error.name, error.error, error.suppressed);\n",
            modules(),
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42 factory 1 2\n");
}

#[test]
fn fresh_error_metadata_proof_keeps_namespace_mutation_conservative() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .build()];
    for (helper, setup, has_global_alias) in [
        (
            "function helper(result, first) { result.make = first; return (result = Error('x'), result.make = first, result); }",
            None,
            false,
        ),
        (
            "function helper(result, first) { function Error() { return api; } return (result = Error('x'), result.make = first, result); }",
            None,
            false,
        ),
        (
            "function helper(result, first) { return (result = Error('x', { cause: api }).cause, result.make = first, result); }",
            None,
            false,
        ),
        (
            "function helper(result, first) { function replace() { result = api; } return (result = Error('x'), replace(), result.make = first, result); }",
            None,
            false,
        ),
        (
            "function helper(result, first) { return (result = lookup(), result.make = first, result); } function lookup() { return api; }",
            None,
            false,
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } globalThis.Error = function() { return api; };",
            Some("globalThis.Error = function() { return api; };"),
            false,
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } globalThis['Error'] = function() { return api; };",
            Some("globalThis['Error'] = function() { return api; };"),
            false,
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } Reflect.set(globalThis, 'Error', function() { return api; });",
            Some("Reflect.set(globalThis, 'Error', function() { return api; });"),
            false,
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } Object.defineProperty(globalThis, 'Error', { value: function() { return api; } });",
            Some(
                "Object.defineProperty(globalThis, 'Error', { value: function() { return api; } });",
            ),
            false,
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } const globalAlias = globalThis; globalAlias.Error = function() { return api; };",
            Some("globalAlias.Error = function() { return api; };"),
            true,
        ),
        (
            "function helper(result, first) { return (result = Error('x'), result.make = first, result); } Error = function() { return api; };",
            Some("Error = function() { return api; };"),
            false,
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n{helper}\n\
             function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
             helper(api, eager);\n\
             let later = 0;\nconst wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let mut callback_members = vec![
            Member::new("wrapped"),
            Member::new("helper"),
            Member::new("eager"),
        ];
        if has_global_alias {
            callback_members.push(Member::new("globalAlias"));
        }
        let mut callback_statements = vec!["helper(api, eager);"];
        if let Some(setup) = setup {
            callback_statements.push(setup);
        }
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &callback_members,
                        &callback_statements,
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "0\n");
    }
}

#[test]
fn fresh_error_metadata_proof_requires_every_write_to_be_adjacent() {
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .build()];
    for body in [
        "result.name = 'before'; return (result = Error(message), result.name = 'factory', result);",
        "return (result = Error(message), noop(), result.name = 'factory', result);",
        "return (result = Error(message), result.name = 'factory', result.name += 'suffix', result);",
        "return (result = Error(message), result.stack = 'factory', result);",
        "return (result = Error(message), result.__proto__ = null, result);",
        "return (result = Error(message), result.name = unknownValue, result);",
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             function noop() {{}}\n\
             function helper(result, message) {{ {body} }}\n\
             const wrapped = api.make(() => later);\n\
             const later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, modules())
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("later")],
                )],
            )
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "42\n");
    }
}

#[test]
fn destructuring_namespace_member_writes_cannot_hide_early_callback_reads() {
    const PEER: &str =
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n";
    for (setup, peer) in [
        ("({ make: api.make } = { make: eager });", PEER),
        ("[api.make] = [eager];", PEER),
        ("({ make: api.make = eager } = {});", PEER),
        ("[api.make = eager] = [];", PEER),
        (
            "({ nested: { make: api.make } } = { nested: { make: eager } });",
            PEER,
        ),
        ("[[api.make]] = [[eager]];", PEER),
        ("({ make: api['make'] } = { make: eager });", PEER),
        ("for ({ make: api.make } of [{ make: eager }]) {}", PEER),
        (
            "({ ...api.methods } = { make: eager });",
            "const deferred = callback => ({ invoke: () => callback() });\n\
             export const api = { methods: { make: deferred }, make(callback) { return this.methods.make(callback); } };\n",
        ),
        (
            "[...api.methods] = [eager];",
            "const deferred = callback => ({ invoke: () => callback() });\n\
             export const api = { methods: [deferred], make(callback) { return this.methods[0](callback); } };\n",
        ),
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
             let later = 0;\n{setup}\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let peers = [("static/peer", peer)];
        let contracts = [ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &[Member::new("wrapped"), Member::new("eager")],
                        &[setup],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "0\n");
    }
}

#[test]
fn unrelated_destructuring_member_writes_keep_factory_callbacks_deferred() {
    let source = "import { api } from './peer.js';\n\
                  const target = {};\n\
                  ({ kind: target.kind } = { kind: 'metadata' });\n\
                  const wrapped = api.make(() => later);\n\
                  const later = 42;\nconsole.log(wrapped.invoke(), target.kind);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("target")],
                    &["({ kind: target.kind } = { kind: 'metadata' });"],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )])
        .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()]),
    );
    assert_preserved_entry_output(&fixture, "42 metadata\n");
}

#[test]
fn destructuring_member_values_keep_namespace_publication_visible() {
    for setup in [
        "({ value: target.value } = { value: api });",
        "({ value: target.value = api } = {});",
        "[target.value = api] = [];",
    ] {
        let source = format!(
            "import {{ api }} from './peer.js';\n\
             function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
             const target = {{}};\nlet later = 0;\n{setup}\n\
             target.value.make = eager;\n\
             const wrapped = api.make(() => later);\n\
             later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let peers = [(
            "static/peer",
            "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
        )];
        let contracts = [ChunkExportPurityBuilder::new("static/peer")
            .with_callback_factory_members("api", &["make"])
            .build()];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon(
                        "callbacks/wrapped",
                        &[
                            Member::new("wrapped"),
                            Member::new("target"),
                            Member::new("eager"),
                        ],
                        &[setup, "target.value.make = eager;"],
                    ),
                    logical_module_with_anon(
                        "values/later",
                        &[Member::new("later")],
                        &["later = 42;"],
                    ),
                ],
            )
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "0\n");
    }
}

#[test]
fn private_namespace_capabilities_keep_unrelated_receivers_independent() {
    let source = "import { api } from './peer.js';\n\
        function shadow(api) { return api.unlisted; }\n\
        class Buffer { get value() { return this.item; } set(value) { this.item = value; } }\n\
        const buffer = new Buffer();\n\
        const marker = api.marker;\n\
        const children = api.children.list([1, 2]);\n\
        class Local extends api.Base {}\n\
        const wrapped = api.make(() => later);\n\
        const later = 42;\n\
        buffer.set(children.length);\n\
        console.log(wrapped.invoke(), marker, buffer.value, new Local().value);\n";
    for facade in [
        "export const api = backing;",
        "export const api = { get marker() { return backing.marker; }, get children() { return backing.children; }, get Base() { return backing.Base; }, get make() { return backing.make; } };",
    ] {
        let peer = format!(
            "const backing = {{ marker: 'ready', children: {{ list(items) {{ return items; }} }}, Base: class {{ value = 7; }}, make(callback) {{ return {{ invoke: () => callback() }}; }} }};{facade}\n"
        );
        let fixture = run_fixture(
            FixtureOpts::new(
                source,
                vec![
                    logical_module(
                        "callbacks/wrapped",
                        &[
                            Member::new("shadow"),
                            Member::new("Buffer"),
                            Member::new("buffer"),
                            Member::new("marker"),
                            Member::new("children"),
                            Member::new("Local"),
                            Member::new("wrapped"),
                        ],
                    ),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_extra_chunks(&[("static/peer", &peer)])
            .with_chunk_export_purity(&[ChunkExportPurityBuilder::new("static/peer")
                .with_callback_factory_members("api", &["make"])
                .with_namespace_independent_members("api", &["make", "marker", "children", "Base"])
                .build()]),
        );
        assert_preserved_entry_output(&fixture, "42 ready 2 7\n");
    }
}

#[test]
fn namespace_capability_privacy_rejects_explicit_namespace_routes() {
    let peers = [(
        "static/peer",
        "export const api = { marker: 'ready', make(callback) { return { invoke: () => callback() }; } };\nexport const other = api;\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .with_namespace_independent_members("api", &["make", "marker"])
        .with_namespace_independent_members("other", &["make", "marker"])
        .build()];
    for (declarations, setup, binding) in [
        ("", "api.make = eager;", None),
        ("const alias = api;", "alias.make = eager;", Some("alias")),
        (
            "const holder = { api };",
            "holder.api.make = eager;",
            Some("holder"),
        ),
        (
            "const holder = { __proto__: api };",
            "Object.getPrototypeOf(holder).make = eager;",
            Some("holder"),
        ),
        (
            "",
            "Object.defineProperty(api, 'make', { value: eager });",
            None,
        ),
        (
            "",
            "{ Object.defineProperty(api, 'marker', { get() { api.make = eager; return 'ready'; } }); api.marker; }",
            None,
        ),
        (
            "function replace() { this.make = eager; }",
            "replace.call(api);",
            Some("replace"),
        ),
        (
            "function replace() { this.api.make = eager; }",
            "replace.call({ api });",
            Some("replace"),
        ),
        (
            "function replace() { this.make = eager; }",
            "Reflect.apply(replace, api, []);",
            Some("replace"),
        ),
        (
            "function replace() { this.make = eager; }",
            "replace.apply(api, []);",
            Some("replace"),
        ),
        (
            "function replace() { this.make = eager; }",
            "replace.bind(api)();",
            Some("replace"),
        ),
        ("", "({ make: api.make } = { make: eager });", None),
        ("", "[api.make] = [eager];", None),
        ("", "api['make'] = eager;", None),
        ("const alias = other;", "alias.make = eager;", Some("alias")),
    ] {
        eprintln!("namespace route: {declarations} {setup}");
        let source = format!(
            "import {{ api, other }} from './peer.js';\n\
            let later = 0;\n\
            function eager(callback) {{ const value = callback(); return {{ invoke: () => value }}; }}\n\
            {declarations}\n{setup}\n\
            const wrapped = api.make(() => later);\n\
            later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        // All setup belongs to wrapped; only the lexical cell and its final
        // assignment belong to the other destination.
        let mut members = vec![Member::new("eager"), Member::new("wrapped")];
        if let Some(binding) = binding {
            members.push(Member::new(binding));
        }
        let split = vec![
            logical_module_with_anon("callbacks/wrapped", &members, &[setup]),
            logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
        ];
        expect_atomic_conflict_rejection(
            FixtureOpts::new(&source, split)
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
        let colocated = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&colocated, "0\n");
    }
}

#[test]
fn namespace_capability_privacy_preserves_early_result_invocation() {
    let source = "import { api } from './peer.js';\n\
        let later = 0;\n\
        const wrapped = api.make(() => later);\n\
        const observed = wrapped.invoke();\n\
        later = 42;\nconsole.log(observed);\n";
    let peers = [(
        "static/peer",
        "export const api = { make(callback) { return { invoke: () => callback() }; } };\n",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/peer")
        .with_callback_factory_members("api", &["make"])
        .with_namespace_independent_members("api", &["make"])
        .build()];
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "callbacks/wrapped",
                    &[Member::new("wrapped"), Member::new("observed")],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&peers)
        .with_chunk_export_purity(&contracts),
        &["callbacks/wrapped", "values/later"],
        &[],
    );
    let colocated = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
    );
    assert_preserved_entry_output(&colocated, "0\n");
}

#[test]
fn namespace_capability_privacy_requires_total_supported_family_uses() {
    let peers = [(
        "static/peer",
        "export const api = { marker: 1, make(callback) { return { invoke: () => callback() }; } }; export const other = api;\n",
    )];
    for (use_source, complete, timing) in [
        ("const ignored = api['marker'];", true, true),
        ("const ignored = api?.marker;", true, true),
        ("const ignored = api.make?.(() => 1);", true, true),
        ("const ignored = api.marker;", false, true),
        ("const ignored = other.make;", true, true),
        ("api.marker++;", true, true),
        ("delete api.marker;", true, true),
        ("({ value: api.marker } = { value: 2 });", true, true),
        ("for (api.marker of [2]) {}", true, true),
        ("try { api = {}; } catch {}", true, true),
        (
            "try { ({ value: api } = { value: {} }); } catch {}",
            true,
            true,
        ),
        ("try { for (api of [{}]) {} } catch {}", true, true),
        ("export default api;", true, true),
        ("eval('');", true, true),
        ("const ignored = api.marker;", true, false),
    ] {
        eprintln!("namespace use: {use_source}, complete={complete}, timing={timing}");
        let source = format!(
            "import {{ api, other }} from './peer.js';\n\
            class Noise {{ set(value) {{ this.value = value; }} }}\n\
            {use_source}\n\
            const wrapped = api.make(() => later);\n\
            const later = 42;\nconsole.log(wrapped.invoke());\n"
        );
        let mut contract = ChunkExportPurityBuilder::new("static/peer")
            .with_namespace_independent_members(
                "api",
                if complete {
                    &["make", "marker"]
                } else {
                    &["make"]
                },
            );
        if timing {
            contract = contract
                .with_callback_factory_members("api", &["make"])
                .with_callback_factory_members("other", &["make"]);
        }
        let contracts = [contract.build()];
        // The empty eval is audited only to get past the existing input gate;
        // dynamic scope must still defeat the namespace privacy proof.
        let overrides: &[&str] = if use_source.starts_with("eval") {
            &["a1_eval"]
        } else {
            &[]
        };
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![])
                .with_admission_overrides(overrides)
                .with_extra_chunks(&peers)
                .with_chunk_export_purity(&contracts),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        let mut members = vec![Member::new("Noise"), Member::new("wrapped")];
        let setup = if use_source.starts_with("const ignored") {
            members.push(Member::new("ignored"));
            vec![]
        } else if use_source.starts_with("export") {
            vec![]
        } else {
            vec![use_source]
        };
        expect_atomic_conflict_rejection(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module_with_anon("callbacks/wrapped", &members, &setup),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_admission_overrides(overrides)
            .with_extra_chunks(&peers)
            .with_chunk_export_purity(&contracts),
            &["callbacks/wrapped", "values/later"],
            &[],
        );
    }
}
