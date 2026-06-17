//! End-to-end coverage for the shared minified+readable binding-name
//! resolver (`peel::resolve_binding_owners`) used by `debundle
//! describe`, `show-source`, `cluster`, and `scc --binding`.
//!
//! Pins the `CLI_DOGFOOD.md` binding-name-resolution contract: all
//! top-level graph/source query verbs share one helper; minified and
//! readable forms work, both forms produce the same owner ids, and the
//! minified form wins on the rare cross-binding spell collision.
//!
//! Calls the library entry points directly so the test runs in-process
//! and doesn't depend on the built debundler binary.

use std::fs;
use std::path::Path;

use analysis::{
    AtomicGraphReport, AtomicUnitReport, BindingReport, DepKind, OwnerGraphEdgeReport,
    OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport, Purity, QuotientEdgeReport,
    QuotientSccReport, SourceLocation, StatementKind, StatementOrdinal,
};
use debundle_e2e_support::write_text_file;
use peel::{
    CommonArgs, ExplainArgs, SelectionArgs, SourceSliceArgs, resolve_binding_owners,
    run_explain_report, run_source_slice_report,
};
use report_fixtures::{module_entry, module_ref};
use tempfile::TempDir;

fn member(binding: &str, export: &str) -> BindingReport {
    BindingReport {
        binding: binding.into(),
        export_name: export.into(),
    }
}

fn owner(
    id: &str,
    ordinal: usize,
    binding: &str,
    export: &str,
    destination: analysis::ModuleKey,
) -> OwnerGraphNodeReport {
    OwnerGraphNodeReport {
        id: id.to_string(),
        statement_ordinal: StatementOrdinal(ordinal),
        source_location: Some(SourceLocation {
            source_path: "static/index.js".to_string(),
            start_line: ordinal + 1,
            end_line: ordinal + 1,
        }),
        declared_bindings: vec![member(binding, export)],
        statement_kind: StatementKind::VarDecl,
        purity: Purity::Pure,
        destination,
    }
}

/// Build a fixture where:
///   * `owner:0` declares minified `XOe`, renamed to readable
///     `PluginSettingsAccessor` — home `logical:ui/plugins`.
///   * `owner:1` declares minified `YOe` (no rename) — home
///     `logical:residual`.
fn renamed_fixture() -> (TempDir, CommonArgs) {
    let dir = TempDir::new().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules_root = dir.path().join("spec/modules");
    let plugins = owner(
        "owner:0",
        1,
        "XOe",
        "PluginSettingsAccessor",
        module_ref("ui/plugins"),
    );
    let other = owner("owner:1", 2, "YOe", "YOe", module_ref("residual"));
    let quotient = OwnerGraphQuotientReport {
        nodes: vec![
            module_entry(&module_ref("ui/plugins")),
            module_entry(&module_ref("residual")),
        ],
        edges: vec![QuotientEdgeReport {
            id: "q_edge:0".to_string(),
            source: module_ref("residual"),
            target: module_ref("ui/plugins"),
            edge_kinds: vec![DepKind::EagerUse],
            constrains_init_order: true,
        }],
        sccs: Vec::<QuotientSccReport>::new(),
    };
    let report = OwnerGraphReport {
        chunk_id: "static/index".to_string(),
        nodes: vec![plugins, other],
        edges: vec![OwnerGraphEdgeReport {
            id: "edge:0".to_string(),
            source: "owner:1".to_string(),
            target: "owner:0".to_string(),
            edge_kind: DepKind::EagerUse,
            binding: Some("XOe".into()),
            statement_ordinal: StatementOrdinal(2),
            constrains_init_order: true,
            role: None,
        }],
        quotient,
        atomic_graph: AtomicGraphReport {
            nodes: vec![
                AtomicUnitReport {
                    id: "atomic:0".to_string(),
                    owner_ids: vec!["owner:0".to_string()],
                    members: vec![member("XOe", "PluginSettingsAccessor")],
                    anonymous_statement_owner_ids: Vec::new(),
                    destinations: vec![module_ref("ui/plugins")],
                    causes: Vec::new(),
                    size_lines_estimate: 1,
                    source_line_range: Some([2, 2]),
                    ordinal_span: 0,
                },
                AtomicUnitReport {
                    id: "atomic:1".to_string(),
                    owner_ids: vec!["owner:1".to_string()],
                    members: vec![member("YOe", "YOe")],
                    anonymous_statement_owner_ids: Vec::new(),
                    destinations: vec![module_ref("residual")],
                    causes: Vec::new(),
                    size_lines_estimate: 1,
                    source_line_range: Some([3, 3]),
                    ordinal_span: 0,
                },
            ],
            edges: Vec::new(),
        },
    };
    write_text_file(&graph_path, &serde_json::to_string(&report).unwrap());
    write_text_file(&modules_root.join(".keep"), "");
    write_text_file(
        &dir.path().join("static/index.js"),
        "const first = 1;\n\
         const XOe = class PluginSettingsAccessor {};\n\
         const YOe = XOe;\n",
    );
    (
        dir,
        CommonArgs {
            owner_graph_path: graph_path,
            modules_root,
        },
    )
}

fn explain_with_binding(common: CommonArgs, sym: &str) -> peel::ExplainReport {
    run_explain_report(&ExplainArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: None,
            module_id: None,
            binding_id: Some(sym.to_string()),
            proposal_id: None,
            unit_id: None,
            diagnostic_id: None,
        },
        size_cap_lines: 10_000,
        source_root: None,
        limit: 0,
        include_proposals: false,
        format: None,
    })
    .expect("explain report")
}

fn show_source_with_binding(
    common: CommonArgs,
    sym: &str,
    source_root: &Path,
) -> peel::SourceSliceReport {
    run_source_slice_report(&SourceSliceArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: None,
            module_id: None,
            binding_id: Some(sym.to_string()),
            proposal_id: None,
            unit_id: None,
            diagnostic_id: None,
        },
        size_cap_lines: 10_000,
        context_lines: 1,
        source_root: Some(source_root.to_path_buf()),
        format: None,
    })
    .expect("source slice report")
}

#[test]
fn describe_accepts_readable_name() {
    // CLI_DOGFOOD.md contract: readable names resolve to the same
    // owner as the minified form.
    let (_dir, common) = renamed_fixture();
    let report = explain_with_binding(common, "PluginSettingsAccessor");
    assert_eq!(report.owner_ids, vec!["owner:0"]);
}

#[test]
fn describe_accepts_minified_name() {
    // Regression guard: the helper extraction didn't break the
    // pre-existing minified-name path.
    let (_dir, common) = renamed_fixture();
    let report = explain_with_binding(common, "XOe");
    assert_eq!(report.owner_ids, vec!["owner:0"]);
}

#[test]
fn show_source_accepts_readable_name() {
    // Companion to describe: `show-source` used to reject readable
    // names with the same error. After the fix it returns the same
    // source slice the minified form would.
    let (dir, common) = renamed_fixture();
    let report = show_source_with_binding(common, "PluginSettingsAccessor", dir.path());
    assert_eq!(report.slices.len(), 1);
    assert!(report.slices[0].text.contains("PluginSettingsAccessor"));
}

#[test]
fn show_source_accepts_minified_name() {
    // Regression guard: the helper extraction didn't break the
    // pre-existing minified-name path for show-source either.
    let (dir, common) = renamed_fixture();
    let report = show_source_with_binding(common, "XOe", dir.path());
    assert_eq!(report.slices.len(), 1);
    assert!(report.slices[0].text.contains("PluginSettingsAccessor"));
}

#[test]
fn resolve_binding_owners_matches_both_name_forms() {
    // Regression guard for `cluster` / `scc --binding`: the shared
    // helper that powers all four verbs must return the same owner
    // for both name forms in the unambiguous case.
    let (_dir, common) = renamed_fixture();
    let graph: OwnerGraphReport =
        serde_json::from_str(&fs::read_to_string(&common.owner_graph_path).unwrap()).unwrap();
    let by_minified = resolve_binding_owners(&graph, "XOe");
    let by_readable = resolve_binding_owners(&graph, "PluginSettingsAccessor");
    assert_eq!(by_minified.len(), 1);
    assert_eq!(by_readable.len(), 1);
    assert_eq!(by_minified[0].id, "owner:0");
    assert_eq!(by_readable[0].id, "owner:0");
}

#[test]
fn resolve_binding_owners_prefers_minified_on_name_collision() {
    // Disambiguation contract: if one owner's minified name and a
    // different owner's readable name both spell the same string,
    // the minified-name match wins the slice ordering (back-compat
    // with the pre-fix behavior, which never saw readable matches).
    // Both still appear so callers that want to detect ambiguity can.
    let dir = TempDir::new().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules_root = dir.path().join("spec/modules");
    let by_min = owner(
        "owner:minified",
        1,
        "Collide",
        "Collide",
        module_ref("ui/plugins"),
    );
    let by_readable = owner(
        "owner:readable",
        2,
        "ZZZ",
        "Collide",
        module_ref("residual"),
    );
    let report = OwnerGraphReport {
        chunk_id: "static/index".to_string(),
        nodes: vec![by_min, by_readable],
        edges: Vec::new(),
        quotient: OwnerGraphQuotientReport {
            nodes: Vec::new(),
            edges: Vec::new(),
            sccs: Vec::new(),
        },
        atomic_graph: AtomicGraphReport {
            nodes: Vec::new(),
            edges: Vec::new(),
        },
    };
    write_text_file(&graph_path, &serde_json::to_string(&report).unwrap());
    write_text_file(&modules_root.join(".keep"), "");

    let owners = resolve_binding_owners(&report, "Collide");
    assert_eq!(owners.len(), 2, "both collide entries should appear");
    assert_eq!(
        owners[0].id, "owner:minified",
        "minified match wins ordering for back-compat"
    );
    assert_eq!(
        owners[1].id, "owner:readable",
        "readable match still surfaces so callers can detect ambiguity"
    );
}
