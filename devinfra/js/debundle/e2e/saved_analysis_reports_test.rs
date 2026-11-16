//! Report sinks and export filters agree between direct runs and runs over
//! a saved analysis artifact.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use debundle_e2e_support::saved_analysis::*;
use debundle_e2e_support::*;
use serde_json::Value;

/// Initialized binding copies preserve runtime output across a module-order
/// inversion. Public anchors exclude the private-initialization crossing
/// exception. Ordinary and exported results also agree after saved replay.
#[test]
fn initialized_object_binding_copies_match_direct_and_saved_execution() {
    for (declaration, imported) in [
        ("const copy = value;", false),
        ("export const copy = value;", false),
        ("const copy = value;", true),
        ("export const copy = value;", true),
    ] {
        let head = if imported {
            "import { value } from './peer.js';"
        } else {
            "const value = {};"
        };
        let source = format!(
            "{head}\nconst before = 0;\n{declaration}\nconst after = 1;\nconsole.log(before, typeof copy, after);\nexport {{ before, after }};\n"
        );
        let members = if imported {
            vec![Member::new("copy")]
        } else {
            vec![Member::new("value"), Member::new("copy")]
        };
        let peers = [("static/peer", "export const value = {};\n")];
        let mut opts = FixtureOpts::new(
            &source,
            vec![
                logical_module("anchors", &[Member::new("before"), Member::new("after")]),
                logical_module("copy", &members),
            ],
        );
        if imported {
            opts = opts.with_extra_chunks(&peers);
        }
        let (staged, fixture) = staged(opts);
        assert_node_output(
            &fixture.workspace().path().join("snapshot/static/app.js"),
            "0 object 1\n",
            "",
        );
        let direct = staged.run(&[]);
        let saved = staged.run(&["--analysis", path(&staged.artifact)]);
        for run in [&direct, &saved] {
            assert_ok(&run.result, "initialized object binding copy");
            let assessment: Value = serde_json::from_slice(
                &fs::read(
                    run.output_root()
                        .join("reports/tree/static/app/effect_translation_validation.json"),
                )
                .unwrap(),
            )
            .unwrap();
            assert!(
                assessment["swapped_pairs"].as_u64().unwrap() > 0,
                "the binding copy must cross an anchor: {assessment:#}"
            );
            assert_node_output(
                &run.output_root().join("app/static/app/entry.js"),
                "0 object 1\n",
                "",
            );
        }
        assert_same_invocation_trees(&direct, &saved);
    }
}

#[test]
fn uninitialized_binding_copies_still_refuse_a_reordered_schedule() {
    for declaration in ["const copy = value;", "export const copy = value;"] {
        let source = format!(
            "const before = 0;\n{declaration}\nconst value = {{}};\nconst after = 1;\nconsole.log(before, typeof copy, after);\nexport {{ before, after }};\n"
        );
        let (staged, fixture) = staged(FixtureOpts::new(
            &source,
            vec![
                logical_module("anchors", &[Member::new("before"), Member::new("after")]),
                logical_module("copy", &[Member::new("value"), Member::new("copy")]),
            ],
        ));
        assert_node_error(
            &fixture.workspace().path().join("snapshot/static/app.js"),
            "ReferenceError",
        );
        let direct = staged.run(&[]);
        let saved = staged.run(&["--analysis", path(&staged.artifact)]);
        for run in [&direct, &saved] {
            assert!(!run.result.status.success());
            let refusal: Value = serde_json::from_slice(
                &fs::read(
                    run.output_root()
                        .join("reports/tree/evaluation_schedule.json"),
                )
                .unwrap(),
            )
            .unwrap();
            assert_eq!(
                refusal["kind"], "evaluation_schedule_refused",
                "{refusal:#}"
            );
            assert_eq!(
                refusal["rejection"]["source_entry"], "static/app",
                "{refusal:#}"
            );
        }
        assert_same_invocation_trees(&direct, &saved);
    }
}

fn primitive_declarations(initializers: &[&str], comma: bool, exported: bool) -> String {
    let prefix = if exported { "export const" } else { "const" };
    if comma {
        format!("{prefix} {};", initializers.join(", "))
    } else {
        initializers
            .iter()
            .map(|initializer| format!("{prefix} {initializer};\n"))
            .collect()
    }
}

/// Equivalent declaration forms retain the same completion proof when their
/// values move across public anchors, including after saved-analysis replay.
#[test]
fn canonical_primitive_initializers_match_direct_and_saved_execution() {
    for (comma, exported) in [(false, false), (true, false), (false, true), (true, true)] {
        let declarations =
            primitive_declarations(&["a = 1", "b = 2", "c = a + b"], comma, exported);
        let exports = if exported { "" } else { "a, b, c, " };
        let source = format!(
            "const before = 0;\n{declarations}\nconst after = 1;\nconsole.log(before, c, after);\nexport {{ {exports}before, after }};\n"
        );
        let (staged, fixture) = staged(FixtureOpts::new(
            &source,
            vec![
                logical_module("anchors", &[Member::new("before"), Member::new("after")]),
                logical_module("value", &[Member::new("c")]),
            ],
        ));
        assert_node_output(
            &fixture.workspace().path().join("snapshot/static/app.js"),
            "0 3 1\n",
            "",
        );
        let direct = staged.run(&[]);
        let saved = staged.run(&["--analysis", path(&staged.artifact)]);
        // Export probes create runtime helper files; compare the CLI trees first.
        assert_same_invocation_trees(&direct, &saved);
        for run in [&direct, &saved] {
            assert_ok(&run.result, "canonical primitive initializer");
            let assessment: Value = serde_json::from_slice(
                &fs::read(
                    run.output_root()
                        .join("reports/tree/static/app/effect_translation_validation.json"),
                )
                .unwrap(),
            )
            .unwrap();
            assert!(
                assessment["swapped_pairs"].as_u64().unwrap() > 0,
                "the initializer must cross a public anchor: {assessment:#}"
            );
            assert_module_exports(
                &run.output_root().join("app"),
                "static/app/modules/value.js",
                &["c"],
                &[],
            );
            assert_node_output(
                &run.output_root().join("app/static/app/entry.js"),
                "0 3 1\n",
                "",
            );
        }
    }
}

#[test]
fn canonical_primitive_initializers_keep_tdz_and_abrupt_schedules_conservative() {
    for (initializers, error) in [
        (["a = 1", "c = a + b", "b = 2"], "ReferenceError"),
        (["a = 1n", "b = 2", "c = a + b"], "TypeError"),
        (["a = 1n", "b = 0n", "c = a / b"], "RangeError"),
    ] {
        for (comma, exported) in [(false, false), (true, false), (false, true), (true, true)] {
            let declarations = primitive_declarations(&initializers, comma, exported);
            let exports = if exported { "" } else { "a, b, c, " };
            let source = format!(
                "const before = 0;\n{declarations}\nconst after = 1;\nconsole.log(before, c, after);\nexport {{ {exports}before, after }};\n"
            );
            let (staged, fixture) = staged(FixtureOpts::new(
                &source,
                vec![
                    logical_module("anchors", &[Member::new("before"), Member::new("after")]),
                    // Keep the TDZ reader and later declaration co-located so
                    // schedule validation, rather than an atomic split, decides.
                    logical_module(
                        "value",
                        &[Member::new("a"), Member::new("b"), Member::new("c")],
                    ),
                ],
            ));
            assert_node_error(
                &fixture.workspace().path().join("snapshot/static/app.js"),
                error,
            );
            let direct = staged.run(&[]);
            let saved = staged.run(&["--analysis", path(&staged.artifact)]);
            for run in [&direct, &saved] {
                assert!(!run.result.status.success());
                let refusal: Value = serde_json::from_slice(
                    &fs::read(
                        run.output_root()
                            .join("reports/tree/evaluation_schedule.json"),
                    )
                    .unwrap(),
                )
                .unwrap();
                assert_eq!(
                    refusal["kind"], "evaluation_schedule_refused",
                    "{refusal:#}"
                );
                assert_eq!(
                    refusal["rejection"]["source_entry"], "static/app",
                    "{refusal:#}"
                );
                assert!(!run.output_root().join("app/static/app/entry.js").exists());
            }
            assert_same_invocation_trees(&direct, &saved);
        }
    }
}

#[test]
fn full_effect_stream_matches_between_direct_and_staged_runs() {
    assert_staged_run_matches(split_with_pure_import(), &["--effect-conflicts-jsonl"]);
    assert_staged_run_matches(
        rejected_split(),
        &["--effect-conflicts-jsonl", "--emit-on-rejection"],
    );
}

fn assert_export_filter_changes_only_sinks(
    export_args: &[&str],
    filter_flag: &str,
    filename: &str,
) {
    let extra_modules = [(
        "static/peer",
        vec![logical_module_with_anon(
            "provider",
            &[Member::new("value")],
            &["globalThis.externalMarker = 1;"],
        )],
    )];
    let opts = FixtureOpts::new(
        "import { value } from './peer.js';\nconst first = value + 1;\nconst second = 2;\nconst tail = second;\nconsole.log(first, tail);\nexport { first, second, tail };\n",
        vec![logical_module("outer", &[Member::new("first"), Member::new("tail")]), logical_module("inner", &[Member::new("second")])],
    ).with_extra_chunks(&[("static/peer", "globalThis.externalMarker = 1;\nexport const value = 1;\n")]).with_extra_chunk_logical_modules(&extra_modules);
    let (staged, fixture) = staged(opts);
    assert_node_output(
        &fixture.workspace().path().join("snapshot/static/app.js"),
        "2 2\n",
        "",
    );
    let all = staged.run(export_args);
    assert_ok(&all.result, "unfiltered export");
    assert_node_output(
        &all.output_root().join("app/static/app/entry.js"),
        "2 2\n",
        "",
    );
    let mut flags = export_args.to_vec();
    flags.extend([filter_flag, "static/app"]);
    let filtered = staged.run(&flags);
    assert_ok(&filtered.result, "filtered export");
    assert_node_output(
        &filtered.output_root().join("app/static/app/entry.js"),
        "2 2\n",
        "",
    );
    let without_streams = |root: &Path| {
        tree(root)
            .into_iter()
            .filter(|(path, _)| path.file_name().is_none_or(|name| name != filename))
            .collect::<BTreeMap<_, _>>()
    };
    // tree() removes only command provenance. Every other byte, including
    // all graphs, summary counts, native census and emitted JS, must match.
    assert_eq!(
        without_streams(&all.output_root()),
        without_streams(&filtered.output_root())
    );
    let main_stream = PathBuf::from(format!("reports/tree/static/app/{filename}"));
    let peer_stream = PathBuf::from(format!("reports/tree/static/peer/{filename}"));
    assert_eq!(
        fs::read(all.output_root().join(&main_stream)).unwrap(),
        fs::read(filtered.output_root().join(&main_stream)).unwrap()
    );
    assert!(all.output_root().join(&peer_stream).exists());
    assert!(!filtered.output_root().join(&peer_stream).exists());
    let artifact = path(&staged.artifact).to_owned();
    let mut loaded_flags = flags.to_vec();
    loaded_flags.extend(["--analysis", &artifact]);
    let filtered_loaded = staged.run(&loaded_flags);
    assert_ok(
        &filtered_loaded.result,
        "filtered export from full staged analysis",
    );
    assert_node_output(
        &filtered_loaded
            .output_root()
            .join("app/static/app/entry.js"),
        "2 2\n",
        "",
    );
    assert_same_invocation_trees(&filtered, &filtered_loaded);
    let mut all_selected = export_args.to_vec();
    all_selected.extend([filter_flag, "static/app", filter_flag, "static/peer"]);
    let all_selected = staged.run(&all_selected);
    assert_ok(&all_selected.result, "repeated filter selects both chunks");
    assert_same_invocation_trees(&all, &all_selected);
}

#[test]
fn export_filter_changes_only_jsonl_sinks_and_keeps_staged_facts() {
    assert_export_filter_changes_only_sinks(
        &["--effect-conflicts-jsonl"],
        "--effect-conflicts-chunk",
        "effect_conflicts.jsonl",
    );
}

#[test]
fn full_report_filter_changes_only_sinks_and_keeps_staged_facts() {
    assert_export_filter_changes_only_sinks(
        &["--full-report", "translation_validation"],
        "--full-report-chunk",
        "translation_validation.full.jsonl",
    );
}

#[test]
fn residual_atomic_report_matches_staged_run_and_filters_chunks() {
    assert_export_filter_changes_only_sinks(
        &["--full-report", "residual_atomic_groups"],
        "--full-report-chunk",
        "residual_atomic_groups.json",
    );
}

#[test]
fn full_translation_report_matches_direct_and_staged_runs() {
    assert_staged_run_matches(
        rejected_split(),
        &[
            "--emit-on-rejection",
            "--full-report",
            "translation_validation",
        ],
    );
}

#[test]
fn full_reentry_export_matches_staged_and_filters_only_sinks() {
    assert_staged_run_matches(
        split_with_pure_import(),
        &["--full-report", "export_reentry"],
    );
    assert_staged_run_matches(
        rejected_split(),
        &["--emit-on-rejection", "--full-report", "export_reentry"],
    );
    assert_export_filter_changes_only_sinks(
        &["--full-report", "export_reentry"],
        "--full-report-chunk",
        "export_reentry.full.json",
    );
}

#[test]
fn default_reports_are_graph_free_and_full_export_reuses_the_analysis() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const first = 1;\nconst second = first + 1;\nconsole.log(second);\nexport { first, second };\n",
        vec![
            logical_module("first", &[Member::new("first")]),
            logical_module("second", &[Member::new("second")]),
        ],
    ));
    let suppressed_graphs = staged.run(&["--dry-run"]);
    assert_eq!(
        suppressed_graphs.result.status.code(),
        Some(1),
        "{}",
        suppressed_graphs.result.stderr
    );
    assert!(
        suppressed_graphs
            .result
            .stderr
            .contains("--graphs requested"),
        "{}",
        suppressed_graphs.result.stderr
    );
    let small = staged.run_without_graphs(&[]);
    assert_ok(&small.result, "small default run");
    assert_node_output(
        &small.output_root().join("app/static/app/entry.js"),
        "2\n",
        "",
    );
    let small_files = tree(&small.output_root());
    assert!(!small_files.keys().any(|p| {
        p.file_name()
            .is_some_and(|n| n == "dependency_graph.json.gz" || n == "program_graph.json.gz")
    }));
    let full_run = staged.run(&["--analysis", path(&staged.artifact)]);
    assert_ok(&full_run.result, "full graphs from shared analysis");
    let full = tree(&full_run.output_root());
    let without_graphs = |files: BTreeMap<PathBuf, Vec<u8>>| {
        files
            .into_iter()
            .filter_map(|(p, mut bytes)| {
                if p.file_name().is_some_and(|n| {
                    n == "dependency_graph.json.gz" || n == "program_graph.json.gz"
                }) {
                    return None;
                }
                if p.file_name().is_some_and(|n| n == "graph_summary.json") {
                    let mut summary: Value = serde_json::from_slice(&bytes).unwrap();
                    summary.as_object_mut().unwrap().remove("full_graphs");
                    bytes = serde_json::to_vec(&summary).unwrap();
                }
                Some((p, bytes))
            })
            .collect::<BTreeMap<_, _>>()
    };
    assert_eq!(without_graphs(small_files), without_graphs(full));
    let small_export = staged.root().join("small-report-export");
    assert_ok(
        &command(&[
            "run",
            "--spec",
            path(&staged.spec),
            "--analysis",
            path(&staged.artifact),
            "--reports-only",
            path(&small_export),
        ]),
        "report-only selection without full graphs",
    );
    assert!(!small_export.join("app").exists());
    assert!(
        !small_export
            .join("reports/tree/program_graph.json.gz")
            .exists()
    );
    assert!(
        !small_export
            .join("reports/tree/static/app/dependency_graph.json.gz")
            .exists()
    );
    let export = staged.root().join("graph-export");
    assert_ok(
        &staged
            .run(&[
                "--analysis",
                path(&staged.artifact),
                "--reports-only",
                path(&export),
            ])
            .result,
        "reports-only full export",
    );
    assert!(!export.join("app").exists());
    assert!(!export.join("GATE_REJECTED").exists());
    let exported = tree(&export.join("reports/tree"));
    let program_reports = tree(&full_run.output_root().join("reports/tree"));
    // Program emission adds per-file metrics. Every report-only artifact is
    // identical to its canonical full-run counterpart after provenance removal.
    for (path, bytes) in exported {
        assert_eq!(
            Some(&bytes),
            program_reports.get(&path),
            "{}",
            path.display()
        );
    }
    let summary: Value = serde_json::from_slice(
        &fs::read(export.join("reports/tree/static/app/graph_summary.json")).unwrap(),
    )
    .unwrap();
    let graph: Value =
        read_gzip_json(&export.join("reports/tree/static/app/dependency_graph.json.gz"));
    assert_eq!(
        summary["source_units"],
        graph["nodes"].as_array().unwrap().len()
    );
    assert_eq!(summary["edges"], graph["edges"].as_array().unwrap().len());
}

#[test]
fn reports_only_keeps_typed_rejection_without_emitting_a_program() {
    let (staged, _fixture) = staged(rejected_split());
    let small = staged.run_without_graphs(&["--emit-on-rejection"]);
    assert_eq!(
        small.result.status.code(),
        Some(3),
        "{}",
        small.result.stderr
    );
    let small_marker = fs::read_to_string(small.output_root().join("GATE_REJECTED")).unwrap();
    assert!(
        !small_marker.contains("dependency graph:"),
        "{small_marker}"
    );
    assert!(
        !small
            .output_root()
            .join("reports/tree/static/app/dependency_graph.json.gz")
            .exists()
    );
    let export = staged.root().join("rejected-export");
    let result = staged.run(&[
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
        "--emit-on-rejection",
    ]);
    assert_eq!(
        result.result.status.code(),
        Some(3),
        "{}",
        result.result.stderr
    );
    assert!(!export.join("app").exists());
    let marker = fs::read_to_string(export.join("GATE_REJECTED")).unwrap();
    assert!(
        marker.contains("reports-only") && marker.contains("NOT runnable"),
        "{marker}"
    );
    assert!(
        export
            .join("reports/tree/static/app/dependency_graph.json.gz")
            .exists()
    );
}

#[test]
fn reports_only_requires_analysis_and_keeps_final_program_validation() {
    let (staged, _fixture) = staged(FixtureOpts::new(
        "const present = 1;\nconsole.log(present);\n",
        vec![logical_module("missing", &[Member::new("missing")])],
    ));
    let export = staged.root().join("invalid-export");
    let contradictory = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
        "--out-root",
        path(&staged.root().join("different-root")),
    ]);
    assert_eq!(
        contradictory.status.code(),
        Some(1),
        "{}",
        contradictory.stderr
    );
    assert!(
        contradictory
            .stderr
            .contains("must name the same output root"),
        "{}",
        contradictory.stderr
    );
    let dry = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
        "--dry-run",
    ]);
    assert_eq!(dry.status.code(), Some(2), "{}", dry.stderr);
    let no_analysis = command(&[
        "run",
        "--spec",
        path(&staged.spec),
        "--reports-only",
        path(&export),
    ]);
    assert_eq!(no_analysis.status.code(), Some(2), "{}", no_analysis.stderr);
    let result = staged.run(&[
        "--analysis",
        path(&staged.artifact),
        "--reports-only",
        path(&export),
    ]);
    assert_eq!(
        result.result.status.code(),
        Some(1),
        "{}",
        result.result.stderr
    );
    assert!(
        result.result.stderr.contains("missing"),
        "{}",
        result.result.stderr
    );
    assert!(!export.join("app").exists());
    assert!(!export.join("GATE_REJECTED").exists());
}
