//! Realizability gate (`I ∪ S` per <DESIGN.md>): the
//! materializer accepts a spec and emits a behaviour-preserving
//! bundle iff every imports plus side-effect-ordering SCC is
//! realizable. Lazy-only import cycles are allowed; cycles with
//! at-init or side-effect-order edges are rejected. Each test feeds
//! a fixture spec and asserts either acceptance + entry-stdout
//! match, or rejection with cycle evidence naming the implicated
//! modules.

use analysis::{BindingReport, DepKind, OwnerGraphReport, ResidualOwnerPeelStatus};
use debundle_e2e_support::*;
use serde::de::DeserializeOwned;
use serde_json::json;
use std::{fs, path::Path};

fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

fn binding_names(members: &[BindingReport]) -> Vec<String> {
    members
        .iter()
        .map(|member| member.binding.clone())
        .collect()
}

fn binding_report(binding: &str, export_name: &str) -> BindingReport {
    BindingReport {
        binding: binding.to_string(),
        export_name: export_name.to_string(),
    }
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
            (
                "mod_x".to_string(),
                json!({
                    "members": [
                        { "name": "A", "selector": { "binding": { "name": "A" } } },
                        { "name": "D", "selector": { "binding": { "name": "D" } } },
                    ],
                }),
            ),
            (
                "mod_y".to_string(),
                json!({
                    "members": [
                        { "name": "B", "selector": { "binding": { "name": "B" } } },
                        { "name": "C", "selector": { "binding": { "name": "C" } } },
                    ],
                }),
            ),
        ],
    );
    expect_rejection(opts, &["cycle", "mod_x", "mod_y"]);
}

// --- I cycles via lazy back-edges ----------------------------------------

#[test]
fn rejects_cycle_through_lazy_back_edge() {
    // mod_b reads A from mod_a at-init (B's initializer);
    // mod_a's `readB` body reads B from mod_b lazily. `R` is
    // acyclic by itself, but the SCC `{mod_a, mod_b}` in `I ∪ S`
    // contains the at-init `mod_b → mod_a` edge — that single
    // `R` cross-module edge inside the SCC is enough for the
    // realizability gate to bail. (Without the bail the linker
    // would TDZ on B's initializer.)
    expect_rejection_containing_all(
        FixtureOpts::new(
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
        ),
        &["cycle", "mod_a", "mod_b"],
    );
}

#[test]
fn accepts_cycle_when_all_back_edges_are_lazy() {
    // mod_a owns A and readB; readB() lazily reads B.
    // mod_b owns B and readA; readA() lazily reads A.
    // No cross-module read fires at-init: when the linker
    // evaluates either module, no top-level statement reaches
    // into the other one. The function bodies only run *after*
    // both modules finish evaluating — no TDZ.
    //
    // The SCC in the imports graph `I` is `{mod_a, mod_b}`, but
    // it carries no at-init (`R`) and no side-effect (`S`)
    // cross-module edges — only `L` edges. The realizability
    // gate must accept this spec; rejecting would over-restrict
    // the realizable subset of `I ∪ S` cycles.
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
    // ESM evaluates both modules to completion before the
    // residual entry's `console.log(readA(), readB())` fires;
    // both function bodies see fully-assigned bindings.
    assert_entry_output(&fixture, "a-value b-value\n");
}

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
        graph.peelability.residual_owner_horizon.is_empty(),
        "fixture has no declared residual binding to peel: {graph:#?}",
    );
    assert!(
        graph.peelability.minimal_peel_sets.is_empty(),
        "fixture has no residual peel set: {graph:#?}",
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

    let manifest: serde_json::Value = read_json(&fixture.out_root.join("static/app/manifest.json"));
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
    assert_eq!(by_file["modules/mod_a.js"]["module_path"], "mod_a");
    assert_eq!(by_file["modules/mod_a.js"]["bytes"], named.0);
    assert_eq!(
        by_file["modules/residual/unhandled.js"]["role"],
        "residual_module",
    );
    assert_eq!(
        by_file["modules/residual/unhandled.js"]["module_path"],
        "residual/unhandled",
    );
    assert_eq!(
        by_file["modules/residual/unhandled.js"]["bytes"],
        residual.0,
    );

    let root_manifest: serde_json::Value = read_json(&fixture.out_root.join("manifest.json"));
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
            && file["module_path"] == "mod_a"
    }));
}

#[test]
fn owner_graph_report_identifies_pair_only_residual_peel_in_emitted_js_fixture() {
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
    let peelability = &graph.peelability;
    for binding in ["A", "B"] {
        assert!(
            peelability.residual_owner_horizon.iter().any(|owner| {
                binding_names(&owner.members) == vec![binding.to_string()]
                    && owner.status == ResidualOwnerPeelStatus::WithCompanions
                    && owner.current_destination.residual
                    && owner.source_location.is_some()
            }),
            "{binding} should be classified as peelable only with companions: {graph:#?}",
        );
    }
    assert!(
        peelability.minimal_peel_sets.iter().any(|closure| {
            binding_names(&closure.members) == vec!["A".to_string(), "B".to_string()]
                && closure.owner_ids.len() == 2
        }),
        "pair-only peelability should be summarized in minimal_peel_sets: {graph:#?}",
    );
}

#[test]
fn owner_graph_report_allows_lazy_only_residual_dependency_peel_candidate() {
    let mut opts = FixtureOpts::new(
        r#"function Leaf() { return Dep; }
const Dep = "dep";
const Existing = "existing";
console.log(Existing);
export { Leaf, Dep, Existing };
"#,
        vec![logical_module("existing", &[Member::new("Existing")])],
    );
    opts.chunk_renames = Some(json!({
        "id": "chunk_renames__static_app",
        "members": [
            { "name": "ReadableLeaf", "selector": { "binding": { "name": "Leaf" } } },
            { "name": "ReadableDep", "selector": { "binding": { "name": "Dep" } } }
        ],
    }));
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "existing\n");

    let graph: OwnerGraphReport =
        read_json(&fixture.report_root.join("static/app/owner_graph.json"));
    let peelability = &graph.peelability;
    assert!(
        peelability.residual_owner_horizon.iter().any(|owner| {
            owner.members == vec![binding_report("Leaf", "ReadableLeaf")]
                && owner.status == ResidualOwnerPeelStatus::Direct
                && owner.current_destination.residual
                && owner.statement_ordinal.0 == 0
        }),
        "Leaf's only cross-edge to residual is a lazy read, so it should be Direct-peelable: {graph:#?}",
    );
    assert!(
        peelability.minimal_peel_sets.iter().any(|closure| {
            closure.owner_ids.len() == 1
                && closure.members == vec![binding_report("Leaf", "ReadableLeaf")]
        }),
        "singleton {{Leaf}} should be in minimal_peel_sets: {graph:#?}",
    );
}

#[test]
fn owner_graph_report_does_not_offer_singleton_peel_for_residual_written_binding() {
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
    let peelability = &graph.peelability;
    assert!(
        peelability.residual_owner_horizon.iter().any(|owner| {
            binding_names(&owner.members) == vec!["a".to_string()]
                && owner.status == ResidualOwnerPeelStatus::WithCompanions
                && owner
                    .companion_options
                    .iter()
                    .any(|option| binding_names(&option.companion_members) == vec!["b".to_string()])
        }),
        "a must require its residual assigner b as a companion peel: {graph:#?}",
    );
    assert!(
        !peelability.minimal_peel_sets.iter().any(|candidate| {
            candidate.owner_ids.len() == 1
                && binding_names(&candidate.members) == vec!["a".to_string()]
        }),
        "peelability must not propose extracting only written binding a: {graph:#?}",
    );
    assert!(
        peelability.minimal_peel_sets.iter().any(|candidate| {
            candidate.owner_ids.len() == 2
                && binding_names(&candidate.members) == vec!["a".to_string(), "b".to_string()]
        }),
        "a+b should be the minimal safe peel set: {graph:#?}",
    );
}

#[test]
fn owner_graph_report_is_written_before_rejection() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
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
    assert!(
        rejected.stderr.contains("owner graph written"),
        "stderr should point at the owner graph report:\n{}",
        rejected.stderr,
    );

    let graph: OwnerGraphReport =
        read_json(&rejected.report_root.join("static/app/owner_graph.json"));
    assert!(
        rejected
            .report_root
            .join("static/app/schedule.json")
            .exists(),
        "schedule report should be written alongside owner graph",
    );
    assert!(
        rejected.report_root.join("static/app/cycles.json").exists(),
        "cycle report should be written before rejection",
    );
    assert!(
        graph.quotient.sccs.iter().any(|scc| {
            scc.is_cycle && !scc.realizable && !scc.constraining_module_edge_ids.is_empty()
        }),
        "unrealizable quotient SCC should be reported with constraining edge ids: {graph:#?}",
    );
}

// --- Acyclic specs and cross-module init order ----------------------------

#[test]
fn init_call_order_respects_cross_module_dependency() {
    // mod_a owns x1 + x2 (x2 reads y.id at-init); mod_b owns y.
    // R-edge mod_a → mod_b; ESM linker evaluates mod_b first.
    let fixture = run_fixture(FixtureOpts::new(
        r#"const x1 = { id: "x1" };
const y = { id: "k" };
const x2 = { [y.id]: "v" };
console.log(x1.id, y.id, x2[y.id]);
export { x1, y, x2 };
"#,
        vec![
            (
                "mod_a".to_string(),
                json!({
                    "members": [
                        { "name": "x1", "selector": { "binding": { "name": "x1" } } },
                        { "name": "x2", "selector": { "binding": { "name": "x2" } } },
                    ],
                }),
            ),
            (
                "mod_b".to_string(),
                json!({
                    "members": [
                        { "name": "y", "selector": { "binding": { "name": "y" } } },
                    ],
                }),
            ),
        ],
    ));
    assert_entry_output(&fixture, "x1 k v\n");
}

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

// --- Top-level await is rejected (DESIGN.md A2) --------------------------

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
