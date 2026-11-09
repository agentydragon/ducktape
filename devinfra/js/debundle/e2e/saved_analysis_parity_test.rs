//! `debundle run --analysis` over a saved `debundle analyze` artifact
//! reproduces a direct `debundle run`, including its refusals.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// Parser-owned unresolved identity must survive both fresh and restored
/// analysis paths. The explicit member rename introduces `unbound_value` in
/// the same moved module as an unchanged `typeof unbound_value` reference.
#[test]
fn unresolved_target_capture_refusal_matches_direct_and_saved_analysis() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        r#"const source_value = "A";
function read() {
  return source_value + typeof unbound_value;
}
console.log(read());
export { source_value, read };
"#,
        vec![logical_module(
            "values",
            &[
                Member::renamed("unbound_value", "source_value"),
                Member::new("read"),
            ],
        )],
    ));

    for (label, invocation) in [
        ("direct", staged.run(&[])),
        (
            "saved analysis",
            staged.run(&["--analysis", path(&staged.artifact)]),
        ),
    ] {
        assert_eq!(
            invocation.result.status.code(),
            Some(1),
            "{label} rename refusal exited {:?}\nstderr:\n{}",
            invocation.result.status.code(),
            invocation.result.stderr,
        );
        assert!(
            invocation.result.stderr.contains("source_value"),
            "{label} diagnostic omitted the rename source:\n{}",
            invocation.result.stderr,
        );
        assert!(
            invocation.result.stderr.contains("unbound_value"),
            "{label} diagnostic omitted the introduced target:\n{}",
            invocation.result.stderr,
        );
        assert!(
            invocation
                .result
                .stderr
                .contains("change reference resolution"),
            "{label} diagnostic omitted the reason:\n{}",
            invocation.result.stderr,
        );
    }
}

#[test]
fn linkage_export_effects_survive_saved_analysis() {
    assert_staged_run_matches(
        FixtureOpts::new(
            "const first = 1;\nexport { first };\nconst second = 2;\nconsole.log(first, second);\n",
            vec![logical_module("second", &[Member::new("second")])],
        ),
        &[],
    );
}

#[test]
fn imported_impure_call_causes_survive_staged_analysis_as_shared_refs() {
    let provider = "let calls = 0;\n\
                    function leaf() { calls += 1; return calls; }\n\
                    function middle() { return leaf(); }\n\
                    export function sharedImpure() { return middle(); }\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            r#"import { sharedImpure } from "./provider.js";
const first = sharedImpure();
const second = sharedImpure();
console.log(first, second);
export { first, second };
"#,
            vec![logical_module(
                "results",
                &[Member::new("first"), Member::new("second")],
            )],
        )
        .with_extra_chunks(&[("static/provider", provider)]),
    );

    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct run of shared imported impure calls");
    let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(
        &loaded.result,
        "run shared imported impure calls from analysis",
    );
    assert_same_invocation_trees(&direct, &loaded);

    let direct_entry = direct.output_root().join("app/static/app/entry.js");
    let loaded_entry = loaded.output_root().join("app/static/app/entry.js");
    assert_node_output(&direct_entry, "1 2\n", "");
    assert_node_output(&loaded_entry, "1 2\n", "");

    let graph: Value = read_gzip_json(
        &direct
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let first_reasons = graph_node_for_binding(&graph, "first")["purity"]["reasons"]
        .as_array()
        .expect("first verdict reasons");
    let second_reasons = graph_node_for_binding(&graph, "second")["purity"]["reasons"]
        .as_array()
        .expect("second verdict reasons");
    assert_eq!(first_reasons.len(), 1, "{first_reasons:#?}");
    assert_eq!(second_reasons.len(), 1, "{second_reasons:#?}");
    for reasons in [first_reasons, second_reasons] {
        assert_eq!(reasons[0]["rule"], "impure_function_call");
        assert!(
            reasons[0].get("cause").is_none(),
            "the JSON reason must point to a shared cause row instead of nesting it: {reasons:#?}"
        );
    }
    let shared_cause = first_reasons[0]["cause_ref"]
        .as_str()
        .expect("first imported call has a cause reference");
    assert_eq!(
        second_reasons[0]["cause_ref"].as_str(),
        Some(shared_cause),
        "both statements call the same imported impure callable"
    );

    let causes = graph["purity_causes"]
        .as_array()
        .expect("factored purity cause rows");
    assert_eq!(
        causes.len(),
        3,
        "the sharedImpure -> middle -> leaf chain appears once: {causes:#?}"
    );
    let causes_by_id = causes
        .iter()
        .map(|cause| (cause["id"].as_str().expect("cause row id"), cause))
        .collect::<BTreeMap<_, _>>();
    assert_eq!(causes_by_id.len(), causes.len(), "cause ids are unique");

    let mut pending = vec![shared_cause];
    let mut visited = BTreeSet::new();
    let mut cause_edges = 0;
    let mut saw_leaf_mutation = false;
    while let Some(id) = pending.pop() {
        if !visited.insert(id) {
            continue;
        }
        let cause = causes_by_id
            .get(id)
            .unwrap_or_else(|| panic!("unresolved purity cause {id:?}: {causes:#?}"));
        let reasons = cause["purity"]["reasons"]
            .as_array()
            .expect("cause purity reasons");
        for reason in reasons {
            assert!(
                reason.get("cause").is_none(),
                "nested causes stay factored in the table: {reason:#}"
            );
            if reason["rule"] == "assign_or_update" {
                saw_leaf_mutation = true;
            }
            if let Some(cause_ref) = reason["cause_ref"].as_str() {
                cause_edges += 1;
                pending.push(cause_ref);
            }
        }
    }
    assert_eq!(visited.len(), causes.len(), "all cause rows are reachable");
    assert!(
        cause_edges >= 2,
        "the provider explanation is nested: {causes:#?}"
    );
    assert!(
        saw_leaf_mutation,
        "the chain reaches leaf's state update: {causes:#?}"
    );
}

fn assert_lexical_authority_staged_parity(
    source: &'static str,
    members: &[Member],
    expected_authority: &str,
) {
    let (staged, _fixture) = staged(FixtureOpts::new(
        source,
        vec![logical_module("all", members)],
    ));
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct lexical-authority run");
    let direct_out = direct.output_root();
    let direct_graph: Value =
        read_gzip_json(&direct_out.join("reports/tree/static/app/dependency_graph.json.gz"));
    let direct_result = graph_node_for_binding(&direct_graph, "result");
    let direct_execution = &direct_result["synchronous_execution"];
    assert_eq!(direct_execution["complete"], true, "{direct_execution:#}");
    assert_eq!(
        direct_execution["binding_effect_authority"]["authority"], expected_authority,
        "{direct_execution:#}"
    );

    let seed = graph_node_for_binding(&direct_graph, "seed");
    assert!(
        direct_graph["edges"]
            .as_array()
            .unwrap()
            .iter()
            .any(|edge| {
                edge["source"] == direct_result["id"]
                    && edge["target"] == seed["id"]
                    && edge["edge_kind"] == "eager_use"
                    && edge["binding"] == "seed"
            }),
        "the observed readSeed effect must remain in the graph: {direct_graph:#}"
    );

    let analysis = path(&staged.artifact).to_owned();
    let loaded = staged.run(&["--analysis", analysis.as_str()]);
    assert_ok(&loaded.result, "run from saved analysis artifact");
    assert_same_invocation_trees(&direct, &loaded);
    assert_node_output(&direct_out.join("app/static/app/entry.js"), "41\n", "");
    assert_node_output(
        &loaded.output_root().join("app/static/app/entry.js"),
        "41\n",
        "",
    );
    let loaded_graph: Value = read_gzip_json(
        &loaded
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let loaded_execution =
        &graph_node_for_binding(&loaded_graph, "result")["synchronous_execution"];
    assert_eq!(
        loaded_execution["binding_effect_authority"]["authority"], expected_authority,
        "the typed authority must survive the validated artifact round-trip: {loaded_execution:#}"
    );
}

#[test]
fn lexical_effect_authority_and_fallback_survive_staged_artifact_round_trip() {
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nconst values = [];\nfunction readSeed() { values.push(seed); return seed; }\nvar result = readSeed();\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("values"),
            Member::new("readSeed"),
            Member::new("result"),
        ],
        "operation_receipt",
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = readSeed();\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("readSeed"),
            Member::new("result"),
        ],
        "operation_receipt",
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nvar result = alias();\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("later"),
            Member::new("readSeed"),
            Member::new("alias"),
            Member::new("result"),
        ],
        "ordered_root_initializer_uncovered",
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("readSeed"),
            Member::new("result"),
        ],
        "operation_receipt",
    );
    assert_lexical_authority_staged_parity(
        "const seed = 41;\nfunction readSeed() { return seed; }\nconst alias = readSeed;\nvar result = (alias(), alias());\nconsole.log(result);\n",
        &[
            Member::new("seed"),
            Member::new("readSeed"),
            Member::new("alias"),
            Member::new("result"),
        ],
        "ordered_root_initializer_uncovered",
    );
}

#[test]
fn captured_value_bindings_survive_saved_program_summary() {
    const PROVIDER: &str = "var captured = globalThis.runtimeSettings.state;\nexport function read() { return captured.value; }\n";
    const SETUP: &str = "globalThis.runtimeSettings = { state: { value: 41 } };\n";
    let input = ValueInputContract {
        source_match: serde_yaml::from_str(
            "match: var captured = globalThis.runtimeSettings.state;\nbindings: [captured]\n",
        )
        .expect("source selector"),
        value: CapturedValueContract {
            kind: CapturedValueKind::OrdinaryObject,
        },
        audited_source_sha256: Sha256::digest(PROVIDER.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        expected_export: None,
        note: None,
    };
    let opts = FixtureOpts::new(
        "import './setup.js';\nimport { read } from './peer.js';\nconst result = read();\nconsole.log(result);\nexport { result };\n",
        vec![logical_module("all", &[Member::new("result")])],
    )
    .with_extra_chunks(&[("static/setup", SETUP), ("static/peer", PROVIDER)])
    .with_value_inputs("static/peer", vec![input]);
    let (staged, _fixture) = staged(opts);
    // The premise provider belongs to the static-request closure, but is not a
    // materialization root. Analyze must retain it from canonical source sites.
    let projection: Value = rmp_serde::from_slice(&fs::read(&staged.projection).unwrap()).unwrap();
    assert_eq!(projection["analysed_chunks"], json!(["static/app"]));
    assert_eq!(
        projection["source_sites"]["value_inputs"]["static/peer"][0],
        json!({"binding": "captured", "body_index": 0}),
    );
    assert!(projection["source_sites"]["source_sha256"]["static/peer"].is_string());
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct captured-value run");
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&saved.result, "saved captured-value run");
    assert_same_invocation_trees(&direct, &saved);
    assert_node_output(
        &saved.output_root().join("app/static/app/entry.js"),
        "41\n",
        "",
    );
}

fn assert_reexported_value_input_provider_is_in_saved_summary(bridge: &str) {
    const PROVIDER: &str = "var captured = globalThis.runtimeSettings.state;\nObject.assign(captured, { ready: true });\nexport function read() { return captured.value; }\n";
    const SETUP: &str = "globalThis.runtimeSettings = { state: { value: 41 } };\n";
    let input = ValueInputContract {
        source_match: serde_yaml::from_str(
            "match: var captured = globalThis.runtimeSettings.state;\nbindings: [captured]\n",
        )
        .expect("source selector"),
        value: CapturedValueContract {
            kind: CapturedValueKind::OrdinaryObject,
        },
        audited_source_sha256: Sha256::digest(PROVIDER.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        expected_export: None,
        note: None,
    };
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            "import './setup.js';\nimport './bridge.js';\nconst result = 41;\nconsole.log(result, globalThis.runtimeSettings.state.ready);\nexport { result };\n",
            vec![logical_module("all", &[Member::new("result")])],
        )
        .with_extra_chunks(&[
            ("static/setup", SETUP),
            ("static/bridge", bridge),
            ("static/peer", PROVIDER),
        ])
        .with_value_inputs("static/peer", vec![input]),
    );

    // The source premise belongs to the request closure, while only the
    // consumer remains a selected analysis root.
    let projection: Value = rmp_serde::from_slice(&fs::read(&staged.projection).unwrap()).unwrap();
    assert_eq!(projection["analysed_chunks"], json!(["static/app"]));
    assert_eq!(
        projection["source_sites"]["value_inputs"]["static/peer"][0],
        json!({"binding": "captured", "body_index": 0}),
    );

    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct re-exported captured-value run");
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&saved.result, "saved re-exported captured-value run");
    assert_same_invocation_trees(&direct, &saved);

    let source_entry = staged.root().join("snapshot/static/app.js");
    let direct_entry = direct.output_root().join("app/static/app/entry.js");
    let saved_entry = saved.output_root().join("app/static/app/entry.js");
    for entry in [&source_entry, &direct_entry, &saved_entry] {
        assert_node_output(entry, "41 true\n", "");
    }

    for invocation in [&direct, &saved] {
        let program: Value = read_gzip_json(
            &invocation
                .output_root()
                .join("reports/tree/program_graph.json.gz"),
        );
        let witnesses = program["native_integrity_witnesses"]
            .as_array()
            .expect("native-integrity census");
        let peer_witnesses: Vec<_> = witnesses
            .iter()
            .filter(|witness| witness["chunk_id"] == "static/peer")
            .collect();
        assert_eq!(peer_witnesses.len(), 1, "{program:#}");
        let witness = peer_witnesses[0];
        assert_eq!(witness["operation"], "mutation_call", "{witness}");
        assert_eq!(witness["affected_roots"], json!([]), "{witness}");
        assert!(
            witness["value_input_refs"]
                .as_array()
                .is_some_and(|refs| refs.iter().any(|reference| {
                    reference["defining_chunk"] == "static/peer" && reference["input_index"] == 0
                })),
            "the peer writer must consume the selected captured-value input: {witness}"
        );
    }
}

#[test]
fn named_and_star_reexports_keep_provider_inputs_in_saved_analysis() {
    for bridge in [
        "export { read } from './peer.js';\n",
        "export * from './peer.js';\n",
    ] {
        assert_reexported_value_input_provider_is_in_saved_summary(bridge);
    }
}

fn assert_unproved_reexported_primitive_refusal_matches_saved(provider: &str) {
    let source = "import { limit as bytes } from './bridge.js';\n\
         let later = 0;\nconst callback = () => later;\n\
         globalThis.saved = callback;\nconst computed = 4 * bytes + 256;\n\
         later = 42;\nconsole.log(computed, callback());\n";
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "compute",
                    &[Member::new("callback"), Member::new("computed")],
                    &["globalThis.saved = callback;"],
                ),
                logical_module_with_anon("later", &[Member::new("later")], &["later = 42;"]),
            ],
        )
        .with_extra_chunks(&[
            ("static/bridge", "export { limit } from './provider.js';\n"),
            ("static/provider", provider),
        ]),
    );
    let artifact = fs::read(&staged.artifact).unwrap();
    let direct = staged.run(&[]);
    let saved = staged.run(&["--analysis", path(&staged.artifact)]);

    for (invocation, label) in [(&direct, "direct"), (&saved, "saved")] {
        assert_eq!(
            invocation.result.status.code(),
            Some(1),
            "the {label} run must refuse a re-exported primitive without an imported-read proof:\n{}",
            invocation.result.stderr
        );
        let report_root = invocation.output_root().join("reports/tree/static/app");
        for report in [
            "dependency_graph.json.gz",
            "atomic_group_conflicts.json",
            "effect_translation_validation.json",
            "candidate_relocation_assessment.json",
        ] {
            assert!(
                report_root.join(report).is_file(),
                "the {label} refusal must preserve {report}"
            );
        }
        let conflicts: Vec<Value> = serde_json::from_slice(
            &fs::read(report_root.join("atomic_group_conflicts.json")).unwrap(),
        )
        .unwrap();
        assert!(
            conflicts.iter().any(|conflict| {
                let claims = conflict["claims"].as_array().expect("conflict claims");
                let causes = conflict["causes"].as_array().expect("conflict causes");
                ["compute", "later"].iter().all(|module| {
                    claims
                        .iter()
                        .any(|claim| claim["module"].as_str() == Some(module))
                }) && causes
                    .iter()
                    .any(|cause| cause.as_str() == Some("eager_use"))
            }),
            "the {label} refusal must preserve the source ownership conflict: {conflicts:#?}"
        );
    }
    assert_same_invocation_trees(&direct, &saved);
    assert_eq!(fs::read(&staged.artifact).unwrap(), artifact);
}

#[test]
fn cyclic_and_unresolved_reexported_primitive_reads_refuse_with_saved_analysis() {
    for provider in [
        "import './app.js';\nexport const limit = 2;\n",
        "import './missing.js';\nexport const limit = 2;\n",
    ] {
        assert_unproved_reexported_primitive_refusal_matches_saved(provider);
    }
}

#[test]
fn accepted_split_matches_a_direct_run() {
    assert_staged_run_matches(split_with_pure_import(), &[]);
    assert_staged_run_matches(
        FixtureOpts::new(
            "const value = 1;\nconst closure = () => { throw value; };\nconst erased = (globalThis.marker = 1, 1);\nconst first = 1, second = 2;\nexport { value, closure, erased, first, second };\n",
            vec![logical_module(
                "all",
                &[
                    Member::new("value"),
                    Member::new("closure"),
                    Member::new("erased"),
                    Member::new("first"),
                    Member::new("second"),
                ],
            )],
        ),
        &[],
    );
}

#[test]
fn callback_factory_privacy_decision_matches_saved_analysis() {
    for initializer in [
        "factory(() => later)",
        "((factory)((() => later)))",
        "(0, factory(() => later))",
        "factory((globalThis.marker = 1, () => later))",
        "({get current() { return factory; }}).current(() => later)",
    ] {
        let source = format!(
            "import {{ factory }} from './peer.js';\n\
             const wrapped = {initializer};\nconst later = 42;\nconsole.log(wrapped());\n"
        );
        assert_staged_run_matches(
            FixtureOpts::new(&source, vec![]).with_extra_chunks(&[(
                "static/peer",
                "export function factory(callback) { return () => callback(); }\n",
            )]),
            &["--effect-conflicts-jsonl"],
        );
    }
}

#[test]
fn lexical_and_compound_class_heritage_match_saved_analysis() {
    for heritage in ["Core", "(globalThis.saved = Core, Core)", "holder.Base"] {
        let source = format!(
            "class Core {{ read() {{ return later; }} }}\n\
             const holder = {{ get Base() {{ return Core; }} }};\n\
             class Derived extends {heritage} {{ static snapshot = 1; }}\n\
             const later = 42;\nconsole.log(new Derived().read(), Derived.snapshot);\n"
        );
        assert_staged_run_matches(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "classes",
                    &[
                        Member::new("Core"),
                        Member::new("holder"),
                        Member::new("Derived"),
                        Member::new("later"),
                    ],
                )],
            ),
            &["--effect-conflicts-jsonl"],
        );
    }
}

#[test]
fn gate_rejected_split_matches_a_direct_run() {
    assert_staged_run_matches(rejected_split(), &["--emit-on-rejection"]);
}

#[test]
fn destructuring_split_refusal_matches_with_saved_facts() {
    let source = "const source = { left: 1, right: 2 };\n\
         const { left, right } = source;\n\
         console.log(left, right);\n\
         export { left, right };\n";
    // Build a matching analysis artifact while the source has one explicit
    // destructuring owner, then change only the module assignment to create the
    // conflicting request. Assignment edits do not change these saved facts.
    let (staged, _fixture) = staged(FixtureOpts::new(
        source,
        vec![logical_module("left_module", &[Member::new("left")])],
    ));
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let right_module = serde_yaml::from_str(
        "members:\n  - name: right\n    selector:\n      binding:\n        name: right\n",
    )
    .unwrap();
    spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .unwrap()
        .insert(
            serde_yaml::Value::String("right_module".into()),
            right_module,
        );
    fs::write(&staged.spec, serde_yaml::to_string(&spec).unwrap()).unwrap();
    let direct = staged.run(&[]);
    let from_artifact = staged.run(&["--analysis", path(&staged.artifact)]);

    assert!(
        !direct.result.status.success(),
        "direct run unexpectedly succeeded"
    );
    assert_eq!(
        direct.result.status.code(),
        from_artifact.result.status.code()
    );
    assert_same_invocation_trees(&direct, &from_artifact);
    for result in [&direct, &from_artifact] {
        assert!(
            result
                .result
                .stderr
                .contains("source/destination placement projection is unmappable")
                && result
                    .result
                    .stderr
                    .contains("OwnedBindingDestinationMismatch")
                && result
                    .result
                    .stderr
                    .contains("DestructuringDestinationSplit")
                && result.result.stderr.contains("left_module")
                && result.result.stderr.contains("right_module")
                && result.result.stderr.contains("unresolved checks: {}"),
            "expected the common typed placement refusal with complete destinations in stderr:\n{}",
            result.result.stderr
        );
    }
}

#[test]
fn nested_var_split_refusal_matches_with_saved_facts() {
    let source = "try { var left = 10; var right = 20; } catch (e) {}\n\
console.log(left, right);\nexport { left, right };\n";
    // Save analysis while both nested-var bindings have one coherent owner,
    // then change only their module assignments to create a split request.
    let (staged, _fixture) = staged(FixtureOpts::new(
        source,
        vec![logical_module(
            "left_module",
            &[Member::new("left"), Member::new("right")],
        )],
    ));
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let modules = spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .unwrap();
    modules.insert(
        serde_yaml::Value::String("left_module".into()),
        serde_yaml::from_str(
            "members:\n  - name: left\n    selector:\n      binding:\n        name: left\n",
        )
        .unwrap(),
    );
    modules.insert(
        serde_yaml::Value::String("right_module".into()),
        serde_yaml::from_str(
            "members:\n  - name: right\n    selector:\n      binding:\n        name: right\n",
        )
        .unwrap(),
    );
    fs::write(&staged.spec, serde_yaml::to_string(&spec).unwrap()).unwrap();

    let direct = staged.run(&[]);
    let from_artifact = staged.run(&["--analysis", path(&staged.artifact)]);

    assert!(
        !direct.result.status.success(),
        "direct run unexpectedly succeeded"
    );
    assert_eq!(
        direct.result.status.code(),
        from_artifact.result.status.code()
    );
    assert_same_invocation_trees(&direct, &from_artifact);
    for result in [&direct, &from_artifact] {
        assert!(
            result.result.stderr.contains("NestedVarDestinationSplit")
                && result.result.stderr.contains("left_module")
                && result.result.stderr.contains("right_module"),
            "expected the shared typed nested-var placement refusal in stderr:\n{}",
            result.result.stderr
        );
    }
}

#[test]
fn composed_global_sites_and_dependencies_round_trip() {
    assert_staged_run_matches(
        composed_global_access_fixture(),
        &["--effect-conflicts-jsonl", "--emit-on-rejection"],
    );
}

#[test]
fn inferred_and_selected_set_spread_bindings_survive_saved_analysis() {
    const DECLARATION: &str = "var seed = new Set(['a', 'b']), sibling = new Set(['x']), copied = new Set([...seed, 'c']);";
    const SOURCE: &str = "console.log('before');\nvar seed = new Set(['a', 'b']), sibling = new Set(['x']), copied = new Set([...seed, 'c']);\nconsole.log('after', copied.size);\nexport {};\n";
    for selected in [false, true] {
        let mut opts = FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "sets",
                &[
                    Member::new("seed"),
                    Member::new("sibling"),
                    Member::new("copied"),
                ],
            )],
        );
        if selected {
            let inputs = [
                ("seed", NativeOperationKind::SetFromPrimitiveArray),
                ("sibling", NativeOperationKind::SetFromPrimitiveArray),
                ("copied", NativeOperationKind::SetFromSetSpread),
            ]
            .into_iter()
            .map(|(binding, operation)| NativeOperationInputContract {
                selector: SourceUnitSelector::SourceMatch(
                    serde_yaml::from_str(&format!(
                        "match: {DECLARATION:?}\nbindings: [{binding}]\n"
                    ))
                    .expect("native construction selector"),
                ),
                operation,
                audited_source_sha256: Sha256::digest(SOURCE.as_bytes())
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect(),
                note: None,
            })
            .collect();
            opts = opts.with_native_operation_inputs("static/app", inputs);
        }
        let (staged, _fixture) = staged(opts);
        let direct = staged.run(&[]);
        assert_ok(&direct.result, "direct Set spread run");
        let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
        assert_ok(&loaded.result, "Set spread run from saved analysis");
        assert_same_invocation_trees(&direct, &loaded);
        for invocation in [&direct, &loaded] {
            assert_node_output(
                &invocation.output_root().join("app/static/app/entry.js"),
                "before\nafter 3\n",
                "",
            );
        }
        let graph: Value = read_gzip_json(
            &loaded
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        let execution = &graph_node_for_binding(&graph, "copied")["synchronous_execution"];
        assert_eq!(execution["complete"], true, "{execution:#}");
        if selected {
            assert!(
                execution["native_operation_input_uses"]
                    .as_array()
                    .expect("selected Set spread input uses")
                    .iter()
                    .any(|use_site| {
                        use_site["evidence"]["operation"] == "set_from_set_spread"
                            && use_site["evidence"]["selected_binding"] == "copied"
                    }),
                "{execution:#}"
            );
        } else {
            assert!(
                execution["inference_uses"]
                    .as_array()
                    .expect("Set spread inference uses")
                    .iter()
                    .any(|proof| proof["conclusion"] == "primitive_set_iteration"),
                "{execution:#}"
            );
        }
    }
}
