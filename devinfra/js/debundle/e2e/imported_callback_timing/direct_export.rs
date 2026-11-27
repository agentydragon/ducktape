//! Direct-export and namespace no-reentry assertions keep published callbacks from re-entering.

use super::SplitCase;
use debundle_e2e_support::*;

const SLOT_PEER: &str = "export function makeSlot() { return { kind: 'slot' }; }\n";
const API_SLOT_PEER: &str = "export const api = { makeSlot() { return { kind: 'slot' }; } };\n";

#[test]
fn no_reentry_assertion_admits_split_around_published_callback() {
    for (name, source, peer, contract) in [
        (
            "direct export",
            "import { makeSlot as localMakeSlot } from './peer.js';\n\
             let later = 0;\n\
             const callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = localMakeSlot();\n\
             later = 42;\n\
             console.log(slot.kind, globalThis.saved());\n",
            SLOT_PEER,
            ChunkExportPurityBuilder::new("static/peer").with_no_reentry_exports(&["makeSlot"]),
        ),
        (
            "primitive namespace argument",
            "import { api } from './peer.js';\n\
             let later = 0;\n\
             const callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = api.makeSlot(void 0);\n\
             later = 42;\n\
             console.log(slot.kind, globalThis.saved());\n",
            API_SLOT_PEER,
            ChunkExportPurityBuilder::new("static/peer")
                .with_no_reentry_members("api", &["makeSlot"])
                .with_namespace_independent_members("api", &["makeSlot"]),
        ),
    ] {
        let peers = [("static/peer", peer)];
        let contracts = [contract.build()];
        SplitCase {
            name,
            source,
            wrapped: &["callback", "slot"],
            wrapped_statements: &["globalThis.saved = callback;"],
            later_write: true,
            import_order: Some(&["values/later", "callbacks/wrapped"]),
            peers: &peers,
            contracts: &contracts,
            ..Default::default()
        }
        .assert_split_runs("slot 42\n");
    }
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

/// Which no-reentry assertion a control case supplies for `makeSlot`.
enum NoReentry {
    /// `makeSlot` is a direct export; `asserted` lists it as no-reentry.
    Export { asserted: bool },
    /// `api.makeSlot` is a no-reentry member, optionally with independent
    /// member lookup and the record-argument extension.
    Member {
        independent_lookup: bool,
        record: bool,
    },
}

impl NoReentry {
    fn contract(&self) -> (&'static str, ChunkExportPurity) {
        let builder = ChunkExportPurityBuilder::new("static/peer");
        match *self {
            Self::Export { asserted } => builder
                .with_no_reentry_exports(if asserted { &["makeSlot"] } else { &[] })
                .build(),
            Self::Member {
                independent_lookup,
                record,
            } => {
                let mut builder = builder.with_no_reentry_members("api", &["makeSlot"]);
                if record {
                    builder = builder.with_no_reentry_record_members("api", &["makeSlot"]);
                }
                if independent_lookup {
                    builder = builder.with_namespace_independent_members("api", &["makeSlot"]);
                }
                builder.build()
            }
        }
    }
}

/// The colocated program prints `output`; splitting the callback owner from the
/// later write stays one atomic conflict.
fn assert_no_reentry_control(
    name: &str,
    source: &str,
    peer: &str,
    members: &[&'static str],
    statements: &[&str],
    output: &str,
    assertion: NoReentry,
) {
    let peers = [("static/peer", peer)];
    let contracts = [assertion.contract()];
    let case = SplitCase {
        name,
        source,
        wrapped: members,
        wrapped_statements: statements,
        later_write: true,
        peers: &peers,
        contracts: &contracts,
        ..Default::default()
    };
    case.assert_colocated_runs(output);
    case.assert_split_rejected(&[]);
}

#[test]
fn direct_export_no_reentry_controls() {
    const ASSERTED: NoReentry = NoReentry::Export { asserted: true };
    let mut cases = vec![
        (
            "unasserted export can invoke the published callback".to_string(),
            "import { makeSlot as localMakeSlot } from './peer.js';\n\
             let later = 0;\nconst callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = localMakeSlot();\n\
             later = 42;\nconsole.log(globalThis.observed, slot.kind, globalThis.saved());\n"
                .to_string(),
            "export function makeSlot() { globalThis.observed = globalThis.saved(); return { kind: 'slot' }; }\n",
            vec!["callback", "slot"],
            vec!["globalThis.saved = callback;"],
            "0 slot 42\n".to_string(),
            NoReentry::Export { asserted: false },
        ),
        (
            "result invocation is not deferred".to_string(),
            "import { makeSlot as localMakeSlot } from './peer.js';\n\
             let later = 0;\nconst callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = localMakeSlot();\n\
             globalThis.observed = slot.invoke();\n\
             later = 42;\nconsole.log(globalThis.observed, globalThis.saved());\n"
                .to_string(),
            "export function makeSlot() { return { invoke() { return globalThis.saved(); } }; }\n",
            vec!["callback", "slot"],
            vec![
                "globalThis.saved = callback;",
                "globalThis.observed = slot.invoke();",
            ],
            "0 42\n".to_string(),
            ASSERTED,
        ),
        (
            "construction is not covered".to_string(),
            "import { makeSlot as localMakeSlot } from './peer.js';\n\
             let later = 0;\nconst callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = new localMakeSlot();\n\
             later = 42;\nconsole.log(slot.kind, globalThis.saved());\n"
                .to_string(),
            SLOT_PEER,
            vec!["callback", "slot"],
            vec!["globalThis.saved = callback;"],
            "slot 42\n".to_string(),
            ASSERTED,
        ),
    ];
    // Uncertain arguments and aliases stay opaque under the assertion.
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
        cases.push((
            format!("uncertain call {call}"),
            source,
            SLOT_PEER,
            members,
            vec!["globalThis.saved = callback;"],
            format!("{observed}slot 42\n"),
            ASSERTED,
        ));
    }
    for (name, source, peer, members, statements, output, assertion) in cases {
        assert_no_reentry_control(
            &name,
            &source,
            peer,
            &members,
            &statements,
            &output,
            assertion,
        );
    }
}

#[test]
fn namespace_no_reentry_controls() {
    const LOOKUP: NoReentry = NoReentry::Member {
        independent_lookup: true,
        record: false,
    };
    const RECORD: NoReentry = NoReentry::Member {
        independent_lookup: true,
        record: true,
    };
    const REPLACEMENT_SOURCE: &str = "import { api } from './peer.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\n\
         const eager = () => { globalThis.observed = globalThis.saved(); return { kind: 'slot' }; };\n\
         api.makeSlot = eager;\nconst slot = api.makeSlot(void 0);\n\
         later = 42;\nconsole.log(globalThis.observed, slot.kind, globalThis.saved());\n";
    let mut cases = vec![
        (
            "primitive argument without independent member lookup".to_string(),
            "import { api } from './peer.js';\n\
             let later = 0;\nconst callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = api.makeSlot(void 0);\n\
             later = 42;\nconsole.log(slot.kind, globalThis.saved());\n"
                .to_string(),
            API_SLOT_PEER,
            vec!["callback", "slot"],
            vec!["globalThis.saved = callback;"],
            "slot 42\n",
            NoReentry::Member {
                independent_lookup: false,
                record: false,
            },
        ),
        (
            "member replacement".to_string(),
            REPLACEMENT_SOURCE.to_string(),
            API_SLOT_PEER,
            vec!["callback", "eager", "slot"],
            vec!["globalThis.saved = callback;", "api.makeSlot = eager;"],
            "0 slot 42\n",
            LOOKUP,
        ),
        (
            "primitive assertion does not admit a record branch".to_string(),
            "import { api } from './peer.js';\n\
             let later = 0;\n\
             const callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = api.makeSlot({ field: 0 });\n\
             later = 42;\n\
             console.log(globalThis.observed, slot.kind, globalThis.saved());\n"
                .to_string(),
            "export const api = { makeSlot(defaults) { if (typeof defaults === 'object') globalThis.observed = globalThis.saved(); return { kind: 'slot' }; } };\n",
            vec!["callback", "slot"],
            vec!["globalThis.saved = callback;"],
            "0 slot 42\n",
            LOOKUP,
        ),
        (
            "record assertion without independent member lookup".to_string(),
            "import { api } from './peer.js';\n\
             let later = 0;\n\
             const callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = api.makeSlot({ field: 0 });\n\
             later = 42;\n\
             console.log(slot.kind, globalThis.saved());\n"
                .to_string(),
            API_SLOT_PEER,
            vec!["callback", "slot"],
            vec!["globalThis.saved = callback;"],
            "slot 42\n",
            NoReentry::Member {
                independent_lookup: false,
                record: true,
            },
        ),
        (
            "record assertion with member replacement".to_string(),
            "import { api } from './peer.js';\n\
             let later = 0;\n\
             const callback = () => later;\n\
             globalThis.saved = callback;\n\
             const eager = () => { globalThis.observed = globalThis.saved(); return { kind: 'slot' }; };\n\
             api.makeSlot = eager;\n\
             const slot = api.makeSlot({ field: 0 });\n\
             later = 42;\n\
             console.log(globalThis.observed, slot.kind, globalThis.saved());\n"
                .to_string(),
            API_SLOT_PEER,
            vec!["callback", "eager", "slot"],
            vec!["globalThis.saved = callback;", "api.makeSlot = eager;"],
            "0 slot 42\n",
            RECORD,
        ),
        (
            "opaque result invocation is not deferred".to_string(),
            "import { api } from './peer.js';\n\
             let later = 0;\nconst callback = () => later;\n\
             globalThis.saved = callback;\n\
             const slot = api.makeSlot(void 0);\n\
             globalThis.observed = slot.invoke();\n\
             later = 42;\nconsole.log(globalThis.observed, globalThis.saved());\n"
                .to_string(),
            "export const api = { makeSlot() { return { invoke() { return globalThis.saved(); } }; } };\n",
            vec!["callback", "slot"],
            vec![
                "globalThis.saved = callback;",
                "globalThis.observed = slot.invoke();",
            ],
            "0 42\n",
            LOOKUP,
        ),
    ];
    // Uncertain arguments stay opaque and argument calls stay eager.
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
        cases.push((
            format!("uncertain argument {argument}"),
            source,
            API_SLOT_PEER,
            members,
            vec!["globalThis.saved = callback;"],
            expected,
            LOOKUP,
        ));
    }
    // The record extension admits only fresh literal records.
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
        cases.push((
            format!("nonliteral record argument {argument}"),
            source,
            API_SLOT_PEER,
            members,
            vec!["globalThis.saved = callback;"],
            "slot 42\n",
            RECORD,
        ));
    }
    for (name, source, peer, members, statements, output, assertion) in cases {
        assert_no_reentry_control(
            &name,
            &source,
            peer,
            &members,
            &statements,
            output,
            assertion,
        );
    }
}
