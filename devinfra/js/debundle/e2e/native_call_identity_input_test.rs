//! Exact-site native identity can retain the ordinary call model under opaque
//! startup writes without granting purity or whole-family integrity.

use debundle_e2e_support::*;
use sha2::{Digest, Sha256};

fn input(source: &str, binding: &str, declaration: &str) -> NativeCallIdentityInputContract {
    NativeCallIdentityInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [{binding}]\n",
            declaration
        ))
        .expect("valid function selector"),
        callee: NativeCallIdentityKind::ObjectFreeze,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("The original Object.freeze binding is audited at this call.".to_owned()),
    }
}

fn empty_set_field_input(source: &str, declaration: &str) -> NativeCallIdentityInputContract {
    NativeCallIdentityInputContract {
        source_match: serde_yaml::from_str(&format!(
            "match: {:?}\nbindings: [Registry]\n",
            declaration
        ))
        .expect("valid class selector"),
        callee: NativeCallIdentityKind::SetEmptyPrivateField,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("Original Set binding is audited for every field initialization.".into()),
    }
}

const EMPTY_SET_CLASS: &str = "class Registry {\n  #items = new Set;\n  #armed = false;\n  #onExit = () => this.clear();\n  add(item) { this.#items.add(item); }\n  clear() { this.#items.clear(); }\n  get size() { return this.#items.size; }\n}";
const EMPTY_SET_SOURCE: &str = "const startupTarget = globalThis.plugin || {};\nstartupTarget.Set = 1;\nconst saved = () => later;\nglobalThis.saved = saved;\nclass Registry {\n  #items = new Set;\n  #armed = false;\n  #onExit = () => this.clear();\n  add(item) { this.#items.add(item); }\n  clear() { this.#items.clear(); }\n  get size() { return this.#items.size; }\n}\nconst registry = new Registry();\nclass Client { run() { return registry.size; } }\nconst client = new Client();\nprocess.env.NEUTRAL = '1';\nconst later = 42;\nconsole.log(registry.size, client.run(), later, globalThis.saved());\nexport { registry, client, later };\n";

fn empty_set_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "registry/state",
            &[
                Member::new("Registry").with_purity(MemberPurity::PureNew),
                Member::new("registry"),
            ],
        ),
        logical_module(
            "client/api",
            &[
                Member::new("Client").with_purity(MemberPurity::PureNew),
                Member::new("client"),
            ],
        ),
        logical_module("value/later", &[Member::new("later")]),
    ]
}

#[test]
fn exact_empty_set_private_field_breaks_false_constructor_reentry_cycle() {
    let unproved = run_rejection_fixture(FixtureOpts::new(EMPTY_SET_SOURCE, empty_set_modules()));
    assert!(!unproved.atomic_group_conflicts().is_empty());
    let fixture = run_fixture(
        FixtureOpts::new(EMPTY_SET_SOURCE, empty_set_modules()).with_native_call_identity_inputs(
            "static/app",
            vec![empty_set_field_input(EMPTY_SET_SOURCE, EMPTY_SET_CLASS)],
        ),
    );
    assert_preserved_entry_output(&fixture, "0 0 42 42\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    let registry = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|binding| binding["binding"] == "registry")
                })
        })
        .unwrap();
    assert_eq!(registry["purity"]["kind"], "pure");
    assert_eq!(registry["synchronous_execution"]["complete"], true);
    assert!(
        registry["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .is_some_and(|uses| uses
                .iter()
                .any(|used| { used["evidence"]["operation"] == "set_empty_private_field" }))
    );
    assert!(
        program_graph["native_integrity_witnesses"]
            .as_array()
            .is_some_and(|witnesses| witnesses.iter().any(|witness| {
                witness["target_slots"]
                    .as_array()
                    .is_some_and(|slots| slots.iter().any(|slot| slot["key"]["string"] == "Set"))
            }))
    );
}

#[test]
fn selected_empty_set_field_does_not_certify_another_set_constructor() {
    let source = EMPTY_SET_SOURCE.replace(
        "const registry = new Registry();",
        "const other = new Set;\nconst registry = new Registry();",
    );
    let rejected = run_rejection_fixture(
        FixtureOpts::new(&source, empty_set_modules()).with_native_call_identity_inputs(
            "static/app",
            vec![empty_set_field_input(&source, EMPTY_SET_CLASS)],
        ),
    );
    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    let other = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings.iter().any(|binding| binding["binding"] == "other")
                })
        })
        .unwrap();
    assert_eq!(other["synchronous_execution"]["complete"], false);
    assert!(
        other["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .is_none_or(Vec::is_empty)
    );
}

#[test]
fn empty_set_field_rejects_known_global_set_replacement() {
    for write in [
        "globalThis.Set = function Replacement() {};",
        "const globalAlias = globalThis; globalAlias.Set = function Replacement() {};",
        "Object.defineProperty(globalThis, 'Set', { value: function Replacement() {} });",
        "const originalSet = Set; delete globalThis.Set; globalThis.Set = originalSet;",
    ] {
        let source = format!("{write}\n{EMPTY_SET_SOURCE}");
        expect_rejection_containing_all(
            FixtureOpts::new(&source, empty_set_modules()).with_native_call_identity_inputs(
                "static/app",
                vec![empty_set_field_input(&source, EMPTY_SET_CLASS)],
            ),
            &["global Set slot write"],
        );
    }
}

#[test]
fn empty_set_field_rejects_cross_chunk_global_set_write() {
    let source = format!("import './mutation.js';\n{EMPTY_SET_SOURCE}");
    let mutation = "const originalSet = Set;\nglobalThis.Set = function Replacement() {};\nglobalThis.Set = originalSet;\nexport {};\n";
    expect_rejection_containing_all(
        FixtureOpts::new(&source, empty_set_modules())
            .with_extra_chunks(&[("static/mutation", mutation)])
            .with_native_call_identity_inputs(
                "static/app",
                vec![empty_set_field_input(&source, EMPTY_SET_CLASS)],
            ),
        &["global Set slot write", "static/mutation"],
    );
}

#[test]
fn empty_set_field_rejects_unsupported_class_or_field_shape() {
    for class in [
        EMPTY_SET_CLASS.replace("class Registry {", "class Registry extends Object {"),
        EMPTY_SET_CLASS.replace("new Set;", "new Set([1]);"),
        EMPTY_SET_CLASS.replace("new Set;", "new Set(...[]);"),
        EMPTY_SET_CLASS.replace(
            "#items = new Set;",
            "#items = new Set;\n  #other = new Set;",
        ),
        EMPTY_SET_CLASS.replace("#items = new Set;", "constructor() {}\n  #items = new Set;"),
    ] {
        let source = EMPTY_SET_SOURCE.replace(EMPTY_SET_CLASS, &class);
        expect_rejection_containing_all(
            FixtureOpts::new(&source, empty_set_modules()).with_native_call_identity_inputs(
                "static/app",
                vec![empty_set_field_input(&source, &class)],
            ),
            &["requires one stable non-derived implicit-constructor class"],
        );
    }
}

#[test]
fn empty_set_field_rejects_shadowing_rebinding_and_dynamic_scope() {
    for source in [
        format!("const Set = class LocalSet {{}};\n{EMPTY_SET_SOURCE}"),
        format!("Set = function Replacement() {{}};\n{EMPTY_SET_SOURCE}"),
        EMPTY_SET_SOURCE.replace(
            "const registry = new Registry();",
            "Registry = class Replacement {};\nconst registry = new Registry();",
        ),
        format!("function contaminate() {{ eval('0'); }}\n{EMPTY_SET_SOURCE}"),
    ] {
        let expected = if source.contains("eval('0')") {
            "dynamic-scope contamination"
        } else if source.starts_with("Set = ") {
            // The assignment leaves `Set` the global binding and replaces it.
            "positively identified global Set slot write"
        } else {
            "requires one stable non-derived implicit-constructor class"
        };
        expect_rejection_containing_all(
            FixtureOpts::new(&source, empty_set_modules()).with_native_call_identity_inputs(
                "static/app",
                vec![empty_set_field_input(&source, EMPTY_SET_CLASS)],
            ),
            &[expected],
        );
    }
}

const FUNCTION: &str =
    "function makeRecord(name, header) {\n  return Object.freeze({ name, header });\n}";
const SOURCE: &str = "function makeRecord(name, header) {\n  return Object.freeze({ name, header });\n}\nconst startupTarget = globalThis.plugin || {};\nstartupTarget.freeze = 1;\nconst peek = () => later;\nglobalThis.peek = peek;\nconst early = makeRecord('first', 'ready');\nconst later = 'later';\nconsole.log(early.name, later);\nexport { early, later };\n";

fn modules(with_write: bool) -> Vec<LogicalModuleEntry> {
    let mut anonymous = vec!["globalThis.peek = peek;"];
    if with_write {
        anonymous.push("startupTarget.freeze = 1;");
    }
    vec![
        logical_module_with_anon(
            "records/early",
            &[
                Member::new("startupTarget"),
                Member::new("peek"),
                Member::new("early"),
            ],
            &anonymous,
        ),
        logical_module("values/later", &[Member::new("later")]),
    ]
}

#[test]
fn exact_freeze_site_restores_complete_synchronous_call_under_opaque_write() {
    let control = SOURCE.replace("startupTarget.freeze = 1;\n", "");
    assert_preserved_entry_output(
        &run_fixture(FixtureOpts::new(&control, modules(false))),
        "first later\n",
    );
    let unproved = run_rejection_fixture(FixtureOpts::new(SOURCE, modules(true)));
    assert!(!unproved.atomic_group_conflicts().is_empty());
    let fixture = run_fixture(
        FixtureOpts::new(SOURCE, modules(true)).with_native_call_identity_inputs(
            "static/app",
            vec![input(SOURCE, "makeRecord", FUNCTION)],
        ),
    );
    assert_preserved_entry_output(&fixture, "first later\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let program_graph = serde_json::to_value(fixture.program_graph()).unwrap();
    assert!(
        program_graph["native_integrity_witnesses"]
            .as_array()
            .is_some_and(|witnesses| witnesses.iter().any(|w| {
                w["target_slots"]
                    .as_array()
                    .is_some_and(|slots| slots.iter().any(|slot| slot["key"]["string"] == "freeze"))
            }))
    );
    assert!(graph["nodes"].as_array().unwrap().iter().any(|node| {
        node["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .is_some_and(|uses| {
                uses.iter()
                    .any(|used| used["evidence"]["operation"] == "object_freeze")
            })
    }));
}

#[test]
fn certified_fresh_record_freeze_does_not_observe_an_unrelated_live_array() {
    let function = "function makeRecord(name) { return Object.freeze({ name }); }";
    let source = format!(
        "{function}\nconst startupTarget = globalThis.plugin || {{}};\nstartupTarget.freeze = 1;\nconst values = [];\nconst frozen = makeRecord('ready');\nconst later = values.length;\nconsole.log(frozen.name, later);\nexport {{ frozen, later }};\n"
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("heap/a", &[Member::new("values"), Member::new("later")]),
                logical_module("records/b", &[Member::new("frozen")]),
            ],
        )
        .with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", function)],
        ),
    );
    assert_preserved_entry_output(&fixture, "ready 0\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(graph["nodes"].as_array().unwrap().iter().any(|node| {
        node["declared_bindings"]
            .as_array()
            .is_some_and(|bindings| {
                bindings
                    .iter()
                    .any(|binding| binding["binding"] == "frozen")
            })
            && node["synchronous_execution"]["native_operation_input_uses"]
                .as_array()
                .is_some_and(|uses| {
                    uses.iter()
                        .any(|used| used["evidence"]["operation"] == "object_freeze")
                })
    }));
}

#[test]
fn freeze_heap_proof_requires_a_fresh_plain_data_record() {
    for (function, argument) in [
        (
            "function makeRecord() { return Object.freeze({ get name() { return 'ready'; } }); }",
            "",
        ),
        (
            "function makeRecord(value) { return Object.freeze(value); }",
            "new Proxy({ name: 'ready' }, {})",
        ),
    ] {
        let source = format!(
            "{function}\nconst startupTarget = globalThis.plugin || {{}};\nstartupTarget.freeze = 1;\nconst values = [];\nconst frozen = makeRecord({argument});\nconst later = values.length;\nconsole.log(frozen.name, later);\nexport {{ frozen, later }};\n"
        );
        let rejected = run_rejection_fixture(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module("heap/a", &[Member::new("values"), Member::new("later")]),
                    logical_module("records/b", &[Member::new("frozen")]),
                ],
            )
            .with_native_call_identity_inputs(
                "static/app",
                vec![input(&source, "makeRecord", function)],
            ),
        );
        assert!(
            rejected.stderr.contains("module-quotient SCC"),
            "{}",
            rejected.stderr
        );
    }
}

#[test]
fn freeze_heap_proof_retains_nested_array_mutation() {
    let function =
        "function makeRecord() { return Object.freeze({ name: values.push('ready') }); }";
    let source = format!(
        "{function}\nconst startupTarget = globalThis.plugin || {{}};\nstartupTarget.freeze = 1;\nconst values = [];\nconst frozen = makeRecord();\nconst later = values.length;\nconsole.log(frozen.name, later);\nexport {{ frozen, later }};\n"
    );
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "heap/a",
                    &[
                        Member::new("values"),
                        Member::new("later").with_purity(MemberPurity::Pure),
                    ],
                ),
                logical_module("records/b", &[Member::new("frozen")]),
            ],
        )
        .with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", function)],
        ),
    );
    assert!(
        rejected.stderr.contains("module-quotient SCC"),
        "{}",
        rejected.stderr
    );
    assert!(
        rejected
            .stderr
            .contains("frozen (records/b)  --side-effect-->  <side-effect> (heap/a)"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn returned_record_with_live_array_descendant_stays_ordered_conservatively() {
    let function = "function makeRecord() { return Object.freeze({ values }); }";
    let source = format!(
        "{function}\nconst startupTarget = globalThis.plugin || {{}};\nstartupTarget.freeze = 1;\nconst values = [];\nconst frozen = makeRecord();\nconst exposed = frozen.values;\nconst pushed = exposed.push('ready');\nconst later = values.length;\nconsole.log(later);\nexport {{ frozen, exposed, pushed, later }};\n"
    );
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module("heap/a", &[Member::new("values"), Member::new("later")]),
                logical_module(
                    "records/b",
                    &[
                        Member::new("frozen"),
                        Member::new("exposed"),
                        Member::new("pushed"),
                    ],
                ),
            ],
        )
        .with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", function)],
        ),
    );
    assert!(
        rejected.stderr.contains("module-quotient SCC"),
        "{}",
        rejected.stderr
    );
    assert!(
        rejected
            .stderr
            .contains("later (heap/a)  --side-effect-->  <side-effect> (records/b)"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn exact_freeze_site_rejects_known_required_slot_writes() {
    for write in [
        "Object.freeze = function(value) { return value; };",
        "const ctor = Object; ctor.freeze = function(value) { return value; };",
        "delete Object.freeze;",
        "Object.defineProperty(Object, 'freeze', { get() { return function(value) { return value; }; } });",
        "Object.defineProperty(Object.prototype, 'probe', { set() { Object.freeze = function(value) { return value; }; } }); Object.probe = 1;",
        "const original = Object.freeze; Object.freeze = function(value) { return value; }; Object.freeze = original;",
        "globalThis.Object = function replacement() {};",
    ] {
        let source = SOURCE.replace(
            "const startupTarget =",
            &format!("{write}\nconst startupTarget ="),
        );
        expect_rejection_containing_all(
            FixtureOpts::new(&source, modules(true)).with_native_call_identity_inputs(
                "static/app",
                vec![input(&source, "makeRecord", FUNCTION)],
            ),
            &["Object.freeze slot write"],
        );
    }
}

#[test]
fn known_sibling_object_write_does_not_contradict_exact_freeze_slot() {
    let source = SOURCE.replace(
        "const startupTarget =",
        "Object.unrelated = 1;\nconst startupTarget =",
    );
    let modules = vec![
        logical_module_with_anon(
            "records/early",
            &[
                Member::new("startupTarget"),
                Member::new("peek"),
                Member::new("early"),
            ],
            &[
                "Object.unrelated = 1;",
                "startupTarget.freeze = 1;",
                "globalThis.peek = peek;",
            ],
        ),
        logical_module("values/later", &[Member::new("later")]),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(&source, modules).with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", FUNCTION)],
        ),
    );
    assert_preserved_entry_output(&fixture, "first later\n");
}

#[test]
fn selected_site_rejects_shadowing_and_ambiguous_body() {
    for function in [
        "function makeRecord(Object) { return Object.freeze({ name: 'x' }); }",
        "function makeRecord(name) { return Object['freeze']({ name }); }",
        "function makeRecord(name) { Object.freeze({ name }); return Object.freeze({ name }); }",
        "async function makeRecord(name) { return Object.freeze({ name }); }",
    ] {
        let source = SOURCE.replace(FUNCTION, function);
        expect_rejection_containing_all(
            FixtureOpts::new(&source, modules(true)).with_native_call_identity_inputs(
                "static/app",
                vec![input(&source, "makeRecord", function)],
            ),
            &["requires a nonasync top-level function"],
        );
    }
}

#[test]
fn known_cross_chunk_replacement_contradicts_selected_site() {
    const MUTATION: &str = "const original = Object.freeze;\nObject.freeze = function(value) { return value; };\nObject.freeze = original;\nexport {};\n";
    let source = format!("import './mutation.js';\n{SOURCE}");
    expect_rejection_containing_all(
        FixtureOpts::new(&source, modules(true))
            .with_extra_chunks(&[("static/mutation", MUTATION)])
            .with_native_call_identity_inputs(
                "static/app",
                vec![input(&source, "makeRecord", FUNCTION)],
            ),
        &["Object.freeze slot write", "static/mutation"],
    );
}

#[test]
fn selected_identity_does_not_certify_proxy_or_getter_arguments() {
    for source in [
        "function makeRecord(value) { return Object.freeze(value); }\nconst peek = () => later;\nglobalThis.peek = peek;\nconst target = new Proxy({}, { ownKeys() { globalThis.peek(); return []; } });\nconst early = makeRecord(target);\nvar later = 'later';\nconsole.log(later);\nexport { early, later };\n",
        "function makeRecord(value) { return Object.freeze({ name: value.name }); }\nconst peek = () => later;\nglobalThis.peek = peek;\nconst target = { get name() { globalThis.peek(); return 'x'; } };\nconst early = makeRecord(target);\nvar later = 'later';\nconsole.log(later);\nexport { early, later };\n",
        "function makeRecord() { return Object.freeze({ get name() { return later; } }); }\nconst peek = () => later;\nglobalThis.peek = peek;\nconst early = makeRecord();\nvar later = 'later';\nconsole.log(later);\nexport { early, later };\n",
    ] {
        let function = source.split("\nconst peek").next().unwrap();
        let rejected = run_rejection_fixture(
            FixtureOpts::new(
                source,
                vec![
                    logical_module("records/early", &[Member::new("early")]),
                    logical_module("values/later", &[Member::new("later")]),
                ],
            )
            .with_native_call_identity_inputs(
                "static/app",
                vec![input(source, "makeRecord", function)],
            ),
        );
        assert!(
            rejected.stderr.contains("spec is unrealizable")
                || rejected.stderr.contains("atomic-group conflict"),
            "{}",
            rejected.stderr
        );
        assert!(
            rejected.stderr.contains("impure_function_call"),
            "{}",
            rejected.stderr
        );
    }
}

#[test]
fn an_unselected_freeze_call_keeps_the_opaque_barrier() {
    let other = "function other(name) { return Object.freeze({ title: name }); }";
    let source = SOURCE
        .replace(
            "const startupTarget =",
            &format!("{other}\nconst startupTarget ="),
        )
        .replace("const early = makeRecord(", "const early = other(");
    let rejected = run_rejection_fixture(
        FixtureOpts::new(&source, modules(true)).with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", FUNCTION)],
        ),
    );
    assert!(!rejected.atomic_group_conflicts().is_empty());
}

#[test]
fn exact_call_input_rejects_stale_digest_and_ambiguous_selector() {
    let mut stale = input(SOURCE, "makeRecord", FUNCTION);
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        FixtureOpts::new(SOURCE, modules(true))
            .with_native_call_identity_inputs("static/app", vec![stale]),
        &["stale audited_source_sha256"],
    );

    let other = "function other(name, header) { return Object.freeze({ name, header }); }";
    let source = SOURCE.replace(
        "const startupTarget =",
        &format!("{other}\nconst startupTarget ="),
    );
    let rejected = run_rejection_fixture(
        FixtureOpts::new(&source, modules(true)).with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", FUNCTION)],
        ),
    );
    assert!(
        rejected.stderr.contains("ambiguous")
            || rejected.stderr.contains("multiple")
            || rejected.stderr.contains("exactly one"),
        "{}",
        rejected.stderr
    );
}
