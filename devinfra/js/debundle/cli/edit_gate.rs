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
//!    the owner graph) maps to a single destination module under the
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
    BlockingSccEntry, render_atomic_group_conflict_summary, render_cycle_summary,
    validate_assignment,
};
use analysis::{
    AtomicGroup, AtomicGroupConflict, AtomicGroupConflictReport, ConflictingClaim, DepKind,
    ModuleAssignment, ModuleId, OwnerGraph, OwnerGraphReport, OwnerId, compute_atomic_groups,
};
use anonymous_resolution::{ResolvedSourceClaims, SourceClaimSet, resolve_source_claims};
use anyhow::{Context, Result, bail};
use serde::Serialize;
use spec::{BindingSourceKind, MemberSelectorSpec, ModulePath};
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
    /// Owner graph for validation; required unless --no-verify or the edit
    /// cannot change ownership (such as deleting a structurally empty module).
    #[arg(long = "graph", env = "DEBUNDLE_GRAPH")]
    pub owner_graph_path: Option<PathBuf>,
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
            self.owner_graph_path.as_deref(),
            self.source_root.as_deref(),
        )
    }
}

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

/// Complete owner assignment implied by a scoped module tree. `None` is the
/// residual destination; every graph owner appears so edit callers can prove
/// the post-edit spec matches the whole reviewed assignment.
pub type OwnerModuleAssignments = BTreeMap<String, Option<String>>;

/// Resolution of one local binding name against the owner graph.
///
/// Multiple declaration sites are accepted only when the report carries
/// `CoDeclaration` evidence for this exact binding connecting all of them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LocalBindingResolution {
    Resolved(BTreeSet<OwnerId>),
    Missing,
    Ambiguous(BTreeSet<OwnerId>),
}

/// How a member's binding claim reached the owner projection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectedBindingKind {
    /// A direct local-name pin, retaining its optional source kind.
    LocalName {
        source_kind: Option<BindingSourceKind>,
    },
    /// A direct import claim. Imports are handled as re-exports and do not
    /// claim a local declaration owner.
    ImportSpecifier,
    /// A non-name selector resolved by the canonical source resolver.
    SourceResolved,
}

/// One member's typed projection. `None` resolution is reserved for imports;
/// local claims always retain either resolved owners or an explicit mismatch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectedMemberClaim {
    pub binding_name: String,
    pub kind: ProjectedBindingKind,
    pub resolution: Option<LocalBindingResolution>,
}

/// A local name claim that could not be assigned to a unique declaration
/// identity using the owner graph's declaration and co-declaration evidence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedLocalBindingClaim {
    pub module_path: PathBuf,
    pub claim_name: String,
    pub binding_name: String,
    pub source_kind: Option<BindingSourceKind>,
    pub resolution: LocalBindingResolution,
}

/// Typed claim projection shared by the normal realizability gate and the
/// complete owner-assignment check used by `modules refine`.
#[derive(Debug, Clone, Default)]
pub struct ModuleClaimProjection {
    /// In the same order as `PostEditModule::claims.member_selectors`.
    pub members: Vec<ProjectedMemberClaim>,
    /// Local binding names contributed by this module, including source-backed
    /// claims, with declaration-site identity preserved.
    pub local_bindings: BTreeMap<String, LocalBindingResolution>,
    /// Every local owner contributed by the module's resolved claims.
    pub owners: BTreeSet<OwnerId>,
    /// Anonymous statement owners resolved by source location.
    pub anonymous_owners: BTreeSet<OwnerId>,
}

#[derive(Debug, Clone, Default)]
pub struct OwnerClaimProjection {
    /// Per-module claims remain aligned with `PostEditSpec::modules`.
    pub modules: Vec<ModuleClaimProjection>,
    /// Local direct claims whose owner identity is missing or ambiguous.
    /// The first projection consumer records these; the strict CLI edit check
    /// decides whether to reject them.
    pub unresolved_local_bindings: Vec<UnresolvedLocalBindingClaim>,
}

/// Per-projection index for mapping local binding spellings to declaration
/// owners. The report is scanned once; claim resolution then visits only
/// owners and co-declaration edges for the requested spelling.
struct LocalBindingIndex {
    owners_by_binding: BTreeMap<String, BTreeSet<OwnerId>>,
    co_declaration_neighbors: BTreeMap<String, BTreeMap<OwnerId, BTreeSet<OwnerId>>>,
}

impl LocalBindingIndex {
    fn from_report(report: &OwnerGraphReport) -> Self {
        let mut owners_by_binding = BTreeMap::<String, BTreeSet<OwnerId>>::new();
        let mut owner_by_report_id = HashMap::<&str, OwnerId>::new();
        for (index, node) in report.nodes.iter().enumerate() {
            let owner = OwnerId(index);
            owner_by_report_id.insert(node.id.as_str(), owner);
            for binding in &node.declared_bindings {
                owners_by_binding
                    .entry(binding.binding.to_string())
                    .or_default()
                    .insert(owner);
            }
        }

        let mut co_declaration_neighbors =
            BTreeMap::<String, BTreeMap<OwnerId, BTreeSet<OwnerId>>>::new();
        for edge in &report.edges {
            if edge.edge_kind != DepKind::CoDeclaration {
                continue;
            }
            let Some(binding) = edge.binding.as_ref() else {
                continue;
            };
            let (Some(&source), Some(&target)) = (
                owner_by_report_id.get(edge.source.as_str()),
                owner_by_report_id.get(edge.target.as_str()),
            ) else {
                continue;
            };
            let neighbors = co_declaration_neighbors
                .entry(binding.to_string())
                .or_default();
            neighbors.entry(source).or_default().insert(target);
            neighbors.entry(target).or_default().insert(source);
        }

        Self {
            owners_by_binding,
            co_declaration_neighbors,
        }
    }

    fn resolve(&self, binding_name: &str) -> LocalBindingResolution {
        let Some(candidates) = self.owners_by_binding.get(binding_name) else {
            return LocalBindingResolution::Missing;
        };
        if candidates.len() == 1 {
            return LocalBindingResolution::Resolved(candidates.clone());
        }

        // A repeated spelling is one local binding only when the report proves
        // that exact binding's declaration sites are connected by CoDeclaration
        // edges. Name equality alone is insufficient: separate hygienic
        // identities with the same spelling remain ambiguous.
        let mut reached = BTreeSet::new();
        let start = *candidates.first().expect("multiple candidates exist");
        reached.insert(start);
        let mut pending = vec![start];
        if let Some(neighbors_by_owner) = self.co_declaration_neighbors.get(binding_name) {
            while let Some(owner) = pending.pop() {
                for next in neighbors_by_owner.get(&owner).into_iter().flatten() {
                    if candidates.contains(next) && reached.insert(*next) {
                        pending.push(*next);
                    }
                }
            }
        }
        if reached == *candidates {
            LocalBindingResolution::Resolved(candidates.clone())
        } else {
            LocalBindingResolution::Ambiguous(candidates.clone())
        }
    }
}

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

/// Resolve typed binding claims to owner identities. Direct imports remain
/// distinct from local declarations, and repeated local declarations are
/// joined only when `owner_graph.json` carries matching `CoDeclaration` edges.
pub fn project_owner_claims(
    owner_graph_report: &OwnerGraphReport,
    post_spec: &PostEditSpec,
    resolved_claims: &[ResolvedSourceClaims],
) -> Result<OwnerClaimProjection> {
    anyhow::ensure!(
        post_spec.modules.len() == resolved_claims.len(),
        "resolved selector results do not match the post-edit module list"
    );
    let binding_index = LocalBindingIndex::from_report(owner_graph_report);
    let mut projection = OwnerClaimProjection::default();
    for (module, resolved) in post_spec.modules.iter().zip(resolved_claims) {
        let mut module_projection = ModuleClaimProjection::default();
        for (claim_name, selector) in &module.claims.member_selectors {
            match selector {
                MemberSelectorSpec::Binding(binding)
                    if binding.kind == Some(BindingSourceKind::ImportSpecifier) =>
                {
                    module_projection.members.push(ProjectedMemberClaim {
                        binding_name: binding.name.clone(),
                        kind: ProjectedBindingKind::ImportSpecifier,
                        resolution: None,
                    });
                }
                MemberSelectorSpec::Binding(binding) => {
                    let resolution = binding_index.resolve(&binding.name);
                    record_local_binding_claim(
                        &mut projection,
                        &mut module_projection,
                        &module.path,
                        claim_name,
                        &binding.name,
                        binding.kind,
                        resolution.clone(),
                    );
                    module_projection.members.push(ProjectedMemberClaim {
                        binding_name: binding.name.clone(),
                        kind: ProjectedBindingKind::LocalName {
                            source_kind: binding.kind,
                        },
                        resolution: Some(resolution),
                    });
                }
                _ => {
                    let binding_name =
                        resolved.member_bindings.get(claim_name).with_context(|| {
                            format!(
                                "member {claim_name:?} in {} has no resolved binding",
                                module.path.display()
                            )
                        })?;
                    let resolution = binding_index.resolve(binding_name);
                    record_local_binding_claim(
                        &mut projection,
                        &mut module_projection,
                        &module.path,
                        claim_name,
                        binding_name,
                        None,
                        resolution.clone(),
                    );
                    module_projection.members.push(ProjectedMemberClaim {
                        binding_name: binding_name.clone(),
                        kind: ProjectedBindingKind::SourceResolved,
                        resolution: Some(resolution),
                    });
                }
            }
        }
        for binding_name in &resolved.bindings {
            if module_projection.local_bindings.contains_key(binding_name) {
                continue;
            }
            let resolution = binding_index.resolve(binding_name);
            record_local_binding_claim(
                &mut projection,
                &mut module_projection,
                &module.path,
                "source-backed claim",
                binding_name,
                None,
                resolution,
            );
        }
        module_projection.anonymous_owners = resolved.anonymous_owners.clone();
        module_projection
            .owners
            .extend(module_projection.anonymous_owners.iter().copied());
        projection.modules.push(module_projection);
    }
    Ok(projection)
}

fn record_local_binding_claim(
    projection: &mut OwnerClaimProjection,
    module_projection: &mut ModuleClaimProjection,
    module_path: &Path,
    claim_name: &str,
    binding_name: &str,
    source_kind: Option<BindingSourceKind>,
    resolution: LocalBindingResolution,
) {
    match &resolution {
        LocalBindingResolution::Resolved(owners) => {
            module_projection.owners.extend(owners.iter().copied());
        }
        LocalBindingResolution::Missing | LocalBindingResolution::Ambiguous(_) => {
            projection
                .unresolved_local_bindings
                .push(UnresolvedLocalBindingClaim {
                    module_path: module_path.to_path_buf(),
                    claim_name: claim_name.to_string(),
                    binding_name: binding_name.to_string(),
                    source_kind,
                    resolution: resolution.clone(),
                });
        }
    }
    module_projection
        .local_bindings
        .entry(binding_name.to_string())
        .or_insert(resolution);
}

/// Build the complete owner assignment represented by a typed claim
/// projection. Unresolved local claims remain visible in the projection and
/// are rejected by the strict edit check.
pub fn owner_module_assignments_from_projection(
    owner_graph_report: &OwnerGraphReport,
    modules_root: &Path,
    post_spec: &PostEditSpec,
    projection: &OwnerClaimProjection,
    scope: Option<&ModuleTreeScope>,
) -> Result<OwnerModuleAssignments> {
    anyhow::ensure!(
        post_spec.modules.len() == projection.modules.len(),
        "projected selector results do not match the post-edit module list"
    );
    let mut assignments = BTreeMap::new();
    for node in &owner_graph_report.nodes {
        anyhow::ensure!(
            assignments.insert(node.id.clone(), None).is_none(),
            "owner_graph.json repeats owner ID {:?}",
            node.id
        );
    }
    for (module, projected) in post_spec.modules.iter().zip(&projection.modules) {
        let raw_path = module_path_from_file(&module.path, modules_root);
        let module_path = if let Some(scope) = scope {
            scope.canonical_destination(&raw_path)?
        } else {
            ModulePath::parse(&raw_path, "")?.as_str().to_string()
        };
        for owner in &projected.owners {
            assign_owner_module(&mut assignments, owner_graph_report, *owner, &module_path)?;
        }
    }
    Ok(assignments)
}

fn assign_owner_module(
    assignments: &mut OwnerModuleAssignments,
    owner_graph_report: &OwnerGraphReport,
    owner: OwnerId,
    module_path: &str,
) -> Result<()> {
    let node = owner_graph_report.nodes.get(owner.0).with_context(|| {
        format!(
            "resolved owner index {} is absent from owner_graph.json",
            owner.0
        )
    })?;
    let destination = assignments.get_mut(&node.id).with_context(|| {
        format!(
            "resolved owner {:?} is absent from owner_graph.json",
            node.id
        )
    })?;
    match destination {
        None => *destination = Some(module_path.to_string()),
        Some(existing) if existing == module_path => {}
        Some(existing) => anyhow::bail!(
            "owner {:?} is claimed by multiple modules: {} and {}",
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

/// Reconstruct the `OwnerGraph` from `owner_graph_path`, build the
/// `ModuleAssignment` implied by `post_spec`, and run BOTH realizability
/// gates: the cross-module init-order check (cycles) AND the
/// atom-split check (every `AtomicGroup`'s members must co-locate).
/// Returns `Ok(())` when both verdicts are clean. Prints the
/// `render_cycle_summary` / `render_atomic_group_conflict_summary`
/// blame report to stderr and returns an `anyhow::Error` when either
/// rejects, so the CLI exit code is non-zero and the caller bails
/// before writing.
pub fn gate_post_edit_assignment(
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    post_spec: &PostEditSpec,
) -> Result<()> {
    gate_post_edit_assignment_with_expected(
        owner_graph_path,
        modules_root,
        source_root,
        post_spec,
        &[],
        None,
        None,
    )
}

/// Run the normal gate and prove the entire resolved post-edit owner assignment
/// is exactly the reviewed assignment, including owners that remain residual.
pub fn gate_post_edit_assignment_with_expected_owner_modules(
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    post_spec: &PostEditSpec,
    expected_owner_modules: &OwnerModuleAssignments,
    scope: Option<&ModuleTreeScope>,
) -> Result<()> {
    gate_post_edit_assignment_with_expected(
        owner_graph_path,
        modules_root,
        source_root,
        post_spec,
        &[],
        Some(expected_owner_modules),
        scope,
    )
}

fn gate_post_edit_assignment_with_expected(
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    post_spec: &PostEditSpec,
    expected: &[ExpectedResolvedMember],
    expected_owner_modules: Option<&OwnerModuleAssignments>,
    scope: Option<&ModuleTreeScope>,
) -> Result<()> {
    let owner_graph_report = crate::load_owner_graph_report(owner_graph_path)?;

    // The gate algorithm walks edges + assignment, not declared sets:
    // `from_report` leaves `declared` empty, which is fine for
    // `check_realizability`/`validate_assignment` (both consume the
    // assignment we build below, not the per-owner declared field).
    let owner_graph = OwnerGraph::from_report(&owner_graph_report)
        .with_context(|| format!("reconstructing owner graph {}", owner_graph_path.display()))?;

    // Source-backed claims resolve to the chunk-top bindings and anonymous
    // owners they claim. A claim the gate cannot resolve (missing chunk
    // source, unmatched/ambiguous selector) is a hard error — the claimed
    // owner must never silently fall to residual.
    let claim_sets = source_claim_sets_from_post_spec(post_spec);
    let resolved_claims = resolve_source_claims(
        &owner_graph_report,
        owner_graph_path,
        modules_root,
        source_root,
        &claim_sets,
    )?;
    let claim_projection = project_owner_claims(&owner_graph_report, post_spec, &resolved_claims)?;
    if let Some(expected_owner_modules) = expected_owner_modules {
        let actual_owner_modules = owner_module_assignments_from_projection(
            &owner_graph_report,
            modules_root,
            post_spec,
            &claim_projection,
            scope,
        )?;
        if &actual_owner_modules != expected_owner_modules {
            let owner_ids = actual_owner_modules
                .keys()
                .chain(expected_owner_modules.keys())
                .collect::<BTreeSet<_>>();
            let first_mismatch = owner_ids.into_iter().find(|owner_id| {
                actual_owner_modules.get(*owner_id) != expected_owner_modules.get(*owner_id)
            });
            if let Some(owner_id) = first_mismatch {
                bail!(
                    "post-edit selector resolution changed the reviewed owner assignment for {owner_id:?}: requested {:?}, resolved {:?}",
                    expected_owner_modules.get(owner_id),
                    actual_owner_modules.get(owner_id)
                );
            }
            bail!("post-edit owner assignment does not match the reviewed complete candidate");
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
    let mut of: Vec<ModuleId> = vec![residual; owner_graph.num_nodes()];
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
        for owner in &claim_projection.modules[module_idx].owners {
            of[owner.0] = mid;
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
    let atomic_groups = compute_atomic_groups(&owner_graph);
    let atomic_conflicts =
        detect_atomic_group_conflicts(&atomic_groups, &assignment, &owner_graph_report);
    if !atomic_conflicts.is_empty() {
        let conflicts = AtomicGroupConflictReport::from_conflicts(
            &atomic_conflicts,
            &module_path,
            &owner_graph,
        );
        write_rejection_artifact(
            owner_graph_path,
            output_layout::ATOMIC_GROUP_CONFLICTS_REPORT,
            &conflicts,
        )?;
        remove_rejection_artifact(owner_graph_path, output_layout::CYCLES_REPORT)?;
        let summary =
            render_atomic_group_conflict_summary(&atomic_conflicts, &module_path, &owner_graph);
        eprintln!("error: post-edit spec splits one or more atomic groups:\n{summary}");
        return Err(GateRejection {
            report: GateRejectionReport::AtomSplit { conflicts },
            message: "realizability gate rejected the edit (atom-split)",
        }
        .into());
    }

    // Check 2 — module-quotient cycles.
    let report = validate_assignment(&owner_graph, &assignment, &module_path);
    if !report.cycles.is_empty() {
        let blocking_sccs = BlockingSccEntry::from_cycle_reports(&report.cycles);
        write_rejection_artifact(
            owner_graph_path,
            output_layout::CYCLES_REPORT,
            &blocking_sccs,
        )?;
        remove_rejection_artifact(
            owner_graph_path,
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
    remove_rejection_artifact(owner_graph_path, output_layout::CYCLES_REPORT)?;
    remove_rejection_artifact(
        owner_graph_path,
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
    report: &OwnerGraphReport,
) -> Vec<AtomicGroupConflict> {
    let mut conflicts = Vec::new();
    for atomic_group in atomic_groups {
        let resolved: Vec<(OwnerId, ModuleId)> = atomic_group
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
        conflicts.push(AtomicGroupConflict {
            members: atomic_group.members.iter().copied().collect(),
            claims,
            causes: atomic_group.causes.clone(),
        });
    }
    conflicts
}
