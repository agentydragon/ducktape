//! Shared realizability + atom-split gate for spec-mutating CLI verbs.
//!
//! Every command that edits the spec on disk (`modules merge`,
//! `modules delete --force`, `bindings assign`) must reject edits that
//! would render the spec unrealizable BEFORE writing any YAML. Two
//! invariants the gate enforces, mirroring what `debundle run` checks
//! against the on-disk spec:
//!
//! 1. **No cross-module init-order cycles** — `check_realizability` /
//!    `validate_assignment` over the post-edit assignment.
//! 2. **No atom-split** — every `AtomicGroup` (constraining-edge SCC of
//!    the dependency graph) maps to a single destination module under the
//!    post-edit assignment. Splitting a member off into a different
//!    module is unrealizable by construction (see
//!    `docs/design.md` §"Two classes of atom").
//!
//! Both checks share the same `PostEditSpec` view: a list of surviving
//! module paths, each with the binding names and anonymous owner ids
//! it declares. Each verb projects its planned documents or deletions into a
//! `PostEditSpec` and then calls
//! `gate_post_edit_assignment` which is the single entry point
//! responsible for verdict + diagnostic rendering. The function bails
//! with a [`GateRejection`] error carrying the same
//! `render_cycle_summary` / `render_atomic_group_conflict_summary`
//! text the materializer emits when its own gate rejects, so authors
//! see one consistent diagnostic regardless of whether the rejection
//! came from `debundle run` or a CLI edit verb. The error also
//! carries a machine-readable [`GateRejectionReport`] payload (the
//! same `BlockingSccEntry` / `AtomicGroupConflictReport` projections
//! the pipeline writes to disk), which the CLI dispatchers serialize
//! to stdout when a JSON format is selected.
//!
//! On rejection the gate also writes the same on-disk artifacts the
//! pipeline writes — `cycles.json` / `atomic_group_conflicts.json` as
//! siblings of the supplied `dependency_graph.json` (the location
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
    BlockingSccEntry, render_atomic_group_conflict_summary, render_cycle_summary,
    validate_assignment,
};
use analysis::{
    AtomicGroup, AtomicGroupConflict, AtomicGroupConflictReport, ConflictingClaim, DependencyGraph,
    DependencyGraphReport, ModuleAssignment, ModuleId, SourceUnitId, compute_atomic_groups,
};
use anonymous_resolution::{ResolvedSourceClaims, SourceClaimSet, resolve_source_claims};
use anyhow::{Context, Result, bail};
use serde::Serialize;
use spec::ModulePath;
use spec_modules::{
    ModuleClaims, ModuleFile, collect_module_files, is_module_yaml, module_claims,
    module_path_from_file, read_module_claims,
};
use spec_tree::ModuleTreeScope;

/// Shared CLI contract for edits that may need graph validation.
#[derive(Debug, clap::Args)]
pub struct GraphEditArgs {
    /// Root directory containing the per-module YAML tree.
    #[arg(long = "modules", env = "DEBUNDLE_MODULES")]
    pub modules_root: PathBuf,
    /// Validate and report the edit without modifying spec files.
    #[arg(long)]
    pub dry_run: bool,
    /// Skip validation. Can permit name collisions or unrealizable specs.
    #[arg(long)]
    pub no_verify: bool,
    /// Dependency graph for validation; required unless --no-verify or the edit
    /// cannot change ownership (such as deleting a structurally empty module).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub dependency_graph_path: Option<PathBuf>,
    /// Root for relative source paths used to resolve source-backed claims.
    #[arg(long = "source-root", env = "DEBUNDLE_SOURCE_ROOT")]
    pub source_root: Option<PathBuf>,
    /// Output format. Default text on tty, JSON on pipe.
    #[arg(long, value_enum)]
    pub format: Option<peel::OutputFormat>,
}

impl GraphEditArgs {
    pub fn gate(&self) -> Result<Gate<'_>> {
        Gate::from_cli(
            self.no_verify,
            self.dependency_graph_path.as_deref(),
            self.source_root.as_deref(),
        )
    }
}

/// How a spec-mutating CLI verb validates its edit. Replaces the
/// `(no_verify: bool, dependency_graph_path: Option<&Path>)` pair whose
/// `(false, None)` combination used to silently skip the
/// realizability gate.
#[derive(Debug, Clone, Copy)]
pub enum Gate<'a> {
    /// Run name-collision checks and the post-edit realizability +
    /// atom-split gate against this dependency graph.
    Run {
        graph: &'a Path,
        /// Root used to resolve relative `source_location.source_path`
        /// values when the gate resolves source-backed selectors.
        source_root: Option<&'a Path>,
    },
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
                "realizability gate requires --graph (path to dependency_graph.json) or --no-verify"
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
        self.check_expected(modules_root, post_spec, &[])
    }

    /// Check the gate and prove newly authored selectors still claim the
    /// binding their edit intended, not merely some unique declaration.
    pub fn check_expected(
        &self,
        modules_root: &Path,
        post_spec: impl FnOnce() -> Result<PostEditSpec>,
        expected: &[ExpectedResolvedMember],
    ) -> Result<()> {
        match self {
            Self::Run { graph, source_root } => gate_post_edit_assignment_with_expected(
                graph,
                modules_root,
                *source_root,
                &post_spec()?,
                expected,
                None,
                None,
            ),
            Self::Skip => Ok(()),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ExpectedResolvedMember {
    pub module_path: PathBuf,
    pub export_name: String,
    pub binding_name: String,
}

/// Machine-readable projection of a realizability-gate rejection.
/// Reuses the canonical wire shapes the pipeline writes on rejection
/// (`cycles.json` → [`BlockingSccEntry`], `atomic_group_conflicts.json`
/// → [`AtomicGroupConflictReport`]) — there is no parallel schema.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum GateRejectionReport {
    /// The post-edit spec splits one or more atomic groups across
    /// destination modules.
    AtomSplit {
        conflicts: Vec<AtomicGroupConflictReport>,
    },
    /// The post-edit module quotient carries blocking SCCs; each entry
    /// names the SCC's module paths and the binding-pair cut edges.
    UnrealizableCycles {
        blocking_sccs: Vec<BlockingSccEntry>,
    },
}

/// Error returned by [`gate_post_edit_assignment`] on rejection. The
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

/// Write a rejection artifact next to `dependency_graph_path` — the
/// default location `debundle gate list/describe/cut` resolves
/// (`GateCommonArgs::resolved_cycles_path`).
fn write_rejection_artifact<T: Serialize>(
    dependency_graph_path: &Path,
    filename: &str,
    value: &T,
) -> Result<()> {
    let path = rejection_artifact_path(dependency_graph_path, filename);
    fs::write(&path, serde_json::to_string_pretty(value)?)
        .with_context(|| format!("writing {}", path.display()))
}

/// Remove a stale rejection artifact so `gate list` reflects the
/// latest gate verdict (a missing `cycles.json` is the documented
/// clean state). A pass clears both artifacts; each rejection kind
/// clears the other kind's file.
fn remove_rejection_artifact(dependency_graph_path: &Path, filename: &str) -> Result<()> {
    let path = rejection_artifact_path(dependency_graph_path, filename);
    if path.exists() {
        fs::remove_file(&path).with_context(|| format!("removing stale {}", path.display()))?;
    }
    Ok(())
}

fn rejection_artifact_path(dependency_graph_path: &Path, filename: &str) -> PathBuf {
    dependency_graph_path
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

/// Complete owner assignment implied by a scoped module tree. `None` is the
/// residual destination; every graph source unit appears so edit callers can prove
/// the post-edit spec matches the whole reviewed assignment.
pub type SourceUnitModuleAssignments = BTreeMap<String, Option<String>>;

/// Project the typed in-memory spec used by edit commands into the canonical
/// resolver's per-module claim sets.
pub fn source_claim_sets_from_post_spec(post_spec: &PostEditSpec) -> Vec<SourceClaimSet<'_>> {
    post_spec
        .modules
        .iter()
        .map(|module| SourceClaimSet {
            module_path: &module.path,
            member_selectors: &module.claims.member_selectors,
            source_matches: &module.claims.source_matches,
            anonymous_selectors: &module.claims.anonymous_selectors,
        })
        .collect()
}

/// Resolve all source units claimed by a candidate spec. The result uses module-file
/// paths relative to `modules_root` and keeps unclaimed source units as residual.
pub fn source_unit_module_assignments(
    dependency_graph_report: &DependencyGraphReport,
    modules_root: &Path,
    post_spec: &PostEditSpec,
    resolved_claims: &[ResolvedSourceClaims],
    scope: Option<&ModuleTreeScope>,
) -> Result<SourceUnitModuleAssignments> {
    anyhow::ensure!(
        post_spec.modules.len() == resolved_claims.len(),
        "resolved selector results do not match the post-edit module list"
    );
    let mut assignments = BTreeMap::new();
    let mut source_unit_by_binding_name: HashMap<String, SourceUnitId> = HashMap::new();
    for (idx, node) in dependency_graph_report.nodes.iter().enumerate() {
        anyhow::ensure!(
            assignments.insert(node.id.clone(), None).is_none(),
            "dependency_graph.json repeats source-unit ID {:?}",
            node.id
        );
        let source_unit = SourceUnitId(idx);
        for binding in &node.declared_bindings {
            let name = binding.binding.to_string();
            if let Some(previous) = source_unit_by_binding_name.insert(name.clone(), source_unit)
                && previous != source_unit
            {
                anyhow::bail!("dependency_graph.json declares binding {name:?} in multiple owners");
            }
        }
    }

    for (module, resolved) in post_spec.modules.iter().zip(resolved_claims) {
        let raw_path = module_path_from_file(&module.path, modules_root);
        let module_path = if let Some(scope) = scope {
            scope.canonical_destination(&raw_path)?
        } else {
            ModulePath::parse(&raw_path, "")?.as_str().to_string()
        };
        let claimed_names = module
            .claims
            .bindings
            .iter()
            .chain(&resolved.bindings)
            .collect::<BTreeSet<_>>();
        for name in claimed_names {
            let owner = source_unit_by_binding_name.get(name).with_context(|| {
                format!(
                    "module {} claims binding {name:?}, absent from dependency_graph.json",
                    module.path.display()
                )
            })?;
            assign_source_unit_module(
                &mut assignments,
                dependency_graph_report,
                *owner,
                &module_path,
            )?;
        }
        for owner in &resolved.anonymous_source_units {
            assign_source_unit_module(
                &mut assignments,
                dependency_graph_report,
                *owner,
                &module_path,
            )?;
        }
    }
    Ok(assignments)
}

fn assign_source_unit_module(
    assignments: &mut SourceUnitModuleAssignments,
    dependency_graph_report: &DependencyGraphReport,
    source_unit: SourceUnitId,
    module_path: &str,
) -> Result<()> {
    let node = dependency_graph_report
        .nodes
        .get(source_unit.0)
        .with_context(|| {
            format!(
                "resolved source-unit index {} is absent from dependency_graph.json",
                source_unit.0
            )
        })?;
    let destination = assignments.get_mut(&node.id).with_context(|| {
        format!(
            "resolved source unit {:?} is absent from dependency_graph.json",
            node.id
        )
    })?;
    match destination {
        None => *destination = Some(module_path.to_string()),
        Some(existing) if existing == module_path => {}
        Some(existing) => anyhow::bail!(
            "source unit {:?} is claimed by multiple modules: {} and {}",
            node.id,
            existing,
            module_path
        ),
    }
    Ok(())
}

/// Build the post-delete spec view in memory. Drops the deleted YAML
/// paths from the modules tree; bindings they used to declare are
/// implicitly unclaimed in the resulting assignment (i.e. fall back to
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
/// will write. Each typed doc uses the same `ModuleFile` →
/// `module_claims` path `debundle run`'s spec loading uses, so plain
/// binding members, canonical `source_matches:` claims, and
/// `anonymous_statements:` all contribute the same claims to the gate
/// that they would contribute to a run over the written spec.
///
/// `deleted_module_paths` lists the module paths (keys of `docs`) the
/// writer will delete on apply (drained move sources); they drop out
/// of the gate's view so the gate runs against the same module set
/// the post-write spec will have.
pub fn post_edit_spec_from_docs(
    docs: &BTreeMap<String, (PathBuf, ModuleFile)>,
    deleted_module_paths: &BTreeSet<String>,
) -> Result<PostEditSpec> {
    let mut modules: Vec<PostEditModule> = Vec::new();
    for (module_path, (file, doc)) in docs {
        if deleted_module_paths.contains(module_path) {
            continue;
        }
        let claims = module_claims(doc.clone())?;
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

/// Reconstruct the `DependencyGraph` from `dependency_graph_path`, build the
/// `ModuleAssignment` implied by `post_spec`, and run BOTH realizability
/// gates: the cross-module init-order check (cycles) AND the
/// atom-split check (every `AtomicGroup`'s members must co-locate).
/// Returns `Ok(())` when both verdicts are clean. Prints the
/// `render_cycle_summary` / `render_atomic_group_conflict_summary`
/// blame report to stderr and returns an `anyhow::Error` when either
/// rejects, so the CLI exit code is non-zero and the caller bails
/// before writing.
pub fn gate_post_edit_assignment(
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    post_spec: &PostEditSpec,
) -> Result<()> {
    gate_post_edit_assignment_with_expected(
        dependency_graph_path,
        modules_root,
        source_root,
        post_spec,
        &[],
        None,
        None,
    )
}

/// Run the normal gate and prove the entire resolved post-edit source-unit assignment
/// is exactly the reviewed assignment, including owners that remain residual.
pub fn gate_post_edit_assignment_with_expected_source_unit_modules(
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    post_spec: &PostEditSpec,
    expected_source_unit_modules: &SourceUnitModuleAssignments,
    scope: Option<&ModuleTreeScope>,
) -> Result<()> {
    gate_post_edit_assignment_with_expected(
        dependency_graph_path,
        modules_root,
        source_root,
        post_spec,
        &[],
        Some(expected_source_unit_modules),
        scope,
    )
}

fn gate_post_edit_assignment_with_expected(
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    post_spec: &PostEditSpec,
    expected: &[ExpectedResolvedMember],
    expected_source_unit_modules: Option<&SourceUnitModuleAssignments>,
    scope: Option<&ModuleTreeScope>,
) -> Result<()> {
    let dependency_graph_report = crate::load_dependency_graph_report(dependency_graph_path)?;

    // The gate algorithm walks edges + assignment, not declared sets:
    // `from_report` leaves `declared` empty, which is fine for
    // `check_realizability`/`validate_assignment` (both consume the
    // assignment we build below, not the per-owner declared field).
    let dependency_graph =
        DependencyGraph::from_report(&dependency_graph_report).with_context(|| {
            format!(
                "reconstructing dependency graph {}",
                dependency_graph_path.display()
            )
        })?;

    // source_unit_by_binding_name uses the Atom-only declared_bindings the
    // wire shape carries; that's enough because the spec author also
    // references bindings by name (no hygienic context). When a
    // declared binding name is ambiguous across owners the first one
    // wins — the materializer's spec validator catches that
    // separately as a duplicate-binding diagnostic.
    let mut source_unit_by_binding_name: HashMap<String, SourceUnitId> = HashMap::new();
    for (idx, node) in dependency_graph_report.nodes.iter().enumerate() {
        let source_unit = SourceUnitId(idx);
        for b in &node.declared_bindings {
            source_unit_by_binding_name
                .entry(b.binding.to_string())
                .or_insert(source_unit);
        }
    }
    // Source-backed claims resolve to the chunk-top bindings and anonymous
    // owners they claim. A claim the gate cannot resolve (missing chunk
    // source, unmatched/ambiguous selector) is a hard error — the claimed
    // owner must never silently fall to residual.
    let claim_sets = source_claim_sets_from_post_spec(post_spec);
    let resolved_claims = resolve_source_claims(
        &dependency_graph_report,
        dependency_graph_path,
        modules_root,
        source_root,
        &claim_sets,
    )?;
    if let Some(expected_source_unit_modules) = expected_source_unit_modules {
        let actual_source_unit_modules = source_unit_module_assignments(
            &dependency_graph_report,
            modules_root,
            post_spec,
            &resolved_claims,
            scope,
        )?;
        if &actual_source_unit_modules != expected_source_unit_modules {
            let source_unit_ids = actual_source_unit_modules
                .keys()
                .chain(expected_source_unit_modules.keys())
                .collect::<BTreeSet<_>>();
            let first_mismatch = source_unit_ids.into_iter().find(|source_unit_id| {
                actual_source_unit_modules.get(*source_unit_id)
                    != expected_source_unit_modules.get(*source_unit_id)
            });
            if let Some(source_unit_id) = first_mismatch {
                bail!(
                    "post-edit selector resolution changed the reviewed source-unit assignment for {source_unit_id:?}: requested {:?}, resolved {:?}",
                    expected_source_unit_modules.get(source_unit_id),
                    actual_source_unit_modules.get(source_unit_id)
                );
            }
            bail!(
                "post-edit source-unit assignment does not match the reviewed complete candidate"
            );
        }
    }
    for target in expected {
        let module_idx = post_spec
            .modules
            .iter()
            .position(|module| module.path == target.module_path)
            .with_context(|| {
                format!("expected module {} is absent", target.module_path.display())
            })?;
        let actual = resolved_claims[module_idx]
            .member_bindings
            .get(&target.export_name);
        if actual != Some(&target.binding_name) {
            bail!(
                "selector for {} in {} resolved binding {:?}, expected {:?}",
                target.export_name,
                target.module_path.display(),
                actual,
                target.binding_name,
            );
        }
    }

    // ModuleId assignment: residual at logical:0, every surviving
    // spec module gets a fresh logical:N starting at 1. The path map
    // renders diagnostics with the same canonical [`ModulePath`]
    // every wire artifact uses (the module YAML's path relative to
    // the modules root, `.yaml` stripped).
    let residual = ModuleId(0);
    let mut of: Vec<ModuleId> = vec![residual; dependency_graph.num_nodes()];
    let mut module_path_by_id: HashMap<ModuleId, ModulePath> = [(
        residual,
        ModulePath::parse("residual", "").expect("residual is a canonical path"),
    )]
    .into_iter()
    .collect();
    for (next_idx, (module_idx, module)) in (1usize..).zip(post_spec.modules.iter().enumerate()) {
        let mid = ModuleId(next_idx);
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
            .chain(&resolved_claims[module_idx].bindings)
        {
            if let Some(&owner) = source_unit_by_binding_name.get(name) {
                of[owner.0] = mid;
            }
        }
        for source_unit in &resolved_claims[module_idx].anonymous_source_units {
            of[source_unit.0] = mid;
        }
    }
    let assignment = ModuleAssignment::from_assignments(of, residual);

    let module_path = |m: ModuleId| {
        module_path_by_id.get(&m).cloned().unwrap_or_else(|| {
            panic!(
                "module id logical:{} not assigned by the post-edit spec",
                m.0
            )
        })
    };

    // Check 1 — atom splits. Run before cycles so the diagnostic
    // surfaces the structural co-location violation first; an
    // atom-split commonly induces a cycle in the module quotient, and
    // the cycle diagnostic is less actionable for the author than
    // "here is the atomic group you split".
    let atomic_groups = compute_atomic_groups(&dependency_graph);
    let atomic_conflicts =
        detect_atomic_group_conflicts(&atomic_groups, &assignment, &dependency_graph_report);
    if !atomic_conflicts.is_empty() {
        let conflicts = AtomicGroupConflictReport::from_conflicts(
            &atomic_conflicts,
            &module_path,
            &dependency_graph,
        );
        write_rejection_artifact(
            dependency_graph_path,
            output_layout::ATOMIC_GROUP_CONFLICTS_REPORT,
            &conflicts,
        )?;
        remove_rejection_artifact(dependency_graph_path, output_layout::CYCLES_REPORT)?;
        let summary = render_atomic_group_conflict_summary(
            &atomic_conflicts,
            &module_path,
            &dependency_graph,
        );
        eprintln!("error: post-edit spec splits one or more atomic groups:\n{summary}");
        return Err(GateRejection {
            report: GateRejectionReport::AtomSplit { conflicts },
            message: "realizability gate rejected the edit (atom-split)",
        }
        .into());
    }

    // Check 2 — module-quotient cycles.
    let report = validate_assignment(&dependency_graph, &assignment, &module_path);
    if !report.cycles.is_empty() {
        let blocking_sccs = BlockingSccEntry::from_cycle_reports(&report.cycles);
        write_rejection_artifact(
            dependency_graph_path,
            output_layout::CYCLES_REPORT,
            &blocking_sccs,
        )?;
        remove_rejection_artifact(
            dependency_graph_path,
            output_layout::ATOMIC_GROUP_CONFLICTS_REPORT,
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
    remove_rejection_artifact(dependency_graph_path, output_layout::CYCLES_REPORT)?;
    remove_rejection_artifact(
        dependency_graph_path,
        output_layout::ATOMIC_GROUP_CONFLICTS_REPORT,
    )?;
    Ok(())
}

/// Per-group atom-split detection over the post-edit assignment.
///
/// Mirrors `assignment_assembly::detect_atomic_group_conflict` but operates on
/// `ModuleAssignment::of` rather than the explicit `claims` array
/// `assemble_assignment` builds — the CLI gate's assignment already
/// encodes "claim or fall back to residual" via
/// `ModuleAssignment::from_assignments`, so this is structurally equivalent
/// (each member's `ModuleAssignment::of` value IS the effective claim).
///
/// `report` supplies the wire-shape `declared_bindings` used to label
/// each conflicting claim. The resulting `AtomicGroupConflict` mirrors
/// the materializer's diagnostic shape exactly so spec authors see the
/// same evidence regardless of whether the rejection came from
/// `debundle run` or a CLI edit verb.
fn detect_atomic_group_conflicts(
    atomic_groups: &[AtomicGroup],
    assignment: &ModuleAssignment,
    report: &DependencyGraphReport,
) -> Vec<AtomicGroupConflict> {
    let mut conflicts = Vec::new();
    for atomic_group in atomic_groups {
        let resolved: Vec<(SourceUnitId, ModuleId)> = atomic_group
            .members
            .iter()
            .map(|o| (*o, assignment.of(*o)))
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
                    .get(source_unit.0)
                    .map(|n| {
                        n.declared_bindings
                            .iter()
                            .map(|b| b.binding.clone())
                            .collect()
                    })
                    .unwrap_or_default();
                ConflictingClaim {
                    source_unit,
                    binding_names,
                    module,
                }
            })
            .collect();
        conflicts.push(AtomicGroupConflict {
            members: atomic_group.members.iter().copied().collect(),
            claims,
            causes: atomic_group.causes.clone(),
        });
    }
    conflicts
}
