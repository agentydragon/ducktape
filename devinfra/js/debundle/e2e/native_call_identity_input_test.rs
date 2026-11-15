//! Exact-site native identity can retain the ordinary call model under opaque
//! startup writes without granting purity or whole-family integrity.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

fn input(source: &str, binding: &str, declaration: &str) -> NativeCallIdentityInputContract {
    NativeCallIdentityInputContract {
        source_match: source_match_selector(declaration, &[binding]),
        callee: NativeCallIdentityKind::ObjectFreeze,
        audited_source_sha256: source_sha256(source),
        note: Some("The original Object.freeze binding is audited at this call.".to_owned()),
    }
}

const FUNCTION: &str =
    "function makeRecord(name, header) {\n  return Object.freeze({ name, header });\n}";
const SOURCE: &str = "function makeRecord(name, header) {\n  return Object.freeze({ name, header });\n}\nconst startupTarget = globalThis.plugin || {};\nstartupTarget.freeze = 1;\nconst peek = () => later;\nglobalThis.peek = peek;\nconst early = makeRecord('first', 'ready');\nconst later = 'later';\nconsole.log(early.name, later);\nexport { early, later };\n";

fn modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module_with_anon(
            "records/early",
            &[Member::new("startupTarget"), Member::new("early")],
            &["startupTarget.freeze = 1;"],
        ),
        logical_module_with_anon(
            "values/later",
            &[Member::new("peek"), Member::new("later")],
            &["globalThis.peek = peek;"],
        ),
    ]
}

fn assert_freeze_input_use(fixture: &impl FixtureGraphExt) {
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(graph["nodes"].as_array().unwrap().iter().any(|node| {
        node["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .is_some_and(|uses| {
                uses.iter()
                    .any(|used| used["evidence"]["operation"] == "object_freeze")
            })
    }));
}

fn assert_selected_freeze_proofs(fixture: &impl FixtureGraphExt) {
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
    assert_freeze_input_use(fixture);
}

#[test]
fn exact_freeze_site_restores_complete_synchronous_call_under_opaque_write() {
    let control = SOURCE.replace("startupTarget.freeze = 1;\n", "");
    let colocated = run_fixture_with_graphs(
        FixtureOpts::new(
            &control,
            vec![logical_module_with_anon(
                "records/all",
                &[
                    Member::new("startupTarget"),
                    Member::new("peek"),
                    Member::new("early"),
                    Member::new("later"),
                ],
                &["globalThis.peek = peek;"],
            )],
        )
        .with_native_call_identity_inputs(
            "static/app",
            vec![input(&control, "makeRecord", FUNCTION)],
        ),
    );
    assert_preserved_entry_output(&colocated, "first later\n");
    assert_freeze_input_use(&colocated);
    let unproved = run_rejection_fixture_with_graphs(FixtureOpts::new(SOURCE, modules()));
    assert!(!unproved.atomic_group_conflicts().is_empty());
    let candidate = FixtureOpts::new(SOURCE, modules()).with_native_call_identity_inputs(
        "static/app",
        vec![input(SOURCE, "makeRecord", FUNCTION)],
    );
    let rejected = assert_conservative_schedule_refusal(
        candidate,
        "evaluation_schedule_unknown_reordered",
        "first later\n",
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["legacy_gate_accepts"],
        true
    );
    assert_selected_freeze_proofs(&rejected);
}

/// The callable heap summary of a function nobody calls during
/// initialization still consumes the freeze proof, so the input's use is
/// recorded on the declaring source unit (regression: only an interpreted call
/// recorded it, leaving this consumption invisible).
#[test]
fn uncalled_certified_freeze_records_its_heap_proof_use() {
    let function = "function makeRecord(name) { return Object.freeze({ name }); }";
    let source = format!("{function}\nconsole.log(typeof makeRecord);\nexport {{ makeRecord }};\n");
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            &source,
            vec![logical_module(
                "records/factory",
                &[Member::new("makeRecord")],
            )],
        )
        .with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", function)],
        ),
    );
    assert_preserved_entry_output(&fixture, "function\n");
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert!(
        graph["nodes"].as_array().unwrap().iter().any(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| {
                    bindings
                        .iter()
                        .any(|binding| binding["binding"] == "makeRecord")
                })
                && node["synchronous_execution"]["native_operation_input_uses"]
                    .as_array()
                    .is_some_and(|uses| {
                        uses.iter()
                            .any(|used| used["evidence"]["operation"] == "object_freeze")
                    })
        }),
        "{graph}"
    );
}

#[test]
fn certified_fresh_record_freeze_does_not_observe_an_unrelated_live_array() {
    let function = "function makeRecord(name) { return Object.freeze({ name }); }";
    let source = format!(
        "{function}\nconst startupTarget = globalThis.plugin || {{}};\nstartupTarget.freeze = 1;\nconst values = [];\nconst frozen = makeRecord('ready');\nconst later = values.length;\nconsole.log(frozen.name, later);\nexport {{ frozen, later }};\n"
    );
    let rejected = assert_conservative_schedule_refusal(
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
        "evaluation_schedule_unknown_reordered",
        "ready 0\n",
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["legacy_gate_accepts"],
        true
    );
    assert_selected_freeze_proofs(&rejected);
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "records/all",
                &[
                    Member::new("startupTarget"),
                    Member::new("values"),
                    Member::new("frozen"),
                    Member::new("later"),
                ],
                &["startupTarget.freeze = 1;"],
            )],
        )
        .with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", function)],
        ),
    );
    assert_preserved_entry_output(&fixture, "ready 0\n");
    assert_selected_freeze_proofs(&fixture);
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
    let rejected = run_rejection_fixture_with_graphs(
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
    // The Pass-1 cut is a feedback arc set and need not pick this edge, so
    // read it from the graph: `frozen`'s push into `values` stays ordered
    // after `heap/a`.
    let graph: serde_json::Value = read_gzip_json(
        &rejected
            .report_root
            .join("static")
            .join("app")
            .join("dependency_graph.json.gz"),
    );
    let module_of: std::collections::HashMap<&str, &str> = graph["module_graph"]["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|node| {
            (
                node["key"].as_str().unwrap(),
                node["path"].as_str().unwrap(),
            )
        })
        .collect();
    let nodes = graph["nodes"].as_array().unwrap();
    let unit_module = |id: &str| {
        let node = nodes.iter().find(|node| node["id"] == id).unwrap();
        module_of[node["destination"].as_str().unwrap()]
    };
    let frozen = nodes
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|binding| binding["binding"] == "frozen")
        })
        .unwrap()["id"]
        .as_str()
        .unwrap();
    assert!(
        graph["edges"].as_array().unwrap().iter().any(|edge| {
            edge["edge_kind"] == "sequenced"
                && edge["source"] == frozen
                && unit_module(edge["target"].as_str().unwrap()) == "heap/a"
        }),
        "no sequenced edge from `frozen` into heap/a:\n{}",
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
            FixtureOpts::new(&source, modules()).with_native_call_identity_inputs(
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
            &[Member::new("startupTarget"), Member::new("early")],
            &["Object.unrelated = 1;", "startupTarget.freeze = 1;"],
        ),
        logical_module_with_anon(
            "values/later",
            &[Member::new("peek"), Member::new("later")],
            &["globalThis.peek = peek;"],
        ),
    ];
    let rejected = assert_conservative_schedule_refusal(
        FixtureOpts::new(&source, modules).with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", FUNCTION)],
        ),
        "evaluation_schedule_unknown_reordered",
        "first later\n",
    );
    assert_eq!(
        rejected.candidate_relocation_assessment()["legacy_gate_accepts"],
        true
    );
    assert_selected_freeze_proofs(&rejected);
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            &source,
            vec![logical_module_with_anon(
                "records/all",
                &[
                    Member::new("startupTarget"),
                    Member::new("peek"),
                    Member::new("early"),
                    Member::new("later"),
                ],
                &[
                    "Object.unrelated = 1;",
                    "startupTarget.freeze = 1;",
                    "globalThis.peek = peek;",
                ],
            )],
        )
        .with_native_call_identity_inputs(
            "static/app",
            vec![input(&source, "makeRecord", FUNCTION)],
        ),
    );
    assert_preserved_entry_output(&fixture, "first later\n");
    assert_selected_freeze_proofs(&fixture);
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
            FixtureOpts::new(&source, modules()).with_native_call_identity_inputs(
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
        FixtureOpts::new(&source, modules())
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
    let rejected = run_rejection_fixture_with_graphs(
        FixtureOpts::new(&source, modules()).with_native_call_identity_inputs(
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
        FixtureOpts::new(SOURCE, modules())
            .with_native_call_identity_inputs("static/app", vec![stale]),
        &["stale audited_source_sha256"],
    );

    let other = "function other(name, header) { return Object.freeze({ name, header }); }";
    let source = SOURCE.replace(
        "const startupTarget =",
        &format!("{other}\nconst startupTarget ="),
    );
    let rejected = run_rejection_fixture_with_graphs(
        FixtureOpts::new(&source, modules()).with_native_call_identity_inputs(
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
