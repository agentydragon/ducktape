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
//! with a [`GateRejection`] error carrying the same
//! `render_cycle_summary` / `render_atomic_unit_conflict_summary`
//! text the materializer emits when its own gate rejects, so authors
//! see one consistent diagnostic regardless of whether the rejection
//! came from `debundle run` or a CLI edit verb. The error also
//! carries a machine-readable [`GateRejectionReport`] payload (the
//! same `BlockingSccEntry` / `AtomicUnitConflictReport` projections
//! the pipeline writes to disk), which the CLI dispatchers serialize
//! to stdout when a JSON format is selected.
//!
//! On rejection the gate also writes the same on-disk artifacts the
//! pipeline writes — `cycles.json` / `atomic_unit_conflicts.json` as
//! siblings of the supplied `owner_graph.json` (the location
//! `debundle gate list/describe` reads by default) — so the
//! documented `gate` follow-up queries work after an edit-gate or
//! `--dry-run` rejection. Stale artifacts from a previous rejection
//! are removed when the gate passes (or when the rejection kind
//! changes), keeping `gate list` consistent with the latest verdict.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use ::gate::{
    BlockingSccEntry, render_atomic_unit_conflict_summary, render_cycle_summary,
    validate_factorization,
};
use analysis::{
    AtomicUnit, AtomicUnitConflict, AtomicUnitConflictReport, ConflictingClaim, ModuleId,
    OwnerGraph, OwnerGraphReport, OwnerId, Partition, compute_atomic_units,
};
use anonymous_resolution::{
    AnonymousStatementClaimSet, MemberSelectorClaimSet, resolve_anonymous_statement_claims,
    resolve_member_selector_claims,
};
use anyhow::{Context, Result, bail};
use serde::Serialize;
use serde_yaml::Value;
use spec::{AnonymousStatementSelector, ModulePath};
use spec_modules::{
    ModuleClaims, ModuleFile, collect_module_files, is_module_yaml, module_claims,
    module_path_from_file, read_module_claims,
};

/// How a spec-mutating CLI verb validates its edit. Replaces the
/// `(no_verify: bool, owner_graph_path: Option<&Path>)` pair whose
/// `(false, None)` combination used to silently skip the
/// realizability gate.
#[derive(Debug, Clone, Copy)]
pub enum Gate<'a> {
    /// Run name-collision checks and the post-edit realizability +
    /// atom-split gate against this owner graph.
    Run {
        graph: &'a Path,
        /// Root used to resolve relative `source_location.source_path`
        /// values when the gate resolves source-backed selectors.
        source_root: Option<&'a Path>,
    },
    /// Run name-collision checks but no graph-backed gate. Not
    /// constructible via [`Gate::from_cli`] (the dispatcher requires
    /// `--graph` or `--no-verify`); for library callers that have no
    /// owner graph.
    NamesOnly,
    /// `--no-verify`: skip all validation.
    Skip,
}

impl<'a> Gate<'a> {
    /// The single "graph or `--no-verify`" policy every spec-mutating
    /// verb shares.
    pub fn from_cli(
        no_verify: bool,
        graph: Option<&'a Path>,
        source_root: Option<&'a Path>,
    ) -> Result<Self> {
        if no_verify {
            return Ok(Self::Skip);
        }
        match graph {
            Some(graph) => Ok(Self::Run { graph, source_root }),
            None => bail!(
                "realizability gate requires --graph (path to owner_graph.json) or --no-verify"
            ),
        }
    }

    /// Whether name-collision validation runs.
    pub fn verify_names(&self) -> bool {
        !matches!(self, Self::Skip)
    }

    /// The [`GateOutcome`] label a successful edit reports for this
    /// validation mode.
    pub fn outcome(&self) -> crate::outcome::GateOutcome {
        match self {
            Self::Run { .. } => crate::outcome::GateOutcome::Passed,
            Self::NamesOnly => crate::outcome::GateOutcome::NamesOnly,
            Self::Skip => crate::outcome::GateOutcome::Skipped,
        }
    }

    /// Run the realizability + atom-split gate against the post-edit
    /// spec when this is [`Gate::Run`]. `post_spec` is lazy so
    /// skipped gates don't pay for spec assembly.
    pub fn check(
        &self,
        modules_root: &Path,
        post_spec: impl FnOnce() -> Result<PostEditSpec>,
    ) -> Result<()> {
        match self {
            Self::Run { graph, source_root } => {
                gate_post_edit_partition(graph, modules_root, *source_root, &post_spec()?)
            }
            Self::NamesOnly | Self::Skip => Ok(()),
        }
    }
}

/// Machine-readable projection of a realizability-gate rejection.
/// Reuses the canonical wire shapes the pipeline writes on rejection
/// (`cycles.json` → [`BlockingSccEntry`], `atomic_unit_conflicts.json`
/// → [`AtomicUnitConflictReport`]) — there is no parallel schema.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum GateRejectionReport {
    /// The post-edit spec splits one or more atomic units across
    /// destination modules.
    AtomSplit {
        conflicts: Vec<AtomicUnitConflictReport>,
    },
    /// The post-edit module quotient carries blocking SCCs; each entry
    /// names the SCC's module paths and the binding-pair cut edges.
    UnrealizableCycles {
        blocking_sccs: Vec<BlockingSccEntry>,
    },
}

/// Error returned by [`gate_post_edit_partition`] on rejection. The
/// `Display` text is the terse one-line verdict (the blame report has
/// already gone to stderr); `report` is the structured payload CLI
/// dispatchers serialize to stdout under a JSON format.
#[derive(Debug)]
pub struct GateRejection {
    pub report: GateRejectionReport,
    message: &'static str,
}

impl fmt::Display for GateRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message)
    }
}

impl std::error::Error for GateRejection {}

/// Write a rejection artifact next to `owner_graph_path` — the
/// default location `debundle gate list/describe/cut` resolves
/// (`GateCommonArgs::resolved_cycles_path`).
fn write_rejection_artifact<T: Serialize>(
    owner_graph_path: &Path,
    filename: &str,
    value: &T,
) -> Result<()> {
    let path = rejection_artifact_path(owner_graph_path, filename);
    fs::write(&path, serde_json::to_string_pretty(value)?)
        .with_context(|| format!("writing {}", path.display()))
}

/// Remove a stale rejection artifact so `gate list` reflects the
/// latest gate verdict (a missing `cycles.json` is the documented
/// clean state). A pass clears both artifacts; each rejection kind
/// clears the other kind's file.
fn remove_rejection_artifact(owner_graph_path: &Path, filename: &str) -> Result<()> {
    let path = rejection_artifact_path(owner_graph_path, filename);
    if path.exists() {
        fs::remove_file(&path).with_context(|| format!("removing stale {}", path.display()))?;
    }
    Ok(())
}

fn rejection_artifact_path(owner_graph_path: &Path, filename: &str) -> PathBuf {
    owner_graph_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(filename)
}

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

/// Build the post-edit spec view from in-memory module docs — the
/// exact post-batch state `bindings assign` / `bindings unassign`
/// will write. Each doc is parsed through the same `ModuleFile` →
/// `module_claims` path `debundle run`'s spec loading uses, so plain
/// binding members, `source_match` members, `binding_groups:`, and
/// `anonymous_statements:` all contribute the same claims to the gate
/// that they would contribute to a run over the written spec.
///
/// `deleted_module_paths` lists the module paths (keys of `docs`) the
/// writer will delete on apply (drained move sources); they drop out
/// of the gate's view so the gate runs against the same module set
/// the post-write spec will have.
pub fn post_edit_spec_from_docs(
    docs: &BTreeMap<String, (PathBuf, Value)>,
    deleted_module_paths: &BTreeSet<String>,
) -> Result<PostEditSpec> {
    let mut modules: Vec<PostEditModule> = Vec::new();
    for (module_path, (file, doc)) in docs {
        if deleted_module_paths.contains(module_path) {
            continue;
        }
        let module: ModuleFile = serde_yaml::from_value(doc.clone())
            .with_context(|| format!("parsing module {}", file.display()))?;
        let claims = module_claims(module)?;
        if claims.is_empty() {
            continue;
        }
        modules.push(PostEditModule {
            path: file.clone(),
            claims,
        });
    }
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
    let (owner_graph, _index) = OwnerGraph::from_report(&owner_graph_report, &[])
        .with_context(|| format!("reconstructing owner graph {}", owner_graph_path.display()))?;

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
            selectors: &module.claims.anonymous_selectors,
        })
        .collect();
    let anonymous_owners_by_module = resolve_anonymous_statement_claims(
        &owner_graph_report,
        owner_graph_path,
        modules_root,
        source_root,
        &claim_sets,
    )?;

    // Member-form `source_match` claims. `binding_groups:` entries
    // expand into per-binding member selectors through the same
    // expansion the run pipeline's member assembly applies
    // (`source_match::binding_group_member_selectors`), then resolve
    // source-backed to the chunk-top binding names they claim. A
    // selector the gate cannot resolve (missing chunk source,
    // unmatched/ambiguous selector) is a hard error — the claimed
    // owner must never silently fall to residual.
    let expanded_member_selectors: Vec<BTreeSet<AnonymousStatementSelector>> =
        js_ast::with_swc_globals(|| {
            post_spec
                .modules
                .iter()
                .map(|module| {
                    let mut selectors = module.claims.member_selectors.clone();
                    let request_id = module.path.to_string_lossy();
                    for group in &module.claims.binding_groups {
                        for expanded in
                            source_match::binding_group_member_selectors(&request_id, group)?
                        {
                            selectors.insert(expanded.selector);
                        }
                    }
                    Ok(selectors)
                })
                .collect::<Result<_>>()
        })?;
    let member_claim_sets: Vec<MemberSelectorClaimSet<'_>> = post_spec
        .modules
        .iter()
        .zip(&expanded_member_selectors)
        .map(|(module, selectors)| MemberSelectorClaimSet {
            module_path: &module.path,
            selectors,
        })
        .collect();
    let member_bindings_by_module = resolve_member_selector_claims(
        &owner_graph_report,
        owner_graph_path,
        modules_root,
        source_root,
        &member_claim_sets,
    )?;

    // ModuleId assignment: residual at logical:0, every surviving
    // spec module gets a fresh logical:N starting at 1. The path map
    // renders diagnostics with the same canonical [`ModulePath`]
    // every wire artifact uses (the module YAML's path relative to
    // the modules root, `.yaml` stripped).
    let residual = ModuleId::logical(0);
    let mut of: Vec<ModuleId> = vec![residual; owner_graph.num_nodes()];
    let mut module_path_by_id: HashMap<ModuleId, ModulePath> = [(
        residual,
        ModulePath::parse("residual", "").expect("residual is a canonical path"),
    )]
    .into_iter()
    .collect();
    let mut next_idx = 1usize;
    for (module_idx, module) in post_spec.modules.iter().enumerate() {
        let mid = ModuleId::logical(next_idx);
        next_idx += 1;
        let raw = module_path_from_file(&module.path, modules_root);
        module_path_by_id.insert(
            mid,
            ModulePath::parse(&raw, "")
                .with_context(|| format!("module YAML {} path", module.path.display()))?,
        );
        for name in module
            .claims
            .bindings
            .iter()
            .chain(&member_bindings_by_module[module_idx])
        {
            if let Some(&owner) = owner_by_binding_name.get(name) {
                of[owner.0] = mid;
            }
        }
        for owner in &anonymous_owners_by_module[module_idx] {
            of[owner.0] = mid;
        }
    }
    let partition = Partition::from_assignments(of, residual);

    let module_path = |m: ModuleId| {
        module_path_by_id.get(&m).cloned().unwrap_or_else(|| {
            panic!(
                "module id logical:{} not assigned by the post-edit spec",
                m.0.0
            )
        })
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
        let conflicts = AtomicUnitConflictReport::from_conflicts(&atomic_conflicts, &module_path);
        write_rejection_artifact(
            owner_graph_path,
            output_layout::ATOMIC_UNIT_CONFLICTS_REPORT,
            &conflicts,
        )?;
        remove_rejection_artifact(owner_graph_path, output_layout::CYCLES_REPORT)?;
        let summary = render_atomic_unit_conflict_summary(&atomic_conflicts, &module_path);
        eprintln!("error: post-edit spec splits one or more atomic units:\n{summary}");
        return Err(GateRejection {
            report: GateRejectionReport::AtomSplit { conflicts },
            message: "realizability gate rejected the edit (atom-split)",
        }
        .into());
    }

    // Check 2 — module-quotient cycles.
    let report = validate_factorization(&owner_graph, &partition, &module_path);
    if !report.cycles.is_empty() {
        let blocking_sccs = BlockingSccEntry::from_cycle_reports(&report.cycles);
        write_rejection_artifact(
            owner_graph_path,
            output_layout::CYCLES_REPORT,
            &blocking_sccs,
        )?;
        remove_rejection_artifact(
            owner_graph_path,
            output_layout::ATOMIC_UNIT_CONFLICTS_REPORT,
        )?;
        let summary = render_cycle_summary(&report.cycles);
        eprintln!("error: post-edit spec is unrealizable:\n{summary}");
        return Err(GateRejection {
            report: GateRejectionReport::UnrealizableCycles { blocking_sccs },
            message: "realizability gate rejected the edit",
        }
        .into());
    }
    // Pass: clear stale rejection artifacts from a previous rejected
    // edit/run so `gate list` reports the documented clean state.
    remove_rejection_artifact(owner_graph_path, output_layout::CYCLES_REPORT)?;
    remove_rejection_artifact(
        owner_graph_path,
        output_layout::ATOMIC_UNIT_CONFLICTS_REPORT,
    )?;
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
