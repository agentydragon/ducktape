//! Build a parseable inventory of peelable bindings from a debundle
//! `owner_graph.json` plus the spec-root `binding_patches.yaml`.
//!
//! For each candidate in `peelability.minimal_peel_sets[]`, emit a record
//! with everything an agent needs to assign a destination — without
//! re-deriving from the graph:
//!
//! - `members` : `[(input_binding, export_name)]`. `export_name` differs
//!   from `binding` when the rename queue has already named it readably.
//! - `has_readable` : at least one member has a renamed export name.
//! - `patch_files` : non-emitting patch files that currently list any of
//!   the candidate's input bindings.
//! - `primary_source` : first `patch_files` entry, or `<residual_only>`
//!   if none.
//! - `source_lines` : `(start_line, end_line)` aggregated from
//!   `peelability.residual_owner_horizon[].source_location` for the
//!   candidate's owners — useful for grouping co-located bindings.
//! - `proposed_dir` : best-guess destination directory derived from the
//!   readable export name and `primary_source`. Hint, not a rule.
//! - `forbidden` : list of cycle / residual-dependency blockers from
//!   `peelability.evaluated_owner_sets[]` (when present in the graph)
//!   that prevent specific destinations. Empty for clean direct peels.
//!
//! This module is generic over `owner_graph.json` — it knows nothing
//! about any specific bundle, only the debundler's own
//! report schema and binding patch authoring convention.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use analysis::OwnerGraphReport;
use spec::DEFAULT_RESIDUAL_MODULE_PATH;
use spec_modules::load_binding_patch_members;

#[derive(Debug, Clone)]
pub struct PeelInventoryOptions {
    pub owner_graph_path: PathBuf,
    pub modules_root: PathBuf,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PeelInventoryRecord {
    pub candidate_id: String,
    pub owner_count: usize,
    /// `(input_binding, export_name)` pairs.
    pub members: Vec<(String, String)>,
    pub has_readable: bool,
    pub patch_files: Vec<String>,
    pub primary_source: String,
    /// `[start_line, end_line]` aggregated across the candidate's owners,
    /// or `None` if no owner has a `source_location`.
    pub source_lines: Option<(usize, usize)>,
    pub proposed_dir: String,
    pub forbidden: Vec<ForbiddenRecord>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum ForbiddenRecord {
    BlockedCycle { blocking_edges: Vec<String> },
    ResidualDependency { missing: Vec<String> },
}

/// `peelability.evaluated_owner_sets[]` entries.
#[derive(Debug, Clone, Deserialize)]
struct EvaluatedOwnerSet {
    #[serde(default)]
    status: String,
    #[serde(default)]
    owner_ids: Vec<String>,
    #[serde(default)]
    cycle_blockers: Vec<String>,
    #[serde(default)]
    residual_dependency_blockers: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct EvaluatedSetsRoot {
    peelability: EvaluatedSetsPeelability,
}

#[derive(Debug, Clone, Deserialize)]
struct EvaluatedSetsPeelability {
    #[serde(default)]
    evaluated_owner_sets: Vec<EvaluatedOwnerSet>,
}

pub fn build_inventory(options: &PeelInventoryOptions) -> Result<Vec<PeelInventoryRecord>> {
    let body = fs::read_to_string(&options.owner_graph_path)
        .with_context(|| format!("reading {}", options.owner_graph_path.display()))?;
    let graph: OwnerGraphReport = serde_json::from_str(&body)
        .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    let evaluated: EvaluatedSetsRoot = serde_json::from_str(&body)
        .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    let binding_to_patch = build_binding_to_patch(&options.modules_root)?;
    Ok(build_inventory_from(
        &graph,
        &evaluated.peelability.evaluated_owner_sets,
        &binding_to_patch,
    ))
}

/// Pure transformation used by tests and by `build_inventory`.
fn build_inventory_from(
    graph: &OwnerGraphReport,
    evaluated_owner_sets: &[EvaluatedOwnerSet],
    binding_to_patch: &BTreeMap<String, Vec<String>>,
) -> Vec<PeelInventoryRecord> {
    let horizon_by_owner: BTreeMap<&str, &analysis::ResidualOwnerPeelHorizonReport> = graph
        .peelability
        .residual_owner_horizon
        .iter()
        .map(|horizon| (horizon.owner_id.as_str(), horizon))
        .collect();

    let mut blocked_by_owner: BTreeMap<String, Vec<ForbiddenRecord>> = BTreeMap::new();
    for evaluated in evaluated_owner_sets {
        // Status keys mirror the snake-case rendering of
        // `PeelCandidateStatus` in `report_schema.rs`. The producer
        // pre-1.x called residual-dep "blocked_residual_dep"; the
        // current rendering is "blocked_residual_dependency". Accept
        // both for backward compat with older `owner_graph.json` files
        // (e.g. when bisecting through devel).
        let record = match evaluated.status.as_str() {
            "blocked_cycle" => Some(ForbiddenRecord::BlockedCycle {
                blocking_edges: evaluated.cycle_blockers.clone(),
            }),
            "blocked_residual_dep" | "blocked_residual_dependency" => {
                Some(ForbiddenRecord::ResidualDependency {
                    missing: evaluated.residual_dependency_blockers.clone(),
                })
            }
            _ => None,
        };
        if let Some(record) = record {
            for owner_id in &evaluated.owner_ids {
                blocked_by_owner
                    .entry(owner_id.clone())
                    .or_default()
                    .push(record.clone());
            }
        }
    }

    let mut inventory = Vec::with_capacity(graph.peelability.minimal_peel_sets.len());
    for peel_set in &graph.peelability.minimal_peel_sets {
        let members: Vec<(String, String)> = peel_set
            .members
            .iter()
            .map(|member| (member.binding.to_string(), member.export_name.to_string()))
            .collect();
        let has_readable = members.iter().any(|(binding, name)| binding != name);

        let mut source_loc: Option<(usize, usize)> = None;
        for owner_id in &peel_set.owner_ids {
            let Some(horizon) = horizon_by_owner.get(owner_id.as_str()) else {
                continue;
            };
            let Some(location) = &horizon.source_location else {
                continue;
            };
            source_loc = Some(match source_loc {
                None => (location.start_line, location.end_line),
                Some((start, end)) => (start.min(location.start_line), end.max(location.end_line)),
            });
        }

        let mut patch_files: BTreeSet<String> = BTreeSet::new();
        for (binding, _) in &members {
            if let Some(files) = binding_to_patch.get(binding) {
                patch_files.extend(files.iter().cloned());
            }
        }
        let patch_files: Vec<String> = patch_files.into_iter().collect();
        let primary_source = patch_files
            .first()
            .cloned()
            .unwrap_or_else(|| "<residual_only>".to_string());

        let primary_readable = members
            .iter()
            .find(|(binding, name)| binding != name)
            .map(|(_, name)| name.clone())
            .unwrap_or_else(|| members[0].1.clone());
        let proposed_dir = derive_proposed_dir(&primary_readable, &primary_source);

        let mut forbidden: Vec<ForbiddenRecord> = Vec::new();
        for owner_id in &peel_set.owner_ids {
            if let Some(records) = blocked_by_owner.get(owner_id) {
                forbidden.extend(records.iter().cloned());
            }
        }

        inventory.push(PeelInventoryRecord {
            candidate_id: peel_set.candidate_id.clone(),
            owner_count: peel_set.owner_ids.len(),
            members,
            has_readable,
            patch_files,
            primary_source,
            source_lines: source_loc,
            proposed_dir,
            forbidden,
        });
    }
    inventory
}

fn build_binding_to_patch(modules_root: &Path) -> Result<BTreeMap<String, Vec<String>>> {
    let mut result: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for member in load_binding_patch_members(modules_root)? {
        result
            .entry(member.selector.binding.name)
            .or_default()
            .push("binding_patches".to_string());
    }
    for entries in result.values_mut() {
        entries.sort();
        entries.dedup();
    }
    Ok(result)
}

/// Heuristic destination rules — generic across spec trees. They activate
/// when the candidate has no current emitted module home; the spec-root
/// `binding_patches` source is not itself a destination hint.
fn derive_proposed_dir(export: &str, current_source: &str) -> String {
    if !current_source.is_empty()
        && current_source != "<residual_only>"
        && current_source != "binding_patches"
        && current_source != DEFAULT_RESIDUAL_MODULE_PATH
    {
        // Mirror Python's `os.path.dirname(current_source) or current_source`:
        // strip the last `/`-segment; if there is none (or the parent is
        // empty), fall back to `current_source` itself.
        return match current_source.rsplit_once('/') {
            Some((parent, _)) if !parent.is_empty() => parent.to_string(),
            _ => current_source.to_string(),
        };
    }
    if matches_error_suffix(export) {
        return "domains/errors".to_string();
    }
    if matches_create_route(export) {
        return "local_api/routes".to_string();
    }
    if matches_migrate_prefix(export) {
        return "workspace/migration".to_string();
    }
    "TBD".to_string()
}

/// `Error$` — Python's `re.compile(r"Error$").match(export)` semantics:
/// `re.match` is anchored at the start, and the pattern is anchored at
/// the end, so this matches only the literal string `"Error"`. Mirrored
/// faithfully (the rule is rarely productive in practice — most error
/// classes end in `Error` but do not equal it).
fn matches_error_suffix(export: &str) -> bool {
    export == "Error"
}

/// `^create[A-Z][a-zA-Z]*Route$`
fn matches_create_route(export: &str) -> bool {
    let Some(after_create) = export.strip_prefix("create") else {
        return false;
    };
    let Some(suffix) = after_create.strip_suffix("Route") else {
        return false;
    };
    let mut chars = suffix.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !first.is_ascii_uppercase() {
        return false;
    }
    chars.all(|c| c.is_ascii_alphabetic())
}

/// `^migrate[A-Z]`
fn matches_migrate_prefix(export: &str) -> bool {
    let Some(after_migrate) = export.strip_prefix("migrate") else {
        return false;
    };
    after_migrate
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_uppercase())
}

#[derive(Debug, Clone, Copy)]
pub enum InventoryView {
    Flat { limit: usize },
    ByDestination { limit: usize },
    Json,
}

pub fn render_inventory(records: &[PeelInventoryRecord], view: InventoryView) -> String {
    match view {
        InventoryView::Flat { limit } => render_flat(records, limit),
        InventoryView::ByDestination { limit } => render_by_destination(records, limit),
        InventoryView::Json => serde_json::to_string_pretty(records).expect("serialize inventory"),
    }
}

/// Short owner-count label, e.g. `n=1`, `n=2`, `n=3`.
fn owner_count_short(owner_count: usize) -> String {
    format!("n={owner_count}")
}

fn render_flat(records: &[PeelInventoryRecord], limit: usize) -> String {
    let mut sorted: Vec<&PeelInventoryRecord> = records.iter().collect();
    sorted.sort_by_key(|record| (record.owner_count, Reverse(record.has_readable)));
    let mut out = String::new();
    for record in sorted.into_iter().take(limit) {
        let members = format_members(&record.members);
        let marker = if record.has_readable { "★" } else { " " };
        out.push_str(&format!(
            "  {marker} [{label:14}] -> {dest:40}  {members}\n",
            label = owner_count_short(record.owner_count),
            dest = record.proposed_dir,
            members = members,
        ));
    }
    out
}

fn render_by_destination(records: &[PeelInventoryRecord], limit: usize) -> String {
    let mut groups: BTreeMap<String, Vec<&PeelInventoryRecord>> = BTreeMap::new();
    for record in records {
        groups
            .entry(record.proposed_dir.clone())
            .or_default()
            .push(record);
    }
    let mut ranked: Vec<(String, Vec<&PeelInventoryRecord>)> = groups.into_iter().collect();
    ranked.sort_by_key(|(dest, candidates)| (Reverse(candidates.len()), dest.clone()));

    let mut out = String::new();
    for (dest, candidates) in ranked.into_iter().take(limit) {
        out.push_str(&format!(
            "\n=== suggested dir: {dest}  ({} candidates) ===\n",
            candidates.len()
        ));
        for record in candidates.into_iter().take(30) {
            let members = format_members(&record.members);
            let marker = if record.has_readable { "★" } else { " " };
            let label = owner_count_short(record.owner_count);
            out.push_str(&format!(
                "  {marker} [{label}] {members}    (from {primary})\n",
                primary = record.primary_source,
            ));
        }
    }
    out
}

fn format_members(members: &[(String, String)]) -> String {
    members
        .iter()
        .map(|(binding, name)| format!("{binding}={name}"))
        .collect::<Vec<_>>()
        .join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{
        BindingReport, ModuleReportRef, OwnerGraphPeelSetReport, OwnerGraphPeelabilityReport,
        OwnerGraphQuotientReport, Purity, ResidualOwnerPeelHorizonReport, ResidualOwnerPeelStatus,
        SourceLocation, StatementKind, StatementOrdinal,
    };

    fn write_file(path: &Path, body: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    fn member(binding: &str, export_name: &str) -> BindingReport {
        BindingReport {
            binding: binding.into(),
            export_name: export_name.into(),
        }
    }

    fn module_ref(label: &str, residual: bool) -> ModuleReportRef {
        ModuleReportRef {
            id: label.to_string(),
            label: label.to_string(),
            residual,
            index: None,
            target_file: None,
        }
    }

    fn horizon(
        owner_id: &str,
        ordinal: usize,
        start_line: usize,
        end_line: usize,
        members: Vec<BindingReport>,
    ) -> ResidualOwnerPeelHorizonReport {
        ResidualOwnerPeelHorizonReport {
            owner_id: owner_id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal),
            source_location: Some(SourceLocation {
                source_path: "static/index.js".to_string(),
                start_line,
                end_line,
            }),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            current_destination: module_ref("residual", true),
            members,
            status: ResidualOwnerPeelStatus::Direct,
            peel_set_ids: Vec::new(),
            companion_options: Vec::new(),
        }
    }

    fn graph_fixture() -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "static/app".to_string(),
            nodes: Vec::new(),
            edges: Vec::new(),
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            peelability: OwnerGraphPeelabilityReport {
                residual_destinations: Vec::new(),
                minimal_peel_sets: vec![
                    // Direct, single owner, has readable rename.
                    OwnerGraphPeelSetReport {
                        candidate_id: "peel_candidate:owner:0".to_string(),
                        owner_ids: vec!["owner:0".to_string()],
                        members: vec![member("ZZ", "PaymentError")],
                    },
                    // Single owner with no readable rename and no patch.
                    OwnerGraphPeelSetReport {
                        candidate_id: "peel_candidate:owner:1".to_string(),
                        owner_ids: vec!["owner:1".to_string()],
                        members: vec![member("createBillingRoute", "createBillingRoute")],
                    },
                    // Owner pair sharing the patch file.
                    OwnerGraphPeelSetReport {
                        candidate_id: "peel_candidate:owner:2".to_string(),
                        owner_ids: vec!["owner:2".to_string(), "owner:3".to_string()],
                        members: vec![member("aa", "loadInvoice"), member("bb", "bb")],
                    },
                ],
                residual_owner_horizon: vec![
                    horizon("owner:0", 0, 10, 20, vec![member("ZZ", "PaymentError")]),
                    horizon("owner:2", 2, 100, 140, vec![member("aa", "loadInvoice")]),
                    horizon("owner:3", 3, 90, 110, vec![member("bb", "bb")]),
                ],
                evaluated_owner_sets: Vec::new(),
            },
            factorize: analysis::FactorizeReport::default(),
        }
    }

    #[test]
    fn build_inventory_from_synthetic_graph() {
        let temp = tempfile::tempdir().unwrap();
        let modules = temp.path().join("modules");
        // ZZ, aa, and bb live in the non-emitting patch stream.
        write_file(
            &temp.path().join("binding_patches.yaml"),
            "members:\n  - name: PaymentError\n    selector:\n      binding:\n        name: ZZ\n  - name: loadInvoice\n    selector:\n      binding:\n        kind: function_declaration\n        name: aa\n  - name: bb\n    selector:\n      binding:\n        name: bb\n",
        );
        // Emitted module YAML files are not patch sources.
        write_file(
            &modules.join("billing/other.yaml"),
            "members:\n  - name: keep\n    selector:\n      binding:\n        name: ZZ\n",
        );

        let bindings = build_binding_to_patch(&modules).unwrap();
        let graph = graph_fixture();
        let inventory = build_inventory_from(&graph, &[], &bindings);

        assert_eq!(inventory.len(), 3);

        let by_id: BTreeMap<&str, &PeelInventoryRecord> = inventory
            .iter()
            .map(|record| (record.candidate_id.as_str(), record))
            .collect();

        let zz = by_id["peel_candidate:owner:0"];
        assert_eq!(
            zz.members,
            vec![("ZZ".to_string(), "PaymentError".to_string())]
        );
        assert!(zz.has_readable);
        assert_eq!(zz.patch_files, vec!["binding_patches".to_string()]);
        assert_eq!(zz.primary_source, "binding_patches");
        assert_eq!(zz.proposed_dir, "TBD");
        assert_eq!(zz.source_lines, Some((10, 20)));
        assert!(zz.forbidden.is_empty());

        let create_route = by_id["peel_candidate:owner:1"];
        assert!(!create_route.has_readable);
        assert!(create_route.patch_files.is_empty());
        assert_eq!(create_route.primary_source, "<residual_only>");
        // Falls into the create*Route heuristic because no current home.
        assert_eq!(create_route.proposed_dir, "local_api/routes");
        assert_eq!(create_route.source_lines, None);

        let pair = by_id["peel_candidate:owner:2"];
        assert!(pair.has_readable);
        assert_eq!(pair.patch_files, vec!["binding_patches".to_string()]);
        assert_eq!(pair.primary_source, "binding_patches");
        // start = min(100, 90) = 90, end = max(140, 110) = 140
        assert_eq!(pair.source_lines, Some((90, 140)));
        assert_eq!(pair.proposed_dir, "TBD");
    }

    #[test]
    fn evaluated_owner_sets_populate_forbidden() {
        let graph = graph_fixture();
        let evaluated = vec![
            EvaluatedOwnerSet {
                status: "blocked_cycle".to_string(),
                owner_ids: vec!["owner:1".to_string()],
                cycle_blockers: vec!["edge:42".to_string()],
                residual_dependency_blockers: Vec::new(),
            },
            EvaluatedOwnerSet {
                status: "blocked_residual_dep".to_string(),
                owner_ids: vec!["owner:2".to_string()],
                cycle_blockers: Vec::new(),
                residual_dependency_blockers: vec!["dep:Foo".to_string()],
            },
            EvaluatedOwnerSet {
                status: "peelable_now".to_string(),
                owner_ids: vec!["owner:0".to_string()],
                cycle_blockers: Vec::new(),
                residual_dependency_blockers: Vec::new(),
            },
        ];

        let inventory = build_inventory_from(&graph, &evaluated, &BTreeMap::new());
        let by_id: BTreeMap<&str, &PeelInventoryRecord> = inventory
            .iter()
            .map(|record| (record.candidate_id.as_str(), record))
            .collect();

        assert!(by_id["peel_candidate:owner:0"].forbidden.is_empty());
        assert_eq!(
            by_id["peel_candidate:owner:1"].forbidden,
            vec![ForbiddenRecord::BlockedCycle {
                blocking_edges: vec!["edge:42".to_string()]
            }]
        );
        assert_eq!(
            by_id["peel_candidate:owner:2"].forbidden,
            vec![ForbiddenRecord::ResidualDependency {
                missing: vec!["dep:Foo".to_string()]
            }]
        );
    }

    #[test]
    fn heuristic_dest_rules_match_python_prototype() {
        // Python's `re.compile(r"Error$").match(export)` is anchored at
        // both ends and only matches the literal string "Error".
        assert_eq!(
            derive_proposed_dir("Error", "<residual_only>"),
            "domains/errors"
        );
        // Anything else ending in `Error` falls through to "TBD".
        assert_eq!(
            derive_proposed_dir("PaymentError", "<residual_only>"),
            "TBD"
        );
        assert_eq!(
            derive_proposed_dir("createWidgetRoute", "<residual_only>"),
            "local_api/routes"
        );
        assert_eq!(derive_proposed_dir("createRoute", "<residual_only>"), "TBD");
        assert_eq!(
            derive_proposed_dir("migrateUserPrefs", "<residual_only>"),
            "workspace/migration"
        );
        assert_eq!(derive_proposed_dir("migrateuser", "<residual_only>"), "TBD");
        assert_eq!(derive_proposed_dir("foo", "<residual_only>"), "TBD");
        // Heuristics do not override an existing emitted-module home.
        assert_eq!(
            derive_proposed_dir("PaymentError", "billing/payments"),
            "billing"
        );
        // The patch stream is not itself a destination hint.
        assert_eq!(
            derive_proposed_dir("PaymentError", "binding_patches"),
            "TBD"
        );
        // Single-segment emitted-module homes fall back to themselves.
        assert_eq!(derive_proposed_dir("PaymentError", "topfile"), "topfile");
    }
}
