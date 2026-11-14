//! `initializer: source_only` member edits reuse the raw saved analysis and
//! change only the materialized effects.

use std::collections::BTreeSet;
use std::fs;

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use serde_json::Value;
use spec::{
    BindingAnnotation, InitializerBindingEffects, InitializerExternalEffects, IntrinsicFamilies,
    IntrinsicFamily, IntrinsicsNeverWritten, ProgramPremises,
};

fn annotated_encoder_split(source: &'static str) -> FixtureOpts<'static> {
    let mut codec = logical_module(
        "codec",
        &[
            Member::new("encoder"),
            Member::new("left"),
            Member::new("right"),
        ],
    );
    for binding in ["left", "right"] {
        codec.1.annotations.insert(
            binding.to_owned(),
            BindingAnnotation {
                initializer_purity: InitializerPurity::Pure,
                initializer_binding_effects: Some(InitializerBindingEffects::SourceOnly),
                ..Default::default()
            },
        );
    }
    FixtureOpts::new(source, vec![codec]).with_unassigned_mode(unassigned_mode_inline())
}

#[test]
fn audited_encoder_initializers_emit_together_and_preserve_output() {
    let fixture = run_fixture(annotated_encoder_split(
        "const encoder = new TextEncoder();\n\
         function peek() { return later; }\n\
         globalThis.saved = peek;\n\
         const left = encoder.encode('a');\n\
         globalThis.marker = 1;\n\
         const right = encoder.encode('b');\n\
         const later = 2;\n\
         console.log(left[0], right[0], globalThis.marker, globalThis.saved());\n\
         export { encoder, left, right };\n",
    ));
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/codec.js",
        &["encoder", "left", "right"],
        &[],
    );
    assert_preserved_entry_output(&fixture, "97 98 1 2\n");
}

#[test]
fn encoder_initializer_contract_rejects_observed_application_state_writes() {
    expect_rejection_containing_all(
        annotated_encoder_split(
            "let state = 0;\n\
             const encoder = { encode(value) { state++; return value; } };\n\
             const left = encoder.encode('a');\n\
             const right = encoder.encode('b');\n\
             export { encoder, left, right };\n",
        ),
        &["initializer_binding_effects", "contradicts", "state"],
    );
}

#[test]
fn encoder_initializers_commute_with_independent_allocations() {
    let fixture = run_fixture(
        annotated_encoder_split(
            "const encoder = new TextEncoder();\n\
             const values = [];\n\
             const decoder = new TextDecoder('utf8');\n\
             const left = encoder.encode('a');\n\
             const right = encoder.encode('b');\n\
             console.log(values.length, decoder.decode(left), decoder.decode(right));\n\
             export { encoder, left, right };\n",
        )
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries()
        .with_commuting_statements(
            "static/app",
            ["left", "right"]
                .into_iter()
                .flat_map(|binding| {
                    [
                        ("const values = [];", "values"),
                        ("const decoder = new TextDecoder('utf8');", "decoder"),
                    ]
                    .into_iter()
                    .map(move |(statement, allocation)| {
                        commuting_pair(
                            member_source_unit("codec", binding),
                            declared_source_unit(statement, allocation),
                        )
                    })
                })
                .collect(),
        ),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/codec.js",
        &["encoder", "left", "right"],
        &[],
    );
    assert_preserved_entry_output(&fixture, "0 a b\n");
}

#[test]
fn isolated_encoder_initializers_emit_across_heap_barriers_without_pairwise_promises() {
    let mut options = annotated_encoder_split(
        "const encoder = new TextEncoder();\n\
         const values = [];\n\
         const decoder = new TextDecoder('utf8');\n\
         const left = encoder.encode('a');\n\
         const right = encoder.encode('b');\n\
         console.log(values.length, decoder.decode(left), decoder.decode(right));\n\
         export { encoder, left, right };\n",
    )
    .with_dataflow_aware_s_chain()
    .with_trusted_dataflow_summaries();
    for (_, module) in &mut options.logical_modules {
        for binding in ["left", "right"] {
            module
                .annotations
                .get_mut(binding)
                .unwrap()
                .initializer_external_effects = Some(InitializerExternalEffects::Isolated);
        }
    }
    let fixture = run_fixture(options);
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/codec.js",
        &["encoder", "left", "right"],
        &[],
    );
    assert_preserved_entry_output(&fixture, "0 a b\n");
}

#[test]
fn isolated_initializer_rejects_observed_shared_array_reads_and_writes() {
    for source in [
        "const state = [1]; const value = state[0]; export { value };",
        "const state = [1]; const value = (state[0] = 2); export { value };",
        "function factory(previous) { const fresh = []; if (previous) previous[0] = 1; return fresh; }\n\
         const first = factory(); const value = factory(first); export { value };",
    ] {
        let mut module = logical_module("a", &[Member::new("value")]);
        module.1.annotations.insert(
            "value".to_owned(),
            BindingAnnotation {
                initializer_external_effects: Some(InitializerExternalEffects::Isolated),
                ..Default::default()
            },
        );
        expect_rejection_containing_all(
            FixtureOpts::new(source, vec![module])
                .with_unassigned_mode(unassigned_mode_inline())
                .with_dataflow_aware_s_chain()
                .with_trusted_dataflow_summaries(),
            &["initializer_external_effects", "observed heap accesses"],
        );
    }
}

#[test]
fn isolated_initializer_keeps_shared_var_redeclarations() {
    let mut module = logical_module("a", &[Member::new("value")]);
    module.1.annotations.insert(
        "value".to_owned(),
        BindingAnnotation {
            initializer_external_effects: Some(InitializerExternalEffects::Isolated),
            ..Default::default()
        },
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            "var value = 0; var value; console.log(value); export { value };",
            vec![module],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries(),
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/a.js",
        &["value"],
        &[],
    );
    assert_preserved_entry_output(&fixture, "0\n");
}

#[test]
fn isolated_initializer_rejects_global_property_reads_and_writes() {
    for source in [
        "globalThis.flag = 1; const value = globalThis.flag; export { value };",
        "const value = (globalThis.flag = 2); export { value };",
        "globalThis.flag = 1; function read() { return globalThis.flag; } const value = read(); export { value };",
    ] {
        let mut module = logical_module("a", &[Member::new("value")]);
        module.1.annotations.insert(
            "value".to_owned(),
            BindingAnnotation {
                initializer_external_effects: Some(InitializerExternalEffects::Isolated),
                ..Default::default()
            },
        );
        expect_rejection_containing_all(
            FixtureOpts::new(source, vec![module])
                .with_unassigned_mode(unassigned_mode_inline())
                .with_dataflow_aware_s_chain()
                .with_trusted_dataflow_summaries(),
            &["initializer_external_effects", "global-property accesses"],
        );
    }
}

fn set_source_only(staged: &Staged, binding: &str, chunk_rename: bool) {
    let mut spec: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&staged.spec).unwrap()).unwrap();
    let annotation = serde_yaml::from_str("initializer_binding_effects: source_only").unwrap();
    if chunk_rename {
        spec["chunk_renames"]["static/app"] = serde_yaml::from_str(&format!(
            "members:\n  - selector:\n      binding:\n        name: {binding}\nannotations: {{}}\n"
        ))
        .unwrap();
        spec["chunk_renames"]["static/app"]["annotations"][binding] = annotation;
    } else {
        spec["logical_modules"]["static/app"]["all"]["annotations"][binding] = annotation;
    }
    write_yaml_file(&staged.spec, &spec);
}

#[test]
fn initializer_source_only_reuses_raw_analysis_and_changes_materialized_effects() {
    struct Case {
        source: &'static str,
        binding: &'static str,
        members: &'static [&'static str],
        anonymous_statements: &'static [&'static str],
        static_footprint: &'static [(&'static str, &'static str)],
        output: &'static str,
        expects_rebind: bool,
        array_integrity_premise: bool,
    }
    let cases = [
        Case {
            source: "const seed = 41;\n\
function factory() { globalThis.host?.(() => { const value = later; later = 3; return value; }); return seed; }\n\
var result = (seed, factory(), factory());\n\
var later = 0;\n\
console.log(result, later);\n",
            binding: "result",
            members: &["seed", "factory", "result", "later"],
            anonymous_statements: &[],
            static_footprint: &[("seed", "eager_use"), ("factory", "hoisted_use")],
            output: "41 0\n",
            expects_rebind: true,
            array_integrity_premise: false,
        },
        Case {
            source: "function readLater() { return later; }\n\
globalThis.saved = readLater;\n\
var table = (() => {\n\
  const values = [];\n\
  values.push((0).toString());\n\
  return values;\n\
})();\n\
var later = \"ready\";\n\
console.log(table[0], later, globalThis.saved === readLater);\n\
export { table, readLater };\n",
            binding: "table",
            members: &["readLater", "table", "later"],
            anonymous_statements: &[
                "globalThis.saved = readLater;",
                "console.log(table[0], later, globalThis.saved === readLater);",
            ],
            static_footprint: &[],
            output: "0 ready true\n",
            expects_rebind: false,
            array_integrity_premise: true,
        },
    ];
    for case in cases {
        for chunk_rename in [false, true] {
            let members = case
                .members
                .iter()
                .map(|name| Member::new(name))
                .collect::<Vec<_>>();
            let mut opts = FixtureOpts::new(
                case.source,
                vec![logical_module_with_anon(
                    "all",
                    &members,
                    case.anonymous_statements,
                )],
            );
            if case.array_integrity_premise {
                opts = opts.with_program_premises(ProgramPremises {
                    intrinsics_never_written: Some(IntrinsicsNeverWritten {
                        families: IntrinsicFamilies::Listed(BTreeSet::from([
                            IntrinsicFamily::Array,
                        ])),
                        note: Some("Neutral source-only receiver provenance fixture".to_owned()),
                    }),
                    ..ProgramPremises::default()
                });
            }
            let (staged, _fixture) = staged(opts);
            let before = staged.run(&[]);
            assert_ok(&before.result, "unannotated materialization");
            let raw_graph: Value = read_gzip_json(
                &before
                    .output_root()
                    .join("reports/tree/static/app/dependency_graph.json.gz"),
            );
            let result = graph_node_for_binding(&raw_graph, case.binding);
            let later = graph_node_for_binding(&raw_graph, "later");
            assert!(
                raw_graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == result["id"]
                        && edge["target"] == later["id"]
                        && edge["edge_kind"] == "eager_use"
                        && edge["binding"] == "later"
                }),
                "fallback must contribute a dependency before the overlay: {raw_graph:#}"
            );
            assert!(
                raw_graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == later["id"]
                        && edge["target"] == result["id"]
                        && edge["edge_kind"] == "sequenced"
                }),
                "an assumed observation before initialization must retain source order: {raw_graph:#}"
            );
            let artifact_before = fs::read(&staged.artifact).unwrap();
            set_source_only(&staged, case.binding, chunk_rename);
            let edited = staged.root().join("source-only.projection");
            assert_ok(&staged.project(&edited), "project source-only annotation");
            assert!(
                fs::read(&staged.projection).unwrap() == fs::read(edited).unwrap(),
                "a lexical effect overlay must preserve raw analysis identity"
            );
            assert_ok(&staged.analyze(), "reanalyze with unchanged raw projection");
            assert!(
                artifact_before == fs::read(&staged.artifact).unwrap(),
                "source-only annotations must leave raw source facts and receipts unchanged"
            );
            let direct = staged.run(&[]);
            let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
            assert_ok(&direct.result, "direct source-only materialization");
            assert_ok(&loaded.result, "loaded source-only materialization");
            assert_same_invocation_trees(&direct, &loaded);
            assert_node_output(
                &loaded.output_root().join("app/static/app/entry.js"),
                case.output,
                "",
            );
            let graph: Value = read_gzip_json(
                &loaded
                    .output_root()
                    .join("reports/tree/static/app/dependency_graph.json.gz"),
            );
            let result = graph_node_for_binding(&graph, case.binding);
            let execution = &result["synchronous_execution"];
            assert_eq!(
                execution["binding_effect_authority"]["authority"], "author_source_only",
                "{execution:#}"
            );
            let effect_use = &execution["initializer_binding_effects"];
            assert_eq!(effect_use["outcome"], "applied", "{execution:#}");
            for field in ["narrowed_reads", "narrowed_uninitialized_reads"] {
                assert!(effect_use[field].as_u64().unwrap() > 0, "{effect_use:#}");
            }
            if case.expects_rebind {
                assert!(
                    effect_use["narrowed_rebinds"].as_u64().unwrap() > 0,
                    "{effect_use:#}"
                );
            } else {
                assert_eq!(effect_use["narrowed_rebinds"], 0, "{effect_use:#}");
            }
            for &(binding, edge_kind) in case.static_footprint {
                let target = graph_node_for_binding(&graph, binding);
                assert!(
                    graph["edges"].as_array().unwrap().iter().any(|edge| {
                        edge["source"] == result["id"]
                            && edge["target"] == target["id"]
                            && edge["edge_kind"] == edge_kind
                            && edge["binding"] == binding
                    }),
                    "static footprint dependency must survive: {graph:#}"
                );
            }
            assert!(
                !graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == result["id"]
                        && edge["edge_kind"] == "eager_use"
                        && edge["binding"] == "later"
                }),
                "source-only authority must suppress fallback roots: {graph:#}"
            );
            let later = graph_node_for_binding(&graph, "later");
            assert!(
                !graph["edges"].as_array().unwrap().iter().any(|edge| {
                    edge["source"] == later["id"]
                        && edge["target"] == result["id"]
                        && edge["edge_kind"] == "sequenced"
                }),
                "narrowed assumed observations must leave the ordering graph: {graph:#}"
            );
        }
    }
}

#[test]
fn initializer_source_only_still_rejects_observed_reads_and_rebinds_with_saved_facts() {
    let mut cases = ["return seed;", "seed = 9; return 7;"]
        .into_iter()
        .map(|body| {
            (
                format!(
                    "let seed = 41;\nfunction factory() {{ {body} }}\nvar result = factory();\nconsole.log(result);\n"
                ),
                "result",
                vec!["seed", "factory", "result"],
            )
        })
        .collect::<Vec<_>>();
    cases.push((
        "function readLater() { return later; }\nvar table = readLater();\nvar later = \"ready\";\nconsole.log(table);\nexport { table, readLater };\n".to_owned(),
        "table",
        vec!["readLater", "table", "later"],
    ));
    for (source, binding, member_names) in cases {
        let members = member_names
            .iter()
            .map(|name| Member::new(name))
            .collect::<Vec<_>>();
        let (staged, _fixture) = staged(FixtureOpts::new(
            &source,
            vec![logical_module("all", &members)],
        ));
        set_source_only(&staged, binding, false);
        for args in [vec![], vec!["--analysis", path(&staged.artifact)]] {
            let invocation = staged.run(&args);
            assert!(
                !invocation.result.status.success(),
                "observed lexical effects must contradict the premise"
            );
            assert!(
                invocation
                    .result
                    .stderr
                    .contains("contradicts observed lexical effects"),
                "{}",
                invocation.result.stderr
            );
        }
    }
}

#[test]
fn initializer_source_only_preserves_simple_declarator_admission() {
    for source in [
        "var result;\n",
        "function result() { return 7; }\n",
        "const {result} = {result: 7};\n",
        "var result = 7; var result = 8;\n",
    ] {
        for chunk_rename in [false, true] {
            let (staged, _fixture) = staged(FixtureOpts::new(
                source,
                vec![logical_module("all", &[Member::new("result")])],
            ));
            set_source_only(&staged, "result", chunk_rename);
            let projection = staged.root().join("inadmissible.projection");
            let result = staged.project(&projection);
            assert!(
                !result.status.success(),
                "unsupported target must fail before raw analysis"
            );
            assert!(
                result.stderr.contains("initializer_binding_effects"),
                "{}",
                result.stderr
            );
            for args in [vec![], vec!["--analysis", path(&staged.artifact)]] {
                let result = staged.run(&args);
                assert!(
                    !result.result.status.success(),
                    "unsupported target must fail materialization"
                );
                assert!(
                    result.result.stderr.contains("initializer_binding_effects"),
                    "{}",
                    result.result.stderr
                );
            }
        }
    }
}

#[test]
fn initializer_source_only_uses_resolved_binding_and_keeps_comma_siblings_raw() {
    for bare_redeclaration in ["", "var result;\n"] {
        let source = format!(
            "const seed = 41; function factory() {{ return seed; }}\nvar sibling = factory(), result = 7;\n{bare_redeclaration}console.log(sibling, result);\n"
        );
        let (staged, _fixture) = staged(FixtureOpts::new(
            &source,
            vec![logical_module(
                "all",
                &[
                    Member::new("seed"),
                    Member::new("factory"),
                    Member::new("sibling"),
                    Member::renamed("readable", "result"),
                ],
            )],
        ));
        set_source_only(&staged, "readable", false);
        let edited = staged.root().join("renamed.projection");
        assert_ok(&staged.project(&edited), "project resolved annotation");
        assert_eq!(
            fs::read(&staged.projection).unwrap(),
            fs::read(edited).unwrap()
        );
        let direct = staged.run(&[]);
        let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
        assert_ok(&direct.result, "direct comma-list annotation");
        assert_ok(&loaded.result, "loaded comma-list annotation");
        assert_same_invocation_trees(&direct, &loaded);
        assert_node_output(
            &loaded.output_root().join("app/static/app/entry.js"),
            "41 7\n",
            "",
        );
        let graph: Value = read_gzip_json(
            &loaded
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        let selected = graph_node_for_binding(&graph, "result");
        assert_eq!(
            selected["synchronous_execution"]["initializer_binding_effects"]["binding"],
            "result"
        );
        assert_eq!(
            selected["synchronous_execution"]["binding_effect_authority"]["authority"],
            "author_source_only"
        );
        let sibling = graph_node_for_binding(&graph, "sibling");
        assert!(sibling["synchronous_execution"]["initializer_binding_effects"].is_null());
        let seed = graph_node_for_binding(&graph, "seed");
        assert!(
            graph["edges"].as_array().unwrap().iter().any(|edge| {
                edge["source"] == sibling["id"]
                    && edge["target"] == seed["id"]
                    && edge["edge_kind"] == "eager_use"
                    && edge["binding"] == "seed"
            }),
            "the sibling's observed promoted read must remain: {graph:#}"
        );
    }
}
