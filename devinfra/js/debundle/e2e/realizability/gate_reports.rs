//! Realizability graph, manifest, metric, and cut-edge contracts.
//! Plain behavior and refusal cases live in `cases/realizability/`.

use analysis_model::DepKind;
use debundle_e2e_graph_support::FixtureGraphExt;
use std::fs;
use std::path::Path;

use analysis::BindingReport;
use debundle_e2e_support::*;

#[test]
fn disposal_symbol_method_keys_do_not_block_class_split() {
    for declaration in ["class a", "const a = class"] {
        let source = format!(
            r#"console.log("before");
{declaration} {{
  [Symbol.dispose]() {{ return "sync"; }}
  [Symbol.asyncDispose]() {{ return "async"; }}
}}
const instance = new a();
console.log("after", a.name, instance[Symbol.dispose](), instance[Symbol.asyncDispose]());
export {{ a }};
"#
        );
        let fixture = run_fixture(
            FixtureOpts::new(&source, vec![logical_module("mod_x", &[Member::new("a")])])
                .with_import_order(&["mod_x", "residual"]),
        );
        assert_preserved_entry_output(&fixture, "before\nafter a sync async\n");
    }
}

#[test]
fn unclaimed_interleaved_statements_preserve_order() {
    for (mode, destination, priority) in [
        (unassigned_mode_inline(), "static/app/entry.js", vec![]),
        (
            unassigned_mode_catchall_file(None),
            "static/app/modules/residual/unhandled.js",
            vec!["residual"],
        ),
        (
            unassigned_mode_catchall_file(Some("rest")),
            "static/app/modules/rest.js",
            vec!["residual"],
        ),
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(
                r#"console.log("before");
const a = console.log("middle");
console.log("after", a);
export { a };
"#,
                vec![],
            )
            .with_unassigned_mode(mode)
            .with_import_order(&priority),
        );
        assert_entry_output(&fixture, "before\nmiddle\nafter undefined\n");
        assert_module_source(
            &fixture.out_root,
            destination,
            &["before", "middle", "after"],
            &[],
        );
    }
}

fn binding_names(members: &[BindingReport]) -> Vec<String> {
    members
        .iter()
        .map(|member| member.binding.to_string())
        .collect()
}

fn file_size(path: &Path) -> (usize, usize) {
    let content =
        fs::read_to_string(path).unwrap_or_else(|err| panic!("read {}: {err}", path.display()));
    (content.len(), content.lines().count())
}

fn assert_size_metric(report: &serde_json::Value, path: &[&str], expected: (usize, usize, usize)) {
    let metric = path.iter().fold(report, |value, key| &value[*key]);
    assert_eq!(metric["files"], expected.0, "{path:?} files");
    assert_eq!(metric["bytes"], expected.1, "{path:?} bytes");
    assert_eq!(metric["lines"], expected.2, "{path:?} lines");
}

fn assert_fraction_metric(
    report: &serde_json::Value,
    path: &[&str],
    expected_bytes: usize,
    expected_lines: usize,
    total_bytes: usize,
    total_lines: usize,
) {
    let metric = path.iter().fold(report, |value, key| &value[*key]);
    let expected_byte_fraction = expected_bytes as f64 / total_bytes as f64;
    let expected_line_fraction = expected_lines as f64 / total_lines as f64;
    assert!(
        (metric["bytes"].as_f64().expect("bytes fraction") - expected_byte_fraction).abs()
            < f64::EPSILON,
        "{path:?} byte fraction mismatch: {metric:#?}",
    );
    assert!(
        (metric["lines"].as_f64().expect("lines fraction") - expected_line_fraction).abs()
            < f64::EPSILON,
        "{path:?} line fraction mismatch: {metric:#?}",
    );
}

/// Look up `key` in a JSON pair-list (`[[k, v], …]`), returning the value.
/// Used for `DirectoryBoundarySummary` / `FileBoundarySummary` histograms
/// (`edge_count_by_kind`, `symbols`, `files`), which serialize as
/// `Vec<(String, usize)>` for perf but are conceptually maps in tests.
fn pair_list_get<'a>(array: &'a serde_json::Value, key: &str) -> Option<&'a serde_json::Value> {
    array.as_array()?.iter().find_map(|entry| {
        let pair = entry.as_array()?;
        if pair.first()?.as_str()? == key {
            pair.get(1)
        } else {
            None
        }
    })
}

// A mixed cycle whose lazy edge is only read at call time is accepted
// (`cases/initialization/dependent_first_cycle`); calling the lazy reader at
// initialization closes it. The original source is valid; splitting it is not.
#[test]
fn at_init_call_closes_mixed_cycle() {
    let rejected = run_rejection_fixture_with_graphs(FixtureOpts::new(
        r#"const A = 1;
const B = A + 1;
function readB() { return B; }
const trigger = readB();
console.log(trigger);
"#,
        vec![
            logical_module("mod_a", &binding_members(["A", "readB", "trigger"])),
            logical_module("mod_b", &[Member::new("B")]),
        ],
    ));
    let cycles: Vec<serde_json::Value> =
        read_json(&rejected.report_root.join("static/app/cycles.json"));
    assert!(!cycles.is_empty(), "the at-init call must close the cycle");
    for cycle in cycles {
        let cut = cycle["cut"].as_array().expect("cycle cut");
        assert!(!cut.is_empty());
        assert!(
            cut.iter().all(|edge| edge["kind"] != "lazy_use"),
            "lazy reads must not constrain initialization: {cycle}"
        );
    }
}

#[test]
fn dependency_graph_report_is_written_for_successful_specs() {
    let fixture = run_fixture_with_graphs(FixtureOpts::new(
        r#"const A = "a-value";
const B = "b-value";
function readA() { return A; }
function readB() { return B; }
console.log(readA(), readB());
export { A, B, readA, readB };
"#,
        vec![
            logical_module("mod_a", &binding_members(["A", "readB"])),
            logical_module("mod_b", &binding_members(["B", "readA"])),
        ],
    ));
    assert_entry_output(&fixture, "a-value b-value\n");

    let graph = fixture.dependency_graph();
    assert!(
        graph.nodes.len() >= 4,
        "dependency graph should expose source-source_unit nodes: {graph:#?}",
    );
    assert!(
        graph
            .nodes
            .iter()
            .any(|node| node.source_location.is_some()),
        "dependency graph should expose source locations for source-source_unit nodes: {graph:#?}",
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::LazyUse
                && edge.binding.as_deref() == Some("B")
                && !edge.constrains_init_order
        }),
        "dependency graph should expose lazy source_unit read edges: {graph:#?}",
    );
    assert!(
        graph
            .quotient
            .edges
            .iter()
            .any(|edge| { edge.edge_kinds.contains(&DepKind::LazyUse) }),
        "quotient edges should retain aggregated edge kinds: {graph:#?}",
    );
    assert!(
        graph
            .quotient
            .sccs
            .iter()
            .any(|scc| scc.is_cycle && scc.constraining_module_edge_ids.is_empty()),
        "lazy-only quotient SCC should have no initialization constraints: {graph:#?}",
    );
    assert!(
        graph.atomic_graph.nodes.iter().any(|atomic_group| {
            atomic_group
                .source_unit_ids
                .iter()
                .all(|source_unit_id| graph.nodes.iter().any(|node| node.id == *source_unit_id))
        }),
        "atomic graph should reference dependency graph nodes by id: {graph:#?}",
    );
    assert!(
        graph
            .atomic_graph
            .edges
            .iter()
            .all(|edge| edge.constrains_init_order),
        "atomic graph should contain only constraining DAG edges: {graph:#?}",
    );
    assert!(
        graph.atomic_graph.edges.iter().all(|edge| {
            edge.dependency_edge_ids.iter().all(|dependency_edge_id| {
                graph
                    .edges
                    .iter()
                    .any(|dependency_edge| dependency_edge.id == *dependency_edge_id)
            })
        }),
        "atomic graph edges should reference dependency graph edges by id: {graph:#?}",
    );
}

#[test]
fn dependency_graph_report_identifies_pair_only_residual_atomic_group() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"var A = B || "fallback";
var B = A + "-b";
const Existing = B + "-existing";
console.log(A, B, Existing);
export { A, B, Existing };
"#,
            vec![logical_module("mod_existing", &[Member::new("Existing")])],
        )
        .with_import_order(&["mod_existing", "residual"]),
    );
    assert_entry_output(&fixture, "fallback fallback-b fallback-b-existing\n");

    let graph = fixture.dependency_graph();
    assert!(
        graph.atomic_graph.nodes.iter().any(|atomic_group| {
            atomic_group.source_unit_ids.len() == 2
                && binding_names(&atomic_group.members) == vec!["A".to_string(), "B".to_string()]
                && atomic_group
                    .destinations
                    .iter()
                    .any(|destination| graph.is_unassigned_or_entry(destination))
        }),
        "atomic graph should collapse the residual A/B eager cycle into one atomic group: {graph:#?}",
    );
}

#[test]
fn dependency_graph_report_leaves_lazy_only_residual_dependency_as_separate_units() {
    let mut opts = FixtureOpts::new(
        r#"function Leaf() { return Dep; }
const Dep = "dep";
const Existing = "existing";
console.log(Existing);
export { Leaf, Dep, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.chunk_renames = Some(chunk_renames(&[
        ChunkRenameEntry::new("ReadableLeaf", "Leaf"),
        ChunkRenameEntry::new("ReadableDep", "Dep"),
    ]));
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture_with_graphs(opts);
    assert_entry_output(&fixture, "existing\n");

    let graph = fixture.dependency_graph();
    assert!(
        graph.atomic_graph.nodes.iter().any(|atomic_group| {
            atomic_group.source_unit_ids.len() == 1
                && atomic_group.members
                    == vec![BindingReport {
                        binding: "Leaf".into(),
                        export_name: "ReadableLeaf".into(),
                    }]
        }),
        "Leaf's lazy-only residual read should not merge it into Dep's atomic group: {graph:#?}",
    );
    assert!(
        graph.atomic_graph.edges.iter().all(|edge| {
            !edge.dependency_edge_ids.iter().any(|dependency_edge_id| {
                graph.edges.iter().any(|dependency_edge| {
                    dependency_edge.id == *dependency_edge_id
                        && dependency_edge.binding.as_deref() == Some("Dep")
                })
            })
        }),
        "non-constraining lazy residual read should not appear in the atomic DAG: {graph:#?}",
    );
}

#[test]
fn dependency_graph_report_collapses_residual_written_binding_with_assigner() {
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            r#"let a = 0;
function b() {
  a = 1;
}
const existing = "existing";
console.log(existing);
export { a, b, existing };
"#,
            vec![logical_module("existing", &[Member::new("existing")])],
        )
        .with_import_order(&["existing", "residual"]),
    );
    assert_entry_output(&fixture, "existing\n");

    let graph = fixture.dependency_graph();
    assert!(
        graph.atomic_graph.nodes.iter().any(|atomic_group| {
            atomic_group.source_unit_ids.len() == 2
                && binding_names(&atomic_group.members) == vec!["a".to_string(), "b".to_string()]
                && atomic_group
                    .destinations
                    .iter()
                    .any(|destination| graph.is_unassigned_or_entry(destination))
        }),
        "written residual binding a should share an atomic group with assigner b: {graph:#?}",
    );
}

#[test]
fn write_tree_manifests_include_output_metrics() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const A = "a-value";
const B = "b-value";
console.log(A, B);
export { A, B };
"#,
            vec![logical_module("mod_a", &[Member::new("A")])],
        )
        .with_import_order(&["mod_a", "residual"]),
    );
    assert_entry_output(&fixture, "a-value b-value\n");

    let manifest: serde_json::Value = read_json(&fixture.report_root.join("static/app/chunk.json"));
    // Wire-shape pin: the analysis-report fields stay flattened at the
    // manifest's top level (`ChunkManifest` embeds `ChunkAnalysisReport`
    // via `#[serde(flatten)]`), not nested under an `analysis` key.
    for key in ["chunk_id", "source_path", "entry_file", "counts", "files"] {
        assert!(
            manifest.get(key).is_some(),
            "chunk.json should carry analysis field {key:?} at top level",
        );
    }
    assert!(
        manifest.get("analysis").is_none(),
        "chunk.json must not nest the analysis report under an `analysis` key",
    );
    let entry_path = fixture.out_root.join("static/app/entry.js");
    let named_path = fixture.out_root.join("static/app/modules/mod_a.js");
    let residual_path = fixture
        .out_root
        .join("static/app/modules/residual/unhandled.js");
    let entry = file_size(&entry_path);
    let named = file_size(&named_path);
    let residual = file_size(&residual_path);
    let total = (
        entry.0 + named.0 + residual.0,
        entry.1 + named.1 + residual.1,
    );

    assert_size_metric(
        &manifest,
        &["output_metrics", "top_level_entry"],
        (1, entry.0, entry.1),
    );
    assert_size_metric(
        &manifest,
        &["output_metrics", "named_modules"],
        (1, named.0, named.1),
    );
    assert_size_metric(
        &manifest,
        &["output_metrics", "residual_modules"],
        (1, residual.0, residual.1),
    );
    assert_size_metric(&manifest, &["output_metrics", "other_files"], (0, 0, 0));
    assert_size_metric(
        &manifest,
        &["output_metrics", "total"],
        (3, total.0, total.1),
    );
    assert_fraction_metric(
        &manifest,
        &["output_metrics", "named_module_fraction"],
        named.0,
        named.1,
        total.0,
        total.1,
    );
    assert_fraction_metric(
        &manifest,
        &["output_metrics", "residual_module_fraction"],
        residual.0,
        residual.1,
        total.0,
        total.1,
    );
    assert_fraction_metric(
        &manifest,
        &["output_metrics", "top_level_entry_fraction"],
        entry.0,
        entry.1,
        total.0,
        total.1,
    );

    let files = manifest["output_metrics"]["largest_files_by_bytes"]
        .as_array()
        .expect("largest_files_by_bytes should be an array");
    assert_eq!(files.len(), 3);
    let by_file = files
        .iter()
        .map(|file| {
            (
                file["file"].as_str().expect("file metric path"),
                file.clone(),
            )
        })
        .collect::<std::collections::BTreeMap<_, _>>();
    assert_eq!(by_file["entry.js"]["role"], "top_level_entry");
    assert_eq!(by_file["entry.js"]["bytes"], entry.0);
    assert_eq!(by_file["modules/mod_a.js"]["role"], "named_module");
    assert_eq!(by_file["modules/mod_a.js"]["module"]["path"], "mod_a");
    assert_eq!(
        by_file["modules/mod_a.js"]["module"]["chunk_id"],
        "static/app"
    );
    assert_eq!(by_file["modules/mod_a.js"]["bytes"], named.0);
    assert_eq!(
        by_file["modules/residual/unhandled.js"]["role"],
        "residual_module",
    );
    assert_eq!(
        by_file["modules/residual/unhandled.js"]["module"]["path"],
        "residual/unhandled",
    );
    assert_eq!(
        by_file["modules/residual/unhandled.js"]["bytes"],
        residual.0,
    );

    let output_root = fixture.out_root.parent().expect("app root has output root");
    let root_manifest: serde_json::Value = read_json(&output_root.join("reports/output.json"));
    assert_size_metric(
        &root_manifest,
        &["output_metrics", "total"],
        (3, total.0, total.1),
    );
    let root_files = root_manifest["output_metrics"]["largest_files_by_bytes"]
        .as_array()
        .expect("root largest_files_by_bytes should be an array");
    assert!(root_files.iter().any(|file| {
        file["file"] == "static/app/modules/mod_a.js"
            && file["role"] == "named_module"
            && file["module"]["path"] == "mod_a"
    }));
}

#[test]
fn write_tree_emits_directory_dependency_manifests() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const Value = "v";
function consume() {
  return Value;
}
console.log(consume());
export { consume };
"#,
        vec![
            logical_module("domain/value", &[Member::new("Value")]),
            logical_module("feature/consumer", &[Member::new("consume")]),
        ],
    ));
    assert_entry_output(&fixture, "v\n");

    let index: serde_json::Value = read_json(&fixture.report_root.join("index.json"));
    let directories = index["directories"]
        .as_array()
        .expect("directory manifest index entries");
    assert!(directories.iter().any(|entry| entry == "static"));

    let feature: serde_json::Value = read_json(
        &fixture
            .report_root
            .join("static/app/modules/feature/index.json"),
    );
    assert_eq!(feature["path"], "static/app/modules/feature");
    assert_eq!(feature["outgoing"]["edge_count"], 1);
    assert_eq!(
        pair_list_get(
            &feature["outgoing"]["symbols"],
            "static/app/modules/domain/value.js#Value",
        )
        .expect("outgoing symbols entry"),
        1,
    );
    assert_eq!(
        pair_list_get(
            &feature["outgoing"]["files"],
            "static/app/modules/domain/value.js",
        )
        .expect("outgoing files entry"),
        1,
    );
    assert_eq!(
        pair_list_get(&feature["outgoing"]["edge_count_by_kind"], "lazy_use")
            .expect("outgoing edge_count_by_kind entry"),
        1,
    );
    assert_eq!(
        feature["outgoing"]["edges"][0]["target_dir"],
        "static/app/modules/domain",
    );

    let domain: serde_json::Value = read_json(
        &fixture
            .report_root
            .join("static/app/modules/domain/index.json"),
    );
    // Synchronous reads retain both dependency evidence and source-prefix ordering.
    assert_eq!(domain["incoming"]["edge_count"], 4);
    assert_eq!(
        pair_list_get(
            &domain["incoming"]["symbols"],
            "static/app/modules/domain/value.js#Value",
        )
        .expect("incoming symbols entry"),
        3,
    );
    assert_eq!(
        pair_list_get(
            &domain["incoming"]["files"],
            "static/app/modules/feature/consumer.js",
        )
        .expect("incoming files consumer entry"),
        1,
    );
    assert_eq!(
        pair_list_get(
            &domain["incoming"]["files"],
            "static/app/modules/residual/unhandled.js",
        )
        .expect("incoming files unhandled entry"),
        3,
    );

    let modules: serde_json::Value =
        read_json(&fixture.report_root.join("static/app/modules/index.json"));
    assert_eq!(modules["incoming"]["edge_count"], 0);
    assert_eq!(modules["outgoing"]["edge_count"], 0);
}

#[test]
fn dependency_graph_report_is_written_before_rejection() {
    // S-only cycle: three side-effecting writes interleaved across
    // mod_a (ordinal 0, 2) and mod_b (ordinal 1). The constraining-
    // edge subgraph has a bi-directional sequenced cycle that no
    // entry-import order can resolve.
    let rejected = run_rejection_fixture_with_graphs(FixtureOpts::new(
        r#"const a1 = (globalThis.tag = "a1", 1);
const b1 = (globalThis.tag = "b1", 2);
const a2 = (globalThis.tag = "a2", 3);
console.log(a1, a2, b1, globalThis.tag);
export { a1, a2, b1 };
"#,
        vec![
            logical_module("mod_a", &binding_members(["a1", "a2"])),
            logical_module("mod_b", &[Member::new("b1")]),
        ],
    ));
    assert!(
        rejected.stderr.contains("dependency_graph.json.gz"),
        "stderr should point at the dependency graph report:\n{}",
        rejected.stderr,
    );
    let graph = rejected.dependency_graph();
    assert!(
        graph
            .edges
            .iter()
            .any(|edge| { edge.edge_kind == DepKind::Sequenced && edge.binding.is_none() }),
        "side-effect edges must omit the binding: {graph:#?}"
    );
    assert!(
        rejected.report_root.join("static/app/cycles.json").exists(),
        "cycle report should be written before rejection",
    );
    // Wire-shape check: `cycles.json` is now the trimmed
    // `BlockingSccEntry` array — each entry has `id`, `modules`, `rejection`,
    // `cut` and `pass_one_witness`, with no full `evidence` block (recoverable via
    // `debundle gate describe`). See `validation.rs::BlockingSccEntry`
    // and `docs/wire_format.md`.
    let cycles: Vec<serde_json::Value> =
        read_json(&rejected.report_root.join("static/app/cycles.json"));
    assert!(!cycles.is_empty(), "at least one blocking SCC");
    for (i, entry) in cycles.iter().enumerate() {
        let cut = entry["cut"].as_array().expect("cycle cut");
        assert!(!cut.is_empty(), "a blocking cycle needs a cut: {entry}");
        let mut modules: Vec<&str> = entry["modules"]
            .as_array()
            .expect("SCC modules")
            .iter()
            .map(|module| module.as_str().expect("module path"))
            .collect();
        modules.sort_unstable();
        assert_eq!(modules, ["mod_a", "mod_b"], "{entry}");
        assert!(
            cut.iter().all(|edge| edge["kind"] == "sequenced"),
            "S-only cycle cut must contain only side-effect reasons: {entry}"
        );
        let obj = entry.as_object().expect("blocking-SCC entry is an object");
        let mut keys: Vec<&str> = obj.keys().map(String::as_str).collect();
        keys.sort();
        assert_eq!(
            keys,
            vec!["cut", "id", "modules", "pass_one_witness", "rejection"],
            "trimmed cycles.json entry should have only id/modules/cut/pass_one_witness/rejection; got {keys:?}",
        );
        assert_eq!(
            obj["id"].as_u64().expect("id is u64"),
            i as u64,
            "id should be the entry's index in cycles.json",
        );
        assert!(
            !obj.contains_key("evidence"),
            "evidence is recomputed on demand by `debundle gate describe`, not on disk"
        );
    }
    assert!(
        graph
            .quotient
            .sccs
            .iter()
            .any(|scc| { scc.is_cycle && !scc.constraining_module_edge_ids.is_empty() }),
        "unrealizable quotient SCC should be reported with constraining edge ids: {graph:#?}",
    );
}

// Acyclic cross-module at-init read correctness (Lemma 3's named
// pin) lives in `cases/initialization/computed_key_read`.

// --- Top-level await is rejected (docs/design.md A2) --------------------------
