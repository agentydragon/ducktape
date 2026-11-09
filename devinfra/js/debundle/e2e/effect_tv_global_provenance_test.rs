//! Report-only effect translation validation: global cells, linkage and
//! conservative provenance.

use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;
use serde_json::Value;

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
