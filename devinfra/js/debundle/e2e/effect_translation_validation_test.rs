//! Public-pipeline controls for conservative, report-only effect TV.

use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

#[test]
fn linkage_export_does_not_add_runtime_havoc_to_relocation() {
    let fixture = run_fixture(FixtureOpts::new(
        "const first = 1;\nexport { first };\nconst second = 2;\nconsole.log(first, second);\n",
        vec![logical_module("second", &[Member::new("second")])],
    ));
    assert_preserved_entry_output(&fixture, "1 2\n");
    let report = fixture.effect_translation_validation();
    assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report}");
    assert_eq!(report["decision"]["status"], "accepted", "{report}");
}

#[test]
fn linkage_reexports_keep_dependency_evaluation_coverage_open() {
    for linkage in [
        "export { shared } from './peer.js';",
        "export * from './peer.js';",
    ] {
        let source = format!("{linkage}\nconst second = 2;\nconsole.log(second);\n");
        let fixture = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module("second", &[Member::new("second")])],
            )
            .with_extra_chunks(&[(
                "static/peer",
                "console.log('peer'); export const shared = 1;\n",
            )]),
        );
        assert_preserved_entry_output(&fixture, "peer\n2\n");
        let report = fixture.effect_translation_validation();
        assert!(report["swapped_pairs"].as_u64().unwrap() > 0, "{report}");
        assert_eq!(report["decision"]["status"], "incomplete", "{report}");
    }
}

fn native_constructor_input(
    source: &str,
    declaration: &str,
    binding: &str,
    operation: NativeOperationKind,
) -> NativeOperationInputContract {
    let audited_source_sha256 = Sha256::digest(source.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    NativeOperationInputContract {
        selector: SourceUnitSelector::SourceMatch(
            serde_yaml::from_str(&format!(
                "match: {:?}\nbindings: [{binding}]\n",
                declaration.trim()
            ))
            .expect("valid direct constructor source match"),
        ),
        operation,
        audited_source_sha256,
        note: Some("Neutral fixture audits one direct empty native constructor.".to_string()),
    }
}

#[test]
fn weak_map_empty_owner_keeps_its_exact_effect_coverage_dependency() {
    const SOURCE: &str = "function publish(key, value) { globalThis[key] = value; }\nconst emptyWeakMap = new WeakMap();\nconst emptyMap = new Map();\nconsole.log(emptyWeakMap ? 'ready' : 'missing', emptyMap ? 'ready' : 'missing');\n";
    let inputs = vec![
        native_constructor_input(
            SOURCE,
            "const emptyWeakMap = new WeakMap();",
            "emptyWeakMap",
            NativeOperationKind::WeakMapEmpty,
        ),
        native_constructor_input(
            SOURCE,
            "const emptyMap = new Map();",
            "emptyMap",
            NativeOperationKind::MapEmpty,
        ),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "maps",
                &[Member::new("emptyWeakMap"), Member::new("emptyMap")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs),
    );
    assert_preserved_entry_output(&fixture, "ready ready\n");

    let report = fixture.effect_translation_validation();
    assert!(report["legacy_gate_accepts"].as_bool().unwrap_or(false));
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["initializer_result_unproved"]
            .as_u64()
            .unwrap_or_default(),
        0,
        "the exact WeakMap and Map sites complete only their named owners: {report:#}"
    );
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["native_operation_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "both owner effect receipts retain their exact input dependency: {report:#}"
    );
}

fn empty_native_collection_inputs(
    source: &str,
    declaration_kind: &str,
) -> Vec<NativeOperationInputContract> {
    let map_declaration = format!("{declaration_kind} map = new Map();");
    let set_declaration = format!("{declaration_kind} set = new Set();");
    vec![
        native_constructor_input(
            source,
            &map_declaration,
            "map",
            NativeOperationKind::MapEmpty,
        ),
        native_constructor_input(
            source,
            &set_declaration,
            "set",
            NativeOperationKind::SetEmpty,
        ),
    ]
}

fn binding_execution_report(fixture: &Fixture, binding: &str) -> Value {
    let graph = fixture.dependency_graph();
    let unit = source_unit_for_binding(&graph, binding);
    let node = graph.nodes.iter().find(|node| node.id == unit).unwrap();
    serde_json::to_value(
        node.synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .expect("serialize execution report")
}

fn assert_inline_operation_receipt(
    fixture: &Fixture,
    binding: &str,
    expected_calls: usize,
    case: &str,
) {
    let execution = binding_execution_report(fixture, binding);
    assert_eq!(execution["complete"], true, "{case}: {execution:#}");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(
        authority["authority"], "operation_receipt",
        "{case}: {execution:#}"
    );
    let receipt = &authority["receipt"];
    assert_eq!(receipt["binding"], binding, "{case}: {receipt:#}");
    assert_eq!(
        receipt["calls"].as_array().unwrap().len(),
        expected_calls,
        "{case}: {receipt:#}"
    );
}

#[test]
fn composed_global_cells_keep_sites_dependencies_and_may_provenance() {
    let fixture = run_rejection_fixture_with_args(
        composed_global_access_fixture(),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    let pair = rows
        .iter()
        .find(|pair| pair["runs_first"]["unit"] == 7 && pair["runs_later"]["unit"] == 6)
        .expect("interleaved tail and writer");
    let cell = pair["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|cell| cell["cell"]["kind"] == "global_prop" && cell["cell"]["key"] == "marker")
        .unwrap_or_else(|| panic!("composed marker cell: {pair:#}"));
    assert_eq!(cell["observed_conflict"], false, "{cell:#}");
    for (access, owner, kind) in [
        ("first_read", 1, "read"),
        ("first_write", 1, "write"),
        ("later_write", 0, "write"),
    ] {
        let evidence = &cell[access];
        assert_eq!(evidence["kind"], "indexed_may_access", "{cell:#}");
        let operations = evidence["global_may_accesses"].as_array().unwrap();
        assert_eq!(
            operations.len(),
            1,
            "physical operation is idempotent across flattened/value/memo paths: {cell:#}"
        );
        assert_eq!(operations[0]["site"]["lexical_owner"], owner);
        assert_eq!(operations[0]["site"]["access"], kind);
        let source = composed_global_access_fixture().source;
        let (start, length) = if kind == "read" {
            (
                source.find("return globalThis.marker;").unwrap() + "return ".len(),
                "globalThis.marker".len(),
            )
        } else {
            let owner_start = if owner == 0 {
                0
            } else {
                source.find("function peek()").unwrap()
            };
            (
                owner_start + source[owner_start..].find("globalThis.marker = 1").unwrap(),
                "globalThis.marker = 1".len(),
            )
        };
        assert_eq!(operations[0]["site"]["start"], start + 1);
        assert_eq!(operations[0]["site"]["end"], start + length + 1);
    }
    let dependency = &cell["first_read"]["global_may_accesses"][0]["evidence"]["dependencies"]["program_premises"];
    assert!(
        dependency
            .as_array()
            .unwrap()
            .contains(&serde_json::json!("intrinsics_never_written")),
        "{cell:#}"
    );
    for endpoint in ["first_opacity", "later_opacity"] {
        assert_eq!(
            pair[endpoint]["unknown_coverage"],
            serde_json::json!(["global_properties", "general_heap", "host_effects"])
        );
    }
    assert!(
        rows.iter()
            .flat_map(|row| row["cells"].as_array().unwrap())
            .all(|cell| cell["cell"]["key"] != "unused"),
        "lazy bodies are not invocation certificates"
    );

    let mut co_located = composed_global_access_fixture();
    co_located.logical_modules = vec![logical_module(
        "all",
        &[
            Member::new("mutate"),
            Member::new("peek"),
            Member::new("nested"),
            Member::new("unused"),
            Member::new("install"),
            Member::new("first"),
            Member::new("second"),
            Member::new("tail"),
        ],
    )];
    assert_preserved_entry_output(&run_fixture(co_located), "1 2 1\n");
}

#[test]
fn global_aliases_and_shadowed_receivers_keep_conservative_provenance() {
    for (alias, output) in [
        ("globalThis", "1 2 1\n"),
        ("globalThis.choose ? globalThis : {}", "1 2 undefined\n"),
        ("global", "1 2 1\n"),
    ] {
        let host = || HostBehavior {
            predefined_globals: if alias == "global" {
                PredefinedGlobals::Standard
            } else {
                PredefinedGlobals::Unknown
            },
            ..HostBehavior::default()
        };
        let source = format!(
            "const alias = {alias};\n\
             function mutate() {{ alias.marker = 1; return 1; }}\n\
             function peek() {{ alias.marker = 1; return alias.marker; }}\n\
             function shadow(globalThis) {{ globalThis.shadowed = 1; return globalThis.shadowed; }}\n\
             const first = peek();\n\
             const second = mutate();\n\
             const tail = (shadow({{}}), peek()) + first;\n\
             console.log(first, tail, globalThis.marker);\n\
             export {{ alias, mutate, peek, shadow, first, second, tail }};\n"
        );
        let fixture = run_rejection_fixture_with_args(
            FixtureOpts::new(
                &source,
                vec![
                    logical_module(
                        "outer",
                        &[
                            Member::new("alias"),
                            Member::new("mutate"),
                            Member::new("peek"),
                            Member::new("shadow"),
                            Member::new("first"),
                            Member::new("tail"),
                        ],
                    ),
                    logical_module("inner", &[Member::new("second")]),
                ],
            )
            .with_unassigned_mode(unassigned_mode_inline())
            .with_host_behavior(host()),
            &["--effect-conflicts-jsonl"],
            1,
        );
        let rows = read_jsonl(
            &fixture
                .report_root
                .join(&fixture.chunk_id)
                .join("effect_conflicts.jsonl"),
        );
        assert_eq!(
            rows.iter()
                .flat_map(|row| row["cells"].as_array().unwrap())
                .any(|cell| cell["cell"]["kind"] == "global_prop"
                    && cell["cell"]["key"] == "marker"
                    && cell["observed_conflict"] == false),
            alias != "global",
            "only aliases modeled by the execution value domain contribute cells; census-only Node global stays opaque: {rows:#?}"
        );
        assert!(
            rows.iter()
                .flat_map(|row| row["cells"].as_array().unwrap())
                .all(|cell| cell["cell"]["key"] != "shadowed"),
            "a parameter named globalThis is an ordinary receiver: {rows:#?}"
        );
        assert!(
            rows.iter().all(|row| row["havoc"] == true),
            "alias/value precision cannot clear missing coverage"
        );
        let co_located = run_fixture(
            FixtureOpts::new(
                &source,
                vec![logical_module(
                    "all",
                    &[
                        Member::new("alias"),
                        Member::new("mutate"),
                        Member::new("peek"),
                        Member::new("shadow"),
                        Member::new("first"),
                        Member::new("second"),
                        Member::new("tail"),
                    ],
                )],
            )
            .with_host_behavior(host()),
        );
        assert_preserved_entry_output(&co_located, output);
    }
}

#[test]
fn opaque_callback_accesses_retain_their_assumption_without_contaminating_other_owners() {
    let source = composed_global_access_fixture().source.replace(
        "const tail = nested() + first;",
        "const tail = JSON.parse(\"[1]\").map(peek);",
    );
    let mut opts = composed_global_access_fixture();
    opts.source = &source;
    let fixture = run_rejection_fixture_with_args(opts, &["--effect-conflicts-jsonl"], 1);
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    let cell = rows
        .iter()
        .flat_map(|row| row["cells"].as_array().unwrap())
        .find(|cell| cell["cell"]["kind"] == "global_prop" && cell["cell"]["key"] == "marker")
        .expect("callback global conflict");
    assert_eq!(cell["observed_conflict"], false);
    let operations = cell["first_read"]["global_may_accesses"]
        .as_array()
        .unwrap();
    assert!(
        operations
            .iter()
            .any(|operation| operation["evidence"]["assumptions"]
                .as_array()
                .unwrap()
                .iter()
                .any(|reason| reason["path"] == "opaque_callback_invocation")),
        "{cell:#}"
    );
    assert!(
        cell["later_write"]["global_may_accesses"]
            .as_array()
            .unwrap()
            .iter()
            .all(|operation| operation["evidence"]["assumptions"]
                .as_array()
                .unwrap()
                .is_empty()),
        "another owner's direct write must not inherit the opaque callback path: {cell:#}"
    );
}

#[test]
fn default_export_value_evaluation_keeps_child_global_effects() {
    let source = "function mutate() { globalThis.marker = 1; return 1; }\n\
                  function peek() { return globalThis.marker; }\n\
                  function nested() { return peek(); }\n\
                  const before = mutate();\n\
                  const gap = mutate();\n\
                  export default nested();\n\
                  console.log(before, gap, globalThis.marker);\n";
    let members = || {
        [
            Member::new("mutate"),
            Member::new("peek"),
            Member::new("nested"),
            Member::new("before"),
        ]
    };
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon("outer", &members(), &["export default nested();"]),
                logical_module("inner", &[Member::new("gap")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    assert!(
        rows.iter().any(|row| row["runs_first"]["unit"] == 5
            && row["cells"]
                .as_array()
                .unwrap()
                .iter()
                .any(|cell| cell["cell"]["kind"] == "global_prop"
                    && cell["cell"]["key"] == "marker"
                    && cell["first_read"]["global_may_accesses"]
                        .as_array()
                        .is_some_and(|operations| operations
                            .iter()
                            .any(|operation| operation["site"]["lexical_owner"] == 1)))),
        "default export must retain nested return evaluation: {rows:#?}"
    );
    let mut all = members().into_iter().collect::<Vec<_>>();
    all.push(Member::new("gap"));
    assert_preserved_entry_output(
        &run_fixture(FixtureOpts::new(
            source,
            vec![logical_module_with_anon(
                "all",
                &all,
                &["export default nested();"],
            )],
        )),
        "1 1 1\n",
    );
}

#[test]
fn default_cell_provenance_is_bounded_but_full_stream_keeps_every_site() {
    let writes = "globalThis.marker = 1; ".repeat(24);
    let source = format!(
        "function mutate() {{ globalThis.marker = 1; return 1; }}\n\
         function many() {{ {writes} return globalThis.marker; }}\n\
         const first = many();\n\
         const second = mutate();\n\
         const tail = many() + first;\n\
         export {{ mutate, many, first, second, tail }};\n"
    );
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "outer",
                    &[
                        Member::new("mutate"),
                        Member::new("many"),
                        Member::new("first"),
                        Member::new("tail"),
                    ],
                ),
                logical_module("inner", &[Member::new("second")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    let report = fixture.effect_translation_validation();
    let find_write = |pairs: &[Value]| {
        pairs
            .iter()
            .flat_map(|row| row["cells"].as_array().unwrap())
            .find(|cell| {
                cell["cell"]["kind"] == "global_prop"
                    && cell["cell"]["key"] == "marker"
                    && cell["first_write"]["global_may_access_count"] == 24
            })
            .unwrap()["first_write"]
            .clone()
    };
    let full = find_write(&rows);
    let sampled = find_write(examples(&report));
    assert_eq!(report["global_access_example_limit"], 20);
    assert_eq!(full["global_may_accesses"].as_array().unwrap().len(), 24);
    assert!(full["global_may_accesses_truncated"].is_null());
    assert_eq!(sampled["global_may_access_count"], 24);
    assert_eq!(sampled["global_may_accesses"].as_array().unwrap().len(), 20);
    assert_eq!(sampled["global_may_accesses_truncated"], true);
    assert_eq!(
        sampled["global_may_accesses"],
        serde_json::json!(&full["global_may_accesses"].as_array().unwrap()[..20])
    );
}

fn examples(report: &Value) -> &[Value] {
    report["examples"]
        .as_array()
        .expect("effect conflict examples")
}

#[test]
fn a_pair_premise_does_not_hide_another_reader() {
    // JSON.stringify does not execute these function elements. Its opaque
    // fallback nevertheless contributes peek's read, so an author pair
    // premise is legitimate and the other possible read must remain visible.
    let padding = vec!["() => 3"; 64].join(", ");
    let source = format!(
        "let state = 0;\nfunction peek() {{ return state; }}\nconst first = (state = 1, 3);\nconst second = JSON.stringify([peek, {padding}]);\nconst third = JSON.stringify([peek, {padding}]);\nconst tail = second.length + third.length;\nconsole.log(first, tail, state);\nexport {{ state, peek, first, second, third, tail }};\n"
    );
    // In the rejected cyclic order, a_writer is the first entry import;
    // its reader dependencies therefore evaluate before its own body.
    let fixture = run_rejection_fixture(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "a_writer",
                    &[
                        Member::new("state"),
                        Member::new("peek"),
                        Member::new("first"),
                        Member::new("tail"),
                    ],
                ),
                logical_module("b_reader", &[Member::new("second")]),
                logical_module("c_reader", &[Member::new("third")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements(
            "static/app",
            vec![commuting_pair(
                member_source_unit("a_writer", "first"),
                member_source_unit("c_reader", "third"),
            )],
        ),
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["report_only"], true, "{report:#}");
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    assert_eq!(
        report["examples_truncated"], false,
        "the control must expose every pair: {report:#}"
    );
    for (reader, disposition) in [(3, "uncovered"), (4, "covered_by_existing_premise")] {
        let pair = examples(&report)
            .iter()
            .find(|pair| pair["runs_first"]["unit"] == reader && pair["runs_later"]["unit"] == 2)
            .unwrap_or_else(|| panic!("both readers must run before the writer: {report:#}"));
        assert_eq!(pair["disposition"], disposition, "{pair:#}");
        assert!(pair["cells"].as_array().unwrap().iter().any(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "state"), "{pair:#}");
    }
}

#[test]
fn hoisted_functions_have_no_evaluation_write_but_function_expressions_do() {
    let fixture = run_fixture(FixtureOpts::new(
        "function hoisted() { return 1; }\nconst seen = hoisted();\nconst expr = () => 2;\nconsole.log(seen, expr());\nexport { hoisted, seen, expr };\n",
        vec![
            logical_module("functions", &[Member::new("hoisted"), Member::new("expr")]),
            logical_module("reader", &[Member::new("seen")]),
        ],
    ));
    assert_preserved_entry_output(&fixture, "1 2\n");
    assert!(
        !fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl")
            .exists()
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["full_semantic_precision"], false, "{report:#}");
    assert!(
        examples(&report)
            .iter()
            .all(|pair| pair["runs_first"]["unit"] != 0 && pair["runs_later"]["unit"] != 0),
        "hoisted declaration must have no effects: {report:#}"
    );
    assert!(
        examples(&report)
            .iter()
            .all(|pair| !(pair["runs_first"]["unit"] == 2
                && pair["runs_later"]["unit"] == 1
                && pair["havoc"] == true)),
        "covered primitive invocation cannot invent a conflict between distinct binding writes: {report:#}"
    );
}

#[test]
fn complete_execution_does_not_erase_missing_global_or_host_coverage() {
    let fixture = run_fixture(FixtureOpts::new(
        "function write() { globalThis.marker = 1; }\nconst first = (write(), 1);\nconst second = globalThis.marker;\nconst tail = second;\nconsole.log(first, tail);\nexport { write, first, second, tail };\n",
        vec![logical_module(
            "all",
            &[
                Member::new("write"),
                Member::new("first"),
                Member::new("second"),
                Member::new("tail"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 1\n");
    let graph = fixture.dependency_graph();
    let first = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 1)
        .expect("caller statement");
    assert!(
        first
            .synchronous_execution
            .as_ref()
            .expect("execution summary")
            .complete,
        "control must exercise complete execution: {first:#?}"
    );
    let report = fixture.effect_translation_validation();
    assert!(
        report["coverage"]["unknown_coverage"]["global_properties"]
            .as_u64()
            .unwrap()
            > 0,
        "{report:#}"
    );
    assert!(
        report["coverage"]["unknown_coverage"]["host_effects"]
            .as_u64()
            .unwrap()
            > 0,
        "{report:#}"
    );

    // The output call is uncovered. Only a checked whole initializer may
    // remove the other statement's unknown domains, even for dead values.
    for (initializer, covered) in [
        ("1", true),
        ("null", true),
        ("true", true),
        ("1n", true),
        ("'text'", true),
        ("(1)", true),
        ("function (parameter) { throw parameter; }", true),
        ("parameter => globalThis.marker = parameter", true),
        ("(globalThis.marker = 1, 1)", false),
        ("(globalThis.marker = 1, () => 0)", false),
        ("`text`", false),
        ("({value: 1})", true),
        ("[1]", true),
        ("(0, {value: 1})", true),
        ("({value: (0, 1), negated: !0, ignored: void 0})", true),
        ("[void 0, !1, 1 === 2]", true),
        ("/pattern/", true),
        ("class {}", false),
        ("function (parameter = 1) { return parameter; }", true),
        ("async () => 0", true),
        (
            "function ({value = globalThis.defaultValue} = globalThis.input, ...rest) { return value; }",
            true,
        ),
        (
            "({value, ...rest}) => globalThis.consume(value, rest)",
            true,
        ),
        ("({[globalThis.key()]: value}) => value", true),
        ("async (value = globalThis.read()) => value", true),
        (
            "async function (value = globalThis.read()) { return value; }",
            true,
        ),
        (
            "function* ([value] = globalThis.input) { yield value; }",
            true,
        ),
        (
            "async function* (...values) { yield globalThis.consume(values); }",
            true,
        ),
    ] {
        let source = format!("const value = {initializer};\nconsole.log('control');\n");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module("all", &[Member::new("value")])],
        ));
        assert_preserved_entry_output(&fixture, "control\n");
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain],
                if covered { 1 } else { 2 },
                "{initializer}: {report:#}"
            );
        }
    }
    for (declaration, members, unknown) in [
        (
            "const value = 1, sibling = 2;",
            vec![Member::new("value"), Member::new("sibling")],
            1,
        ),
        ("export const value = 1;", vec![Member::new("value")], 1),
    ] {
        let source = format!("{declaration}\nconsole.log('control');\n");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module("all", &members)],
        ));
        assert_preserved_entry_output(&fixture, "control\n");
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], unknown,
                "{declaration}: {report:#}"
            );
        }
    }
}

#[test]
fn local_cell_read_effects_compose_without_an_allocation_result() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .expect("compound local-call initializer");
    assert!(
        result
            .synchronous_execution
            .as_ref()
            .expect("execution summary")
            .complete,
        "the missing proof concerns effect domains, not execution: {result:#?}"
    );
    let execution = serde_json::to_value(result.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "the lexical domains are certified by executed local-call receipts: {execution:#}"
    );
    assert_eq!(
        execution["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        2,
        "both source call-sites contribute to the root receipt: {execution:#}"
    );
    let seed = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "seed")
        })
        .expect("seed source unit");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
        }),
        "the observed seed read remains a graph edge: {graph:#?}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "only the output call remains uncovered: {report:#}"
        );
    }
}

#[test]
fn single_local_call_seals_real_lexical_read_and_call_receipts() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = readSeed();\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 2)
        .expect("single-call initializer");
    let execution = serde_json::to_value(result.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "one actual local call must have operation evidence rather than legacy completion authority: {execution:#}"
    );
    let receipt = &execution["binding_effect_authority"]["receipt"];
    assert_eq!(receipt["owner"], 2);
    assert_eq!(receipt["binding"], "result");
    let calls = receipt["calls"].as_array().unwrap();
    assert_eq!(calls.len(), 1, "{receipt:#}");
    assert_eq!(calls[0]["caller"]["kind"], "root_initializer");
    assert_eq!(calls[0]["caller"]["owner"], receipt["owner"]);
    assert_eq!(calls[0]["call_span"], receipt["initializer_span"]);
    assert_eq!(
        calls[0]["caller"]["initializer_span"],
        receipt["initializer_span"]
    );
    assert_eq!(calls[0]["target"]["kind"], "callable");
    assert_eq!(calls[0]["target"]["owner"], 1);
    let seed = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 0)
        .unwrap();
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
                && edge.constrains_init_order
        }),
        "the observed child read must retain its ordering edge: {graph:#?}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn inline_callee_creation_executes_with_exact_operation_receipts() {
    for (case, expression) in [
        ("arrow", "((value) => value)((() => 42)())"),
        (
            "anonymous function",
            "(function(value) { return value; })((function() { return 42; })())",
        ),
    ] {
        let source = format!("const result = {expression};\nconsole.log(result);\n");
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
        );
        assert_preserved_entry_output(&fixture, "42\n");
        assert_inline_operation_receipt(&fixture, "result", 2, case);
    }
}

#[test]
fn inline_callee_invocation_keeps_captured_reads_and_refuses_unsupported_bodies() {
    let captured = run_fixture(
        FixtureOpts::new(
            "const seed = 41;\nconst result = ((value) => value)((() => seed)());\nconsole.log(result);\n",
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&captured, "41\n");
    assert_inline_operation_receipt(&captured, "result", 2, "captured-read");
    let graph = captured.dependency_graph();
    let result = source_unit_for_binding(&graph, "result");
    let seed = source_unit_for_binding(&graph, "seed");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result
                && edge.target == seed
                && edge.binding.as_deref() == Some("seed")
                && edge.constrains_init_order
        }),
        "invoking the inline arrow must retain its captured read: {graph:#?}"
    );

    let unsupported = run_fixture(
        FixtureOpts::new(
            "const result = ((value) => value)((() => { for (let index = 0; index < 1; index++) {} return 42; })());\nconsole.log(result);\n",
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&unsupported, "42\n");
    let execution = binding_execution_report(&unsupported, "result");
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "unsupported callable syntax must keep the root from receiving local-operation authority: {execution:#}"
    );

    let named = run_fixture(
        FixtureOpts::new(
            "const result = (function identity(value) { return value; })((function constant() { return 42; })());\nconsole.log(result);\n",
            vec![],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&named, "42\n");
    let execution = binding_execution_report(&named, "result");
    assert_ne!(
        execution["binding_effect_authority"]["authority"], "operation_receipt",
        "named function self scopes remain outside operation receipts: {execution:#}"
    );
}

#[test]
fn single_aliased_call_keeps_complete_but_uncovered_fallback() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nvar result = alias();\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("later"),
                Member::new("readSeed"),
                Member::new("alias"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 4)
        .unwrap();
    let execution = serde_json::to_value(result.synchronous_execution.as_ref().unwrap()).unwrap();
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "an identifier alias may execute completely without a selected direct-declaration receipt: {execution:#}"
    );
    let seed = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 0)
        .unwrap();
    let later = graph
        .nodes
        .iter()
        .find(|node| node.statement_ordinal.0 == 1)
        .unwrap();
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
        }),
        "the actual seed read remains observed: {graph:#?}"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == later.id
                && edge.binding.as_deref() == Some("later")
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(result.id.as_str())
                    })
                })
        }),
        "the dormant closure needs graph fallback even though the alias call completed: {graph:#?}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap()
                >= 2,
            "the unreceipted result and output remain unknown: {report:#}"
        );
    }
}

#[test]
fn ordered_callback_return_retains_the_selected_call_and_read_receipts() {
    let fixture = run_fixture(FixtureOpts::new(
        "const early = 7;\nconst holder = { callback: () => early };\nfunction mutate() { holder.callback = () => later; return 0; }\nfunction take(value) { return value; }\nconst selected = take(holder.callback, mutate());\nconst result = selected();\nconst later = (result, 42);\nconsole.log(result, later, holder.callback());\n",
        vec![
            logical_module("a_record", &[Member::new("early"), Member::new("holder")]),
            logical_module("m_mutator", &[Member::new("mutate")]),
            logical_module("n_take", &[Member::new("take")]),
            logical_module(
                "z_observed",
                &[Member::new("selected"), Member::new("result")],
            ),
            logical_module("x_later", &[Member::new("later")]),
            logical_module_with_anon(
                "observe",
                &[],
                &["console.log(result, later, holder.callback());"],
            ),
        ],
    ));
    assert_preserved_entry_output(&fixture, "7 42 42\n");
    let graph = fixture.dependency_graph();
    let selected = binding_execution_report(&fixture, "selected");
    assert_eq!(
        selected["binding_effect_authority"]["authority"], "operation_receipt",
        "{selected:#}"
    );
    assert_eq!(
        selected["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        2,
        "the mutating argument and forwarding call both execute: {selected:#}"
    );
    let result = binding_execution_report(&fixture, "result");
    assert_eq!(result["complete"], true, "{result:#}");
    assert_eq!(
        result["binding_effect_authority"]["authority"], "operation_receipt",
        "{result:#}"
    );
    let receipt = &result["binding_effect_authority"]["receipt"];
    assert_eq!(receipt["binding"], "result");
    let calls = receipt["calls"].as_array().unwrap();
    assert_eq!(calls.len(), 1, "{receipt:#}");
    assert_eq!(calls[0]["caller"]["kind"], "root_initializer");
    assert_eq!(calls[0]["caller"]["owner"], receipt["owner"]);
    assert_eq!(calls[0]["call_span"], receipt["initializer_span"]);
    assert_eq!(calls[0]["target"]["kind"], "callable");
    assert_eq!(
        calls[0]["target"]["owner"], 1,
        "the selected callback belongs to holder's original definition: {receipt:#}"
    );
    let result_id = source_unit_for_binding(&graph, "result");
    let early_id = source_unit_for_binding(&graph, "early");
    let holder_id = source_unit_for_binding(&graph, "holder");
    let later_id = source_unit_for_binding(&graph, "later");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result_id
                && edge.target == early_id
                && edge.binding.as_deref() == Some("early")
                && edge.constrains_init_order
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(holder_id)
                    })
                })
        }),
        "the actual selected body read remains ordered: {graph:#?}"
    );
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.source == result_id && edge.target == later_id && edge.constrains_init_order
        }),
        "the replacement callback must stay deferred during result initialization: {graph:#?}"
    );
}

#[test]
fn default_callback_and_parameter_read_have_receipts_without_eager_returned_body_reads() {
    const SOURCE: &str = "const value = 1;\nfunction factory(callback = input => input) { callback(0); return () => value; }\nconst created = factory();\nconsole.log(created());\nexport { value, factory, created };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module("create", &[Member::new("created")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
    let graph = fixture.dependency_graph();
    let created = binding_execution_report(&fixture, "created");
    assert_eq!(created["complete"], true, "{created:#}");
    assert_eq!(
        created["binding_effect_authority"]["authority"], "operation_receipt",
        "{created:#}"
    );
    let receipt = &created["binding_effect_authority"]["receipt"];
    assert_eq!(receipt["binding"], "created");
    let calls = receipt["calls"].as_array().unwrap();
    assert_eq!(
        calls.len(),
        2,
        "the factory and actual default callback invocation both contribute: {receipt:#}"
    );
    let root_call = calls
        .iter()
        .find(|call| call["caller"]["kind"] == "root_initializer")
        .unwrap();
    assert_eq!(root_call["caller"]["owner"], receipt["owner"]);
    assert_eq!(root_call["call_span"], receipt["initializer_span"]);
    assert_eq!(root_call["target"]["owner"], 1);
    let callback_call = calls
        .iter()
        .find(|call| call["caller"]["kind"] == "callable")
        .unwrap();
    assert_eq!(callback_call["caller"], root_call["target"]);
    assert_eq!(callback_call["target"]["owner"], 1);
    assert_ne!(callback_call["target"]["key"], root_call["target"]["key"]);
    let start = SOURCE.find("callback(0)").unwrap();
    assert_eq!(
        callback_call["call_span"],
        serde_json::json!({"start": start + 1, "end": start + 1 + "callback(0)".len()})
    );
    let created_id = source_unit_for_binding(&graph, "created");
    let value_id = source_unit_for_binding(&graph, "value");
    assert!(
        !graph.edges.iter().any(|edge| {
            edge.source == created_id && edge.target == value_id && edge.constrains_init_order
        }),
        "returning the closure must not execute its module-cell read: {graph:#?}"
    );
}

#[test]
fn ordered_forwarding_does_not_borrow_provenance_from_an_equal_memo_argument() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nfunction forward(value) { return value; }\nconst selected = (forward(readSeed), forward(alias));\nvar result = selected();\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("later"),
                Member::new("readSeed"),
                Member::new("alias"),
                Member::new("forward"),
                Member::new("selected"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let selected = binding_execution_report(&fixture, "selected");
    assert_eq!(selected["complete"], true, "{selected:#}");
    assert_eq!(
        selected["binding_effect_authority"]["authority"], "operation_receipt",
        "{selected:#}"
    );
    assert_eq!(
        selected["binding_effect_authority"]["receipt"]["calls"]
            .as_array()
            .unwrap()
            .len(),
        2,
        "both forwards execute with the same callable value but different selection evidence: {selected:#}"
    );
    let result = binding_execution_report(&fixture, "result");
    assert_eq!(result["complete"], true, "{result:#}");
    assert_eq!(
        result["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "an actual forwarding return cannot invent the missing second argument's origin: {result:#}"
    );
    let result_id = source_unit_for_binding(&graph, "result");
    let seed_id = source_unit_for_binding(&graph, "seed");
    let later_id = source_unit_for_binding(&graph, "later");
    assert!(
        graph.edges.iter().any(|edge| edge.source == result_id
            && edge.target == seed_id
            && edge.binding.as_deref() == Some("seed")),
        "the observed seed read remains: {graph:#?}"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result_id
                && edge.target == later_id
                && edge.binding.as_deref() == Some("later")
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(result_id)
                    })
                })
        }),
        "the unproved selected value retains its assumed closure fallback: {graph:#?}"
    );
}

#[test]
fn default_parameter_reads_and_writes_keep_later_parameters_in_tdz() {
    for default in ["later", "(later = 1)"] {
        let source = format!(
            "function factory(first = {default}, later = 2) {{ return first; }}\nconst result = factory();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[Member::new("factory"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_error(&fixture, "ReferenceError");
        let result = binding_execution_report(&fixture, "result");
        assert_eq!(
            result["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
            "an abrupt default cannot acquire normal root authority: {default} / {result:#}"
        );
    }
}

#[test]
fn complete_aliased_local_calls_without_a_receipt_keep_fallback_authority() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nconst later = 99;\nfunction readSeed() { return seed; }\nconst alias = { run: readSeed, dormant: () => later }.run;\nvar result = (alias(), alias());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("later"),
                Member::new("readSeed"),
                Member::new("alias"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41\n");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("aliased local-call root");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("root execution report"),
    )
    .unwrap();
    assert_eq!(execution["complete"], true, "{execution:#}");
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "the alias executes but has no selected direct-call receipt, so complete cannot skip fallback: {execution:#}"
    );
    let later = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "later")
        })
        .expect("deferred closure's later binding source unit");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == later.id
                && edge.binding.as_deref() == Some("later")
                && edge.role.as_ref().is_some_and(|role| {
                    serde_json::to_value(role).is_ok_and(|role| {
                        role["kind"] == "promoted_at_init"
                            && role["callee_source_unit"].as_str() == Some(result.id.as_str())
                    })
                })
        }),
        "the dead deferred closure contributes a fallback-only later dependency; the caller role identifies unresolved-call closure promotion: {graph:#?}"
    );
    let seed = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "seed")
        })
        .expect("seed source unit");
    assert!(
        graph.edges.iter().any(|edge| {
            edge.source == result.id
                && edge.target == seed.id
                && edge.binding.as_deref() == Some("seed")
        }),
        "known seed access stays in the graph when the receipt is absent: {graph:#?}"
    );
}

#[test]
fn local_call_receipts_retain_conditional_prefix_dependencies_when_reordered() {
    const SOURCE: &str = "const native = new Map();\nconst seed = 41;\nfunction readSeed() { return seed; }\nconst marker = 1;\nvar result = (readSeed(), readSeed());\nconsole.log(result, marker, native.size);\n";
    let input = native_constructor_input(
        SOURCE,
        "const native = new Map();",
        "native",
        NativeOperationKind::MapEmpty,
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[
                    Member::new("native"),
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![input]),
    );
    assert_preserved_entry_output(&fixture, "41 1 0\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["swapped_pairs"], 1,
        "only result crosses marker; the native owner stays first: {report:#}"
    );
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["relocation_dependencies"]["native_operation_inputs"],
        serde_json::json!([{ "defining_chunk": "static/app", "input_index": 0 }]),
        "the reordered result needs the established prefix conditions, including memoized child use: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn local_call_receipts_retain_consumed_host_prefix_prerequisites_when_reordered() {
    // A static import links before evaluation even at this later source
    // position; keeping its ordinal later isolates result crossing marker.
    const SOURCE: &str = "const native = new Map();\nconst gate = Promise.resolve();\nconst seed = 41;\nfunction readSeed() { return seed; }\nconst marker = 1;\nvar result = (readSeed(), readSeed());\nimport { reflect } from './reflect.js';\nconsole.log(result, marker, Boolean(gate), native.size);\n";
    const REFLECT_SOURCE: &str = "export function reflect(receiver, key) { Object.defineProperty(Object.getPrototypeOf(receiver), key, { value: 'changed', configurable: true }); }\n";
    let map_input = native_constructor_input(
        SOURCE,
        "const native = new Map();",
        "native",
        NativeOperationKind::MapEmpty,
    );
    let promise_input = NativeOperationInputContract {
        selector: declared_source_unit("const gate = Promise.resolve();", "gate"),
        operation: NativeOperationKind::PromiseResolve,
        audited_source_sha256: Sha256::digest(SOURCE.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect(),
        note: Some("Neutral fixture audits this exact empty Promise.resolve call.".to_string()),
    };
    let fixture = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[
                    Member::new("native"),
                    Member::new("gate"),
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_extra_chunks(&[("static/reflect", REFLECT_SOURCE)])
        .with_extra_files(&[("static/reflect.js", REFLECT_SOURCE)])
        .with_native_operation_inputs("static/app", vec![map_input, promise_input])
        .with_host_behavior(HostBehavior {
            reentry_free_operations: [ReentryFreeOperation::PromiseResolve].into_iter().collect(),
            ..HostBehavior::default()
        }),
    );
    assert_preserved_entry_output_with_bun(&fixture, "41 1 true 0\n");
    let graph = fixture.dependency_graph();
    let gate = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "gate")
        })
        .expect("selected host initializer");
    assert!(!gate.purity.is_pure(), "host timing does not grant purity");
    let execution = serde_json::to_value(
        gate.synchronous_execution
            .as_ref()
            .expect("host execution report"),
    )
    .unwrap();
    let native_uses = execution["native_operation_input_uses"]
        .as_array()
        .expect("actual selected native input use");
    assert_eq!(native_uses.len(), 1, "{execution:#}");
    assert_eq!(
        native_uses[0]["evidence"]["operation"], "promise_resolve",
        "{execution:#}"
    );
    assert_eq!(
        native_uses[0]["evidence"]["selected_binding"], "gate",
        "{execution:#}"
    );
    assert_eq!(
        native_uses[0]["evidence"]["reference"]["input_index"], 1,
        "{execution:#}"
    );
    assert!(
        execution["native_integrity_checks"]
            .as_array()
            .is_some_and(|checks| checks.iter().any(|check| {
                check["guard"] == "host_promise_resolve"
                    && check["affected_roots"]
                        .as_array()
                        .is_some_and(|roots| roots.iter().any(|root| root == "global_object"))
            })),
        "the reflective source must make same-site native identity necessary: {execution:#}"
    );
    let host_uses = execution["author_host_contract_uses"]
        .as_array()
        .expect("actual host contract use");
    assert_eq!(host_uses.len(), 1, "{execution:#}");
    assert_eq!(
        host_uses[0]["operation"], "promise_resolve",
        "{execution:#}"
    );
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("local-call result");
    assert!(
        result
            .synchronous_execution
            .as_ref()
            .expect("result execution")
            .complete
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["swapped_pairs"], 1,
        "only result crosses marker; both native/host owners stay earlier: {report:#}"
    );
    assert_eq!(
        report["decision"]["status"], "accepted",
        "the result must already have positive receipts: {report:#}"
    );
    let native_prerequisites = report["relocation_dependencies"]["native_operation_inputs"]
        .as_array()
        .expect("known Map prerequisite proves nonempty result relocation evidence");
    assert!(
        native_prerequisites
            .iter()
            .any(|input| input["defining_chunk"] == "static/app" && input["input_index"] == 0),
        "{report:#}"
    );
    assert_eq!(
        report["relocation_dependencies"],
        serde_json::json!({
            "native_operation_inputs": [
                { "defining_chunk": "static/app", "input_index": 0 },
                { "defining_chunk": "static/app", "input_index": 1 },
            ],
            "host_behavior": [{ "reentry_free_operation": "promise_resolve" }],
        }),
        "the result's positive/memoized call receipts must retain the actually consumed host and same-site native prerequisites: {report:#}"
    );
}

#[test]
fn later_rebinding_does_not_erase_an_earlier_local_call_receipt() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nreadSeed = () => 0;\nconsole.log(result, readSeed());\n",
        vec![logical_module_with_anon(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
            &["readSeed = () => 0;"],
        )],
    ));
    assert_preserved_entry_output(&fixture, "41 0\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "only later rebinding and output remain uncovered: {report:#}"
        );
    }
}

#[test]
fn local_declaration_operations_cover_private_cells_and_compose_receipts() {
    for body in [
        "const local = seed; return local;",
        "let local = seed; return local;",
        "const first = seed, second = first; return second;",
        "let local; return (local, seed);",
        "return seed; let unreached;",
    ] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ {body} }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(execution["complete"], true, "{body}: {execution:#}");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "actual declaration creation/initialization and reads compose: {body}: {execution:#}"
        );
        let graph = fixture.dependency_graph();
        let result = source_unit_for_binding(&graph, "result");
        let seed = source_unit_for_binding(&graph, "seed");
        assert!(
            graph.edges.iter().any(|edge| {
                edge.source == result
                    && edge.target == seed
                    && edge.binding.as_deref() == Some("seed")
                    && edge.constrains_init_order
            }),
            "the initializer's module read remains ordered: {body}"
        );
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 1,
                "the private cell has no nonlexical effects; only output remains unknown: {body}: {report:#}"
            );
        }
    }
}

#[test]
fn local_declaration_receipts_preserve_callable_selection_requirements() {
    for (selection, authority) in [
        ("readSeed", "operation_receipt"),
        ("alias", "ordered_root_initializer_uncovered"),
    ] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ return seed; }}\nconst alias = {{run: readSeed}}.run;\nfunction invoke() {{ const selected = {selection}; return selected(); }}\nvar result = invoke();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("alias"),
                    Member::new("invoke"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"], authority,
            "a local declaration transports actual selection but cannot mint it from an equal value: {selection}: {execution:#}"
        );
    }
}

#[test]
fn local_declaration_receipts_refuse_captured_cells_and_rebinding() {
    for body in [
        "var local = seed; return local;",
        "let local = seed; local = seed; return local;",
        "const local = seed; const read = () => local; return read();",
    ] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ {body} }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "other cell kinds and assignment effects need their own receipts: {body}: {execution:#}"
        );
    }
}

#[test]
fn abrupt_local_declarations_cannot_seal_binding_effects() {
    for (body, error) in [
        (
            "const first = later; let later = seed; return first;",
            "ReferenceError",
        ),
        ("const local = local; return local;", "ReferenceError"),
        ("const local = throwSeed(); return local;", "41"),
        (
            "const local = seed; local = seed; return local;",
            "TypeError",
        ),
    ] {
        let source = format!(
            "const seed = 41;\nfunction throwSeed() {{ throw seed; }}\nfunction readSeed() {{ {body} }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("throwSeed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_error(&fixture, error);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "TDZ, abrupt initialization and immutable stores cannot seal a normal root: {body}: {execution:#}"
        );
    }
}

#[test]
fn noncoercing_scalar_results_keep_local_call_receipts() {
    for (seed, expression, expected) in [
        ("41", "void seed", "undefined\n"),
        ("41", "!seed", "false\n"),
        ("41", "seed === seed", "true\n"),
        ("41", "seed !== seed", "false\n"),
        (
            "{ valueOf() { throw Error('coerced'); } }",
            "!seed",
            "false\n",
        ),
        (
            "{ valueOf() { throw Error('coerced'); } }",
            "seed === seed",
            "true\n",
        ),
    ] {
        let source = format!(
            "const seed = {seed};\nfunction readSeed() {{ return {expression}; }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, expression);
    }
}

#[test]
fn scalar_call_receipt_accepts_independent_statement_inversion() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return !seed; }\nconst independent = 7;\nvar result = readSeed();\nconsole.log(independent, result);\n",
        vec![logical_module(
            "scalar",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 false\n");
    assert_inline_operation_receipt(&fixture, "result", 1, "scalar inversion");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{report:#}"
    );
}

#[test]
fn numeric_local_updates_keep_receipts_across_an_independent_inversion() {
    for (body, expected) in [
        ("let value = 1; return value++;", "7 1\n"),
        ("let value = 1; return ++value;", "7 2\n"),
        ("let value = 2; return value--;", "7 2\n"),
        ("let value = 2; return --value;", "7 1\n"),
        ("let value = '4'; return ++value;", "7 5\n"),
        ("let value = 1; value++; return value;", "7 2\n"),
    ] {
        let source = format!(
            "function bump() {{ {body} }}\nconst independent = 7;\nvar result = bump();\nconsole.log(independent, result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "update",
                &[Member::new("bump"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_inline_operation_receipt(&fixture, "result", 1, body);
        let report = fixture.effect_translation_validation();
        assert_eq!(report["swapped_pairs"], 1, "{body}: {report:#}");
        assert_eq!(
            report["decision"]["status"], "accepted",
            "{body}: {report:#}"
        );
        assert_eq!(
            report["decision"]["scope"], "statement_order_commutation",
            "{body}: {report:#}"
        );
    }
}

#[test]
fn numeric_parameter_update_store_keeps_receipt_across_an_independent_inversion() {
    let fixture = run_fixture(FixtureOpts::new(
        "function bump(value) { value++; return value; }\nfunction invoke() { return bump(1); }\nconst independent = 7;\nvar result = invoke();\nconsole.log(independent, result);\n",
        vec![logical_module(
            "update",
            &[
                Member::new("bump"),
                Member::new("invoke"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 2\n");
    assert_inline_operation_receipt(&fixture, "result", 2, "parameter update");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{report:#}"
    );
}

#[test]
fn numeric_local_updates_keep_const_tdz_and_symbol_failures_abrupt() {
    for (body, error) in [
        ("const value = 1; return value++;", "TypeError"),
        ("return value++; let value = 1;", "ReferenceError"),
        ("let value = Symbol(); return value++;", "TypeError"),
    ] {
        let source =
            format!("function bump() {{ {body} }}\nvar result = bump();\nconsole.log(result);\n");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[Member::new("bump"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_error(&fixture, error);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{body}: {execution:#}"
        );
    }
}

#[test]
fn numeric_local_updates_refuse_bigint_coercion_and_captured_cells() {
    for (body, expected) in [
        ("let value = 1n; return value++;", "1n 0\n"),
        (
            "let value = { valueOf() { return ++calls; } }; return value++;",
            "1 1\n",
        ),
        (
            "let value = 1; const read = () => value; value++; return read();",
            "2 0\n",
        ),
    ] {
        let source = format!(
            "let calls = 0;\nfunction bump() {{ {body} }}\nvar result = bump();\nconsole.log(result, calls);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("calls"),
                    Member::new("bump"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{body}: {execution:#}"
        );
    }
}

#[test]
fn noncoercing_scalar_operators_do_not_cover_abrupt_children() {
    for expression in ["void later", "!later", "later === later"] {
        let source = format!(
            "const seed = 41;\nfunction readSeed() {{ return {expression}; let later = seed; }}\nvar result = readSeed();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_error(&fixture, "ReferenceError");
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{expression}: {execution:#}"
        );
    }
}

#[test]
fn noncoercing_scalar_operators_refuse_unproved_getters() {
    for (expression, expected) in [
        ("void seed.value", "undefined 1\n"),
        ("!seed.value", "false 1\n"),
        ("seed.value === seed.value", "false 2\n"),
    ] {
        let source = format!(
            "let counter = 0;\nconst seed = {{ get value() {{ return ++counter; }} }};\nfunction readSeed() {{ return {expression}; }}\nvar result = readSeed();\nconsole.log(result, counter);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("counter"),
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        let execution = binding_execution_report(&fixture, "result");
        assert_eq!(
            execution["binding_effect_authority"]["authority"],
            "ordered_root_initializer_uncovered",
            "{expression}: {execution:#}"
        );
    }
}

#[test]
fn local_call_effect_receipts_refuse_unmodeled_operations_and_prologues() {
    for (seed, helper, invocation, lexical_authority, unknown) in [
        (
            "41",
            "function readSeed(unused) { return seed; }",
            "readSeed()",
            "operation_receipt",
            1,
        ),
        (
            "41",
            "function readSeed(unused = seed) { return seed; }",
            "readSeed()",
            "operation_receipt",
            1,
        ),
        (
            "41",
            "function readSeed(...unused) { return seed; }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed() { void arguments; return seed; }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed(unused) { return seed; }",
            "readSeed(seed)",
            "operation_receipt",
            1,
        ),
        (
            "41",
            "function readSeed() { const local = seed; return local; }",
            "readSeed()",
            "operation_receipt",
            1,
        ),
        (
            "({value: 41})",
            "function readSeed() { return seed.value.toString(); }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed() { return Math.abs(-41); }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
        (
            "41",
            "function readSeed() { return globalThis.Number(41); }",
            "readSeed()",
            "ordered_root_initializer_uncovered",
            2,
        ),
    ] {
        let source = format!(
            "const seed = {seed};\n{helper}\nvar result = ({invocation}, {invocation});\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("readSeed"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, "41\n");
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], unknown,
                "only proved private-cell operations can close the initializer; output remains unknown for {helper} / {invocation}: {report:#}"
            );
        }
        let graph = fixture.dependency_graph();
        let result = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|binding| binding.binding == "result")
            })
            .expect("result owner for refused local-call receipt");
        let execution = serde_json::to_value(
            result
                .synchronous_execution
                .as_ref()
                .expect("execution report"),
        )
        .unwrap();
        assert_eq!(
            execution["binding_effect_authority"]["authority"], lexical_authority,
            "only modeled lexical operations and prologues supply binding receipts: {helper} / {invocation}: {execution:#}"
        );
    }
}

#[test]
fn imported_cell_reads_do_not_receive_local_module_cell_receipts() {
    let fixture = run_fixture(FixtureOpts::new(
        "import { sep as seed } from 'node:path';\nfunction readSeed() { return seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[Member::new("readSeed"), Member::new("result")],
        )],
    ));
    assert_preserved_entry_output(&fixture, "/\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap()
                >= 2,
            "{report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "{report:#}"
    );
}

#[test]
fn deferred_creation_and_full_scope_facts_do_not_invent_effect_receipts() {
    for (helper, result_kind, unknown) in [
        ("function make() { return [seed]; }", "object", 2),
        ("function make() { return [, seed]; }", "object", 2),
        ("function make() { return { value: seed }; }", "object", 2),
        (
            "function make() { return { read() { return seed; }, get value() { return seed; }, set value(next) {} }; }",
            "object",
            2,
        ),
        ("function make() { return () => seed; }", "function", 1),
        (
            "const make = function self() { return seed; };",
            "number",
            2,
        ),
        (
            "function make() { return seed; var { unused } = {}; }",
            "number",
            2,
        ),
        ("function make() { return seed; let unused; }", "number", 1),
        (
            "function make() { return seed; function unused() {} }",
            "number",
            2,
        ),
        (
            "function make() { return seed; class Unused {} }",
            "number",
            1,
        ),
        ("function make() { return seed; }", "number", 1),
    ] {
        let source = format!(
            "const seed = 41;\n{helper}\nvar result = (make(), make());\nconsole.log(typeof result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("make"),
                    Member::new("result"),
                ],
            )],
        ));
        assert_preserved_entry_output(&fixture, &format!("{result_kind}\n"));
        if matches!(
            helper,
            "function make() { return seed; let unused; }"
                | "function make() { return seed; class Unused {} }"
        ) {
            let execution = binding_execution_report(&fixture, "result");
            assert_eq!(
                execution["binding_effect_authority"]["authority"], "operation_receipt",
                "an unreached lexical declaration still creates a private cell without exterior effects: {execution:#}"
            );
        }
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], unknown,
                "only actual operation receipts may cover the initializer for {helper}: {report:#}"
            );
        }
    }
}

#[test]
fn refused_call_receipts_preserve_actual_argument_evaluation() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { return seed; }\nfunction actual() { console.log('argument'); return seed; }\nvar result = (readSeed(actual()), readSeed(actual()));\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("actual"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "argument\nargument\n41\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "{report:#}"
        );
    }
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result owner");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .unwrap();
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "actual call arguments remain observed without sealing unsupported nested effects: {execution:#}"
    );
}

#[test]
fn abrupt_local_call_receipt_cannot_complete_root_initialization() {
    let fixture = run_fixture(FixtureOpts::new(
        "const seed = 41;\nfunction readSeed() { throw seed; }\nvar result = (readSeed(), readSeed());\nconsole.log(result);\n",
        vec![logical_module(
            "all",
            &[
                Member::new("seed"),
                Member::new("readSeed"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_error(&fixture, "41");
    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result owner");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("execution report"),
    )
    .unwrap();
    assert_eq!(
        execution["binding_effect_authority"]["authority"], "ordered_root_initializer_uncovered",
        "an abrupt operation never seals lexical coverage: {execution:#}"
    );
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "{report:#}"
        );
    }
}

#[test]
fn composed_initializer_coverage_retains_discarded_reads_and_child_results() {
    for prefix in ["", "export "] {
        let source = format!(
            "{prefix}const first = 1;\n\
{prefix}const value = (first, {{value: !first, ignored: void first, equal: first === first, items: [first]}});\n\
console.log(JSON.stringify(value));\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "values",
                &[Member::new("first"), Member::new("value")],
            )],
        ));
        assert_preserved_entry_output(&fixture, "{\"value\":false,\"equal\":true,\"items\":[1]}\n");
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 1,
                "only the output call remains uncovered: {source}: {report:#}"
            );
        }
    }
}

#[test]
fn exported_split_declarators_keep_composed_coverage_local() {
    let fixture = run_fixture(FixtureOpts::new(
        "export const first = 1, value = (first, {value: first}), unsupported = (``, 2);\n\
console.log(value.value, unsupported);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("first"),
                Member::new("value"),
                Member::new("unsupported"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1 2\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["coverage"]["statement_count"], 4, "{report:#}");
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "{report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the unsupported sibling cannot borrow another declarator's coverage: {report:#}"
    );
}

#[test]
fn repeated_nested_calls_report_each_flat_operation_edge_once() {
    let source = "function leaf() { return 1; }\n\
function branch() { return (leaf(), leaf()); }\n\
function root() { return (branch(), branch()); }\n\
var result = (root(), root());\n\
console.log(result);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[
                Member::new("leaf"),
                Member::new("branch"),
                Member::new("root"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "1\n");

    let graph = fixture.dependency_graph();
    let result = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("compound initializer source unit");
    let execution = serde_json::to_value(
        result
            .synchronous_execution
            .as_ref()
            .expect("initializer execution summary"),
    )
    .expect("execution report serializes");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(authority["authority"], "operation_receipt", "{execution:#}");
    let calls = authority["receipt"]["calls"]
        .as_array()
        .expect("operation receipt call edges");
    assert_eq!(
        calls.len(),
        6,
        "two initializer-to-root, two root-to-branch, and two branch-to-leaf edges: {execution:#}"
    );
    assert_eq!(
        calls
            .iter()
            .filter(|call| call["caller"]["kind"] == "root_initializer")
            .count(),
        2,
        "both initializer call sites remain represented: {execution:#}"
    );
    let unique_edges: std::collections::BTreeSet<_> = calls
        .iter()
        .map(|call| {
            serde_json::to_string(&(&call["caller"], &call["call_span"], &call["target"]))
                .expect("edge identity serializes")
        })
        .collect();
    assert_eq!(
        unique_edges.len(),
        calls.len(),
        "the public report contains one row per exact caller/call-site/target edge: {execution:#}"
    );
}

#[test]
fn discarded_uninitialized_reads_cannot_complete_initializer_coverage() {
    for (kind, expected_error) in [("const", true), ("var", false)] {
        let source = format!(
            "const value = (later, {{value: 1}});\n{kind} later = 2;\nconsole.log(value.value);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "values",
                &[Member::new("value"), Member::new("later")],
            )],
        ));
        if expected_error {
            assert_preserved_entry_error(&fixture, "ReferenceError");
        } else {
            assert_preserved_entry_output(&fixture, "1\n");
        }
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 2,
                "the discarded uninitialized read and output remain uncovered: {report:#}"
            );
        }
        assert_eq!(
            report["coverage"]["owner_coverage_refusals"]["initializer_result_unproved"], 1,
            "the final literal must not erase the discarded read: {report:#}"
        );
    }
}

#[test]
fn function_creation_with_lazy_parameters_can_cross_an_independent_initializer() {
    let source = "const marker = 1;\n\
const render = ({value = globalThis.readDefault(), ...props} = globalThis.input, ...tail) => {\n\
  globalThis.consume(value, props, tail);\n\
};\n\
console.log(marker);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("render", &[Member::new("render")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1\n");
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
    assert_eq!(report["decision"]["status"], "accepted", "{report:#}");
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "the verdict covers the exact closed candidate inversion: {report:#}"
    );
    assert_eq!(report["relocation_dependencies"], serde_json::json!({}));
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn no_swap_and_closed_empty_crossing_have_explicit_effect_decisions() {
    let no_swap = run_fixture(FixtureOpts::new(
        "const written = (globalThis.marker = 1);\n\
const read = globalThis.marker;\n\
console.log(written, read);\n",
        vec![logical_module(
            "all",
            &[Member::new("written"), Member::new("read")],
        )],
    ));
    let no_swap_report = no_swap.effect_translation_validation();
    assert_eq!(no_swap_report["swapped_pairs"], 0, "{no_swap_report:#}");
    assert_eq!(no_swap_report["decision"]["status"], "accepted");
    assert_eq!(no_swap_report["decision"]["scope"], "no_reordering");
    assert_eq!(
        no_swap_report["relocation_dependencies"],
        serde_json::json!({})
    );

    let empty_crossing = run_fixture(FixtureOpts::new(
        "console.log(globalThis.marker);\nfunction inert() {}\n",
        vec![logical_module("inert", &[Member::new("inert")])],
    ));
    assert_preserved_entry_output(&empty_crossing, "undefined\n");
    let empty_report = empty_crossing.effect_translation_validation();
    assert_eq!(empty_report["swapped_pairs"], 1, "{empty_report:#}");
    assert_eq!(empty_report["counts"]["pairs"], 0, "{empty_report:#}");
    assert_eq!(
        empty_report["decision"]["status"], "accepted",
        "{empty_report:#}"
    );
    assert_eq!(
        empty_report["decision"]["scope"], "statement_order_commutation",
        "the no-evaluation function declaration crosses an open console call: {empty_report:#}"
    );
    assert_eq!(
        empty_report["relocation_dependencies"],
        serde_json::json!({})
    );
}

#[test]
fn uncovered_modeled_and_havoc_pair_is_a_conflict() {
    let source = "const written = (globalThis.marker = 1);\n\
const read = globalThis.marker;\n\
console.log(written, read);\n";
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("reader", &[Member::new("read")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["decision"]["status"], "conflict", "{report:#}");
    assert_eq!(
        report["decision"]["uncovered_modeled_may_conflict"], true,
        "{report:#}"
    );
    assert_eq!(report["decision"]["havoc"], true, "{report:#}");
    assert_eq!(report["decision"]["uncovered_havoc"], true, "{report:#}");
    let rows = read_jsonl(&fixture.report_path("effect_conflicts.jsonl"));
    assert_full_rows_match_summary(&report, &rows);
}

#[test]
fn regexp_literal_creation_covers_only_its_exact_owner() {
    let source = "const marker = 1;\n\
const RegExp = () => { throw new Error('literal invoked RegExp'); };\n\
const pattern = /^x$/;\n\
const matched = pattern.test('x');\n\
console.log(matched, marker);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "regex",
                &[Member::new("RegExp"), Member::new("pattern")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "true 1\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    assert!(
        entry.find("import ").unwrap() < entry.find("const marker = 1").unwrap(),
        "the regex module import must run before the residual marker:\n{entry}"
    );
    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 2, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the later pattern.test call remains outside creation coverage: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the later call and console owner remain uncovered: {report:#}"
        );
    }
}

#[test]
fn regexp_literal_binding_read_write_conflict_remains_visible() {
    let source = "const observed = pattern;\n\
var pattern = /^x$/;\n\
console.log(observed === observed, pattern.test('x'));\n";
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("regex", &[Member::new("pattern")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    assert!(
        fixture.stderr.contains("atomic-group conflict"),
        "{}",
        fixture.stderr
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 1, "{report:#}");
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    let creation_pair = rows
        .iter()
        .find(|pair| pair["runs_first"]["unit"] == 1 && pair["runs_later"]["unit"] == 0)
        .unwrap_or_else(|| panic!("the binding conflict must remain visible: {rows:#?}"));
    assert_eq!(creation_pair["havoc"], true, "{creation_pair:#}");
    assert_eq!(
        creation_pair["first_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the RegExp creation endpoint itself remains covered: {creation_pair:#}"
    );
    assert_ne!(
        creation_pair["later_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the read-side refusal keeps its own Havoc: {creation_pair:#}"
    );
    let binding_cell = creation_pair["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "pattern")
        .unwrap_or_else(|| panic!("the creation binding write remains visible: {creation_pair:#}"));
    assert_eq!(
        binding_cell["first_write"]["kind"], "observed",
        "{binding_cell:#}"
    );
    assert_eq!(
        binding_cell["later_read"]["kind"], "observed",
        "{binding_cell:#}"
    );
    assert_eq!(binding_cell["observed_conflict"], true, "{binding_cell:#}");
}

#[test]
fn regexp_literal_creation_does_not_cover_later_prototype_getters() {
    let source = "const marker = 1;\n\
const pattern = /^x$/;\n\
Object.defineProperty(RegExp.prototype, 'source', {\n\
  configurable: true,\n\
  get() { globalThis.regexGetterReads = (globalThis.regexGetterReads || 0) + 1; return 'overridden'; },\n\
});\n\
Object.defineProperty(pattern, 'custom', {\n\
  configurable: true,\n\
  get() { globalThis.regexOwnGetterReads = (globalThis.regexOwnGetterReads || 0) + 1; return 'own'; },\n\
});\n\
const sourceText = pattern.source;\n\
const missingValue = pattern.notPresent;\n\
const lastIndexValue = pattern.lastIndex;\n\
const customValue = pattern.custom;\n\
console.log(sourceText, missingValue, lastIndexValue, customValue, marker, globalThis.regexGetterReads, globalThis.regexOwnGetterReads);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("regex", &[Member::new("pattern")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "overridden undefined 0 own 1 1 1\n");

    let graph = fixture.dependency_graph();
    let execution = |binding: &str| {
        let node = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .unwrap_or_else(|| panic!("missing {binding} source unit"));
        serde_json::to_value(
            node.synchronous_execution
                .as_ref()
                .unwrap_or_else(|| panic!("missing {binding} execution summary")),
        )
        .expect("serialize execution summary")
    };
    let pattern_execution = execution("pattern");
    assert!(
        pattern_execution["complete"] == true
            && pattern_execution["precision_losses"] == serde_json::json!([]),
        "RegExp literal creation alone remains a complete allocation: {pattern_execution:#?}"
    );
    for binding in [
        "sourceText",
        "missingValue",
        "lastIndexValue",
        "customValue",
    ] {
        let read_execution = execution(binding);
        assert!(
            read_execution["complete"] == false
                && read_execution["precision_losses"]
                    .as_array()
                    .is_some_and(|losses| {
                        losses.iter().any(|loss| {
                            loss["reason"]["code"] == "unsupported_operation"
                                && loss["reason"]["operation"] == "member_read"
                        })
                    }),
            "the {binding} RegExp property read stays outside literal-allocation coverage: {read_execution:#?}"
        );
    }

    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 4,
        "each RegExp property read remains outside creation coverage: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap_or_default()
                >= 1,
            "later prototype mutation, getter read, or console effects stay opaque: {report:#}"
        );
    }
}

#[test]
fn regexp_constructor_initialization_remains_opaque() {
    let source = "const marker = 1;\n\
const RegExp = function () { throw new Error('constructor invoked'); };\n\
const pattern = new RegExp('^x$');\n\
console.log(marker);\n";
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "regex",
                &[Member::new("RegExp"), Member::new("pattern")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_error(&fixture, "constructor invoked");

    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "new RegExp remains outside the literal-only proof: {report:#}"
    );
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    let constructor_pair = rows
        .iter()
        .find(|pair| pair["runs_first"]["unit"] == 2 && pair["runs_later"]["unit"] == 0)
        .unwrap_or_else(|| panic!("the constructor owner must cross the marker: {rows:#?}"));
    assert_eq!(constructor_pair["havoc"], true, "{constructor_pair:#}");
    assert_ne!(
        constructor_pair["first_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the constructor endpoint retains unknown coverage: {constructor_pair:#}"
    );
}

#[test]
fn calling_a_function_with_lazy_parameters_retains_unknown_effects() {
    let source = "const input = {get value() { globalThis.parameterReads = (globalThis.parameterReads || 0) + 1; return 7; }};\n\
const select = ({value} = input) => value;\n\
const selected = select();\n\
console.log(selected, globalThis.parameterReads);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[
                Member::new("input"),
                Member::new("select"),
                Member::new("selected"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 1\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 3,
            "{report:#}"
        );
    }
}

#[test]
fn inert_class_declaration_closes_only_its_exact_evaluation_owner() {
    let source = "const marker = 1;\n\
class WorkQueue {\n\
  running = 0;\n\
  pending = [];\n\
  constructor(limit) { this.limit = limit; }\n\
  add(job) {\n\
    return new Promise(resolve => { this.pending.push(job); resolve(); });\n\
  }\n\
}\n\
console.log(marker);\n";
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "1\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    let import = entry.find("import ").expect("class module import");
    let marker = entry
        .find("const marker = 1")
        .expect("residual marker initializer");
    assert!(
        import < marker,
        "class import must run before the residual marker:\n{entry}"
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], true, "{report:#}");
    assert_eq!(report["legacy_tv_accepts"], true, "{report:#}");
    assert_eq!(
        report["swapped_pairs"], 1,
        "the class import precedes source unit 0: {report:#}"
    );
    assert_eq!(report["coverage"]["statement_count"], 3, "{report:#}");
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_owner_kind"], 1,
        "only the console call remains outside the initializer subset: {report:#}"
    );
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
}

#[test]
fn empty_native_collections_cover_only_the_reordered_initializer_owner() {
    const SOURCE: &str = "const earlierMap = (void globalThis.Map, 1);\n\
var map = new Map();\n\
const earlierSet = (void globalThis.Set, 2);\n\
var set = new Set();\n\
const later = map.size + set.size;\n\
console.log(earlierMap, earlierSet, later);\n";
    let inputs = empty_native_collection_inputs(SOURCE, "var");
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "collections",
                &[Member::new("map"), Member::new("set")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "1 2 0\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    let import = entry.find("import ").expect("collection module import");
    let earlier = entry
        .find("const earlierMap")
        .expect("residual harmless read");
    assert!(
        import < earlier,
        "the empty collection constructors must actually move before the residual reads:\n{entry}"
    );
    for property in ["Map", "Set"] {
        assert!(
            entry.contains(&format!("globalThis.{property}")),
            "the residual GlobalProp({property}) read must remain in emitted code:\n{entry}"
        );
    }
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/collections.js",
        &["var map = new Map();", "var set = new Set();"],
        &[],
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"]["native_operation_inputs"],
        serde_json::json!([
            {"defining_chunk": "static/app", "input_index": 0},
            {"defining_chunk": "static/app", "input_index": 1},
        ]),
        "coverage of the two native owners retains their exact author premises: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 4,
            "the two direct constructors alone are covered; sequence reads, later observations, and console remain opaque: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 3,
        "the sequence and later property reads retain their own refusal: {report:#}"
    );
}

#[test]
fn empty_native_collection_owner_does_not_hide_prior_var_binding_reads() {
    const SOURCE: &str = "const earlierMap = map;\n\
var map = new Map();\n\
const earlierSet = set;\n\
var set = new Set();\n\
console.log(earlierMap === undefined, earlierSet === undefined, map.size + set.size);\n";
    let inputs = empty_native_collection_inputs(SOURCE, "var");
    let colocated = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[
                    Member::new("earlierMap"),
                    Member::new("map"),
                    Member::new("earlierSet"),
                    Member::new("set"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs.clone()),
    );
    assert_preserved_entry_output(&colocated, "true true 0\n");

    let reordered = run_rejection_fixture_with_args(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "collections",
                &[Member::new("map"), Member::new("set")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", inputs),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = reordered.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    let rows = read_jsonl(&reordered.report_path("effect_conflicts.jsonl"));
    for binding in ["map", "set"] {
        let pair = rows
            .iter()
            .find(|pair| {
                pair["cells"].as_array().unwrap().iter().any(|cell| {
                    cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == binding
                })
            })
            .unwrap_or_else(|| panic!("reordered {binding} write/read pair: {rows:#?}"));
        let cell = pair["cells"]
            .as_array()
            .unwrap()
            .iter()
            .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == binding)
            .unwrap();
        assert_eq!(cell["first_write"]["kind"], "observed", "{cell:#}");
        assert_eq!(cell["later_read"]["kind"], "observed", "{cell:#}");
        assert_eq!(cell["observed_conflict"], true, "{cell:#}");
        assert_eq!(
            pair["first_opacity"]["unknown_coverage"],
            serde_json::json!([])
        );
    }
}

#[test]
fn text_encoder_initializer_covers_only_its_exact_root_owner() {
    const SOURCE: &str = "const earlier = (void globalThis.TextEncoder, 1);\n\
var encoder = new TextEncoder();\n\
const later = encoder.encoding;\n\
console.log(earlier, later);\n";
    let input = native_constructor_input(
        SOURCE,
        "var encoder = new TextEncoder();",
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    );
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module("encoder", &[Member::new("encoder")])],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![input]),
        &["--effect-conflicts-jsonl"],
    );
    assert_preserved_entry_output(&fixture, "1 utf-8\n");

    let entry = std::fs::read_to_string(&fixture.entry_path).expect("generated entry source");
    let import = entry.find("import ").expect("TextEncoder module import");
    let earlier = entry
        .find("const earlier")
        .expect("residual constructor-global read");
    assert!(
        import < earlier,
        "the certified initializer must move before the harmless residual read:\n{entry}"
    );
    assert!(
        entry.contains("globalThis.TextEncoder"),
        "the residual global read remains in the output:\n{entry}"
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/encoder.js",
        &["var encoder = new TextEncoder();"],
        &[],
    );

    let report = fixture.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], true, "{report:#}");
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 3,
            "only the direct TextEncoder initializer is covered; the sequence read, later property read, and console call remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 2,
        "the wrapped global read and later property read retain their own refusals: {report:#}"
    );
}

#[test]
fn text_encoder_initializer_requires_identity_zero_arguments_and_root_shape() {
    for (source, reason, unknown_statements) in [
        (
            "var encoder = new TextEncoder();\n\
             globalThis.TextEncoder = function replacement() {};\n",
            "initializer_result_unproved",
            2,
        ),
        (
            "var encoder = new TextEncoder('utf-8');\n",
            "unsupported_initializer",
            1,
        ),
        (
            "var encoder = (void globalThis.marker, new TextEncoder());\n",
            "unsupported_initializer",
            1,
        ),
    ] {
        let fixture = run_fixture(FixtureOpts::new(source, vec![]));
        assert_preserved_entry_output(&fixture, "");
        let report = fixture.effect_translation_validation();
        assert_eq!(
            report["coverage"]["owner_coverage_refusals"][reason], 1,
            "unproved TextEncoder initializer: {source}: {report:#}"
        );
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], unknown_statements,
                "unproved TextEncoder initializer remains opaque: {source}: {report:#}"
            );
        }
    }

    // A transfer from the prior direct initializer must not escape through a
    // nested call or close a later transient initializer.
    let source = "var encoder = new TextEncoder();\n\
const transient = (() => new TextEncoder())();\n";
    let input = native_constructor_input(
        source,
        "var encoder = new TextEncoder();",
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    );
    let fixture = run_fixture(
        FixtureOpts::new(source, vec![]).with_native_operation_inputs("static/app", vec![input]),
    );
    assert_preserved_entry_output(&fixture, "");
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the nested/transient call keeps its own refusal: {report:#}"
    );
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "only the direct initializer is covered: {report:#}"
        );
    }
}

#[test]
fn text_encoder_owner_coverage_keeps_prior_var_binding_read_observed() {
    const SOURCE: &str = "const earlier = encoder;\n\
var encoder = new TextEncoder();\n\
console.log(earlier === undefined, encoder.encoding);\n";
    let input = native_constructor_input(
        SOURCE,
        "var encoder = new TextEncoder();",
        "encoder",
        NativeOperationKind::TextEncoderEmpty,
    );
    let opts = |members| {
        FixtureOpts::new(SOURCE, vec![logical_module("encoder", members)])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_native_operation_inputs("static/app", vec![input.clone()])
    };
    let colocated = run_fixture(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module(
                "all",
                &[Member::new("earlier"), Member::new("encoder")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_native_operation_inputs("static/app", vec![input.clone()]),
    );
    assert_preserved_entry_output(&colocated, "true utf-8\n");

    let reordered = run_rejection_fixture_with_args(
        opts(&[Member::new("encoder")]),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = reordered.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    let rows = read_jsonl(&reordered.report_path("effect_conflicts.jsonl"));
    let pair = rows
        .iter()
        .find(|pair| {
            pair["cells"].as_array().unwrap().iter().any(|cell| {
                cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "encoder"
            })
        })
        .unwrap_or_else(|| panic!("reordered encoder read/write pair: {rows:#?}"));
    let cell = pair["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "encoder")
        .unwrap();
    assert_eq!(cell["first_write"]["kind"], "observed", "{cell:#}");
    assert_eq!(cell["later_read"]["kind"], "observed", "{cell:#}");
    assert_eq!(cell["observed_conflict"], true, "{cell:#}");
}

#[test]
fn unsupported_class_shapes_remain_uncovered() {
    for declaration in [
        "class WorkQueue extends Object {}",
        "class WorkQueue { [globalThis.queueKey]() {} }",
        "class WorkQueue { get value() { return 1; } }",
        "class WorkQueue { static state = (globalThis.marker = 1); }",
        "class WorkQueue { static { globalThis.marker = 1; } }",
    ] {
        let source = format!("{declaration}\n");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
        ));
        assert_preserved_entry_output(&fixture, "");
        let report = fixture.effect_translation_validation();
        assert_eq!(
            report["coverage"]["owner_coverage_refusals"]["unsupported_owner_kind"], 1,
            "{declaration}: {report:#}"
        );
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert_eq!(
                report["coverage"]["unknown_coverage"][domain], 1,
                "{declaration}: unsupported class shape remains uncovered: {report:#}"
            );
        }
    }
}

#[test]
fn class_instance_initializers_stay_lazy_until_constructor_invocation() {
    let source = "class WorkQueue {\n\
  running = (globalThis.fieldRuns = (globalThis.fieldRuns || 0) + 1);\n\
  pending = [];\n\
  constructor(limit) { this.limit = limit; globalThis.constructed = limit; }\n\
  add(job) { this.pending.push(job); return this.pending.length; }\n\
}\n\
const queue = new WorkQueue(3);\n\
console.log(queue.running, queue.pending.length, queue.limit, globalThis.fieldRuns, globalThis.constructed);\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
    ));
    assert_preserved_entry_output(&fixture, "1 0 3 1 3\n");

    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["unknown_coverage"]["global_properties"], 2,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["general_heap"], 2,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["host_effects"], 2,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the constructor invocation owner remains uncovered: {report:#}"
    );
}

#[test]
fn class_binding_writes_still_conflict_with_a_tdz_read_when_extraction_reorders_it() {
    let source = "const earlier = WorkQueue;\nclass WorkQueue {}\n";
    let colocated = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "all",
            &[Member::new("earlier"), Member::new("WorkQueue")],
        )],
    ));
    assert_preserved_entry_error(&colocated, "ReferenceError");

    let reordered = run_rejection_fixture_with_args(
        FixtureOpts::new(
            source,
            vec![logical_module("work_queue", &[Member::new("WorkQueue")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = reordered.effect_translation_validation();
    assert_eq!(report["legacy_gate_accepts"], false, "{report:#}");
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(
        report["coverage"]["unknown_coverage"]["global_properties"], 1,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["general_heap"], 1,
        "{report:#}"
    );
    assert_eq!(
        report["coverage"]["unknown_coverage"]["host_effects"], 1,
        "{report:#}"
    );
    let rows = read_jsonl(&reordered.report_path("effect_conflicts.jsonl"));
    let pair = rows
        .iter()
        .find(|pair| {
            pair["cells"].as_array().unwrap().iter().any(|cell| {
                cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "WorkQueue"
            })
        })
        .unwrap_or_else(|| {
            panic!("the class binding write must conflict with the moved TDZ read: {rows:#?}")
        });
    assert_eq!(
        pair["runs_first"]["unit"], 1,
        "the class module is emitted first: {pair:#}"
    );
    assert_eq!(
        pair["runs_later"]["unit"], 0,
        "the residual TDZ read is emitted later: {pair:#}"
    );
    assert_eq!(
        pair["first_opacity"]["unknown_coverage"],
        serde_json::json!([]),
        "the exact class owner is covered even though the moved read is not: {pair:#}"
    );
    let class_cell = pair["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|cell| cell["cell"]["kind"] == "binding" && cell["cell"]["binding"] == "WorkQueue")
        .unwrap();
    assert_eq!(
        class_cell["first_write"]["kind"], "observed",
        "{class_cell:#}"
    );
    assert_eq!(
        class_cell["later_read"]["kind"], "observed",
        "{class_cell:#}"
    );
    assert_eq!(class_cell["observed_conflict"], true, "{class_cell:#}");
}

#[test]
fn default_report_bounds_examples_without_losing_exact_counts() {
    let names = [
        "item0", "item1", "item2", "item3", "item4", "item5", "item6", "item7", "item8", "item9",
        "item10", "item11", "item12", "item13", "item14", "item15", "item16", "item17", "item18",
        "item19", "item20", "item21", "item22", "item23",
    ];
    let mut source = String::new();
    for (index, name) in names.iter().enumerate() {
        // A static template is pure but outside the source-covered value
        // subset, preserving this control's opaque conflict inventory.
        source.push_str(&format!("const {name} = (``, {index});\n"));
    }
    source.push_str(&format!(
        "const tail = {};\nconsole.log(tail);\n",
        names.join(" + ")
    ));
    let mut modules = vec![logical_module(
        "outer",
        &[Member::new("item0"), Member::new("tail")],
    )];
    modules.extend(
        names[1..]
            .iter()
            .map(|name| logical_module(name, &[Member::new(name)])),
    );
    // Outer imports the later literals before item0 runs. Other loader
    // ordering remains emitter-owned; every resulting swap must be counted.
    let fixture = run_fixture_with_args(
        FixtureOpts::new(&source, modules),
        &["--effect-conflicts-jsonl"],
    );
    let report = fixture.effect_translation_validation();
    let count = |name| report["counts"][name].as_u64().unwrap();
    assert_eq!(
        count("pairs"),
        report["swapped_pairs"].as_u64().unwrap(),
        "every runtime statement retains Havoc: {report:#}"
    );
    assert!(count("pairs") > 20, "{report:#}");
    assert_eq!(
        count("pairs"),
        count("uncovered_pairs") + count("premise_covered_pairs") + count("premise_contradictions")
    );
    assert_eq!(
        count("pairs"),
        report["legacy_edge_comparison"]["effect_only_pairs"]
            .as_u64()
            .unwrap()
            + report["legacy_edge_comparison"]["both_pairs"]
                .as_u64()
                .unwrap()
    );
    assert_eq!(examples(&report).len(), 20);
    assert_eq!(report["examples_truncated"], true);
    assert_eq!(report["full_detail_export_available"], true);
    let rows = read_jsonl(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("effect_conflicts.jsonl"),
    );
    assert_full_rows_match_summary(&report, &rows);
}

#[test]
fn bounded_examples_prefer_modeled_rows_after_early_havoc_only_rows() {
    let names = [
        "item0", "item1", "item2", "item3", "item4", "item5", "item6", "item7", "item8", "item9",
        "item10", "item11", "item12", "item13", "item14", "item15", "item16", "item17", "item18",
        "item19", "item20", "item21", "item22", "item23",
    ];
    let mut source = String::new();
    for (index, name) in names.iter().enumerate() {
        // The template is pure but outside the source-covered value subset,
        // retaining an early Havoc-only conflict inventory.
        source.push_str(&format!("const {name} = (``, {index});\n"));
    }
    source.push_str(&format!(
        "const tail = {};\nconst writer = (globalThis.marker = 1);\nconst reader = globalThis.marker;\nconsole.log(tail, writer, reader);\n",
        names.join(" + ")
    ));
    let mut modules = vec![logical_module(
        "outer",
        &[
            Member::new("item0"),
            Member::new("tail"),
            Member::new("reader"),
        ],
    )];
    modules.extend(
        names[1..]
            .iter()
            .map(|name| logical_module(name, &[Member::new(name)])),
    );
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(&source, modules).with_unassigned_mode(unassigned_mode_inline()),
        &["--effect-conflicts-jsonl"],
        1,
    );
    let report = fixture.effect_translation_validation();
    let rows = read_jsonl(&fixture.report_path("effect_conflicts.jsonl"));
    let example_limit = report["example_limit"].as_u64().unwrap() as usize;
    let first_modeled_row = rows
        .iter()
        .position(|row| row["cell_count"].as_u64().unwrap() > 0)
        .expect("the full stream contains modeled-cell conflicts");
    assert!(
        first_modeled_row >= example_limit,
        "first modeled row {first_modeled_row} precedes example limit {example_limit}"
    );
    assert!(
        rows[..example_limit]
            .iter()
            .all(|row| row["cell_count"] == 0 && row["havoc"] == true),
        "the fixture begins with {example_limit} Havoc-only rows"
    );
    assert!(
        examples(&report)
            .iter()
            .any(|example| example["cell_count"].as_u64().unwrap() > 0),
        "the bounded report should expose a modeled-cell witness after the initial Havoc-only rows"
    );
    assert_eq!(report["examples_truncated"], true, "{report:#}");
    assert_eq!(report["decision"]["status"], "conflict");
    assert_eq!(report["legacy_gate_accepts"], false);
    assert_full_rows_match_summary(&report, &rows);
}

fn assert_full_rows_match_summary(summary: &Value, rows: &[Value]) {
    let count = |name| summary["counts"][name].as_u64().unwrap();
    assert_eq!(rows.len() as u64, count("pairs"));
    assert_eq!(
        rows.iter()
            .map(|row| row["cells"].as_array().unwrap().len() as u64)
            .sum::<u64>(),
        count("cell_incidences")
    );
    assert_eq!(
        rows.iter().filter(|row| row["havoc"] == true).count() as u64,
        count("havoc_pairs")
    );
    for (disposition, category) in [
        ("uncovered", "uncovered_pairs"),
        ("covered_by_existing_premise", "premise_covered_pairs"),
        ("contradicts_existing_premise", "premise_contradictions"),
    ] {
        assert_eq!(
            rows.iter()
                .filter(|row| row["disposition"] == disposition)
                .count() as u64,
            count(category)
        );
    }
    assert_eq!(
        rows.iter()
            .filter(|row| row["legacy_edge_conflict"] == true)
            .count() as u64,
        summary["legacy_edge_comparison"]["both_pairs"]
            .as_u64()
            .unwrap()
    );
    for row in rows {
        assert_eq!(
            row["cell_count"].as_u64().unwrap(),
            row["cells"].as_array().unwrap().len() as u64
        );
        assert!(row["first_opacity"].is_object() && row["later_opacity"].is_object());
    }
    let limit = summary["example_limit"].as_u64().unwrap() as usize;
    let mut selected_indexes = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row["cell_count"].as_u64().unwrap() > 0)
        .take(limit)
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    let fallback_slots = limit.saturating_sub(selected_indexes.len());
    selected_indexes.extend(
        rows.iter()
            .enumerate()
            .filter(|(_, row)| row["cell_count"].as_u64().unwrap() == 0)
            .take(fallback_slots)
            .map(|(index, _)| index),
    );
    selected_indexes.sort_unstable();
    assert_eq!(examples(summary).len(), selected_indexes.len());
    for (example, full) in examples(summary)
        .iter()
        .zip(selected_indexes.into_iter().map(|i| &rows[i]))
    {
        for field in [
            "runs_first",
            "runs_later",
            "havoc",
            "disposition",
            "premise_locations",
            "premise_references",
            "cell_count",
        ] {
            assert_eq!(example[field], full[field], "{field}");
        }
        let cells = full["cells"].as_array().unwrap();
        let limit = summary["cell_example_limit"].as_u64().unwrap() as usize;
        assert_eq!(
            example["cells"].as_array().unwrap().as_slice(),
            &cells[..cells.len().min(limit)]
        );
        assert_eq!(example["cells_truncated"], cells.len() > limit);
    }
}

#[test]
fn full_stream_retains_cells_beyond_examples_and_pair_dispositions() {
    let slots = [
        "state0", "state1", "state2", "state3", "state4", "state5", "state6", "state7", "state8",
        "state9", "state10", "state11", "state12", "state13", "state14", "state15", "state16",
        "state17", "state18", "state19", "state20", "state21", "state22", "state23",
    ];
    let declarations = slots
        .iter()
        .map(|slot| format!("{slot} = 0"))
        .collect::<Vec<_>>()
        .join(", ");
    let writes = slots
        .iter()
        .map(|slot| format!("{slot} = 1"))
        .collect::<Vec<_>>()
        .join(", ");
    let padding = vec!["() => 3"; 64].join(", ");
    let source = format!(
        "let {declarations};\nfunction peek() {{ return {}; }}\nconst first = ({writes}, 3);\nconst second = JSON.stringify([peek, {padding}]);\nconst third = JSON.stringify([peek, {padding}]);\nconst tail = second.length + third.length;\nconsole.log(first, tail);\nexport {{ first, second, third, tail }};\n",
        slots.join(" + ")
    );
    let fixture = run_rejection_fixture_with_args(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "a_writer",
                    &[
                        Member::new("state0"),
                        Member::new("peek"),
                        Member::new("first"),
                        Member::new("tail"),
                    ],
                ),
                logical_module("b_reader", &[Member::new("second")]),
                logical_module("c_reader", &[Member::new("third")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements(
            "static/app",
            vec![commuting_pair(
                member_source_unit("a_writer", "first"),
                member_source_unit("c_reader", "third"),
            )],
        ),
        &["--effect-conflicts-jsonl", "--dry-run"],
        1,
    );
    let summary = fixture.effect_translation_validation();
    let rows = read_jsonl(&fixture.report_path("effect_conflicts.jsonl"));
    assert_full_rows_match_summary(&summary, &rows);
    assert_eq!(summary["examples_truncated"], true);
    assert_eq!(summary["decision"]["status"], "conflict", "{summary:#}");
    let covered = rows
        .iter()
        .find(|row| row["disposition"] == "covered_by_existing_premise")
        .expect("covered writer/reader pair");
    assert_eq!(covered["disposition"], "covered_by_existing_premise");
    assert_eq!(
        covered["premise_references"],
        serde_json::json!([{"defining_chunk": "static/app", "input_index": 0}]),
        "the premise remains a typed condition on the covered pair: {covered:#}"
    );
    assert_eq!(
        summary["premise_dependencies"],
        serde_json::json!([{"defining_chunk": "static/app", "input_index": 0}]),
        "the full assessment retains the premise dependency: {summary:#}"
    );
    assert_eq!(covered["cells"].as_array().unwrap().len(), slots.len());
    assert!(
        covered["cells"]
            .as_array()
            .unwrap()
            .iter()
            .all(|cell| cell["first_read"]["kind"] == "fallback")
    );
    assert!(
        !covered["first_opacity"]["execution_losses"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(
        !covered["later_opacity"]["unknown_coverage"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn accepted_dry_run_refuses_requested_export_without_writing_it() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "const value = 1;\nexport { value };\n",
        vec![logical_module("values", &[Member::new("value")])],
    ));
    let result = run_debundle(&[
        "run",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--dry-run",
        "--effect-conflicts-jsonl",
    ]);
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("report policy suppresses output"));
    assert!(
        !fixture
            .spec_path
            .parent()
            .unwrap()
            .join("out/reports/tree/static/app/effect_conflicts.jsonl")
            .exists()
    );
}

#[test]
fn export_chunk_filter_requires_export_and_rejects_unknown_chunks() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "const value = 1;\nexport { value };\n",
        vec![logical_module("values", &[Member::new("value")])],
    ));
    for (flags, diagnostic) in [
        (
            vec!["--effect-conflicts-chunk", "static/app"],
            "--effect-conflicts-jsonl",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "static/typo",
            ],
            "unknown --effect-conflicts-chunk",
        ),
        (
            vec!["--effect-conflicts-jsonl", "--effect-conflicts-chunk", ""],
            "Expected a non-empty relative path",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "/main",
            ],
            "Expected a non-empty relative path",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "../main",
            ],
            "Invalid relative path",
        ),
        (
            vec![
                "--effect-conflicts-jsonl",
                "--effect-conflicts-chunk",
                "main/..",
            ],
            "Invalid relative path",
        ),
    ] {
        let mut args = vec!["run", "--spec", fixture.spec_path.to_str().unwrap()];
        args.extend(flags);
        let result = run_debundle(&args);
        assert!(!result.status.success());
        assert!(
            String::from_utf8_lossy(&result.stderr).contains(diagnostic),
            "{result:?}"
        );
    }
}

#[test]
fn split_simple_middle_and_later_declarators_receive_owner_coverage() {
    let fixture = run_fixture(FixtureOpts::new(
        "const first = 1, middle = 2, later = 3;\nconsole.log(first, middle, later);\n",
        vec![
            logical_module("first", &[Member::new("first")]),
            logical_module("middle", &[Member::new("middle")]),
            logical_module("later", &[Member::new("later")]),
        ],
    ));
    assert_entry_output(&fixture, "1 2 3\n");

    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "only console.log remains uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["statement_count"], 4,
        "the split declarators and trailing call remain separate source owners: {report:#}"
    );
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["split_declaration"]
            .as_u64()
            .unwrap_or_default(),
        0,
        "eligible split owners must not be refused as a whole declaration: {report:#}"
    );
}

#[test]
fn split_declarator_coverage_stays_local_and_requires_completed_initialization() {
    let effectful_sibling = run_fixture(FixtureOpts::new(
        "const first = (globalThis.marker = 1, 1), middle = 2, later = 3;\nconsole.log(first, middle, later, globalThis.marker);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("first"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&effectful_sibling, "1 2 3 1\n");
    let report = effectful_sibling.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the effectful first initializer and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the effectful sibling remains an unsupported source initializer: {report:#}"
    );

    let getter = run_fixture(FixtureOpts::new(
        "const source = { get value() { globalThis.touched = 1; return 4; } }, middle = source.value, later = 5;\nconsole.log(middle, later, globalThis.touched);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("source"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&getter, "4 5 1\n");
    let report = getter.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 3,
            "getter-bearing source, member-read initializer, and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 2,
        "the accessor-bearing source and member read each retain their own refusal: {report:#}"
    );

    let callback = run_fixture(FixtureOpts::new(
        "function invoke(callback) { callback(); return 4; }\nconst callback = () => { globalThis.touched = 7; };\nconst middle = invoke(callback), later = 5;\nconsole.log(middle, later, globalThis.touched);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("callback"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_output(&callback, "4 5 7\n");
    let report = callback.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the invoking initializer and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the call initializer remains outside the literal proof: {report:#}"
    );

    let tdz = run_fixture(FixtureOpts::new(
        "const first = later, middle = 2, later = 3;\nconsole.log(first, middle, later);\n",
        vec![logical_module(
            "values",
            &[
                Member::new("first"),
                Member::new("middle"),
                Member::new("later"),
            ],
        )],
    ));
    assert_preserved_entry_error(&tdz, "ReferenceError");
    let report = tdz.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 2,
            "the TDZ read and console.log remain uncovered: {report:#}"
        );
    }
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["uninitialized_read"], 1,
        "the first declarator's ordered read observes a TDZ cell: {report:#}"
    );
}

#[test]
fn deferred_callable_creation_coverage_composes_through_calls_and_exact_owners() {
    let fixture = run_fixture(FixtureOpts::new(
        "function factory() { return () => 3; }\nfunction nested() { return factory(); }\nconst selected = nested();\nconst result = selected();\nconsole.log(result);\n",
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "3\n");
    for (binding, owner, call_count) in [("selected", 2, 2), ("result", 3, 1)] {
        let execution = binding_execution_report(&fixture, binding);
        assert_eq!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{execution:#}"
        );
        let receipt = &execution["binding_effect_authority"]["receipt"];
        assert_eq!(receipt["owner"], owner, "{execution:#}");
        assert_eq!(receipt["binding"], binding, "{execution:#}");
        let calls = receipt["calls"].as_array().unwrap();
        assert_eq!(calls.len(), call_count, "{receipt:#}");
        // These source-only operations consume no author/value/native premise.
        // Assert the exact dependency set rather than inferring it from an
        // empty observed-access ledger or timing completeness.
        assert_eq!(
            receipt["read_dependencies"],
            serde_json::json!({}),
            "{receipt:#}"
        );
        assert_eq!(
            receipt["rebind_dependencies"],
            serde_json::json!({}),
            "{receipt:#}"
        );
        for call in calls {
            assert_eq!(call["dependencies"], serde_json::json!({}), "{receipt:#}");
        }
    }
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
}

#[test]
fn deferred_callable_creation_does_not_cover_unknown_getter_or_reentry_invocations() {
    for (prefix, body) in [
        ("", "return globalThis.read();"),
        (
            "const record = { get value() { return 3; } };\n",
            "return record.value;",
        ),
        ("", "globalThis.reenter(); return 3;"),
    ] {
        let source = format!(
            "globalThis.read = () => 3;\nglobalThis.reenter = () => undefined;\n{prefix}function factory() {{ return () => {{ {body} }}; }}\nconst selected = factory();\nconst result = selected();\nconsole.log(result);\n"
        );
        let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
        assert_preserved_entry_output(&fixture, "3\n");
        let execution = binding_execution_report(&fixture, "result");
        assert_ne!(
            execution["binding_effect_authority"]["authority"], "operation_receipt",
            "{body}: {execution:#}"
        );
        let report = fixture.effect_translation_validation();
        for domain in ["global_properties", "general_heap", "host_effects"] {
            assert!(
                report["coverage"]["unknown_coverage"][domain]
                    .as_u64()
                    .unwrap()
                    >= 2,
                "{body}: {report:#}"
            );
        }
    }
}

#[test]
fn imported_primitive_return_does_not_inherit_local_operation_authority() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { literal } from './peer.js';\nconst result = literal();\nconsole.log(result);\n",
            vec![],
        ).with_extra_chunks(&[("static/peer", "export function literal() { return 3; }\n")]),
    );
    assert_preserved_entry_output(&fixture, "3\n");
    let report = fixture.effect_translation_validation();
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert!(
            report["coverage"]["unknown_coverage"][domain]
                .as_u64()
                .unwrap()
                >= 2,
            "{report:#}"
        );
    }
}

#[test]
fn deferred_callable_operation_receipts_preserve_exact_native_prefix_dependencies() {
    const SOURCE: &str = "const cache = new Map();\nfunction factory() { return () => cache; }\nfunction nested() { return factory(); }\nconst selected = nested();\nconst result = selected();\nconsole.log(result === cache);\n";
    let fixture = run_fixture(
        FixtureOpts::new(SOURCE, vec![]).with_native_operation_inputs(
            "static/app",
            vec![native_constructor_input(
                SOURCE,
                "const cache = new Map();",
                "cache",
                NativeOperationKind::MapEmpty,
            )],
        ),
    );
    assert_preserved_entry_output(&fixture, "true\n");
    let dependencies = serde_json::json!({
        "native_operation_inputs": [{"defining_chunk": "static/app", "input_index": 0}]
    });
    for binding in ["selected", "result"] {
        let execution = binding_execution_report(&fixture, binding);
        let receipt = &execution["binding_effect_authority"]["receipt"];
        assert_eq!(
            receipt["read_dependencies"], dependencies,
            "{binding}: {execution:#}"
        );
        assert_eq!(
            receipt["rebind_dependencies"], dependencies,
            "{binding}: {execution:#}"
        );
        for call in receipt["calls"].as_array().unwrap() {
            assert_eq!(
                call["dependencies"], dependencies,
                "{binding}: {execution:#}"
            );
        }
    }
    let report = fixture.effect_translation_validation();
    assert_eq!(
        report["coverage"]["covered_owner_dependencies"], dependencies,
        "{report:#}"
    );
}

fn returned_callable_commutation_fixture() -> FixtureOpts<'static> {
    FixtureOpts::new(
        "function make() { return () => 3; }\nconst read = make();\nlet marker = 0;\nmarker = 1;\nconst result = read();\nmarker = 2;\nconsole.log(marker, result);\n",
        vec![
            logical_module_with_anon(
                "markers",
                &[Member::new("marker")],
                &["marker = 1;", "marker = 2;"],
            ),
            logical_module(
                "result",
                &[
                    Member::new("make"),
                    Member::new("read"),
                    Member::new("result"),
                ],
            ),
            logical_module_with_anon("observe", &[], &["console.log(marker, result);"]),
        ],
    )
}

#[test]
fn returned_callable_operation_coverage_discharges_only_its_dataflow_barrier() {
    let fixture =
        run_fixture(returned_callable_commutation_fixture().with_dataflow_aware_s_chain());
    assert_preserved_entry_output(&fixture, "2 3\n");
}

#[test]
fn operation_coverage_does_not_change_strict_callable_purity() {
    let rejected = run_rejection_fixture(returned_callable_commutation_fixture());
    let graph = rejected.dependency_graph();
    let result_id = source_unit_for_binding(&graph, "result");
    let result = graph
        .nodes
        .iter()
        .find(|node| node.id == result_id)
        .unwrap();
    assert_eq!(
        serde_json::to_value(&result.purity).unwrap()["kind"],
        "not_pure"
    );
    assert!(
        graph.edges.iter().any(|edge| {
            serde_json::to_value(&edge.sequenced_cause).unwrap()["rule"] == "strict_chain"
        }),
        "strict-mode rejection must retain the strict chain: {graph:#?}"
    );
}
