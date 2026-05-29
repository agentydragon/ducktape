//! Shared realizability + atom-split gate for spec-mutating CLI verbs.
//!
//! Every command that edits the spec on disk (`modules merge`,
//! `modules delete --force`, `bindings assign`) must reject edits that
//! would render the spec unrealizable BEFORE writing any YAML. Two
//! invariants the gate enforces, mirroring what `debundle run` checks
//! against the on-disk spec:
//!
//! 1. **No cross-module init-order cycles** — `check_realizability` /
//!    `validate_factorization` over the post-edit partition.
//! 2. **No atom-split** — every `AtomicUnit` (constraining-edge SCC of
//!    the owner graph) maps to a single destination module under the
//!    post-edit partition. Splitting a member off into a different
//!    module is unrealizable by construction (see
//!    `docs/design.md` §"Two classes of atom").
//!
//! Both checks share the same `PostEditSpec` view: a list of surviving
//! module paths, each with the binding names and anonymous owner ids
//! it declares. Each verb
//! builds its own `PostEditSpec` (`post_merge_spec`,
//! `post_delete_spec`, `post_assign_spec`) and then calls
//! `gate_post_edit_partition` which is the single entry point
//! responsible for verdict + diagnostic rendering. The function bails
//! with an `anyhow::Error` carrying the same `render_cycle_summary` /
//! `render_atomic_unit_conflict_summary` text the materializer emits
//! when its own gate rejects, so authors see one consistent
//! diagnostic regardless of whether the rejection came from `debundle
//! run` or a CLI edit verb.

use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};

use analysis::{
    AtomicUnit, AtomicUnitConflict, ConflictingClaim, ModuleId, OwnerGraph, OwnerGraphReport,
    OwnerId, Partition, compute_atomic_units, render_atomic_unit_conflict_summary,
    render_cycle_summary, validate_factorization,
};
use anonymous_resolution::{AnonymousStatementClaimSet, resolve_anonymous_statement_claims};
use anyhow::{Context, Result, bail};
use spec_modules::{ModuleClaims, collect_module_files, is_module_yaml, read_module_claims};

#[derive(Debug, Clone)]
pub struct PostEditModule {
    pub path: PathBuf,
    pub claims: ModuleClaims,
}

/// Simulated post-edit spec state — one entry per surviving module,
/// each listing the owner claims declared by its `members:` and
/// `anonymous_statements:` arrays.
/// `modules` is keyed by absolute YAML path so the gate's
/// module-id assignment is deterministic across runs.
#[derive(Debug, Clone)]
pub struct PostEditSpec {
    /// Surviving module YAML paths (absolute), each with the set of
    /// binding/anonymous owner claims it declares after the edit.
    pub modules: Vec<PostEditModule>,
}

/// Build the post-merge spec view in memory without touching the
/// filesystem. Starts from the on-disk modules tree, drops the source
/// files, and folds their claims into the target. If the target file
/// does not exist yet, synthesize it so the gate sees the same module
/// the writer will create.
pub fn post_merge_spec(
    modules_root: &Path,
    target_abs: &Path,
    source_abs: &[PathBuf],
) -> Result<PostEditSpec> {
    let removed: BTreeSet<PathBuf> = source_abs.iter().cloned().collect();
    let mut modules: Vec<PostEditModule> = Vec::new();
    let mut saw_target = false;
    for file in collect_module_files(modules_root)? {
        if removed.contains(&file) {
            continue;
        }
        let claims = if file == target_abs {
            saw_target = true;
            let mut combined = read_gate_claims(&file)?;
            for src in source_abs {
                combined.extend(read_gate_claims(src)?);
            }
            combined
        } else {
            read_gate_claims(&file)?
        };
        modules.push(PostEditModule { path: file, claims });
    }
    if !saw_target {
        let mut claims = ModuleClaims::default();
        for src in source_abs {
            claims.extend(read_gate_claims(src)?);
        }
        if !claims.is_empty() {
            modules.push(PostEditModule {
                path: target_abs.to_path_buf(),
                claims,
            });
        }
    }
    Ok(PostEditSpec { modules })
}

/// Build the post-delete spec view in memory. Drops the deleted YAML
/// paths from the modules tree; bindings they used to declare are
/// implicitly unclaimed in the resulting partition (i.e. fall back to
/// residual).
pub fn post_delete_spec(modules_root: &Path, deleted_abs: &[PathBuf]) -> Result<PostEditSpec> {
    let removed: BTreeSet<PathBuf> = deleted_abs.iter().cloned().collect();
    let mut modules: Vec<PostEditModule> = Vec::new();
    for file in collect_module_files(modules_root)? {
        if removed.contains(&file) {
            continue;
        }
        modules.push(PostEditModule {
            path: file.clone(),
            claims: read_gate_claims(&file)?,
        });
    }
    Ok(PostEditSpec { modules })
}

/// Build the post-unassign spec view in memory. Drops the named
/// bindings from each `(source_module_abs, binding_name)` pair so the
/// resulting partition has them fall through to residual (the default
/// when an owner isn't claimed by any spec module). Source modules
/// that drain to zero members get dropped from the returned spec,
/// mirroring the auto-delete behavior in `run_bindings_unassign`.
///
/// This is structurally `post_assign_spec` with no insertions — but a
/// distinct entry point is worth having because the call sites read
/// differently and the gate diagnostic ("owners go to residual") is
/// the actionable lens for `unassign`. Used by `bindings unassign`.
pub fn post_unassign_spec(
    modules_root: &Path,
    removals: &[(PathBuf, String)],
) -> Result<PostEditSpec> {
    let mut by_path: std::collections::BTreeMap<PathBuf, ModuleClaims> = Default::default();
    for file in collect_module_files(modules_root)? {
        by_path.insert(file.clone(), read_gate_claims(&file)?);
    }
    for (path, name) in removals {
        if let Some(claims) = by_path.get_mut(path) {
            claims.bindings.remove(name);
        }
    }
    let modules: Vec<PostEditModule> = by_path
        .into_iter()
        // Drained modules drop out of the gate's view — the writer
        // path deletes them on apply, so the gate should run against
        // the same module set the post-write spec will have.
        .filter(|(_, claims)| !claims.is_empty())
        .map(|(path, claims)| PostEditModule { path, claims })
        .collect();
    Ok(PostEditSpec { modules })
}

/// Build the post-assign spec view from the current modules tree
/// minus a set of (binding-name, current-source-module) pairs to
/// remove, plus a list of (binding-name, destination-module) pairs to
/// insert. Used by `bindings assign` to feed the gate the in-memory
/// post-batch state.
///
/// `removals` and `insertions` are keyed by absolute module-file
/// path. Destinations missing from the on-disk tree are synthesized as
/// new empty modules so the gate sees the same module-id space the
/// post-write spec will have. Source modules that drain to zero
/// members get dropped from the returned spec (mirroring the auto-
/// delete behavior in `run_bindings_assign`).
pub fn post_assign_spec(
    modules_root: &Path,
    removals: &[(PathBuf, String)],
    insertions: &[(PathBuf, String)],
) -> Result<PostEditSpec> {
    let mut by_path: std::collections::BTreeMap<PathBuf, ModuleClaims> = Default::default();
    for file in collect_module_files(modules_root)? {
        by_path.insert(file.clone(), read_gate_claims(&file)?);
    }
    for (path, name) in removals {
        if let Some(claims) = by_path.get_mut(path) {
            claims.bindings.remove(name);
        }
    }
    for (path, name) in insertions {
        by_path
            .entry(path.clone())
            .or_default()
            .bindings
            .insert(name.clone());
    }
    let modules: Vec<PostEditModule> = by_path
        .into_iter()
        // Drained modules drop out of the gate's view — the writer
        // path deletes them on apply, so the gate should run against
        // the same module set the post-write spec will have.
        .filter(|(_, claims)| !claims.is_empty())
        .map(|(path, claims)| PostEditModule { path, claims })
        .collect();
    Ok(PostEditSpec { modules })
}

/// Parse a spec module YAML and return the owner claims its
/// `members:` and `anonymous_statements:` entries declare.
pub fn read_gate_claims(path: &Path) -> Result<ModuleClaims> {
    if !is_module_yaml(path) {
        return Ok(ModuleClaims::default());
    }
    read_module_claims(path).with_context(|| format!("reading module {}", path.display()))
}

/// Reconstruct the `OwnerGraph` from `owner_graph_path`, build the
/// `Partition` implied by `post_spec`, and run BOTH realizability
/// gates: the cross-module init-order check (cycles) AND the
/// atom-split check (every `AtomicUnit`'s members must co-locate).
/// Returns `Ok(())` when both verdicts are clean. Prints the
/// `render_cycle_summary` / `render_atomic_unit_conflict_summary`
/// blame report to stderr and returns an `anyhow::Error` when either
/// rejects, so the CLI exit code is non-zero and the caller bails
/// before writing.
pub fn gate_post_edit_partition(
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    post_spec: &PostEditSpec,
) -> Result<()> {
    let owner_graph_report: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(owner_graph_path)
            .with_context(|| format!("reading {}", owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing owner graph {}", owner_graph_path.display()))?;

    // The gate algorithm walks edges + partition, not declared sets.
    // Pass `&[]` for facts — `from_report` leaves `declared` empty,
    // which is fine for `check_realizability`/`validate_factorization`
    // (both consume the partition we build below, not the per-owner
    // declared field).
    let (owner_graph, _index) = OwnerGraph::from_report(&owner_graph_report, &[]);

    // owner_by_binding_name uses the Atom-only declared_bindings the
    // wire shape carries; that's enough because the spec author also
    // references bindings by name (no hygienic context). When a
    // declared binding name is ambiguous across owners the first one
    // wins — the materializer's spec validator catches that
    // separately as a duplicate-binding diagnostic.
    let mut owner_by_binding_name: HashMap<String, OwnerId> = HashMap::new();
    for (idx, node) in owner_graph_report.nodes.iter().enumerate() {
        let owner = OwnerId(idx);
        for b in &node.declared_bindings {
            owner_by_binding_name
                .entry(b.binding.to_string())
                .or_insert(owner);
        }
    }
    let claim_sets: Vec<AnonymousStatementClaimSet<'_>> = post_spec
        .modules
        .iter()
        .map(|module| AnonymousStatementClaimSet {
            module_path: &module.path,
            match_sources: &module.claims.anonymous_match_sources,
        })
        .collect();
    let anonymous_owners_by_module = resolve_anonymous_statement_claims(
        &owner_graph_report,
        owner_graph_path,
        modules_root,
        source_root,
        &claim_sets,
    )?;

    // ModuleId assignment: residual at logical:0, every surviving
    // spec module gets a fresh logical:N starting at 1. The label
    // map keeps the renderer's diagnostic readable — we use each
    // module's chunk-relative path as its `module_name` callback
    // output.
    let residual = ModuleId::logical(0);
    let mut of: Vec<ModuleId> = vec![residual; owner_graph.num_nodes()];
    let mut module_label_by_id: HashMap<ModuleId, String> =
        [(residual, "<residual>".to_string())].into_iter().collect();
    let mut next_idx = 1usize;
    for (module_idx, module) in post_spec.modules.iter().enumerate() {
        let mid = ModuleId::logical(next_idx);
        next_idx += 1;
        module_label_by_id.insert(mid, module.path.to_string_lossy().into_owned());
        for name in &module.claims.bindings {
            if let Some(&owner) = owner_by_binding_name.get(name) {
                of[owner.0] = mid;
            }
        }
        for owner in &anonymous_owners_by_module[module_idx] {
            of[owner.0] = mid;
        }
    }
    let partition = Partition::from_assignments(of, residual);

    let module_name = |m: ModuleId| {
        module_label_by_id
            .get(&m)
            .cloned()
            .unwrap_or_else(|| format!("logical:{}", m.0.0))
    };

    // Check 1 — atom splits. Run before cycles so the diagnostic
    // surfaces the structural co-location violation first; an
    // atom-split commonly induces a cycle in the module quotient, and
    // the cycle diagnostic is less actionable for the author than
    // "here is the atomic unit you split".
    let atomic_units = compute_atomic_units(&owner_graph);
    let atomic_conflicts =
        detect_atomic_unit_conflicts(&atomic_units, &partition, &owner_graph_report);
    if !atomic_conflicts.is_empty() {
        let summary = render_atomic_unit_conflict_summary(&atomic_conflicts, &module_name);
        eprintln!("error: post-edit spec splits one or more atomic units:\n{summary}");
        bail!("realizability gate rejected the edit (atom-split)");
    }

    // Check 2 — module-quotient cycles.
    let report = validate_factorization(&owner_graph, &partition, &module_name);
    if !report.cycles.is_empty() {
        let summary = render_cycle_summary(&report.cycles);
        eprintln!("error: post-edit spec is unrealizable:\n{summary}");
        bail!("realizability gate rejected the edit");
    }
    Ok(())
}

/// Per-unit atom-split detection over the post-edit partition.
///
/// Mirrors `factor_assembly::detect_unit_conflict` but operates on
/// `Partition::of` rather than the explicit `claims` array
/// `assemble_partition` builds — the CLI gate's partition already
/// encodes "claim or fall back to residual" via
/// `Partition::from_assignments`, so this is structurally equivalent
/// (each member's `Partition::of` value IS the effective claim).
///
/// `report` supplies the wire-shape `declared_bindings` used to label
/// each conflicting claim. The resulting `AtomicUnitConflict` mirrors
/// the materializer's diagnostic shape exactly so spec authors see the
/// same evidence regardless of whether the rejection came from
/// `debundle run` or a CLI edit verb.
fn detect_atomic_unit_conflicts(
    units: &[AtomicUnit],
    partition: &Partition,
    report: &OwnerGraphReport,
) -> Vec<AtomicUnitConflict> {
    let mut conflicts = Vec::new();
    for unit in units {
        let resolved: Vec<(OwnerId, ModuleId)> = unit
            .members
            .iter()
            .map(|o| (*o, partition.of(*o)))
            .collect();
        let mut first: Option<ModuleId> = None;
        let mut split = false;
        for &(_, m) in &resolved {
            match first {
                None => first = Some(m),
                Some(existing) if existing != m => {
                    split = true;
                    break;
                }
                _ => {}
            }
        }
        if !split {
            continue;
        }
        let claims: Vec<ConflictingClaim> = resolved
            .into_iter()
            .map(|(owner, module)| {
                let binding_names: Vec<swc_atoms::Atom> = report
                    .nodes
                    .get(owner.0)
                    .map(|n| {
                        n.declared_bindings
                            .iter()
                            .map(|b| b.binding.clone())
                            .collect()
                    })
                    .unwrap_or_default();
                ConflictingClaim {
                    owner,
                    binding_names,
                    module,
                }
            })
            .collect();
        conflicts.push(AtomicUnitConflict {
            members: unit.members.iter().copied().collect(),
            claims,
            causes: unit.causes.clone(),
        });
    }
    conflicts
}

#[cfg(test)]
mod tests {
    use std::fs;

    use analysis::{
        AtomicGraphReport, BindingReport, DepKind, OwnerGraphEdgeReport, OwnerGraphNodeReport,
        OwnerGraphQuotientReport, Purity, QuotientSccReport, SourceLocation, StatementKind,
        StatementOrdinal,
    };
    use report_fixtures::{module_ref, module_table};
    use tempfile::TempDir;

    use super::*;

    fn owner(id: &str, ordinal: usize, bindings: Vec<BindingReport>) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal),
            source_location: Some(SourceLocation {
                source_path: "static/index.js".to_string(),
                start_line: ordinal + 1,
                end_line: ordinal + 1,
            }),
            statement_kind: if bindings.is_empty() {
                StatementKind::SideEffect
            } else {
                StatementKind::ClassDecl
            },
            declared_bindings: bindings,
            purity: Purity::Pure,
            destination: module_ref("residual"),
        }
    }

    fn write(path: &Path, body: &str) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    #[test]
    fn edit_gate_resolves_anonymous_statement_selectors_as_claims() {
        let temp = TempDir::new().unwrap();
        let graph_path = temp.path().join("owner_graph.json");
        let modules_root = temp.path().join("spec/modules");
        write(
            &temp.path().join("static/index.js"),
            r#"const ignored = 0;
class Co {}
Ro([Z], Co.prototype, "visible", 2);
"#,
        );
        let class_owner = owner(
            "owner:0",
            1,
            vec![BindingReport {
                binding: "Co".into(),
                export_name: "SearchPopoverState".into(),
            }],
        );
        let decorator_owner = owner("owner:1", 2, Vec::new());
        let nodes = vec![class_owner, decorator_owner];
        let module_nodes = module_table(nodes.iter().map(|n| &n.destination));
        let graph = OwnerGraphReport {
            chunk_id: "static/index".to_string(),
            nodes,
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
                nodes: module_nodes,
                edges: Vec::new(),
                sccs: Vec::<QuotientSccReport>::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        };
        write(&graph_path, &serde_json::to_string(&graph).unwrap());
        write(
            &modules_root.join("features/search/popover_state.yaml"),
            r#"members:
  - selector:
      binding:
        name: Co
anonymous_statements:
  - match: 'Ro([Z], Co.prototype, "visible", 2);'
    note: "observable decorator side effect"
"#,
        );

        let post_spec = post_delete_spec(&modules_root, &[]).unwrap();
        gate_post_edit_partition(&graph_path, &modules_root, None, &post_spec).unwrap();
    }
}
