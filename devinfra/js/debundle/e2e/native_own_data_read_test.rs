//! A native own-data function reference has no heap observation of caller arrays.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

const SOURCE: &str = "const sentinel = [1];\nconst before = external();\nvar alias = Object.defineProperty;\nconst after = alias;\n";
const SOURCE_INFERRED: &str = "const sentinel = [1];\nconst before = Math.random();\nvar alias = Object.defineProperty;\nconst after = alias;\n";

fn input(source: &str, declaration: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        selector: SourceUnitSelector::SourceMatch(
            serde_yaml::from_str(&format!(
                "match: {:?}\nbindings: [alias]\n",
                declaration.trim()
            ))
            .unwrap(),
        ),
        operation: NativeOperationKind::ObjectOwnDataFunctionRead,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("Only this original Object own-data read is audited.".to_owned()),
    }
}

fn split(source: &'static str, certified: bool) -> FixtureOpts<'static> {
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module("alias", &[Member::new("sentinel"), Member::new("alias")]),
            logical_module("consumer", &[Member::new("before"), Member::new("after")]),
        ],
    );
    if certified {
        opts.with_native_operation_inputs(
            "static/app",
            vec![input(source, "var alias = Object.defineProperty;")],
        )
    } else {
        opts
    }
}

fn node<'a>(graph: &'a Value, name: &str) -> &'a Value {
    graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|node| {
            node["declared_bindings"]
                .as_array()
                .is_some_and(|bindings| bindings.iter().any(|binding| binding["binding"] == name))
        })
        .unwrap()
}

fn assert_opaque_neighbor_rejected(outcome: FixtureRunOutcome, audited_read: bool) {
    let FixtureRunOutcome::Rejected(rejected) = outcome else {
        panic!("the opaque neighboring call must keep the candidate rejected");
    };
    assert_eq!(rejected.status, Some(1), "{}", rejected.stderr);

    let assessment = rejected.candidate_relocation_assessment();
    assert_eq!(assessment["emitted_order"], serde_json::json!([0, 2, 1, 3]));
    assert_eq!(assessment["decision"]["status"], "incomplete");
    assert_eq!(assessment["decision"]["uncovered_havoc"], true);
    assert_eq!(assessment["counts"]["uncovered_pairs"], 1);

    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["swapped_pairs"], 1);
    let pair = &effects["examples"][0];
    assert_eq!(pair["runs_first"]["module"], "alias");
    assert_eq!(pair["runs_later"]["module"], "consumer");
    assert_eq!(pair["havoc"], true);
    assert_eq!(pair["disposition"], "uncovered");
    assert_eq!(
        pair["later_owner_coverage_refusal"]["reason"],
        "unsupported_initializer"
    );
    assert_eq!(
        pair["later_owner_coverage_refusal"]["source_location"]["start_line"],
        2
    );

    let graph = serde_json::to_value(rejected.dependency_graph()).unwrap();
    let execution = &node(&graph, "alias")["synchronous_execution"];
    assert_eq!(execution["complete"], true);
    assert_eq!(
        execution["binding_effect_authority"]["authority"],
        "operation_receipt"
    );
    assert_eq!(
        execution["binding_effect_authority"]["receipt"]["root_kind"],
        "binding_initializer"
    );
    assert_eq!(
        execution["binding_effect_authority"]["receipt"]["binding"],
        "alias"
    );
    if audited_read {
        assert_eq!(
            execution["native_operation_input_uses"][0]["evidence"]["operation"],
            "object_own_data_function_read"
        );
    } else {
        assert!(execution["native_operation_input_uses"].is_null());
    }
    assert_eq!(
        execution["inference_uses"][0]["conclusion"],
        "native_own_data_read"
    );
}

fn decorator_trio(suffix: &str, class: &str, role: &str) -> String {
    format!(
        "var g{suffix} = Object.getOwnPropertyDescriptor;\nvar p{suffix} = Object.defineProperty;\n\
         var d{suffix} = (decorators, target, key) => {{\n  const desc = g{suffix}(target, key);\n\
           for (const decorator of decorators) decorator(target, key, desc);\n\
           p{suffix}(target, key, desc);\n}};\n\
         class {class} {{ m() {{ return '{role}'; }} }}\n\
         d{suffix}([], {class}.prototype, 'm', 1);\n"
    )
}

fn relational_opts<'a>(source: &'a str, input: NativeOperationInputContract) -> FixtureOpts<'a> {
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "alpha",
                &[
                    Member::source_alpha("Alpha", "class Alpha { m() { return 'alpha role'; } }"),
                    Member::makes_decorate_call("decorate", "Alpha", None, None),
                    Member::intrinsic_alias("alphaDefine", "defineProperty", "decorate"),
                    Member::intrinsic_alias("alphaGetter", "getOwnPropertyDescriptor", "decorate"),
                ],
            ),
            logical_module(
                "beta",
                &[
                    Member::source_alpha("Beta", "class Beta { m() { return 'beta role'; } }"),
                    Member::makes_decorate_call("decorate", "Beta", None, None),
                    Member::intrinsic_alias("betaDefine", "defineProperty", "decorate"),
                ],
            ),
        ],
    )
    .with_native_operation_inputs("static/app", vec![input])
}

fn relational_input(source: &str, module: &str, name: &str) -> NativeOperationInputContract {
    NativeOperationInputContract {
        selector: SourceUnitSelector::ResolvedMember(ResolvedMemberSelector {
            module: module.to_owned(),
            name: name.to_owned(),
        }),
        operation: NativeOperationKind::ObjectOwnDataFunctionRead,
        audited_source_sha256: Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("One resolved original read is audited.".to_owned()),
    }
}

fn project_analysis(opts: FixtureOpts<'_>) -> CommandResult {
    let fixture = write_validate_fixture_spec(opts);
    let output = std::process::Command::new(debundler_path())
        .args(["spec", "project-analysis", "--spec"])
        .arg(&fixture.spec_path)
        .arg("--out")
        .arg(fixture.spec_path.with_extension("analysis.projection"))
        .output()
        .expect("spawn debundle spec project-analysis");
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

fn assert_project_analysis_rejected(opts: FixtureOpts<'_>, tokens: &[&str]) {
    let result = project_analysis(opts);
    assert_eq!(
        result.status.code(),
        Some(1),
        "expected project-analysis to reject the premise\nstdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    for token in tokens {
        assert!(
            result.stderr.contains(token),
            "project-analysis stderr missing {token:?}\nstderr:\n{}",
            result.stderr,
        );
    }
}

#[test]
fn resolved_member_certifies_only_the_selected_alias_without_covering_decoration() {
    let source = format!(
        "{}{}",
        decorator_trio("a", "Alpha", "alpha role"),
        decorator_trio("b", "Beta", "beta role")
    );
    let outcome = run_fixture_observing_gate(relational_opts(
        &source,
        relational_input(&source, "alpha", "alphaDefine"),
    ));
    let FixtureRunOutcome::Rejected(fixture) = outcome else {
        panic!("the selected alias cannot cover the unproved decoration");
    };
    assert_eq!(fixture.status, Some(1), "{}", fixture.stderr);
    let assessment = fixture.candidate_relocation_assessment();
    assert_eq!(assessment["decision"]["status"], "incomplete");
    assert_eq!(assessment["decision"]["uncovered_havoc"], true);
    assert_eq!(assessment["counts"]["uncovered_pairs"], 4);
    assert_eq!(
        assessment["emitted_order"],
        serde_json::json!([0, 1, 2, 3, 5, 6, 7, 8, 4, 9])
    );
    let effects = fixture.effect_translation_validation();
    let pairs = effects["examples"].as_array().unwrap();
    assert_eq!(pairs.len(), 4);
    for pair in pairs {
        assert_eq!(pair["runs_later"]["unit"], 4);
        assert_eq!(pair["havoc"], true);
        assert_eq!(pair["disposition"], "uncovered");
        assert_eq!(
            pair["later_owner_coverage_refusal"]["source_location"]["start_line"],
            9
        );
    }
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let selected = node(&graph, "pa");
    let other = node(&graph, "pb");
    assert_eq!(
        selected["synchronous_execution"]["binding_effect_authority"]["authority"],
        "operation_receipt"
    );
    let decoration = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|unit| unit["id"] == "unit:4")
        .unwrap();
    assert_eq!(decoration["synchronous_execution"]["complete"], false);
    assert!(
        decoration["synchronous_execution"]["precision_losses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|loss| {
                loss["reason"]["operation"] == "member_read"
                    && loss["reason"]["ordered_field_read_refusal"] == "receiver_not_object"
            })
    );
    let evidence = &selected["synchronous_execution"]["native_operation_input_uses"][0]["evidence"];
    assert_eq!(evidence["reference"]["defining_chunk"], "static/app");
    assert_eq!(evidence["reference"]["input_index"], 0);
    assert!(
        evidence["spec_location"]
            .as_str()
            .unwrap()
            .contains("native_operation_inputs[0]")
    );
    assert_eq!(
        evidence["audited_source_sha256"],
        relational_input(&source, "alpha", "alphaDefine").audited_source_sha256
    );
    assert_eq!(
        selected["synchronous_execution"]["native_operation_input_uses"][0]["evidence"]["selector"]
            ["resolved_member"]["module"],
        "alpha"
    );
    assert_eq!(
        selected["synchronous_execution"]["native_operation_input_uses"][0]["evidence"]["selector"]
            ["resolved_member"]["name"],
        "alphaDefine"
    );
    assert!(
        other["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .is_none_or(Vec::is_empty)
    );
}

#[test]
fn source_match_and_resolved_member_keep_the_same_site_and_dependencies() {
    const SOURCE: &str =
        "var alias = Object.defineProperty;\nconst value = alias;\nexport { alias, value };\n";
    let modules = vec![logical_module(
        "alias_module",
        &[Member::new("alias"), Member::new("value")],
    )];
    let source_match = run_fixture(
        FixtureOpts::new(SOURCE, modules.clone()).with_native_operation_inputs(
            "static/app",
            vec![input(SOURCE, "var alias = Object.defineProperty;")],
        ),
    );
    let resolved_member = run_fixture(
        FixtureOpts::new(SOURCE, modules).with_native_operation_inputs(
            "static/app",
            vec![relational_input(SOURCE, "alias_module", "alias")],
        ),
    );

    let source_graph = serde_json::to_value(source_match.dependency_graph()).unwrap();
    let member_graph = serde_json::to_value(resolved_member.dependency_graph()).unwrap();
    let source_alias = node(&source_graph, "alias");
    let member_alias = node(&member_graph, "alias");
    let source_use = &source_alias["synchronous_execution"]["native_operation_input_uses"][0];
    let member_use = &member_alias["synchronous_execution"]["native_operation_input_uses"][0];
    for field in ["reference", "audited_source_sha256"] {
        assert_eq!(
            source_use["evidence"][field], member_use["evidence"][field],
            "selector form changed the selected site in {field}"
        );
    }
    let spec_claim_location = |use_site: &Value| -> String {
        use_site["evidence"]["spec_location"]
            .as_str()
            .unwrap()
            .split_once(":chunk_analysis_options.")
            .unwrap()
            .1
            .to_owned()
    };
    assert_eq!(
        spec_claim_location(source_use),
        spec_claim_location(member_use),
        "selector form changed the authored claim location"
    );
    assert_eq!(
        source_use["evidence"]["operation"],
        member_use["evidence"]["operation"]
    );
    assert_eq!(
        source_alias["inference_uses"],
        member_alias["inference_uses"]
    );
    assert_eq!(source_graph["edges"], member_graph["edges"]);
}

#[test]
fn native_own_data_read_composes_exact_root_effect_coverage() {
    const SOURCE: &str = "const marker = 1;\nvar alias = Object.defineProperty;\nconst after = alias;\nconsole.log(marker, typeof after);\n";
    for asserted in [false, true] {
        let opts = FixtureOpts::new(
            SOURCE,
            vec![
                logical_module(
                    "alias_module",
                    &[Member::new("alias"), Member::new("after")],
                ),
                logical_module_with_anon(
                    "observe",
                    &[Member::new("marker")],
                    &["console.log(marker, typeof after);"],
                ),
            ],
        );
        let opts = if asserted {
            opts.with_native_operation_inputs(
                "static/app",
                vec![input(SOURCE, "var alias = Object.defineProperty;")],
            )
        } else {
            opts
        };
        let fixture = run_fixture(opts);
        assert_preserved_entry_output(&fixture, "1 function\n");
        let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
        let execution = &node(&graph, "alias")["synchronous_execution"];
        assert_eq!(execution["complete"], true, "{execution:#}");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{execution:#}"
        );
        let report = fixture.effect_translation_validation();
        assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report:#}");
        assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
        if asserted {
            assert!(
                report["relocation_dependencies"]["native_operation_inputs"]
                    .as_array()
                    .is_some_and(|inputs| !inputs.is_empty()),
                "the exact read's author condition survives commutation: {report:#}"
            );
        }
    }
}

#[test]
fn project_analysis_rejects_missing_and_foreign_resolved_members() {
    let source = format!(
        "{}{}",
        decorator_trio("a", "Alpha", "alpha role"),
        decorator_trio("b", "Beta", "beta role")
    );
    let valid = project_analysis(relational_opts(
        &source,
        relational_input(&source, "alpha", "alphaDefine"),
    ));
    assert!(
        valid.status.success(),
        "valid resolved member failed project-analysis\nstdout:\n{}\nstderr:\n{}",
        valid.stdout,
        valid.stderr,
    );

    for (module, name) in [("missing", "alphaDefine"), ("alpha", "missing")] {
        assert_project_analysis_rejected(
            relational_opts(&source, relational_input(&source, module, name)),
            &[
                "chunk_analysis_options.static/app.native_operation_inputs[0]",
                &format!("module {module}"),
                &format!("member {name}"),
                "chunk-relative",
            ],
        );
    }

    const CONSUMER: &str = "import { alias } from './peer.js';\nconsole.log(alias);\n";
    const PROVIDER: &str = "var alias = Object.defineProperty;\nexport { alias };\n";
    assert_project_analysis_rejected(
        FixtureOpts::new(CONSUMER, vec![])
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_extra_chunk_logical_modules(&[(
                "static/peer",
                vec![logical_module("peer", &[Member::new("alias")])],
            )])
            .with_native_operation_inputs(
                "static/app",
                vec![relational_input(CONSUMER, "peer", "alias")],
            ),
        &[
            "chunk_analysis_options.static/app.native_operation_inputs[0]",
            "module peer",
            "member alias",
            "static/peer",
            "chunk-relative",
        ],
    );
}

#[test]
fn resolved_member_rejects_stale_hash_and_wrong_reference_or_shape() {
    let source = format!(
        "{}{}",
        decorator_trio("a", "Alpha", "alpha role"),
        decorator_trio("b", "Beta", "beta role")
    );
    let mut stale = relational_input(&source, "alpha", "alphaDefine");
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        relational_opts(&source, stale),
        &["stale audited_source_sha256"],
    );
    expect_rejection_containing_all(
        relational_opts(&source, relational_input(&source, "alpha", "missing")),
        &["resolved_member names no logical module member"],
    );
    expect_rejection_containing_all(
        relational_opts(&source, relational_input(&source, "alpha", "decorate")),
        &["unshadowed static Object own-data function read initializer"],
    );
}

#[test]
fn resolved_member_cannot_reference_another_provider_chunk() {
    const CONSUMER: &str = "import { alias } from './peer.js';\nconsole.log(alias);\n";
    const PROVIDER: &str = "var alias = Object.defineProperty;\nexport { alias };\n";
    expect_rejection_containing_all(
        FixtureOpts::new(CONSUMER, vec![])
            .with_extra_chunks(&[("static/peer", PROVIDER)])
            .with_extra_chunk_logical_modules(&[(
                "static/peer",
                vec![logical_module("peer", &[Member::new("alias")])],
            )])
            .with_native_operation_inputs(
                "static/app",
                vec![relational_input(CONSUMER, "peer", "alias")],
            ),
        &["resolved_member belongs to another provider"],
    );
}

#[test]
fn native_input_requires_exactly_one_selector_variant() {
    let common = "operation: object_own_data_function_read\naudited_source_sha256: ";
    let hash = "0".repeat(64);
    let none = format!("{common}{hash}\n");
    assert!(
        serde_yaml::from_str::<NativeOperationInputContract>(&none)
            .unwrap_err()
            .to_string()
            .contains("exactly one of source_match or resolved_member")
    );
    let both = format!(
        "{common}{hash}\nsource_match:\n  match: 'var alias = Object.defineProperty;'\n  bindings: [alias]\nresolved_member:\n  module: alpha\n  name: alphaDefine\n"
    );
    assert!(
        serde_yaml::from_str::<NativeOperationInputContract>(&both)
            .unwrap_err()
            .to_string()
            .contains("exactly one of source_match or resolved_member")
    );
    let legacy = format!(
        "{common}{hash}\nsource_match:\n  match: 'var alias = Object.defineProperty;'\n  bindings: [alias]\n"
    );
    assert!(matches!(
        serde_yaml::from_str::<NativeOperationInputContract>(&legacy)
            .unwrap()
            .selector,
        SourceUnitSelector::SourceMatch(_)
    ));
}

#[test]
fn audited_native_read_does_not_cover_an_external_neighbor_call() {
    assert_opaque_neighbor_rejected(run_fixture_observing_gate(split(SOURCE, true)), true);
}

#[test]
fn inferred_native_read_does_not_cover_a_math_random_neighbor_call() {
    assert_opaque_neighbor_rejected(
        run_fixture_observing_gate(split(SOURCE_INFERRED, false)),
        false,
    );
}

#[test]
fn native_own_data_read_crosses_a_covered_lexical_array_store() {
    const SOURCE: &str = "const sentinel = [1];\nconst before = [];\nbefore.push(1);\nvar alias = Object.defineProperty;\nconst after = alias;\nconsole.log(before.length, typeof after);\n";
    let fixture = run_fixture(FixtureOpts::new(
        SOURCE,
        vec![
            logical_module("alias", &[Member::new("sentinel"), Member::new("alias")]),
            logical_module_with_anon(
                "consumer",
                &[Member::new("before"), Member::new("after")],
                &["before.push(1);"],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1 function\n");

    let assessment = fixture.candidate_relocation_assessment();
    assert_eq!(assessment["decision"]["status"], "accepted");
    assert!(
        assessment["swapped_pairs"].as_u64().unwrap() > 0,
        "{assessment:#}"
    );

    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    let alias = node(&graph, "alias");
    let alias_execution = &alias["synchronous_execution"];
    assert_eq!(alias_execution["complete"], true);
    assert_eq!(
        alias_execution["binding_effect_authority"]["authority"],
        "operation_receipt"
    );
    let alias_receipt = &alias_execution["binding_effect_authority"]["receipt"];
    assert_eq!(alias_receipt["root_kind"], "binding_initializer");
    assert_eq!(alias_receipt["binding"], "alias");
    assert_eq!(
        alias_execution["inference_uses"][0]["conclusion"],
        "native_own_data_read"
    );

    let push = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|unit| unit["source_location"]["start_line"] == 3)
        .expect("the source unit for the lexical array store");
    let push_execution = &push["synchronous_execution"];
    assert_eq!(push_execution["complete"], true, "{push_execution:#}");
    assert_eq!(
        push_execution["binding_effect_authority"]["authority"],
        "operation_receipt"
    );
    let push_receipt = &push_execution["binding_effect_authority"]["receipt"];
    assert_eq!(push_receipt["root_kind"], "expression_statement");
    assert_eq!(push_receipt["calls"][0]["target"]["intrinsic"], "array");
    assert_eq!(
        push_receipt["calls"][0]["target"]["property"],
        "prototype.push"
    );
    assert!(
        push_execution["heap_frontier"]["composed_synchronous_calls"]["writes"]["sample"]
            .as_array()
            .unwrap()
            .iter()
            .any(|write| write["property"] == "length")
    );

    let emitted_order = assessment["emitted_order"].as_array().unwrap();
    let unit_index = |unit_id: &str| {
        unit_id
            .strip_prefix("unit:")
            .expect("source-unit ID")
            .parse::<u64>()
            .expect("numeric source-unit ID")
    };
    let alias_unit = unit_index(alias["id"].as_str().unwrap());
    let push_unit = unit_index(push["id"].as_str().unwrap());
    let output_position = |source_unit: u64| {
        emitted_order
            .iter()
            .position(|candidate| candidate.as_u64() == Some(source_unit))
            .expect("source unit in emitted order")
    };
    assert!(alias_unit > push_unit, "the source read follows the store");
    assert!(
        output_position(alias_unit) < output_position(push_unit),
        "the emitted candidate must move the exact native read before the lexical store: {assessment:#}"
    );
}

#[test]
fn exact_input_rejects_unsupported_and_stale_sites() {
    for (source, expected) in [
        (
            "const Object = { get defineProperty() { throw Error('getter'); } };\nvar alias = Object.defineProperty;\n",
            "unshadowed static Object own-data function read",
        ),
        (
            "const Object = [];\nvar alias = Object.defineProperty;\n",
            "unshadowed static Object own-data function read",
        ),
        ("var alias = Object?.defineProperty;\n", "source_match"),
        ("var alias = Object['defineProperty'];\n", "source_match"),
    ] {
        expect_rejection_containing_all(
            FixtureOpts::new(
                source,
                vec![logical_module("alias", &[Member::new("alias")])],
            )
            .with_native_operation_inputs(
                "static/app",
                vec![input(source, "var alias = Object.defineProperty;")],
            ),
            &[expected],
        );
    }
    let mut stale = input(SOURCE, "var alias = Object.defineProperty;");
    stale.audited_source_sha256 = "0".repeat(64);
    expect_rejection_containing_all(
        split(SOURCE, false).with_native_operation_inputs("static/app", vec![stale]),
        &["audited_source_sha256"],
    );
    expect_rejection_containing_all(
        split(SOURCE, false).with_native_operation_inputs(
            "static/app",
            vec![input(SOURCE, "const after = alias;")],
        ),
        &["source_match"],
    );
}

#[test]
fn sibling_initializer_effect_survives_a_certified_read() {
    let source = "var alias = Object.defineProperty, other = external();\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![input(
                    source,
                    "var alias = Object.defineProperty, other = external();",
                )],
            ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_ne!(node(&graph, "other")["purity"]["kind"], "pure");
    assert_eq!(
        node(&graph, "alias")["synchronous_execution"]["native_operation_input_uses"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn uncertified_second_native_read_in_one_declaration_stays_observable() {
    let source = "Object.entries = function replacement() {};\nvar alias = Object.defineProperty, other = Object.entries;\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![input(
                    source,
                    "var alias = Object.defineProperty, other = Object.entries;",
                )],
            ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_ne!(node(&graph, "other")["purity"]["kind"], "pure");
    let alias_uses = node(&graph, "alias")["synchronous_execution"]["native_operation_input_uses"]
        .as_array()
        .unwrap();
    assert_eq!(alias_uses.len(), 1);
}

#[test]
fn known_exact_slot_writes_contradict_only_the_relevant_premise() {
    for write in [
        "Object.defineProperty = function replacement() {};",
        "Object.defineProperty(Object, 'defineProperty', { get() { throw Error('getter'); } });",
        "const ctor = Object; ctor.defineProperty = function replacement() {};",
        "globalThis.Object = function replacement() {};",
    ] {
        let source = format!("{write}\nvar alias = Object.defineProperty;\n");
        expect_rejection_containing_all(
            FixtureOpts::new(
                &source,
                vec![logical_module("alias", &[Member::new("alias")])],
            )
            .with_native_operation_inputs(
                "static/app",
                vec![input(&source, "var alias = Object.defineProperty;")],
            ),
            &["contradicted by a known Object binding or Object.defineProperty write"],
        );
    }
    let source =
        "Object.entries = function replacement() {};\nvar alias = Object.defineProperty;\n";
    let outcome = run_fixture_observing_gate(
        FixtureOpts::new(
            source,
            vec![logical_module("alias", &[Member::new("alias")])],
        )
        .with_native_operation_inputs(
            "static/app",
            vec![input(source, "var alias = Object.defineProperty;")],
        ),
    );
    let FixtureRunOutcome::Rejected(fixture) = outcome else {
        panic!("the selected read premise cannot cover the unrelated property store");
    };
    assert_eq!(fixture.status, Some(1), "{}", fixture.stderr);
    let assessment = fixture.candidate_relocation_assessment();
    assert_eq!(assessment["emitted_order"], serde_json::json!([1, 0]));
    assert_eq!(assessment["decision"]["status"], "incomplete");
    assert_eq!(assessment["counts"]["premise_contradictions"], 0);
    assert_eq!(assessment["counts"]["uncovered_pairs"], 1);
    let effects = fixture.effect_translation_validation();
    let pair = &effects["examples"][0];
    assert_eq!(pair["runs_first"]["unit"], 1);
    assert_eq!(pair["runs_later"]["unit"], 0);
    assert_eq!(pair["havoc"], true);
    assert_eq!(pair["disposition"], "uncovered");
    assert_eq!(
        pair["later_owner_coverage_refusal"]["source_location"]["start_line"],
        1
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_eq!(
        node(&graph, "alias")["synchronous_execution"]["binding_effect_authority"]["authority"],
        "operation_receipt"
    );
    assert!(
        graph["nodes"][0]["synchronous_execution"]["precision_losses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|loss| loss["reason"]["operation"] == "property_write")
    );
    assert_eq!(
        node(&graph, "alias")["synchronous_execution"]["native_operation_input_uses"][0]["evidence"]
            ["operation"],
        "object_own_data_function_read"
    );
}

#[test]
fn known_exact_slot_write_in_an_imported_chunk_contradicts_the_input() {
    let source = "import './mutation.js';\nvar alias = Object.defineProperty;\nexport { alias };\n";
    let mutation = "Object.defineProperty = function replacement() {};\nexport {};\n";
    expect_rejection_containing_all(
        FixtureOpts::new(
            source,
            vec![logical_module("alias", &[Member::new("alias")])],
        )
        .with_extra_chunks(&[("static/mutation", mutation)])
        .with_native_operation_inputs(
            "static/app",
            vec![input(source, "var alias = Object.defineProperty;")],
        ),
        &[
            "contradicted by a known Object binding or Object.defineProperty write",
            "static/mutation",
        ],
    );
}

#[test]
fn invoking_the_alias_keeps_call_heap_effects() {
    let source = "const sentinel = [1];\nvar alias = Object.defineProperty;\nconst after = alias(sentinel, 'x', { value: 2 });\nconsole.log(sentinel.x);\n";
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs(
                "static/app",
                vec![input(source, "var alias = Object.defineProperty;")],
            ),
    );
    let graph = serde_json::to_value(fixture.dependency_graph()).unwrap();
    assert_preserved_entry_output(&fixture, "2\n");
    assert_ne!(node(&graph, "after")["purity"]["kind"], "pure");
    assert!(
        node(&graph, "after")["synchronous_execution"]["native_operation_input_uses"].is_null()
    );
}
