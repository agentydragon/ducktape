//! Direct-export and namespace no-reentry assertions keep published callbacks from re-entering.

use debundle_e2e_support::*;

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
                    &binding_members(["callback", "slot"]),
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/later", "callbacks/slot"])
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
                    &binding_members(["callback", "slot"]),
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/later", "callbacks/slot"])
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
                    &binding_members(["callback", "slot"]),
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("values/later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_import_order(&["values/later", "callbacks/slot"])
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
