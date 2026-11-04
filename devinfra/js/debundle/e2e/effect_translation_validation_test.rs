//! Public-pipeline controls for conservative, report-only effect TV.

use debundle_e2e_support::*;
use serde_json::Value;
use sha2::{Digest, Sha256};

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
            .any(|pair| pair["runs_first"]["unit"] == 2
                && pair["runs_later"]["unit"] == 1
                && pair["havoc"] == true),
        "function-expression initialization remains an evaluation effect: {report:#}"
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
    for domain in ["global_properties", "general_heap", "host_effects"] {
        assert_eq!(
            report["coverage"]["unknown_coverage"][domain], 1,
            "{report:#}"
        );
    }
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
const sourceText = pattern.source;\n\
console.log(sourceText, marker, globalThis.regexGetterReads);\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("regex", &[Member::new("pattern")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "overridden 1 1\n");

    let report = fixture.effect_translation_validation();
    assert_eq!(report["swapped_pairs"], 1, "{report:#}");
    assert_eq!(report["counts"]["havoc_pairs"], 0, "{report:#}");
    assert_eq!(
        report["coverage"]["owner_coverage_refusals"]["unsupported_initializer"], 1,
        "the getter-triggering member read remains outside creation coverage: {report:#}"
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
        count("effect_only_pairs") + count("both_pairs")
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
        count("both_pairs")
    );
    for row in rows {
        assert_eq!(
            row["cell_count"].as_u64().unwrap(),
            row["cells"].as_array().unwrap().len() as u64
        );
        assert!(row["first_opacity"].is_object() && row["later_opacity"].is_object());
    }
    for (example, full) in examples(summary).iter().zip(rows) {
        for field in [
            "runs_first",
            "runs_later",
            "havoc",
            "disposition",
            "premise_locations",
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
    let covered = rows
        .iter()
        .find(|row| row["disposition"] == "covered_by_existing_premise")
        .expect("covered writer/reader pair");
    assert_eq!(covered["disposition"], "covered_by_existing_premise");
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
        report["coverage"]["owner_coverage_refusals"]["initializer_result_unproved"], 1,
        "the first declarator's forward read cannot prove its initializer value: {report:#}"
    );
}
