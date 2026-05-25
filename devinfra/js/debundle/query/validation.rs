//! Spec-edit validation for the mutating `debundle binding` commands.
//!
//! Given the current owner-graph report plus the spec on disk, this
//! module answers: "if I apply this proposed edit, would the resulting
//! spec still pass the pipeline's realizability gate?".
//!
//! The validator runs the same realizability + atomic-unit-conflict
//! checks as the materializer (`check_realizability` and the kernel of
//! `assemble_partition`) but standalone — it reads from disk, applies
//! the proposed edit in-memory, and never touches anything.
//!
//! Failure modes surfaced:
//!
//! * **Realizability cycle** — the constraining-edge subgraph of the
//!   module-quotient acquires a multi-module SCC after the edit. The
//!   cycle's modules and the cut-edge multiplicities are reported so
//!   the spec author can pick the right edge to break.
//! * **Atomic-unit split** — the edit routes some but not all members
//!   of an atomic unit into a different destination, which the
//!   constraining-edge SCC forbids.
//! * **Duplicate claim** — two distinct modules claim the same binding
//!   name. The spec must be 1:1 on chunk-local bindings.
//! * **Unresolved binding** — the binding the edit targets does not
//!   appear in any owner's `declared_bindings`. Usually a typo.
//!
//! The validator works at the report level: it does not re-parse the
//! source chunk and does not run the materializer. The trade-off is
//! that anything the pipeline catches *after* materialization (e.g.
//! lowering errors specific to a particular emission shape) cannot be
//! caught here. The realizability gate is the dominant failure mode in
//! practice, so this is a worthwhile cheap pre-flight.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{
    DepKind, ModuleId, OwnerGraph, OwnerGraphReport, Partition, RESIDUAL_ENTRY_MODULE_ID,
    check_realizability, compute_atomic_units,
};
use spec::BindingSourceKind;
use spec_modules::{
    collect_module_files, is_residual_module_path, module_path_from_file, read_module_file,
};

/// One proposed spec mutation. The validator applies it to the
/// in-memory spec view before running the gate.
#[derive(Debug, Clone, Eq, PartialEq)]
pub enum ProposedEdit {
    /// Move `binding` to `module`. Replaces any previous assignment.
    Assign { binding: String, module: String },
    /// Remove `binding` from whatever module currently owns it. After
    /// the edit it falls back to residual.
    Unassign { binding: String },
}

impl ProposedEdit {
    fn binding(&self) -> &str {
        match self {
            ProposedEdit::Assign { binding, .. } | ProposedEdit::Unassign { binding } => binding,
        }
    }
}

/// Structured validation outcome. `is_ok()` is the headline; the
/// individual lists carry the diagnostic detail.
#[derive(Debug, Clone, Default, Serialize)]
pub struct ValidationReport {
    /// Multi-module SCCs in the constraining-edge subgraph after the
    /// edit. Each entry includes the cycle modules and the cross-edge
    /// multiplicities. Non-empty means clause 3 fails.
    pub cycles: Vec<CycleDiagnostic>,
    /// Atomic units whose members the proposed spec routes to two or
    /// more distinct destinations. Surfaces the same set of conflicts
    /// `factor_assembly::assemble_partition` reports.
    pub atomic_unit_conflicts: Vec<AtomicUnitConflictDiagnostic>,
    /// Bindings claimed by more than one module after the edit. The
    /// materializer rejects this — every chunk-local binding gets one
    /// home.
    pub duplicate_claims: Vec<DuplicateClaimDiagnostic>,
    /// Bindings the edit names that don't appear as any owner's
    /// declared binding. Usually a typo in the binding name argument.
    pub unresolved_bindings: Vec<String>,
    /// Modules listed in the proposed edit that resolve to the
    /// residual catch-all by spec convention — typically a sign the
    /// edit was authored against a stale `--modules` snapshot. The
    /// validator surfaces them but does not gate on them.
    pub residual_destinations: Vec<String>,
}

impl ValidationReport {
    pub fn is_ok(&self) -> bool {
        self.cycles.is_empty()
            && self.atomic_unit_conflicts.is_empty()
            && self.duplicate_claims.is_empty()
            && self.unresolved_bindings.is_empty()
    }

    /// Human-readable rendering suitable for stderr. Each failure mode
    /// gets its own section. Empty when `is_ok()`.
    pub fn render_diagnostic(&self, edit: &ProposedEdit, target_module: Option<&str>) -> String {
        if self.is_ok() {
            return String::new();
        }
        let mut out = String::new();
        for cycle in &self.cycles {
            out.push_str(&render_cycle(cycle, target_module));
            out.push('\n');
        }
        for conflict in &self.atomic_unit_conflicts {
            out.push_str(&render_atomic_unit_conflict(conflict));
            out.push('\n');
        }
        for dup in &self.duplicate_claims {
            out.push_str(&render_duplicate_claim(dup));
            out.push('\n');
        }
        for name in &self.unresolved_bindings {
            out.push_str(&format!(
                "Error: binding {name:?} does not appear in any owner's declared_bindings.\n  The owner graph does not know this name — check spelling or regenerate the report.\n",
            ));
            out.push('\n');
        }
        let _ = edit;
        out
    }
}

/// One unrealizable SCC in the post-edit constraining-edge subgraph.
#[derive(Debug, Clone, Serialize)]
pub struct CycleDiagnostic {
    /// Modules participating in the cycle, in stable order.
    pub modules: Vec<String>,
    /// Constraining cross-edges between members, sorted by edge
    /// multiplicity (count) descending. Each entry is one (from, to)
    /// pair plus the number of owner-edges contributing.
    pub cut_edges: Vec<CutEdge>,
    /// `true` if any member of this cycle is the target of the
    /// pending edit. Helps the renderer point the user at the right
    /// spot.
    pub touches_edit: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct CutEdge {
    pub from: String,
    pub to: String,
    pub count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct AtomicUnitConflictDiagnostic {
    /// Owner ids in the unit (stable order).
    pub owner_ids: Vec<String>,
    /// Distinct modules the unit's members are routed to after the
    /// edit, with the binding names that drove each routing.
    pub claims: Vec<AtomicUnitClaim>,
    /// Constraining-edge kinds inside the unit. Tells the author
    /// *why* the unit must co-locate (eager cycle, rebind, sequenced).
    pub causes: Vec<DepKind>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AtomicUnitClaim {
    pub module: String,
    pub owner_ids: Vec<String>,
    pub bindings: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DuplicateClaimDiagnostic {
    pub binding: String,
    pub modules: Vec<String>,
}

/// Validate a proposed spec edit against the current owner graph and
/// on-disk spec. Returns a `ValidationReport`; `is_ok()` for "safe to
/// commit", otherwise the diagnostic fields list every failure mode.
///
/// `report` is the owner-graph report (`owner_graph.json`) emitted by
/// the pipeline. `modules_root` is the spec's `modules/` root the
/// edit would write to. `edit` is the prospective mutation.
pub fn validate_spec_edit(
    report: &OwnerGraphReport,
    modules_root: &Path,
    edit: &ProposedEdit,
) -> Result<ValidationReport> {
    validate_spec_edits(report, modules_root, std::slice::from_ref(edit))
}

/// Batch form: applies every edit in `edits` to the in-memory spec
/// snapshot in sequence, then runs the realizability gate over the
/// final state. The single-edit `validate_spec_edit` delegates to
/// this with a one-element slice; `binding move` calls it directly.
///
/// Atomicity contract: either the whole batch validates or
/// `ValidationReport.is_ok()` is `false` and no write should happen.
/// Earlier edits in `edits` are visible to later edits — the
/// validator sees the post-batch claim state, not per-step
/// intermediate states.
pub fn validate_spec_edits(
    report: &OwnerGraphReport,
    modules_root: &Path,
    edits: &[ProposedEdit],
) -> Result<ValidationReport> {
    // Step 1: load all current YAML claims, then apply the proposed
    // edits in order. The result is a `name → Vec<module>` map (Vec
    // to catch duplicates). Residual entries are skipped — the
    // pipeline does not treat residual paths as claims.
    let mut claims: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for path in collect_module_files(modules_root)
        .with_context(|| format!("walking {}", modules_root.display()))?
    {
        let module_path = module_path_from_file(&path, modules_root);
        if is_residual_module_path(&module_path) {
            continue;
        }
        let file = read_module_file(&path)?;
        for member in file.members {
            let binding = member.selector.binding;
            if matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier)) {
                continue;
            }
            claims
                .entry(binding.name)
                .or_default()
                .push(module_path.clone());
        }
    }

    let mut report_out = ValidationReport::default();

    // Apply each edit in-memory. `assign` overrides; `unassign`
    // clears. Later edits in the batch overwrite earlier ones for
    // the same binding.
    let mut edit_bindings: Vec<String> = Vec::with_capacity(edits.len());
    for edit in edits {
        let edit_binding = edit.binding().to_string();
        edit_bindings.push(edit_binding.clone());
        match edit {
            ProposedEdit::Assign { binding, module } => {
                claims.insert(binding.clone(), vec![module.clone()]);
                if is_residual_module_path(module) {
                    report_out.residual_destinations.push(module.clone());
                }
            }
            ProposedEdit::Unassign { binding } => {
                claims.remove(binding);
            }
        }
    }

    // Step 2: check duplicate claims (two modules claiming the same
    // name). Pipeline rejects these.
    for (name, modules) in &claims {
        if modules.len() > 1 {
            let distinct: BTreeSet<&String> = modules.iter().collect();
            if distinct.len() > 1 {
                let mut mods: Vec<String> = distinct.into_iter().cloned().collect();
                mods.sort();
                report_out.duplicate_claims.push(DuplicateClaimDiagnostic {
                    binding: name.clone(),
                    modules: mods,
                });
            }
        }
    }

    // Step 3: confirm each edit's binding is actually known to the
    // graph. Otherwise the realizability gate would silently pass and
    // the user would only learn later that nothing moved.
    for edit_binding in &edit_bindings {
        let any_owner = report.nodes.iter().any(|node| {
            node.declared_bindings
                .iter()
                .any(|b| b.binding.as_ref() == edit_binding.as_str())
        });
        if !any_owner && !report_out.unresolved_bindings.contains(edit_binding) {
            report_out.unresolved_bindings.push(edit_binding.clone());
            // Still continue with the rest of the checks so the
            // report is complete — the caller may want to see other
            // latent issues even when one edit name doesn't resolve.
        }
    }

    // Step 4: allocate ModuleIds. Index 0 is reserved for residual;
    // every distinct claimed module gets a successor index. Owners
    // whose declared bindings don't appear in any claim map fall back
    // to residual.
    let residual = ModuleId::logical(0);
    let mut module_id_for_path: BTreeMap<String, ModuleId> = BTreeMap::new();
    let mut module_path_for_id: BTreeMap<ModuleId, String> = BTreeMap::new();
    module_path_for_id.insert(residual, "<residual>".to_string());
    let mut next_idx: usize = 1;
    for module_paths in claims.values() {
        for module_path in module_paths {
            if module_id_for_path.contains_key(module_path) {
                continue;
            }
            let id = ModuleId::logical(next_idx);
            next_idx += 1;
            module_id_for_path.insert(module_path.clone(), id);
            module_path_for_id.insert(id, module_path.clone());
        }
    }

    // Step 5: build the per-owner module assignment from the post-edit
    // claims map.
    let binding_to_module: BTreeMap<String, ModuleId> = claims
        .iter()
        .map(|(name, modules)| {
            // Take the first claim deterministically; the duplicate-
            // claim diagnostic above already reports when this is
            // ambiguous.
            let path = &modules[0];
            (name.clone(), module_id_for_path[path])
        })
        .collect();

    let mut assignments: Vec<ModuleId> = Vec::with_capacity(report.nodes.len());
    let mut owner_id_strings: Vec<String> = Vec::with_capacity(report.nodes.len());
    let mut owner_bindings_by_index: Vec<Vec<String>> = Vec::with_capacity(report.nodes.len());
    for node in &report.nodes {
        owner_id_strings.push(node.id.clone());
        let mut bindings_here: Vec<String> = Vec::new();
        let mut chosen: Option<ModuleId> = None;
        for b in &node.declared_bindings {
            let name = b.binding.to_string();
            bindings_here.push(name.clone());
            if chosen.is_none() {
                if let Some(&m) = binding_to_module.get(&name) {
                    chosen = Some(m);
                }
            }
        }
        owner_bindings_by_index.push(bindings_here);
        // Anonymous-statement assignment via `anonymous_statements:`
        // would need the spec walker; the report-level validator does
        // not currently model anon statements. The realizability gate
        // still gives a useful answer because anon statements don't
        // typically participate in module-crossing constraining edges
        // in the common edit shapes (assign/unassign of one named
        // binding). Owner-level anon assignments stay at residual.
        assignments.push(chosen.unwrap_or(residual));
    }

    // Step 6: build the OwnerGraph IR from the report and run the
    // realizability gate.
    let (owner_graph, _index) = OwnerGraph::from_report(report);
    let partition = Partition::from_assignments(assignments.clone(), residual);
    let verdict = check_realizability(&owner_graph, &partition);

    for scc in verdict.unrealizable_sccs {
        let modules: Vec<String> = scc
            .modules
            .iter()
            .map(|m| {
                module_path_for_id
                    .get(m)
                    .cloned()
                    .unwrap_or_else(|| format!("<module#{}>", m.index().0))
            })
            .collect();
        let cut_edges = collect_cut_edges(&owner_graph, &partition, &scc, &module_path_for_id);
        // Cycle touches the edit if any module in the cycle is the
        // destination of any edit in the batch.
        let edit_targets: Vec<&str> = edits.iter().filter_map(target_module_path).collect();
        let touches_edit = edit_targets
            .iter()
            .any(|target| modules.iter().any(|m| m == *target));
        report_out.cycles.push(CycleDiagnostic {
            modules,
            cut_edges,
            touches_edit,
        });
    }

    // Cross-rebinds in this codebase typically don't arise from a
    // single binding move (they're authored ESM patterns), but the
    // gate flags them so we surface them under cycles for now since
    // they're rare in the binding-edit flow. The full cross-rebind
    // diagnostic schema can be added later if it ever bites.
    for rebind in verdict.cross_rebinds {
        let from = module_path_for_id
            .get(&rebind.from)
            .cloned()
            .unwrap_or_else(|| format!("<module#{}>", rebind.from.index().0));
        let to = module_path_for_id
            .get(&rebind.to)
            .cloned()
            .unwrap_or_else(|| format!("<module#{}>", rebind.to.index().0));
        report_out.cycles.push(CycleDiagnostic {
            modules: vec![from.clone(), to.clone()],
            cut_edges: vec![CutEdge { from, to, count: 1 }],
            touches_edit: false,
        });
    }

    // Step 7: atomic-unit conflict detection. The pipeline runs this
    // inside `assemble_partition`; we reproduce its kernel against the
    // report so anonymous statements aren't required.
    let atomic_units = compute_atomic_units(&owner_graph);
    for unit in &atomic_units {
        if unit.members.len() < 2 {
            continue;
        }
        // For each owner in the unit, look up its post-edit module.
        let mut by_module: BTreeMap<ModuleId, Vec<analysis::OwnerId>> = BTreeMap::new();
        for owner in &unit.members {
            let m = assignments[owner.0];
            by_module.entry(m).or_default().push(*owner);
        }
        if by_module.len() < 2 {
            continue;
        }
        let mut claims_out: Vec<AtomicUnitClaim> = by_module
            .into_iter()
            .map(|(m, owners)| {
                let owner_id_str: Vec<String> = owners
                    .iter()
                    .map(|o| owner_id_strings[o.0].clone())
                    .collect();
                let mut bindings: BTreeSet<String> = BTreeSet::new();
                for o in &owners {
                    for b in &owner_bindings_by_index[o.0] {
                        bindings.insert(b.clone());
                    }
                }
                AtomicUnitClaim {
                    module: module_path_for_id
                        .get(&m)
                        .cloned()
                        .unwrap_or_else(|| format!("<module#{}>", m.index().0)),
                    owner_ids: owner_id_str,
                    bindings: bindings.into_iter().collect(),
                }
            })
            .collect();
        claims_out.sort_by(|a, b| a.module.cmp(&b.module));
        let owner_ids_out: Vec<String> = unit
            .members
            .iter()
            .map(|o| owner_id_strings[o.0].clone())
            .collect();
        let mut causes: Vec<DepKind> = unit.causes.iter().copied().collect();
        causes.sort();
        report_out
            .atomic_unit_conflicts
            .push(AtomicUnitConflictDiagnostic {
                owner_ids: owner_ids_out,
                claims: claims_out,
                causes,
            });
    }

    Ok(report_out)
}

fn target_module_path(edit: &ProposedEdit) -> Option<&str> {
    match edit {
        ProposedEdit::Assign { module, .. } => Some(module.as_str()),
        ProposedEdit::Unassign { .. } => None,
    }
}

fn collect_cut_edges(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    scc: &analysis::UnrealizableScc,
    module_path_for_id: &BTreeMap<ModuleId, String>,
) -> Vec<CutEdge> {
    use std::collections::HashMap;
    let mut counts: HashMap<(ModuleId, ModuleId), usize> = HashMap::new();
    for edge in owner_graph.iter_edges() {
        let from = partition.of(edge.from);
        let to = partition.of(edge.to);
        if from == to {
            continue;
        }
        if !edge.reason.constrains_init_order() {
            continue;
        }
        if !scc.modules.contains(&from) || !scc.modules.contains(&to) {
            continue;
        }
        *counts.entry((from, to)).or_insert(0) += 1;
    }
    let mut edges: Vec<CutEdge> = counts
        .into_iter()
        .map(|((from, to), count)| {
            let from_label = module_path_for_id
                .get(&from)
                .cloned()
                .unwrap_or_else(|| format!("<module#{}>", from.index().0));
            let to_label = module_path_for_id
                .get(&to)
                .cloned()
                .unwrap_or_else(|| format!("<module#{}>", to.index().0));
            CutEdge {
                from: from_label,
                to: to_label,
                count,
            }
        })
        .collect();
    // Highest-count edges first; ties broken lexicographically so the
    // output is stable across runs.
    edges.sort_by(|a, b| {
        b.count
            .cmp(&a.count)
            .then_with(|| a.from.cmp(&b.from))
            .then_with(|| a.to.cmp(&b.to))
    });
    edges
}

fn render_cycle(cycle: &CycleDiagnostic, target_module: Option<&str>) -> String {
    let mut out = String::new();
    out.push_str("Error: proposed edit creates a realizability cycle:\n\n");
    out.push_str(&format!(
        "  Cycle ({} modules, {} cut edge{}):\n",
        cycle.modules.len(),
        cycle.cut_edges.len(),
        if cycle.cut_edges.len() == 1 { "" } else { "s" },
    ));
    for module in &cycle.modules {
        let suffix = match target_module {
            Some(t) if t == module => "     [target of this assign]",
            _ => "",
        };
        out.push_str(&format!("    {module}{suffix}\n"));
    }
    let _ = target_module;
    if !cycle.cut_edges.is_empty() {
        out.push_str("\n  Top cut edges:\n");
        for edge in cycle.cut_edges.iter().take(5) {
            out.push_str(&format!(
                "    {count:>3}  {from} -> {to}\n",
                count = edge.count,
                from = edge.from,
                to = edge.to
            ));
        }
    }
    out.push_str("\n  Re-run with --force to commit anyway.\n");
    out
}

fn render_atomic_unit_conflict(conflict: &AtomicUnitConflictDiagnostic) -> String {
    let mut out = String::new();
    out.push_str("Error: proposed edit splits an atomic unit across modules:\n\n");
    out.push_str(&format!(
        "  Unit ({} owners, causes: {})\n",
        conflict.owner_ids.len(),
        if conflict.causes.is_empty() {
            "<none>".to_string()
        } else {
            conflict
                .causes
                .iter()
                .map(format_dep_kind)
                .collect::<Vec<_>>()
                .join(", ")
        }
    ));
    for claim in &conflict.claims {
        out.push_str(&format!(
            "    -> {module}: {bindings}\n",
            module = claim.module,
            bindings = if claim.bindings.is_empty() {
                "(anonymous)".to_string()
            } else {
                claim.bindings.join(", ")
            }
        ));
    }
    out.push_str("\n  All members of an atomic unit must share one destination.\n");
    out.push_str("  Re-run with --force to commit anyway.\n");
    out
}

fn render_duplicate_claim(dup: &DuplicateClaimDiagnostic) -> String {
    let mut out = String::new();
    out.push_str(&format!(
        "Error: binding {:?} is claimed by multiple modules:\n\n",
        dup.binding,
    ));
    for module in &dup.modules {
        out.push_str(&format!("    {module}\n"));
    }
    out.push_str("\n  The pipeline allows at most one home per chunk-local binding.\n");
    out.push_str("  Re-run with --force to commit anyway.\n");
    out
}

fn format_dep_kind(kind: &DepKind) -> String {
    match kind {
        DepKind::EagerUse => "eager-use".to_string(),
        DepKind::LazyUse => "lazy-use".to_string(),
        DepKind::EagerRebind => "eager-rebind".to_string(),
        DepKind::LazyRebind => "lazy-rebind".to_string(),
        DepKind::Sequenced => "sequenced".to_string(),
        DepKind::LocalEffect => "local-effect".to_string(),
    }
}

// `RESIDUAL_ENTRY_MODULE_ID` reference keeps the report-key contract
// pinned: any change to the constant would surface as a build error
// here, prompting a re-audit of the residual handling above.
#[allow(dead_code)]
const _RESIDUAL_ID_CHECK: &str = RESIDUAL_ENTRY_MODULE_ID;

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    use analysis::{
        AtomicGraphReport, BindingReport, ModuleReportRef, OwnerGraphEdgeReport,
        OwnerGraphNodeReport, OwnerGraphQuotientReport, OwnerGraphReport, Purity, StatementKind,
        StatementOrdinal,
    };
    use swc_atoms::Atom;

    fn binding(name: &str) -> BindingReport {
        BindingReport {
            binding: Atom::from(name),
            export_name: Atom::from(name),
        }
    }

    fn node(idx: usize, declared: &[&str]) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: format!("owner:{idx}"),
            statement_ordinal: StatementOrdinal(idx),
            source_location: None,
            declared_bindings: declared.iter().map(|n| binding(n)).collect(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination: ModuleReportRef {
                id: format!("logical:{idx}"),
                label: format!("module/{idx}"),
                residual: false,
                index: None,
                target_file: None,
            },
        }
    }

    fn eager_edge(from: usize, to: usize, idx: usize) -> OwnerGraphEdgeReport {
        OwnerGraphEdgeReport {
            id: format!("edge:{idx}"),
            source: format!("owner:{from}"),
            target: format!("owner:{to}"),
            edge_kind: DepKind::EagerUse,
            binding: None,
            statement_ordinal: StatementOrdinal(idx),
            constrains_init_order: true,
            at_init_callee_owner: None,
        }
    }

    fn report(
        nodes: Vec<OwnerGraphNodeReport>,
        edges: Vec<OwnerGraphEdgeReport>,
    ) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "static/app".to_string(),
            nodes,
            edges,
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        }
    }

    fn write_module(root: &Path, rel: &str, body: &str) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, body).unwrap();
    }

    #[test]
    fn validates_clean_assign_to_fresh_module() {
        let dir = TempDir::new().unwrap();
        let root = dir.path().to_path_buf();
        let r = report(vec![node(0, &["foo"]), node(1, &["bar"])], Vec::new());
        let v = validate_spec_edit(
            &r,
            &root,
            &ProposedEdit::Assign {
                binding: "foo".to_string(),
                module: "domain/foos".to_string(),
            },
        )
        .unwrap();
        assert!(v.is_ok(), "expected clean validation, got {v:?}");
    }

    #[test]
    fn detects_cycle_when_assign_creates_back_edge() {
        // owners 0 -> 1 (eager), 1 -> 0 (eager). If we put one of them
        // in a module and leave the other in residual, the cycle
        // module<->residual is unrealizable.
        let dir = TempDir::new().unwrap();
        let root = dir.path().to_path_buf();
        let r = report(
            vec![node(0, &["foo"]), node(1, &["bar"])],
            vec![eager_edge(0, 1, 0), eager_edge(1, 0, 1)],
        );
        let v = validate_spec_edit(
            &r,
            &root,
            &ProposedEdit::Assign {
                binding: "foo".to_string(),
                module: "domain/foos".to_string(),
            },
        )
        .unwrap();
        assert!(!v.is_ok(), "expected cycle, got {v:?}");
        assert_eq!(v.cycles.len(), 1);
        assert!(
            v.cycles[0]
                .modules
                .iter()
                .any(|m| m == "domain/foos" || m == "<residual>")
        );
        assert!(v.cycles[0].touches_edit);
    }

    #[test]
    fn detects_unresolved_binding() {
        let dir = TempDir::new().unwrap();
        let root = dir.path().to_path_buf();
        let r = report(vec![node(0, &["foo"])], Vec::new());
        let v = validate_spec_edit(
            &r,
            &root,
            &ProposedEdit::Assign {
                binding: "no_such_binding".to_string(),
                module: "x".to_string(),
            },
        )
        .unwrap();
        assert!(!v.is_ok());
        assert_eq!(v.unresolved_bindings, vec!["no_such_binding".to_string()]);
    }

    #[test]
    fn detects_duplicate_claim_from_existing_spec() {
        let dir = TempDir::new().unwrap();
        let root = dir.path().to_path_buf();
        // Spec already claims `foo` in two modules. The edit is a
        // no-op but the validator should still surface the latent
        // duplicate.
        write_module(
            &root,
            "a/x.yaml",
            "members:\n  - selector:\n      binding:\n        name: foo\n",
        );
        write_module(
            &root,
            "b/x.yaml",
            "members:\n  - selector:\n      binding:\n        name: foo\n",
        );
        let r = report(vec![node(0, &["foo"]), node(1, &["bar"])], Vec::new());
        let v = validate_spec_edit(
            &r,
            &root,
            &ProposedEdit::Assign {
                binding: "bar".to_string(),
                module: "c/y".to_string(),
            },
        )
        .unwrap();
        // `foo` already had two homes; the new edit on `bar` is fine
        // but the validator surfaces the existing problem so the
        // author doesn't ship over it.
        assert!(!v.duplicate_claims.is_empty());
        assert_eq!(v.duplicate_claims[0].binding, "foo");
    }

    #[test]
    fn assign_resolves_existing_duplicate_when_it_overrides() {
        let dir = TempDir::new().unwrap();
        let root = dir.path().to_path_buf();
        write_module(
            &root,
            "a/x.yaml",
            "members:\n  - selector:\n      binding:\n        name: foo\n",
        );
        write_module(
            &root,
            "b/x.yaml",
            "members:\n  - selector:\n      binding:\n        name: foo\n",
        );
        let r = report(vec![node(0, &["foo"])], Vec::new());
        // The assign overrides both existing claims with a single
        // canonical home.
        let v = validate_spec_edit(
            &r,
            &root,
            &ProposedEdit::Assign {
                binding: "foo".to_string(),
                module: "c/y".to_string(),
            },
        )
        .unwrap();
        assert!(
            v.duplicate_claims.is_empty(),
            "assign-override should clear duplicate, got {:?}",
            v.duplicate_claims
        );
        assert!(v.is_ok(), "validation should pass, got {v:?}");
    }

    #[test]
    fn detects_atomic_unit_split() {
        // owners 0, 1 are forced co-located by mutual eager-use.
        // Pre-existing spec puts `foo` in one module; the edit puts
        // `bar` in a different module — the atomic unit is now split.
        let dir = TempDir::new().unwrap();
        let root = dir.path().to_path_buf();
        write_module(
            &root,
            "a/x.yaml",
            "members:\n  - selector:\n      binding:\n        name: foo\n",
        );
        let r = report(
            vec![node(0, &["foo"]), node(1, &["bar"])],
            vec![eager_edge(0, 1, 0), eager_edge(1, 0, 1)],
        );
        let v = validate_spec_edit(
            &r,
            &root,
            &ProposedEdit::Assign {
                binding: "bar".to_string(),
                module: "b/y".to_string(),
            },
        )
        .unwrap();
        assert!(
            !v.atomic_unit_conflicts.is_empty(),
            "expected atomic-unit conflict, got {v:?}",
        );
        let conflict = &v.atomic_unit_conflicts[0];
        assert_eq!(conflict.owner_ids.len(), 2);
        assert!(conflict.claims.len() >= 2);
    }

    #[test]
    fn unassign_clears_existing_home() {
        let dir = TempDir::new().unwrap();
        let root = dir.path().to_path_buf();
        write_module(
            &root,
            "a/x.yaml",
            "members:\n  - selector:\n      binding:\n        name: foo\n",
        );
        let r = report(vec![node(0, &["foo"])], Vec::new());
        let v = validate_spec_edit(
            &r,
            &root,
            &ProposedEdit::Unassign {
                binding: "foo".to_string(),
            },
        )
        .unwrap();
        assert!(v.is_ok());
    }
}
