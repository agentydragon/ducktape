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

fn isolated_encoder_split(source: &'static str) -> FixtureOpts<'static> {
    let mut options = annotated_encoder_split(source);
    for (_, module) in &mut options.logical_modules {
        for binding in ["left", "right"] {
            module
                .annotations
                .get_mut(binding)
                .unwrap()
                .initializer_external_effects = Some(InitializerExternalEffects::Isolated);
        }
    }
    options
}

#[test]
fn audited_encoder_initializers_emit_together_and_preserve_output() {
    let fixture = run_fixture(isolated_encoder_split(
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
    let options = isolated_encoder_split(
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
fn initializer_external_effects_rejects_global_property_reads_and_writes() {
    for effects in [
        InitializerExternalEffects::Isolated,
        InitializerExternalEffects::PrivateMetadata,
    ] {
        for source in [
            "globalThis.flag = 1; const value = globalThis.flag; export { value };",
            "const value = (globalThis.flag = 2); export { value };",
            "globalThis.flag = 1; function read() { return globalThis.flag; } const value = read(); export { value };",
        ] {
            let mut module = logical_module("a", &[Member::new("value")]);
            module.1.annotations.insert(
                "value".to_owned(),
                BindingAnnotation {
                    initializer_external_effects: Some(effects),
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

#[test]
fn shared_metadata_identity_cannot_cross_an_intervening_reader() {
    const PROVIDER: &str = "const metadata = new WeakMap();\n\
        const shared = {};\n\
        export function makeSchema(label) { metadata.set(shared, label); return shared; }\n\
        export function description(schema) { return metadata.get(schema); }\n";
    expect_sequenced_cycle_rejection(
        metadata_boundary_fixture(
            "import { makeSchema, description } from './provider.js';\n\
         const first = makeSchema('a');\n\
         const observed = description(first);\n\
         const second = makeSchema('b');\n\
         console.log(observed, description(second));\n\
         export { first, second };\n",
            &[("static/provider", PROVIDER)],
        ),
        &["schemas", "anon_residual_sentinel"],
    );
}

#[test]
fn published_fresh_metadata_cannot_cross_an_intervening_reader() {
    const PROVIDER: &str = "const metadata = new WeakMap();\n\
        let published;\n\
        export function makeSchema(label) {\n\
          const schema = {}; metadata.set(schema, label); published = schema; return schema;\n\
        }\n\
        export function description(schema) { return metadata.get(schema); }\n\
        export function latest() { return description(published); }\n";
    expect_sequenced_cycle_rejection(
        metadata_boundary_fixture(
            "import { makeSchema, description, latest } from './provider.js';\n\
         const first = makeSchema('a');\n\
         const observed = latest();\n\
         const second = makeSchema('b');\n\
         console.log(observed, description(second));\n\
         export { first, second };\n",
            &[("static/provider", PROVIDER)],
        ),
        &["schemas", "anon_residual_sentinel"],
    );
}

#[test]
fn schema_construction_cannot_cross_a_configuration_change() {
    const PROVIDER: &str = "let mode = '0';\n\
        export function makeSchema(label) { return { value: label + mode }; }\n\
        export function configure(value) { mode = value; }\n";
    expect_sequenced_cycle_rejection(
        metadata_boundary_fixture(
            "import { makeSchema, configure } from './provider.js';\n\
         const first = makeSchema('a');\n\
         configure('1');\n\
         const second = makeSchema('b');\n\
         console.log(first.value, second.value);\n\
         export { first, second };\n",
            &[("static/provider", PROVIDER)],
        ),
        &["schemas", "anon_residual_sentinel"],
    );
}

fn metadata_boundary_fixture<'a>(
    source: &'a str,
    chunks: &'a [(&'a str, &'a str)],
) -> FixtureOpts<'a> {
    let mut schemas = logical_module("schemas", &[Member::new("first"), Member::new("second")]);
    for binding in ["first", "second"] {
        schemas.1.annotations.insert(
            binding.to_owned(),
            BindingAnnotation {
                initializer_binding_effects: Some(InitializerBindingEffects::SourceOnly),
                ..Default::default()
            },
        );
    }
    FixtureOpts::new(source, vec![schemas])
        .with_extra_chunks(chunks)
        .with_unassigned_mode(unassigned_mode_inline())
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries()
}
fn private_metadata_split<'a>(
    source: &'a str,
    chunks: &'a [(&'a str, &'a str)],
) -> FixtureOpts<'a> {
    let mut module = logical_module("a", &[Member::new("first"), Member::new("second")]);
    for binding in ["first", "second"] {
        module.1.annotations.insert(
            binding.to_owned(),
            BindingAnnotation {
                initializer_binding_effects: Some(InitializerBindingEffects::SourceOnly),
                initializer_external_effects: Some(InitializerExternalEffects::PrivateMetadata),
                ..Default::default()
            },
        );
    }
    FixtureOpts::new(source, vec![module])
        .with_extra_chunks(chunks)
        .with_unassigned_mode(unassigned_mode_inline())
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries()
}

fn assert_private_metadata_acceptance(
    source: &'static str,
    provider: &'static str,
    expected_stdout: &str,
) {
    let chunks = [("static/provider", provider)];
    let outcome = run_fixture_to_gate_outcome(private_metadata_split(source, &chunks));
    assert!(outcome.source.status.success(), "{}", outcome.source.stderr);
    assert!(
        outcome.emitted.status.success(),
        "{}",
        outcome.emitted.stderr
    );
    assert_eq!(outcome.source.stdout, expected_stdout);
    assert_eq!(outcome.emitted.stdout, outcome.source.stdout);
    assert_eq!(outcome.source.stderr, "");
    assert_eq!(outcome.emitted.stderr, "");
    assert_module_exports(
        &outcome.out_root,
        "static/app/modules/a.js",
        &["first", "second"],
        &[],
    );
    assert!(
        outcome.accepted,
        "private-metadata split was rejected after source/emitted Node parity and export checks"
    );
}

#[test]
fn private_metadata_contract_accepts_fresh_weak_map_entries() {
    const PROVIDER: &str = "const metadata = new WeakMap();\n\
        export function make(label) {\n\
          const result = {};\n\
          metadata.set(result, label);\n\
          return result;\n\
        }\n\
        export function describe(result) { return metadata.get(result); }\n";
    assert_private_metadata_acceptance(
        "import { make, describe } from './provider.js';\n\
         const before = [];\n\
         const first = make('a');\n\
         const middle = [];\n\
         const second = make('b');\n\
         console.log(describe(first), describe(second), before.length, middle.length);\n\
         export { first, second };\n",
        PROVIDER,
        "a b 0 0\n",
    );
}

#[test]
fn private_metadata_recognizes_fresh_nested_array_arguments() {
    const PROVIDER: &str = "const metadata = new WeakMap();\n\
        export function makeSchema(labels) {\n\
          const schema = {};\n\
          metadata.set(schema, labels[0]);\n\
          return schema;\n\
        }\n\
        export function description(schema) { return metadata.get(schema); }\n";
    assert_private_metadata_acceptance(
        "import { makeSchema, description } from './provider.js';\n\
         makeSchema(['unused']);\n\
         const first = makeSchema(['a']);\n\
         const second = makeSchema(['b']);\n\
         console.log(description(first), description(second));\n\
         export { first, second };\n",
        PROVIDER,
        "a b\n",
    );
}

#[test]
fn private_metadata_contract_accepts_stable_provider_configuration_reads() {
    const PROVIDER: &str = "const config = { prefix: 'p' };\n\
        const metadata = new WeakMap();\n\
        export function make(label) {\n\
          const result = { value: config.prefix + label };\n\
          metadata.set(result, label);\n\
          return result;\n\
        }\n\
        export function describe(result) { return metadata.get(result); }\n";
    assert_private_metadata_acceptance(
        "import { make, describe } from './provider.js';\n\
         const before = [];\n\
         const first = make('a');\n\
         const middle = [];\n\
         const second = make('b');\n\
         console.log(first.value, second.value, describe(first), describe(second), before.length, middle.length);\n\
         export { first, second };\n",
        PROVIDER,
        "pa pb a b 0 0\n",
    );
}

#[test]
fn private_metadata_contract_accepts_idempotent_private_prefix_memoization() {
    const PROVIDER: &str = "const config = { prefix: 'p' };\n\
        let cachedPrefix;\n\
        const metadata = new WeakMap();\n\
        function prefix() {\n\
          if (cachedPrefix === undefined) cachedPrefix = config.prefix;\n\
          return cachedPrefix;\n\
        }\n\
        export function make(label) {\n\
          const result = { value: prefix() + label };\n\
          metadata.set(result, label);\n\
          return result;\n\
        }\n\
        export function describe(result) { return metadata.get(result); }\n";
    assert_private_metadata_acceptance(
        "import { make, describe } from './provider.js';\n\
         const before = [];\n\
         const first = make('a');\n\
         const middle = [];\n\
         const second = make('a');\n\
         console.log(first !== second, first.value, second.value, describe(first), describe(second), before.length, middle.length);\n\
         export { first, second };\n",
        PROVIDER,
        "true pa pa a a 0 0\n",
    );
}

fn set_private_metadata(staged: &Staged, binding: &str, chunk_rename: bool) {
    let mut spec: serde_yaml::Value =
        serde_yaml::from_slice(&fs::read(&staged.spec).unwrap()).unwrap();
    let annotation = serde_yaml::from_str(
        "initializer_binding_effects: source_only\ninitializer_external_effects: private_metadata",
    )
    .unwrap();
    if chunk_rename {
        spec["chunk_renames"]["static/app"] = serde_yaml::from_str(&format!(
            "members:\n  - selector:\n      binding:\n        name: {binding}\nannotations: {{}}\n"
        ))
        .unwrap();
        spec["chunk_renames"]["static/app"]["annotations"][binding] = annotation;
    } else {
        spec["logical_modules"]["static/app"]["a"]["annotations"][binding] = annotation;
    }
    write_yaml_file(&staged.spec, &spec);
}

#[test]
fn private_metadata_contract_replays_raw_analysis_for_comma_declarators_and_chunk_renames() {
    const PROVIDER: &str = "const metadata = new WeakMap();\n\
        export function make(label) {\n\
          const result = {};\n\
          metadata.set(result, label);\n\
          return result;\n\
        }\n\
        export function describe(result) { return metadata.get(result); }\n";
    const SOURCE: &str = "import { make, describe } from './provider.js';\n\
        const first = make('a'), second = make('b');\n\
        console.log(describe(first), describe(second));\n\
        export { first, second };\n";
    for chunk_rename in [false, true] {
        let (staged, _fixture) = staged(
            FixtureOpts::new(SOURCE, vec![logical_module("a", &[Member::new("second")])])
                .with_extra_chunks(&[("static/provider", PROVIDER)])
                .with_unassigned_mode(unassigned_mode_inline())
                .with_dataflow_aware_s_chain()
                .with_trusted_dataflow_summaries(),
        );
        let raw_artifact = fs::read(&staged.artifact).unwrap();
        set_private_metadata(&staged, "second", chunk_rename);
        let projected = staged.root().join("private-metadata.projection");
        assert_ok(
            &staged.project(&projected),
            "project private-metadata annotation",
        );
        assert_eq!(
            fs::read(&staged.projection).unwrap(),
            fs::read(projected).unwrap(),
            "external-effect annotations must not change the raw source projection"
        );
        assert_ok(&staged.analyze(), "reanalyze unchanged raw projection");
        assert_eq!(
            raw_artifact,
            fs::read(&staged.artifact).unwrap(),
            "private-metadata annotation must leave raw facts and receipts unchanged"
        );
        let direct = staged.run(&[]);
        let loaded = staged.run(&["--analysis", path(&staged.artifact)]);
        assert_ok(&direct.result, "direct private-metadata materialization");
        assert_ok(&loaded.result, "loaded private-metadata materialization");
        assert_same_invocation_trees(&direct, &loaded);
        assert_module_exports(
            &loaded.output_root().join("app"),
            "static/app/modules/a.js",
            &["second"],
            &["first"],
        );
        assert_node_output(&staged.root().join("snapshot/static/app.js"), "a b\n", "");
        assert_node_output(
            &loaded.output_root().join("app/static/app/entry.js"),
            "a b\n",
            "",
        );
    }
}

#[test]
fn private_metadata_does_not_erase_source_known_shared_cell_ordering() {
    const PROVIDER: &str = "const metadata = new WeakMap();\n\
        export function makeSchema(label) {\n\
          const schema = {};\n\
          metadata.set(schema, label);\n\
          return schema;\n\
        }\n\
        export function description(schema) { return metadata.get(schema); }\n";
    const SOURCE: &str = "import { makeSchema, description } from './provider.js';\n\
        const state = [0];\n\
        const first = (state[0] = 1, makeSchema('a'));\n\
        const observed = state[0];\n\
        const second = (state[0] = 2, makeSchema('b'));\n\
        console.log(observed, description(first), description(second));\n\
        export { first, second };\n";
    let mut schemas = logical_module("schemas", &[Member::new("first"), Member::new("second")]);
    for binding in ["first", "second"] {
        schemas.1.annotations.insert(
            binding.to_owned(),
            BindingAnnotation {
                initializer_binding_effects: Some(InitializerBindingEffects::SourceOnly),
                initializer_external_effects: Some(InitializerExternalEffects::PrivateMetadata),
                ..Default::default()
            },
        );
    }
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            SOURCE,
            vec![logical_module("state", &[Member::new("state")]), schemas],
        )
        .with_extra_chunks(&[("static/provider", PROVIDER)])
        .with_unassigned_mode(unassigned_mode_inline())
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries(),
        &["schemas", "anon_residual_sentinel"],
    );
}
