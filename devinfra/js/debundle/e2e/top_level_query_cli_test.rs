//! End-to-end exercise of the lifted top-level query commands:
//! `debundle atoms`, `coverage`, `graph-summary`, `describe <id>`,
//! `show-source <id>`, `modules propose`.
//!
//! Calls the library entry points directly (no binary execution) so
//! the test runs in-process and stays fast.

use std::fs;
use std::path::Path;

use analysis::{
    AtomicGraphReport, AtomicUnitReport, BindingReport, DepKind, ModuleEntry, OwnerGraphEdgeReport,
    OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport, Purity, QuotientSccReport,
    SourceLocation, StatementKind, StatementOrdinal,
};
use peel::plan::PatchPlanStatus;
use peel::{
    CommonArgs, ExplainArgs, GraphSummaryArgs, PatchPlanArgs, PlanWorkArgs, SelectionArgs,
    SourceSliceArgs, UnitsArgs, run_explain_report, run_graph_summary_report,
    run_patch_plan_report, run_plan_work_report, run_source_slice_report, run_units_report,
};
use report_fixtures::{module_ref, module_table};
use tempfile::TempDir;

fn write(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

fn member(binding: &str, export: &str) -> BindingReport {
    BindingReport {
        binding: binding.into(),
        export_name: export.into(),
    }
}

/// The module table for a graph whose only module is the residual
/// catch-all (these query fixtures put every owner in residual).
fn residual_table() -> Vec<ModuleEntry> {
    module_table([&module_ref("residual")])
}

fn owner(id: &str, ordinal: usize, binding: &str, export: &str) -> OwnerGraphNodeReport {
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
        destination: module_ref("residual"),
    }
}

fn anonymous_owner(id: &str, ordinal: usize) -> OwnerGraphNodeReport {
    OwnerGraphNodeReport {
        id: id.to_string(),
        statement_ordinal: StatementOrdinal(ordinal),
        source_location: Some(SourceLocation {
            source_path: "static/index.js".to_string(),
            start_line: ordinal + 1,
            end_line: ordinal + 1,
        }),
        declared_bindings: Vec::new(),
        statement_kind: StatementKind::SideEffect,
        purity: Purity::Pure,
        destination: module_ref("residual"),
    }
}

fn fixture() -> (TempDir, CommonArgs) {
    let dir = TempDir::new().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules_root = dir.path().join("spec/modules");
    let zz = owner("owner:0", 1, "ZZ", "ZZ");
    let aa = owner("owner:1", 2, "aa", "aa");
    let report = OwnerGraphReport {
        chunk_id: "static/index".to_string(),
        nodes: vec![zz.clone(), aa.clone()],
        edges: vec![OwnerGraphEdgeReport {
            id: "edge:0".to_string(),
            source: "owner:1".to_string(),
            target: "owner:0".to_string(),
            edge_kind: DepKind::EagerUse,
            binding: Some("ZZ".into()),
            statement_ordinal: StatementOrdinal(2),
            constrains_init_order: true,
            role: None,
        }],
        quotient: OwnerGraphQuotientReport {
            nodes: residual_table(),
            edges: Vec::new(),
            sccs: Vec::<QuotientSccReport>::new(),
        },
        atomic_graph: AtomicGraphReport {
            nodes: vec![
                AtomicUnitReport {
                    id: "atomic:0".to_string(),
                    owner_ids: vec!["owner:0".to_string()],
                    members: vec![member("ZZ", "ZZ")],
                    anonymous_statement_owner_ids: Vec::new(),
                    destinations: vec![module_ref("residual")],
                    causes: Vec::new(),
                    size_lines_estimate: 1,
                    source_line_range: Some([2, 2]),
                    ordinal_span: 0,
                },
                AtomicUnitReport {
                    id: "atomic:1".to_string(),
                    owner_ids: vec!["owner:1".to_string()],
                    members: vec![member("aa", "aa")],
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
    write(&graph_path, &serde_json::to_string(&report).unwrap());
    write(&modules_root.join(".keep"), "");
    write(
        &dir.path().join("static/index.js"),
        "const first = 1;\nconst ZZ = class PaymentError {};\nconst aa = ZZ;\n",
    );
    (
        dir,
        CommonArgs {
            owner_graph_path: graph_path,
            modules_root,
        },
    )
}

fn fixture_with_anonymous_statement_claim() -> (TempDir, CommonArgs) {
    let dir = TempDir::new().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules_root = dir.path().join("spec/modules");
    let class_owner = owner("owner:0", 1, "Co", "SearchPopoverState");
    let decorator_owner = anonymous_owner("owner:1", 2);
    let report = OwnerGraphReport {
        chunk_id: "static/index".to_string(),
        nodes: vec![class_owner, decorator_owner],
        edges: vec![OwnerGraphEdgeReport {
            id: "edge:0".to_string(),
            source: "owner:1".to_string(),
            target: "owner:0".to_string(),
            edge_kind: DepKind::LocalEffect,
            binding: Some("Co".into()),
            statement_ordinal: StatementOrdinal(2),
            constrains_init_order: true,
            role: None,
        }],
        quotient: OwnerGraphQuotientReport {
            nodes: residual_table(),
            edges: Vec::new(),
            sccs: Vec::<QuotientSccReport>::new(),
        },
        atomic_graph: AtomicGraphReport {
            nodes: vec![AtomicUnitReport {
                id: "atomic:0".to_string(),
                owner_ids: vec!["owner:0".to_string(), "owner:1".to_string()],
                members: vec![member("Co", "SearchPopoverState")],
                anonymous_statement_owner_ids: vec!["owner:1".to_string()],
                destinations: vec![module_ref("residual")],
                causes: vec![DepKind::LocalEffect],
                size_lines_estimate: 2,
                source_line_range: Some([2, 3]),
                ordinal_span: 1,
            }],
            edges: Vec::new(),
        },
    };
    write(&graph_path, &serde_json::to_string(&report).unwrap());
    write(
        &modules_root.join("features/search/popover_state.yaml"),
        r#"members:
  - name: SearchPopoverState
    selector:
      binding:
        name: Co
        kind: class_declaration
anonymous_statements:
  - match: 'Ro([Z], Co.prototype, "visible", 2);'
    note: "@observable visible on Co."
"#,
    );
    write(
        &dir.path().join("static/index.js"),
        "const ignored = 0;\nclass Co {}\nRo([Z], Co.prototype, \"visible\", 2);\n",
    );
    (
        dir,
        CommonArgs {
            owner_graph_path: graph_path,
            modules_root,
        },
    )
}

fn fixture_with_anonymous_only_module_claim() -> (TempDir, CommonArgs) {
    let dir = TempDir::new().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules_root = dir.path().join("spec/modules");
    let anonymous = anonymous_owner("owner:0", 0);
    let report = OwnerGraphReport {
        chunk_id: "static/index".to_string(),
        nodes: vec![anonymous.clone()],
        edges: Vec::new(),
        quotient: OwnerGraphQuotientReport {
            nodes: residual_table(),
            edges: Vec::new(),
            sccs: Vec::<QuotientSccReport>::new(),
        },
        atomic_graph: AtomicGraphReport {
            nodes: vec![AtomicUnitReport {
                id: "atomic:0".to_string(),
                owner_ids: vec![anonymous.id.clone()],
                members: Vec::new(),
                anonymous_statement_owner_ids: vec![anonymous.id.clone()],
                destinations: vec![module_ref("residual")],
                causes: Vec::new(),
                size_lines_estimate: 1,
                source_line_range: Some([1, 1]),
                ordinal_span: 0,
            }],
            edges: Vec::new(),
        },
    };
    write(&graph_path, &serde_json::to_string(&report).unwrap());
    write(
        &modules_root.join("auto_partition/auto_partition_0187.yaml"),
        r#"anonymous_statements:
  - match: 'registerSchema("task");'
"#,
    );
    write(
        &dir.path().join("static/index.js"),
        "registerSchema(\"task\");\n",
    );
    (
        dir,
        CommonArgs {
            owner_graph_path: graph_path,
            modules_root,
        },
    )
}

fn fixture_with_ambiguous_anonymous_statements() -> (TempDir, CommonArgs) {
    let dir = TempDir::new().unwrap();
    let graph_path = dir.path().join("owner_graph.json");
    let modules_root = dir.path().join("spec/modules");
    let first = anonymous_owner("owner:0", 0);
    let second = anonymous_owner("owner:1", 1);
    let report = OwnerGraphReport {
        chunk_id: "static/index".to_string(),
        nodes: vec![first.clone(), second.clone()],
        edges: Vec::new(),
        quotient: OwnerGraphQuotientReport {
            nodes: residual_table(),
            edges: Vec::new(),
            sccs: Vec::<QuotientSccReport>::new(),
        },
        atomic_graph: AtomicGraphReport {
            nodes: vec![
                AtomicUnitReport {
                    id: "atomic:0".to_string(),
                    owner_ids: vec![first.id.clone()],
                    members: Vec::new(),
                    anonymous_statement_owner_ids: vec![first.id.clone()],
                    destinations: vec![module_ref("residual")],
                    causes: Vec::new(),
                    size_lines_estimate: 1,
                    source_line_range: Some([1, 1]),
                    ordinal_span: 0,
                },
                AtomicUnitReport {
                    id: "atomic:1".to_string(),
                    owner_ids: vec![second.id.clone()],
                    members: Vec::new(),
                    anonymous_statement_owner_ids: vec![second.id.clone()],
                    destinations: vec![module_ref("residual")],
                    causes: Vec::new(),
                    size_lines_estimate: 1,
                    source_line_range: Some([2, 2]),
                    ordinal_span: 0,
                },
            ],
            edges: Vec::new(),
        },
    };
    write(&graph_path, &serde_json::to_string(&report).unwrap());
    write(&modules_root.join(".keep"), "");
    write(
        &dir.path().join("static/index.js"),
        "registerSchema(\"task\");\nregisterSchema(\"task\");\n",
    );
    (
        dir,
        CommonArgs {
            owner_graph_path: graph_path,
            modules_root,
        },
    )
}

#[test]
fn atoms_lists_units() {
    let (_dir, common) = fixture();
    let report = run_units_report(&UnitsArgs {
        common,
        limit: 0,
        residual_only: false,
        readable_only: false,
        by_destination: false,
        format: None,
    })
    .unwrap();
    assert_eq!(report.units.len(), 2);
}

#[test]
fn coverage_reports_summary() {
    let (_dir, common) = fixture();
    let report = run_patch_plan_report(&PatchPlanArgs {
        common,
        limit: 0,
        include_proposals: false,
        source_root: None,
        format: None,
    })
    .unwrap();
    // With no claimed modules, every atom shows up as a missing patch
    // set. Smoke-test that the summary has counts and the rows vector
    // is well-formed.
    assert_eq!(report.summary.total_patch_sets, report.rows.len());
    assert!(
        report
            .rows
            .iter()
            .all(|row| row.matching_proposal_ids.is_none())
    );
}

#[test]
fn coverage_counts_anonymous_statement_selectors_as_claims() {
    let (_dir, common) = fixture_with_anonymous_statement_claim();
    let report = run_patch_plan_report(&PatchPlanArgs {
        common,
        limit: 0,
        include_proposals: false,
        source_root: None,
        format: None,
    })
    .unwrap();

    assert_eq!(report.summary.total_patch_sets, 1);
    assert_eq!(report.summary.complete_patch_sets, 1);
    assert_eq!(report.summary.split_patch_sets, 0);
    let row = &report.rows[0];
    assert_eq!(row.path, "features/search/popover_state");
    assert_eq!(row.status, PatchPlanStatus::CompleteUnits);
    assert_eq!(row.complete_unit_ids, vec!["atomic:0"]);
    assert!(row.missing_anonymous_owner_ids.is_empty());
}

#[test]
fn graph_summary_reports_counts() {
    let (_dir, common) = fixture();
    let report = run_graph_summary_report(&GraphSummaryArgs {
        common,
        size_cap_lines: 10_000,
        limit: 10,
        include_proposals: false,
        source_root: None,
        format: None,
    })
    .unwrap();
    assert_eq!(report.owner_count, 2);
    assert_eq!(report.atomic_unit_count, 2);
    assert_eq!(report.proposal_count, None);
    assert_eq!(report.diagnostic_count, None);
}

#[test]
fn modules_propose_emits_plan_work_report() {
    let (_dir, common) = fixture();
    let report = run_plan_work_report(&PlanWorkArgs {
        common,
        size_cap_lines: 10_000,
        source_root: None,
        limit: 0,
        format: None,
    })
    .unwrap();
    // Smoke-test: a fresh fixture with two atoms and a single edge
    // should produce at least one proposal.
    assert!(!report.report.proposals.is_empty());
}

#[test]
fn modules_propose_marks_duplicate_full_ast_anonymous_statements_advisory() {
    let (dir, common) = fixture_with_ambiguous_anonymous_statements();
    let report = run_plan_work_report(&PlanWorkArgs {
        common,
        size_cap_lines: 10_000,
        source_root: Some(dir.path().to_path_buf()),
        limit: 0,
        format: None,
    })
    .unwrap();

    assert_eq!(report.report.proposals.len(), 2);
    assert!(report.report.proposals.iter().all(|proposal| {
        !proposal.landable_today
            && proposal.unaddressable_anonymous_owner_ids.len() == 1
            && proposal
                .landability_notes
                .iter()
                .any(|note| note.contains("full-AST selector"))
    }));
}

#[test]
fn describe_binding_resolves_via_selection() {
    let (_dir, common) = fixture();
    let report = run_explain_report(&ExplainArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: None,
            module_id: None,
            binding_id: Some("ZZ".to_string()),
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
    .unwrap();
    assert_eq!(report.owner_ids, vec!["owner:0"]);
    assert_eq!(report.atomic_units[0].id, "atomic:0");
}

#[test]
fn describe_module_id_resolves_all_module_owners() {
    let (_dir, common) = fixture();
    let report = run_explain_report(&ExplainArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: None,
            module_id: Some("residual".to_string()),
            binding_id: None,
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
    .unwrap();
    assert_eq!(report.query.kind, peel::plan::QueryKind::Module);
    assert_eq!(report.owner_ids, vec!["owner:0", "owner:1"]);
}

#[test]
fn describe_module_path_resolves_anonymous_only_module_claim() {
    let (dir, common) = fixture_with_anonymous_only_module_claim();
    let report = run_explain_report(&ExplainArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: Some("auto_partition/auto_partition_0187".to_string()),
            module_id: None,
            binding_id: None,
            proposal_id: None,
            unit_id: None,
            diagnostic_id: None,
        },
        size_cap_lines: 10_000,
        source_root: Some(dir.path().to_path_buf()),
        limit: 0,
        include_proposals: false,
        format: None,
    })
    .unwrap();

    assert_eq!(report.query.kind, peel::plan::QueryKind::Module);
    assert_eq!(report.owner_ids, vec!["owner:0"]);
    assert_eq!(report.atomic_units[0].id, "atomic:0");
    assert_eq!(
        report.atomic_units[0].anonymous_statement_owner_ids,
        vec!["owner:0"]
    );
}

#[test]
fn show_source_binding_resolves_via_selection() {
    let (dir, common) = fixture();
    let report = run_source_slice_report(&SourceSliceArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: None,
            module_id: None,
            binding_id: Some("ZZ".to_string()),
            proposal_id: None,
            unit_id: None,
            diagnostic_id: None,
        },
        size_cap_lines: 10_000,
        context_lines: 1,
        source_root: Some(dir.path().to_path_buf()),
        format: None,
    })
    .unwrap();
    assert_eq!(report.slices.len(), 1);
    assert!(report.slices[0].text.contains("class PaymentError"));
}

#[test]
fn show_source_module_path_resolves_anonymous_only_module_claim() {
    let (dir, common) = fixture_with_anonymous_only_module_claim();
    let report = run_source_slice_report(&SourceSliceArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: Some("auto_partition/auto_partition_0187".to_string()),
            module_id: None,
            binding_id: None,
            proposal_id: None,
            unit_id: None,
            diagnostic_id: None,
        },
        size_cap_lines: 10_000,
        context_lines: 0,
        source_root: Some(dir.path().to_path_buf()),
        format: None,
    })
    .unwrap();

    assert_eq!(report.slices.len(), 1);
    assert!(report.slices[0].text.contains("registerSchema(\"task\");"));
}

#[test]
fn show_source_missing_proposal_reports_stale_id() {
    let (dir, common) = fixture();
    let err = run_source_slice_report(&SourceSliceArgs {
        common,
        selection: SelectionArgs {
            owner_id: None,
            module_path: None,
            module_id: None,
            binding_id: None,
            proposal_id: Some("auto_partition_0499".to_string()),
            unit_id: None,
            diagnostic_id: None,
        },
        size_cap_lines: 10_000,
        context_lines: 0,
        source_root: Some(dir.path().to_path_buf()),
        format: None,
    })
    .unwrap_err();

    let message = format!("{err:#}");
    assert!(message.contains("proposal id \"auto_partition_0499\" not found"));
    assert!(message.contains("debundle modules propose"));
}
