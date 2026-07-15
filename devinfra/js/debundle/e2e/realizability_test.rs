//! Realizability gate (`I ∪ S` per <docs/design.md>): the
//! materializer accepts a spec and emits a behaviour-preserving
//! bundle iff every imports plus side-effect-ordering SCC is
//! realizable. Lazy-only import cycles are allowed; cycles with
//! at-init or side-effect-order edges are rejected. Each test feeds
//! a fixture spec and asserts either acceptance + entry-stdout
//! match, or rejection with cycle evidence naming the implicated
//! modules.

use std::fs;
use std::path::Path;

use analysis::{BindingReport, DepKind, OwnerGraphReport};
use debundle_e2e_support::*;

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

// --- R cycles (both back-edges at-init) ----------------------------------

#[test]
fn cyclic_spec_is_rejected_with_clear_error() {
    // mod_x = {A, D}: D = wrap(C) reads C from mod_y.
    // mod_y = {B, C}: B = wrap(A) reads A from mod_x.
    // Cycle: mod_x ↔ mod_y.
    let opts = FixtureOpts::new(
        r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const C = "c";
const D = wrap(C);
console.log(B.ref, D.ref);
export { A, B, C, D };
"#,
        vec![
            logical_module("mod_x", &[Member::new("A"), Member::new("D")]),
            logical_module("mod_y", &[Member::new("B"), Member::new("C")]),
        ],
    );
    expect_rejection(opts, &["cycle", "mod_x", "mod_y"]);
}

// --- I cycles via lazy back-edges ----------------------------------------

#[test]
fn mixed_cycle_with_lazy_back_edge_is_realizable_when_residual_imports_scc() {
    // mod_a imports B from mod_b (readB body's lazy read); mod_b
    // imports A from mod_a (B's eager initializer). The imports
    // graph `I` has a 2-cycle {mod_a, mod_b}; the constraining-
    // edge subgraph (drops LazyUse) is acyclic — only
    // mod_b → mod_a constrains init order.
    //
    // Residual reads `readB()` and re-exports A, B, readB, so
    // residual has direct I-edges into both SCC members. The
    // materializer's `source_import_position` reversal at
    // residual orders entry's imports as `[mod_b, mod_a]`; ESM
    // DFS enters mod_b → recurses into mod_a (eager) → mod_a's
    // lazy back-edge hits mod_b on the link stack (no-op) → mod_a
    // body evaluates with no TDZ → mod_b body sees A
    // initialized. Lemma 2 rescues. The companion
    // `mediator_reaches_asymmetric_cycle_test` exercises the
    // shape Lemma 2 cannot rescue (non-residual mediator into
    // SCC), and `runtime_tdz_on_imported_class_test` pins the
    // residual-in-cycle rejection.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const A = "a-value";
function readB() { return B; }
const B = A + "-postfix";
console.log(readB());
export { A, B, readB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A"), Member::new("readB")]),
            logical_module("mod_b", &[Member::new("B")]),
        ],
    ));
    assert_entry_output(&fixture, "a-value-postfix\n");
}

// The mutual lazy-only cycle acceptance (Lemma 4's named pin) lives
// in `lemma_four_lazy_read_cycle_test`.

#[test]
fn owner_graph_report_is_written_for_successful_specs() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const A = "a-value";
const B = "b-value";
function readA() { return A; }
function readB() { return B; }
console.log(readA(), readB());
export { A, B, readA, readB };
"#,
        vec![
            logical_module("mod_a", &[Member::new("A"), Member::new("readB")]),
            logical_module("mod_b", &[Member::new("B"), Member::new("readA")]),
        ],
    ));
    assert_entry_output(&fixture, "a-value b-value\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    assert!(
        graph.nodes.len() >= 4,
        "owner graph should expose source-owner nodes: {graph:#?}",
    );
    assert!(
        graph
            .nodes
            .iter()
            .any(|node| node.source_location.is_some()),
        "owner graph should expose source locations for source-owner nodes: {graph:#?}",
    );
    assert!(
        graph.edges.iter().any(|edge| {
            edge.edge_kind == DepKind::LazyUse
                && edge.binding.as_deref() == Some("B")
                && !edge.constrains_init_order
        }),
        "owner graph should expose lazy owner read edges: {graph:#?}",
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
            .any(|scc| scc.is_cycle && scc.realizable),
        "lazy-only quotient SCC should be reported as realizable: {graph:#?}",
    );
    assert!(
        graph.atomic_graph.nodes.iter().any(|unit| {
            unit.owner_ids
                .iter()
                .all(|owner_id| graph.nodes.iter().any(|node| node.id == *owner_id))
        }),
        "atomic graph should reference owner graph nodes by id: {graph:#?}",
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
            edge.owner_edge_ids.iter().all(|owner_edge_id| {
                graph
                    .edges
                    .iter()
                    .any(|owner_edge| owner_edge.id == *owner_edge_id)
            })
        }),
        "atomic graph edges should reference owner graph edges by id: {graph:#?}",
    );
}

#[test]
fn owner_graph_report_identifies_pair_only_residual_atomic_unit() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var A = B || "fallback";
var B = A + "-b";
const Existing = B + "-existing";
console.log(A, B, Existing);
export { A, B, Existing };
"#,
        vec![logical_module("mod_existing", &[Member::new("Existing")])],
    ));
    assert_entry_output(&fixture, "fallback fallback-b fallback-b-existing\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    assert!(
        graph.atomic_graph.nodes.iter().any(|unit| {
            unit.owner_ids.len() == 2
                && binding_names(&unit.members) == vec!["A".to_string(), "B".to_string()]
                && unit
                    .destinations
                    .iter()
                    .any(|destination| graph.is_residual(destination))
        }),
        "atomic graph should collapse the residual A/B eager cycle into one unit: {graph:#?}",
    );
}

#[test]
fn owner_graph_report_leaves_lazy_only_residual_dependency_as_separate_units() {
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
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    assert!(
        graph.atomic_graph.nodes.iter().any(|unit| {
            unit.owner_ids.len() == 1
                && unit.members
                    == vec![BindingReport {
                        binding: "Leaf".into(),
                        export_name: "ReadableLeaf".into(),
                    }]
        }),
        "Leaf's lazy-only residual read should not merge it into Dep's atomic unit: {graph:#?}",
    );
    assert!(
        graph.atomic_graph.edges.iter().all(|edge| {
            !edge.owner_edge_ids.iter().any(|owner_edge_id| {
                graph.edges.iter().any(|owner_edge| {
                    owner_edge.id == *owner_edge_id && owner_edge.binding.as_deref() == Some("Dep")
                })
            })
        }),
        "non-constraining lazy residual read should not appear in the atomic DAG: {graph:#?}",
    );
}

#[test]
fn owner_graph_report_collapses_residual_written_binding_with_assigner() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"let a = 0;
function b() {
  a = 1;
}
const existing = "existing";
console.log(existing);
export { a, b, existing };
"#,
        vec![logical_module("existing", &[Member::new("existing")])],
    ));
    assert_entry_output(&fixture, "existing\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    assert!(
        graph.atomic_graph.nodes.iter().any(|unit| {
            unit.owner_ids.len() == 2
                && binding_names(&unit.members) == vec!["a".to_string(), "b".to_string()]
                && unit
                    .destinations
                    .iter()
                    .any(|destination| graph.is_residual(destination))
        }),
        "written residual binding a should share an atomic unit with assigner b: {graph:#?}",
    );
}

#[test]
fn write_tree_manifests_include_output_metrics() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const A = "a-value";
const B = "b-value";
console.log(A, B);
export { A, B };
"#,
        vec![logical_module("mod_a", &[Member::new("A")])],
    ));
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
    assert_eq!(domain["incoming"]["edge_count"], 2);
    assert_eq!(
        pair_list_get(
            &domain["incoming"]["symbols"],
            "static/app/modules/domain/value.js#Value",
        )
        .expect("incoming symbols entry"),
        2,
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
        1,
    );

    let modules: serde_json::Value =
        read_json(&fixture.report_root.join("static/app/modules/index.json"));
    assert_eq!(modules["incoming"]["edge_count"], 0);
    assert_eq!(modules["outgoing"]["edge_count"], 0);
}

#[test]
fn owner_graph_report_is_written_before_rejection() {
    // S-only cycle: three side-effecting writes interleaved across
    // mod_a (ordinal 0, 2) and mod_b (ordinal 1). The constraining-
    // edge subgraph has a bi-directional sequenced cycle that no
    // entry-import order can resolve.
    let rejected = run_rejection_fixture(FixtureOpts::new(
        r#"const a1 = (globalThis.tag = "a1", 1);
const b1 = (globalThis.tag = "b1", 2);
const a2 = (globalThis.tag = "a2", 3);
console.log(a1, a2, b1, globalThis.tag);
export { a1, a2, b1 };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a1"), Member::new("a2")]),
            logical_module("mod_b", &[Member::new("b1")]),
        ],
    ));
    assert!(
        rejected.stderr.contains("owner graph at"),
        "stderr should point at the owner graph report:\n{}",
        rejected.stderr,
    );

    let graph: OwnerGraphReport =
        read_json(&rejected.report_root.join("static/app/owner_graph.json"));
    assert!(
        !rejected
            .report_root
            .join("static/app/factorization.json")
            .exists(),
        "factorization.json is folded into chunk.json / failure reports",
    );
    assert!(
        rejected.report_root.join("static/app/cycles.json").exists(),
        "cycle report should be written before rejection",
    );
    // Wire-shape check: `cycles.json` is now the trimmed
    // `BlockingSccEntry` array — each entry has `id`, `modules`, and
    // `cut` and **no `evidence` block** (recoverable on demand via
    // `debundle gate describe`). See `validation.rs::BlockingSccEntry`
    // and `docs/wire_format.md`.
    let cycles: Vec<serde_json::Value> =
        read_json(&rejected.report_root.join("static/app/cycles.json"));
    assert!(!cycles.is_empty(), "at least one blocking SCC");
    for (i, entry) in cycles.iter().enumerate() {
        let obj = entry.as_object().expect("blocking-SCC entry is an object");
        let mut keys: Vec<&str> = obj.keys().map(String::as_str).collect();
        keys.sort();
        assert_eq!(
            keys,
            vec!["cut", "id", "modules"],
            "trimmed cycles.json entry should have only id/modules/cut; got {keys:?}",
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
        graph.quotient.sccs.iter().any(|scc| {
            scc.is_cycle && !scc.realizable && !scc.constraining_module_edge_ids.is_empty()
        }),
        "unrealizable quotient SCC should be reported with constraining edge ids: {graph:#?}",
    );
}

// Acyclic cross-module at-init read correctness (Lemma 3's named
// pin) lives in `lemma_three_at_init_read_test`.

// --- Side-effect ordering (`S`) ------------------------------------------

#[test]
fn pure_const_decls_across_modules_dont_create_s_cycles() {
    // Pure literal initializers across mod_a/mod_b in interleaved
    // source order. A coarse `has_side_effect` would generate S
    // edges in both directions; `classify_expr_purity` sees these
    // as Pure and S stays empty.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a1 = 1;
const b1 = 2;
const a2 = "x";
const b2 = "y";
const a3 = { k: a1 };
const b3 = [b1, b2];
console.log(a1, a2, a3.k, b1, b2, b3[0]);
export { a1, a2, a3, b1, b2, b3 };
"#,
        vec![
            logical_module(
                "mod_a",
                &[Member::new("a1"), Member::new("a2"), Member::new("a3")],
            ),
            logical_module(
                "mod_b",
                &[Member::new("b1"), Member::new("b2"), Member::new("b3")],
            ),
        ],
    ));
    assert_entry_output(&fixture, "1 x 1 2 y 2\n");
}

#[test]
fn s_only_cycle_is_rejected() {
    // Three side-effecting `globalThis.tag = ...` writes
    // interleaved across mod_a (ord 0, 2) and mod_b (ord 1). No
    // R/I edges; S alone closes the cycle.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a1 = (globalThis.tag = "a1", 1);
const b1 = (globalThis.tag = "b1", 2);
const a2 = (globalThis.tag = "a2", 3);
console.log(a1, a2, b1, globalThis.tag);
export { a1, a2, b1 };
"#,
            vec![
                logical_module("mod_a", &[Member::new("a1"), Member::new("a2")]),
                logical_module("mod_b", &[Member::new("b1")]),
            ],
        ),
        // `side-effect` substring confirms the rejection comes
        // from S edges, not from a misclassified R/I edge.
        &["cycle", "mod_a", "mod_b", "side-effect"],
    );
}

#[test]
fn side_effect_owner_edges_do_not_use_binding_sentinels() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        r#"const a1 = (globalThis.tag = "a1", 1);
const b1 = (globalThis.tag = "b1", 2);
const a2 = (globalThis.tag = "a2", 3);
console.log(a1, a2, b1, globalThis.tag);
export { a1, a2, b1 };
"#,
        vec![
            logical_module("mod_a", &[Member::new("a1"), Member::new("a2")]),
            logical_module("mod_b", &[Member::new("b1")]),
        ],
    ));
    let graph: OwnerGraphReport =
        read_json(&rejected.report_root.join("static/app/owner_graph.json"));
    assert!(
        graph
            .edges
            .iter()
            .any(|edge| edge.edge_kind == DepKind::Sequenced && edge.binding.is_none()),
        "side-effect owner edges should omit binding rather than using a sentinel: {graph:#?}",
    );
}

// --- Per-declarator attribution across comma-list var-decls --------------

#[test]
fn comma_list_split_does_not_invent_cross_module_cycle() {
    // Without per-declarator attribution, `stmt_owner` picks A's
    // owner (mod_x) for the whole `const A = 1, B = X` and
    // attributes B's read of X to mod_x — inventing an
    // mod_x → mod_y edge that, combined with the real
    // mod_y → mod_x edge from `Y = a_in_x`, closes a cycle.
    // Pre-analysis split: B's row stands alone; X is in mod_y
    // (same module); no spurious edge.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const a_in_x = "x";
const X = 42;
const A = 1, B = X;
const Y = a_in_x;
console.log(A, B, X, Y);
export { A, B, X, Y, a_in_x };
"#,
        vec![
            logical_module("mod_x", &[Member::new("A"), Member::new("a_in_x")]),
            logical_module(
                "mod_y",
                &[Member::new("B"), Member::new("X"), Member::new("Y")],
            ),
        ],
    ));
    // Original chunk evaluates left-to-right: a_in_x="x", X=42,
    assert_entry_output(&fixture, "1 42 42 x\n");
}

#[test]
fn comma_list_split_surfaces_missed_cross_module_cycle() {
    // Real cycle: `B = a_in_x` in mod_y reads mod_x; `fromB = B`
    // in mod_x reads mod_y. Without per-declarator attribution,
    // pre-fix charges `a_in_x`'s read to mod_x (A's owner) — the
    // mod_y → mod_x edge disappears and validator accepts a spec
    // that TDZs at runtime. Pre-analysis split keeps B's row
    // attributed to mod_y; cycle re-surfaces.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const a_in_x = "x";
const A = 1, B = a_in_x;
const fromB = B;
console.log(A, B, fromB);
export { A, B, fromB, a_in_x };
"#,
            vec![
                logical_module(
                    "mod_x",
                    &[
                        Member::new("A"),
                        Member::new("a_in_x"),
                        Member::new("fromB"),
                    ],
                ),
                logical_module("mod_y", &[Member::new("B")]),
            ],
        ),
        &["cycle", "mod_x", "mod_y"],
    );
}

// --- Top-level await is rejected (docs/design.md A2) --------------------------

#[test]
fn top_level_await_is_rejected() {
    // `await` at module-top isn't covered by the realizability
    // theorem (A2). The materializer rejects before fact analysis
    // runs.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"async function fetchData() { return 42; }
const value = await fetchData();
console.log(value);
export { value };
"#,
            vec![logical_module("mod_x", &[Member::new("value")])],
        ),
        &["top-level", "await", "TLA"],
    );
}

#[test]
fn await_inside_async_function_is_allowed() {
    // `await` inside an async function body is fine — the lazy
    // boundary keeps the module synchronous.
    let fixture = run_fixture(FixtureOpts::new(
        r#"async function fetchData() {
  return await Promise.resolve(42);
}
const promise = fetchData();
promise.then((v) => console.log(v));
export { fetchData, promise };
"#,
        vec![logical_module(
            "mod_x",
            &[Member::new("fetchData"), Member::new("promise")],
        )],
    ));
    assert_entry_output(&fixture, "42\n");
}

#[test]
fn await_in_instance_class_field_is_allowed() {
    // Instance field initializers run on `new`, not at class-decl
    // time. An `await` there is *not* top-level. (Per spec the
    // host method must be `async` for the `await` to be syntactically
    // valid; we use a lazy method here.)
    let fixture = run_fixture(FixtureOpts::new(
        r#"class C {
  async run() { return await Promise.resolve("ok"); }
}
const c = new C();
c.run().then((v) => console.log(v));
export { C };
"#,
        vec![logical_module("mod_x", &[Member::new("C")])],
    ));
    assert_entry_output(&fixture, "ok\n");
}

#[test]
fn await_in_static_class_field_is_rejected() {
    // Static field initializers run at class-decl time. If the
    // class is at module-top, an `await` in a static field is
    // top-level. Wrap the await in an IIFE — `static x = await …`
    // is a SyntaxError outside async contexts, but the visitor
    // rejects any reachable AwaitExpr regardless of whether the
    // surrounding host is well-formed.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"async function f() { return 1; }
class C {
  static x = (async () => await f())();
  static y = await f();
}
console.log(C.x, C.y);
export { C };
"#,
            vec![logical_module(
                "mod_x",
                &[Member::new("C"), Member::new("f")],
            )],
        ),
        &["top-level", "await", "TLA"],
    );
}

#[test]
fn await_in_computed_class_method_key_is_rejected() {
    // Computed property keys are evaluated at class-decl time
    // (eager) regardless of `is_static`. An `await` there is
    // top-level.
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"async function k() { return "m"; }
class C {
  [await k()]() {}
}
console.log(C);
export { C };
"#,
            vec![logical_module(
                "mod_x",
                &[Member::new("C"), Member::new("k")],
            )],
        ),
        &["top-level", "await", "TLA"],
    );
}
