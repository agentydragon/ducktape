//! `debundle spec project-analysis` and `debundle analyze` write stable
//! artifacts, and an artifact stays valid across exactly the spec edits
//! analysis does not read.

use analysis_model::DepKind;
use std::fs;

use analysis::DependencyGraphReport;
use analysis_projection::AnalysisSpec;
use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;

#[test]
fn projection_rejects_unresolved_and_duplicate_selectors_before_writing() {
    let source = "function first() { return 'same'; }\nfunction second() { return 'same'; }\n";
    for (label, modules, outcome) in [
        (
            "missing",
            vec![logical_module(
                "selected",
                &[Member::source_alpha(
                    "Missing",
                    "function selected() { return 'absent'; }",
                )],
            )],
            "no_match",
        ),
        (
            "ambiguous",
            vec![logical_module(
                "selected",
                &[Member::source_alpha(
                    "Ambiguous",
                    "function selected() { return 'same'; }",
                )],
            )],
            "ambiguous",
        ),
        (
            "duplicate",
            vec![
                logical_module("left", &[Member::renamed("Left", "first")]),
                logical_module("right", &[Member::renamed("Right", "first")]),
            ],
            "duplicate_claim",
        ),
    ] {
        let fixture = write_validate_fixture_spec(FixtureOpts::new(source, modules));
        let root = fixture.spec_path.parent().unwrap();
        for dedicated in [false, true] {
            let out = root.join(format!("{label}-{dedicated}.projection"));
            let args = ["--spec", path(&fixture.spec_path), "--out", path(&out)];
            let result = if dedicated {
                command_with(&projector_path(), &args)
            } else {
                let mut full_args = vec!["spec", "project-analysis"];
                full_args.extend(args);
                command(&full_args)
            };
            assert!(
                !result.status.success(),
                "{label} selector accepted by dedicated={dedicated}: {}",
                result.stderr
            );
            assert!(
                result.stderr.contains(&format!("[{outcome}]")),
                "{label} lost its selector diagnostic with dedicated={dedicated}: {}",
                result.stderr
            );
            assert!(!out.exists(), "{label} wrote a rejected projection");
        }
    }
}

#[test]
fn analysis_projection_preserves_semantic_note_keys_and_ignores_author_notes() {
    const APP: &str = "import { note } from './note.js';\nconst value = Symbol('value');\nconsole.log(note.note(), value.description);\nexport { value };\n";
    const PROVIDER: &str = "export const note = { note() { return 1; } };\n";
    let purity = ChunkExportPurityBuilder::new("note")
        .with_pure_members("note", &["note"])
        .build();
    let native_input = NativeOperationInputContract {
        selector: SourceUnitSelector::SourceMatch(source_match_selector(
            "const value = Symbol('value');",
            &["value"],
        )),
        operation: NativeOperationKind::Symbol,
        audited_source_sha256: source_sha256(APP),
        note: Some("native rationale before edit".to_owned()),
    };
    let (staged, _fixture) = staged(
        FixtureOpts::new(APP, vec![logical_module("all", &[Member::new("value")])])
            .with_extra_chunks(&[("note", PROVIDER)])
            .with_chunk_export_purity(&[purity])
            .with_native_operation_inputs("static/app", vec![native_input])
            .with_program_premises(
                serde_yaml::from_str(
                    "intrinsics_never_written:\n  families: all\n  note: intrinsic rationale before edit\n",
                )
                .unwrap(),
            ),
    );

    let projection = AnalysisSpec::decode(&fs::read(&staged.projection).unwrap()).unwrap();
    assert_eq!(
        projection.chunk_export_purity["note"].pure_members["note"],
        ["note".to_owned()].into_iter().collect(),
        "chunk, export, and property names equal to `note` are semantic keys",
    );
    assert_eq!(projection.native_operation_inputs["static/app"].len(), 1);

    let mut vendor_spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    vendor_spec["vendor"]["note.js"] = serde_yaml::from_str(
        "identity: fixture-vendor\nlevel: partial_swap\npackages:\n  note:\n    version: '1.0.0'\n    subpath: index.js\nsymbols:\n  note:\n    package: note\n    kind: named\n    upstream_export: note\n",
    )
    .unwrap();
    let package_root = staged.root().join("vendor-package-note");
    fs::create_dir_all(&package_root).unwrap();
    write_text_file(
        &package_root.join("package.json"),
        "{\"name\":\"note\",\"version\":\"1.0.0\"}\n",
    );
    write_text_file(&package_root.join("index.js"), "export const note = 1;\n");
    let vendor_spec_path = staged.root().join("vendor-note.yaml");
    write_yaml_file(&vendor_spec_path, &vendor_spec);
    let vendor_projection_path = staged.root().join("vendor-note.projection");
    let package_root_arg = format!("note={}", package_root.display());
    assert_ok(
        &command(&[
            "spec",
            "project-analysis",
            "--spec",
            path(&vendor_spec_path),
            "--out",
            path(&vendor_projection_path),
            "--package-root",
            &package_root_arg,
        ]),
        "project semantic vendor package and symbol maps named note",
    );
    let vendor_projection =
        AnalysisSpec::decode(&fs::read(vendor_projection_path).unwrap()).unwrap();
    let VendorLevel::PartialSwap(vendor) = &vendor_projection.vendor["note.js"].level else {
        panic!("partial swap vendor level was not retained");
    };
    assert!(vendor.packages.contains_key("note"));
    assert!(vendor.symbols.contains_key("note"));

    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["host_behavior"]["note"] = "host audit source changed".into();
    spec["program_premises"]["note"] = "program audit source changed".into();
    spec["program_premises"]["intrinsics_never_written"]["note"] =
        "intrinsic audit source changed".into();
    spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["note"] =
        "native rationale after edit".into();
    spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["source_match"]["note"] =
        "selector rationale added".into();
    let edited_spec = staged.root().join("author-notes.yaml");
    write_yaml_file(&edited_spec, &spec);

    let edited_projection = staged.root().join("author-notes.projection");
    assert_ok(
        &command(&[
            "spec",
            "project-analysis",
            "--spec",
            path(&edited_spec),
            "--out",
            path(&edited_projection),
        ]),
        "project analysis after note edits",
    );
    assert_eq!(
        fs::read(&staged.projection).unwrap(),
        fs::read(&edited_projection).unwrap(),
        "adding or changing actual author notes must leave analysis identity stable",
    );
    fs::copy(&edited_spec, &staged.spec).unwrap();
    let replay = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&replay.result, "reuse saved analysis after note edits");
    let replay_spec: serde_yaml::Value = serde_yaml::from_slice(
        &fs::read(
            replay
                .invocation_spec()
                .expect("run records invocation spec"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(
        replay_spec["host_behavior"]["note"],
        "host audit source changed"
    );
    assert_eq!(
        replay_spec["program_premises"]["note"],
        "program audit source changed"
    );
    assert_eq!(
        replay_spec["program_premises"]["intrinsics_never_written"]["note"],
        "intrinsic audit source changed"
    );
    assert_eq!(
        replay_spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["note"],
        "native rationale after edit"
    );
    assert_eq!(
        replay_spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["source_match"]
            ["note"],
        "selector rationale added"
    );
}

#[test]
fn graph_only_commuting_note_remains_in_report_provenance() {
    const SOURCE: &str = "const trace = [];\nglobalThis.record = (key) => { trace.push(key); };\nconst alpha = (globalThis.record(\"alpha\"), \"A\");\nconst beta = (globalThis.record(\"beta\"), \"B\");\nconst gamma = (globalThis.record(\"gamma\"), \"C\");\nconsole.log(alpha, beta, gamma, trace.slice().sort().join(\",\"));\nexport { alpha, beta, gamma };\n";
    const RECORD_INSTALL: &str = "globalThis.record = (key) => { trace.push(key); };";
    let mut pair = commuting_pair(
        member_source_unit("probes/inner", "beta"),
        member_source_unit("probes/outer", "gamma"),
    );
    pair.note = Some("the graph audit provenance".to_owned());
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            SOURCE,
            vec![
                logical_module_with_anon(
                    "probes/outer",
                    &[
                        Member::new("trace"),
                        Member::new("alpha"),
                        Member::new("gamma"),
                    ],
                    &[RECORD_INSTALL],
                ),
                logical_module("probes/inner", &[Member::new("beta")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_commuting_statements("static/app", vec![pair]),
    );

    let run = staged.run(&[]);
    assert_ok(&run.result, "run with graph-only note");
    let graph: DependencyGraphReport = read_gzip_json(
        &run.output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let [premise] = graph.commuting_statements.as_slice() else {
        panic!("expected one applied commuting premise: {graph:#?}");
    };
    assert_eq!(premise.note.as_deref(), Some("the graph audit provenance"));
}

fn assert_projector_matches_flat(staged: &Staged, label: &str) {
    let output = staged.root().join(format!("{label}.projector"));
    let args = ["--spec", path(&staged.spec), "--out", path(&output)];
    assert_ok(
        &command_with(&projector_path(), &args),
        &format!("{label} dedicated project-analysis"),
    );
    assert_eq!(
        fs::read(&staged.projection).unwrap(),
        fs::read(output).unwrap(),
        "{label} dedicated projector changed the canonical flat projection bytes",
    );
}

fn assert_projector_matches_tree(staged: &TreeStaged) {
    let output = staged.root().join("dedicated.projector");
    let project_out = staged.root().join("dedicated-project-out");
    let root = staged.root();
    let args = [
        "--tree-config",
        path(&staged.config),
        "--tree-modules",
        path(&staged.modules),
        "--tree-vendor-marks",
        path(&staged.vendor_marks),
        "--tree-source-root",
        path(root),
        "--out-root",
        path(&project_out),
        "--out",
        path(&output),
    ];
    assert_ok(
        &command_with(&projector_path(), &args),
        "tree dedicated project-analysis",
    );
    assert_eq!(
        fs::read(&staged.projection).unwrap(),
        fs::read(output).unwrap(),
        "dedicated projector changed the canonical tree projection bytes",
    );
}

#[test]
fn standalone_cli_and_dedicated_analyzer_write_identical_artifacts() {
    let (staged, _fixture) = staged(split_with_pure_import());
    let dedicated = fs::read(&staged.artifact).unwrap();
    let standalone = StandaloneDebundle::install();
    assert_ok(
        &standalone.run(&[
            "analyze",
            "--projection",
            path(&staged.projection),
            "--tree-source-root",
            path(staged.root()),
            "--out",
            path(&staged.artifact),
        ]),
        "standalone analyze",
    );
    assert_eq!(dedicated, fs::read(&staged.artifact).unwrap());
    assert_ok(
        &standalone.run(&[
            "run",
            "--spec",
            path(&staged.spec),
            "--analysis",
            path(&staged.artifact),
        ]),
        "standalone staged run",
    );
}

#[test]
fn dedicated_projector_matches_flat_tree_and_resolved_member_inputs() {
    let (flat, _flat_fixture) = staged(FixtureOpts::new(
        "const value = 41;\nconsole.log(value);\n",
        vec![logical_module("all", &[Member::new("value")])],
    ));
    assert_projector_matches_flat(&flat, "flat");

    let tree = staged_tree(
        &[("main", "const value = 41;\nconsole.log(value);\n")],
        &[(
            "value.yaml",
            "members: [{selector: {binding: {name: value}}}]\n",
        )],
    );
    assert_projector_matches_tree(&tree);

    let (resolved_member, _resolved_member_fixture) = staged(native_member_fixture());
    let spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&resolved_member.spec).unwrap()).unwrap();
    assert_eq!(
        spec["chunk_analysis_options"]["static/app"]["native_operation_inputs"][0]["resolved_member"]
            ["name"],
        "alias",
        "fixture must exercise the resolved-member native premise",
    );
    assert_projector_matches_flat(&resolved_member, "resolved-member");
}

#[test]
fn identity_mismatch_is_refused() {
    let (staged, _fixture) = staged(split_with_pure_import());
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "unmodified staged artifact",
    );
    let bytes = fs::read(&staged.artifact).unwrap();
    let (mut header, body) = staged_analysis::analysis_artifact::read_header(&bytes).unwrap();
    header.analysis_engine_sha256 = "another-compiled-engine".to_string();
    let encoded = rmp_serde::to_vec_named(&header).unwrap();
    let mut corrupt = u32::try_from(encoded.len()).unwrap().to_le_bytes().to_vec();
    corrupt.extend(encoded);
    corrupt.extend(body);
    fs::write(&staged.artifact, corrupt).unwrap();
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(!refused.result.status.success());
}

#[test]
fn source_mismatch_is_refused() {
    let (staged, _fixture) = staged(split_with_pure_import());
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "unmodified staged artifact",
    );
    let bytes = fs::read(&staged.artifact).unwrap();
    let (mut header, body) = staged_analysis::analysis_artifact::read_header(&bytes).unwrap();
    let source = header.chunk_sources.values_mut().next().unwrap();
    *source = "other-parser-input".to_string();
    let encoded = rmp_serde::to_vec_named(&header).unwrap();
    let mut corrupt = u32::try_from(encoded.len()).unwrap().to_le_bytes().to_vec();
    corrupt.extend(encoded);
    corrupt.extend(body);
    fs::write(&staged.artifact, corrupt).unwrap();
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(!refused.result.status.success());
}

#[test]
fn staged_invocations_keep_outputs_and_refuse_reused_reports_destinations() {
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            "const value = 1;\nconsole.log(value);\n",
            vec![logical_module("all", &[Member::new("value")])],
        )
        .with_browser_harness(),
    );

    let first = staged.run(&[]);
    assert_ok(&first.result, "first isolated staged run");
    let first_entry = first.output_root().join("app/static/app/entry.js");
    let first_entry_bytes = fs::read(&first_entry).unwrap();
    let first_spec_path = first
        .invocation_spec()
        .expect("flat run spec path")
        .to_path_buf();
    let first_spec_bytes = fs::read(&first_spec_path).unwrap();
    let first_harness = first
        .harness_root()
        .expect("fixture requests a browser harness");
    assert!(first_harness.is_dir());

    let second = staged.run(&[]);
    assert_ok(&second.result, "second isolated staged run");
    assert_ne!(first.output_root(), second.output_root());
    assert_ne!(first.invocation_spec(), second.invocation_spec());
    assert_ne!(first.harness_root(), second.harness_root());
    assert!(first_entry.is_file(), "the earlier run output was removed");
    assert_eq!(fs::read(&first_entry).unwrap(), first_entry_bytes);
    assert_eq!(fs::read(&first_spec_path).unwrap(), first_spec_bytes);
    assert!(second.harness_root().unwrap().is_dir());

    let original_artifact = fs::read(&staged.artifact).unwrap();
    let (mut header, body) =
        staged_analysis::analysis_artifact::read_header(&original_artifact).unwrap();
    header.analysis_engine_sha256 = "another-compiled-engine".to_string();
    let encoded_header = rmp_serde::to_vec_named(&header).unwrap();
    let mut invalid_artifact = Vec::new();
    invalid_artifact.extend_from_slice(&u32::try_from(encoded_header.len()).unwrap().to_le_bytes());
    invalid_artifact.extend_from_slice(&encoded_header);
    invalid_artifact.extend_from_slice(body);
    fs::write(&staged.artifact, invalid_artifact).unwrap();

    let refused_first = staged.run(&["--analysis", path(&staged.artifact)]);
    let refused_second = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(!refused_first.result.status.success());
    assert!(!refused_second.result.status.success());
    assert_ne!(refused_first.output_root(), refused_second.output_root());
    for refused in [&refused_first, &refused_second] {
        assert!(
            !refused.output_root().join("app").exists(),
            "refused invocation emitted an app under {}",
            refused.output_root().display(),
        );
        assert!(
            !refused
                .harness_root()
                .expect("fixture requests a browser harness")
                .exists(),
            "refused invocation emitted a browser harness under {}",
            refused.harness_root().unwrap().display(),
        );
    }
    assert_eq!(fs::read(&first_entry).unwrap(), first_entry_bytes);
    fs::write(&staged.artifact, original_artifact).unwrap();

    let reports_only = staged.root().join("reports-only-export");
    let report_args = [
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&reports_only),
    ];
    let exported = staged.run(&report_args);
    assert_ok(
        &exported.result,
        "reports-only export to an explicit destination",
    );
    let exported_tree = module_snapshot(&reports_only);
    assert!(!exported_tree.is_empty());

    let overwrite = staged.run(&report_args);
    assert!(!overwrite.result.status.success());
    assert!(
        overwrite
            .result
            .stderr
            .contains("Output directory is not empty"),
        "reports-only overwrite refusal changed: {}",
        overwrite.result.stderr
    );
    assert_eq!(module_snapshot(&reports_only), exported_tree);
}

/// Renaming a logical module is an assignment edit: the projection keeps its
/// bytes and the artifact computed before the edit serves the edited spec.
#[test]
fn assignment_edit_keeps_the_projection_and_the_artifact() {
    let (staged, _fixture) = staged(split_with_pure_import());
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    let modules = spec["logical_modules"]["static/app"]
        .as_mapping_mut()
        .expect("chunk modules");
    let module = modules.remove("mod_a").expect("mod_a");
    modules.insert("mod_renamed".into(), module);
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("edited.projection");
    assert_ok(
        &staged.project(&edited),
        "spec project-analysis after the edit",
    );
    assert_eq!(
        fs::read(&staged.projection).unwrap(),
        fs::read(&edited).unwrap()
    );
    let artifact = path(&staged.artifact).to_owned();
    assert_ok(
        &staged.run(&["--analysis", &artifact]).result,
        "run --analysis after the edit",
    );
}

#[test]
fn graph_options_survive_the_source_only_analysis_boundary() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const a = (globalThis.left = 1, 1);\nconst b = (globalThis.right = 2, 2);\nconsole.log(a, b);\n",
        vec![
            logical_module("first", &[Member::new("a")]),
            logical_module("second", &[Member::new("b")]),
        ],
    ));
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"] =
        serde_yaml::Value::Mapping(serde_yaml::Mapping::new());
    for enabled in [true, false] {
        spec["chunk_analysis_options"]["static/app"]["dataflow_aware_s_chain"] = enabled.into();
        write_yaml_file(&staged.spec, &spec);
        let edited = staged.root().join("edited.projection");
        assert_ok(&staged.project(&edited), "project changed graph options");
        assert!(
            fs::read(&staged.projection).unwrap() == fs::read(edited).unwrap(),
            "graph-only setting changed analysis identity"
        );
        let reused = staged.run(&["--analysis", path(&staged.artifact)]);
        assert_ok(&reused.result, "run saved facts with changed graph options");
        let graph: DependencyGraphReport = read_gzip_json(
            &reused
                .output_root()
                .join("reports/tree/static/app/dependency_graph.json.gz"),
        );
        let a = debundle_e2e_graph_support::source_unit_for_binding(&graph, "a");
        let b = debundle_e2e_graph_support::source_unit_for_binding(&graph, "b");
        let has_edge = graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::Sequenced
                && ((edge.source == a && edge.target == b)
                    || (edge.source == b && edge.target == a))
        });
        assert_eq!(has_edge, !enabled, "dataflow graph setting was not applied");
        let direct = staged.run(&[]);
        assert_ok(&direct.result, "direct run with changed graph options");
        assert_same_invocation_trees(&reused, &direct);
    }
}

#[test]
fn trusted_dataflow_summaries_reach_staged_facts_and_remain_in_the_projection() {
    let source = r#"class Holder1 { constructor() { this.kind = "h1"; } }
class Holder2 { constructor() { this.kind = "h2"; } }
const instA = new Holder1();
const instB = new Holder2();
console.log(instA.kind, instB.kind);
export { Holder1, Holder2, instA, instB };
"#;
    let (staged, _fixture) = staged(
        FixtureOpts::new(
            source,
            vec![
                logical_module("mod_a", &[Member::new("Holder1"), Member::new("instA")]),
                logical_module("mod_b", &[Member::new("Holder2"), Member::new("instB")]),
            ],
        )
        .with_dataflow_aware_s_chain()
        .with_trusted_dataflow_summaries(),
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(
        &reused.result,
        "run trusted dataflow facts from the staged artifact",
    );
    let graph: DependencyGraphReport = read_gzip_json(
        &reused
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    let inst_a = debundle_e2e_graph_support::source_unit_for_binding(&graph, "instA");
    let inst_b = debundle_e2e_graph_support::source_unit_for_binding(&graph, "instB");
    assert!(
        graph.edges.iter().all(|edge| {
            edge.edge_kind != DepKind::Sequenced
                || !((edge.source == inst_a && edge.target == inst_b)
                    || (edge.source == inst_b && edge.target == inst_a))
        }),
        "trusted syntactic summaries should keep disjoint constructor calls unsequenced"
    );
    let direct = staged.run(&[]);
    assert_ok(&direct.result, "direct run with trusted dataflow summaries");
    assert_same_invocation_trees(&reused, &direct);

    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"]["trusted_dataflow_summaries"] = false.into();
    write_yaml_file(&staged.spec, &spec);
    let edited = staged.root().join("without-trusted-summaries.projection");
    assert_ok(
        &staged.project(&edited),
        "project without trusted dataflow summaries",
    );
    assert_ne!(
        fs::read(&staged.projection).unwrap(),
        fs::read(&edited).unwrap(),
        "trusted dataflow assumptions must remain in analysis identity"
    );
    let refused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert!(
        !refused.result.status.success(),
        "changed trusted dataflow assumptions reused saved facts"
    );
    assert!(
        refused.result.stderr.contains("another projection"),
        "{}",
        refused.result.stderr
    );
}

#[test]
fn graph_only_chunk_options_keep_analysis_identity_and_evidence() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const effects = { left: 0, right: 0 };\nglobalThis.record = (key) => { effects[key] = 1; };\nconst alpha = (globalThis.record(\"left\"), \"A\");\nconst beta = (globalThis.record(\"right\"), \"B\");\nconsole.log(alpha, beta, effects.left, effects.right);\nexport { alpha, beta };\n",
        vec![logical_module_with_anon(
            "all",
            &[Member::new("effects"), Member::new("alpha"), Member::new("beta")],
            &["globalThis.record = (key) => { effects[key] = 1; };"],
        )],
    ).with_unassigned_mode(unassigned_mode_inline()));
    let original = fs::read(&staged.projection).unwrap();
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_analysis_options"]["static/app"] =
        serde_yaml::Value::Mapping(serde_yaml::Mapping::new());
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("empty-chunk-options.projection");
    assert_ok(&staged.project(&edited), "project empty chunk options");
    assert_eq!(
        original,
        fs::read(&edited).unwrap(),
        "an empty authored per-chunk entry changed source-analysis identity"
    );
    assert_ok(
        &staged.run(&["--analysis", path(&staged.artifact)]).result,
        "reuse analysis after adding an empty chunk entry",
    );

    spec["chunk_analysis_options"]["static/app"]["commuting_statements"] =
        serde_yaml::to_value(vec![commuting_pair(
            member_source_unit("all", "alpha"),
            member_source_unit("all", "beta"),
        )])
        .unwrap();
    write_yaml_file(&staged.spec, &spec);

    let edited = staged.root().join("commuting-options.projection");
    assert_ok(
        &staged.project(&edited),
        "project graph-only commuting option",
    );
    assert_eq!(
        original,
        fs::read(&edited).unwrap(),
        "a commuting premise changed source-analysis identity"
    );
    let reused = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&reused.result, "reuse analysis with commuting option");
    let graph: DependencyGraphReport = read_gzip_json(
        &reused
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz"),
    );
    assert_eq!(
        graph.commuting_statements.len(),
        1,
        "the graph-only premise was not applied to the reused analysis"
    );
}

/// An edit analysis reads invalidates the artifact; the run names the field.
#[test]
fn analysis_edit_refuses_the_stale_artifact() {
    let (staged, _fixture) = staged(split_with_pure_import());
    let mut spec: serde_yaml::Value =
        serde_yaml::from_str(&fs::read_to_string(&staged.spec).unwrap()).unwrap();
    spec["chunk_export_purity"]["static/vendorlib"]["pure_exports"] =
        serde_yaml::to_value(["wrap"]).unwrap();
    write_yaml_file(&staged.spec, &spec);

    let artifact = path(&staged.artifact).to_owned();
    let refused = staged.run(&["--analysis", &artifact]);
    assert!(
        !refused.result.status.success(),
        "stale artifact was accepted"
    );
    assert!(
        refused
            .result
            .stderr
            .contains("chunk_export_purity.static/vendorlib"),
        "refusal does not name the differing field:\n{}",
        refused.result.stderr
    );
}
