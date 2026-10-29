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
    assert!(
        graph["native_integrity_witnesses"]
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
