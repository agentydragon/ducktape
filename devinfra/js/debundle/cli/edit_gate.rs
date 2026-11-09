//! Shared graph claim validation for spec-mutating CLI verbs.
//!
//! Ownership edits project their exact planned documents into [`PostEditSpec`]
//! and resolve direct and source-backed claims, expected members, and (for
//! reviewed refinement) the complete source-unit assignment. The canonical
//! candidate assessment supplies the verified write verdict.

use analysis_model::DepKind;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use analysis::{DependencyGraphInput, DependencyGraphReport};
use anonymous_resolution::{
    DirectBindingSourceCheck, DirectBindingSourceResolution, ResolvedSourceClaims, SourceClaimSet,
    resolve_source_claims,
};
use anyhow::{Context, Result, bail};
use graph_provenance::VerifiedGraphSourceContext;
use source_identity::SourceUnitId;
use spec::{BindingSourceKind, MemberSelectorSpec, ModulePath};
use spec_modules::{
    ModuleClaims, ModuleFile, collect_module_files, ensure_unaliased_module_tree, is_module_yaml,
    module_claims, module_path_from_file, read_module_claims,
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
    /// `--no-verify`: skip claim validation and candidate assessment. Can
    /// permit unresolved claims, name collisions, or an unaccepted candidate.
    #[arg(long)]
    pub no_verify: bool,
    /// dependency graph for validation; required unless --no-verify or the edit
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
    pub fn gate<'a>(
        &'a self,
        source_context: Option<&'a VerifiedGraphSourceContext>,
    ) -> Result<Gate<'a>> {
        Gate::from_cli(
            self.no_verify,
            self.dependency_graph_path.as_deref(),
            self.source_root.as_deref(),
            source_context,
        )
    }
}

/// How a spec-mutating CLI verb validates its edit.
#[derive(Debug, Clone)]
pub enum Gate<'a> {
    /// Resolve post-edit claims using this saved dependency graph. The
    /// canonical candidate assessment supplies verified-write acceptance.
    Run {
        graph_path: &'a Path,
        input: Arc<DependencyGraphInput>,
        /// Root used to resolve relative `source_location.source_path`
        /// values when the gate resolves source-backed selectors.
        source_root: Option<&'a Path>,
        /// The graph-preflighted physical source and retained input bytes.
        source_context: Option<&'a VerifiedGraphSourceContext>,
    },
    /// `--no-verify`: skip all validation.
    Skip,
}

#[derive(Clone, Copy)]
struct EditSourceLookup<'a> {
    root: Option<&'a Path>,
    verified: Option<&'a VerifiedGraphSourceContext>,
}

impl<'a> Gate<'a> {
    /// The single "graph or `--no-verify`" policy every spec-mutating
    /// verb shares.
    pub fn from_cli(
        no_verify: bool,
        graph: Option<&'a Path>,
        source_root: Option<&'a Path>,
        source_context: Option<&'a VerifiedGraphSourceContext>,
    ) -> Result<Self> {
        if no_verify {
            return Ok(Self::Skip);
        }
        match graph {
            Some(graph) => Ok(Self::Run {
                graph_path: graph,
                input: Arc::new(DependencyGraphInput::read(graph)?),
                source_root,
                source_context,
            }),
            None => bail!(
                "graph-backed claim validation requires --graph (path to dependency_graph.json.gz) or --no-verify"
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
            Self::Run { .. } => crate::outcome::GateOutcome::ClaimsChecked,
            Self::Skip => crate::outcome::GateOutcome::Skipped,
        }
    }

    /// Resolve post-edit claims and prove newly authored selectors still claim
    /// the binding their edit intended, not merely some unique declaration.
    pub fn check_claims(
        &self,
        modules_root: &Path,
        post_spec: impl FnOnce() -> Result<PostEditSpec>,
    ) -> Result<()> {
        self.check_expected_claims(modules_root, post_spec, &[])
    }

    pub fn check_expected_claims(
        &self,
        modules_root: &Path,
        post_spec: impl FnOnce() -> Result<PostEditSpec>,
        expected: &[ExpectedResolvedMember],
    ) -> Result<()> {
        match self {
            Self::Run {
                graph_path,
                input,
                source_root,
                source_context,
            } => check_post_edit_claims(
                graph_path,
                input,
                modules_root,
                EditSourceLookup {
                    root: *source_root,
                    verified: *source_context,
                },
                &post_spec()?,
                ExpectedPostEditClaims::Members(expected),
            ),
            Self::Skip => Ok(()),
        }
    }

    /// Resolve claims and prove the complete reviewed source-unit map.
    pub fn check_source_unit_claims(
        &self,
        modules_root: &Path,
        post_spec: &PostEditSpec,
        assignments: &SourceUnitModuleAssignments,
        scope: Option<&ModuleTreeScope>,
    ) -> Result<()> {
        match self {
            Self::Run {
                graph_path,
                input,
                source_root,
                source_context,
            } => check_post_edit_claims(
                graph_path,
                input,
                modules_root,
                EditSourceLookup {
                    root: *source_root,
                    verified: *source_context,
                },
                post_spec,
                ExpectedPostEditClaims::SourceUnits { assignments, scope },
            ),
            Self::Skip => Ok(()),
        }
    }
}

enum ExpectedPostEditClaims<'a> {
    Members(&'a [ExpectedResolvedMember]),
    SourceUnits {
        assignments: &'a SourceUnitModuleAssignments,
        scope: Option<&'a ModuleTreeScope>,
    },
}

#[derive(Debug, Clone)]
pub struct ExpectedResolvedMember {
    pub module_path: PathBuf,
    pub export_name: String,
    pub binding_name: String,
}

#[derive(Debug, Clone)]
pub struct PostEditModule {
    pub path: PathBuf,
    pub claims: ModuleClaims,
}

/// Simulated post-edit spec state — one entry per surviving module,
/// each listing the source unit claims declared by its `members:` and
/// `anonymous_statements:` arrays.
/// `modules` is keyed by absolute YAML path so the gate's
/// module-id assignment is deterministic across runs.
#[derive(Debug, Clone)]
pub struct PostEditSpec {
    /// Surviving module YAML paths (absolute), each with the set of
    /// binding/anonymous source unit claims it declares after the edit.
    pub modules: Vec<PostEditModule>,
}

/// Complete source unit assignment implied by a scoped module tree. `None` is the
/// residual destination; every graph source unit appears so edit callers can prove
/// the post-edit spec matches the whole reviewed assignment.
pub type SourceUnitModuleAssignments = BTreeMap<String, Option<String>>;

/// Resolution of one local binding name against the dependency graph.
///
/// Multiple declaration sites are accepted only when the report carries
/// `CoDeclaration` evidence for this exact binding connecting all of them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LocalBindingResolution {
    Resolved(BTreeSet<SourceUnitId>),
    Missing,
    Ambiguous(BTreeSet<SourceUnitId>),
}

/// How a member's binding claim reached the source unit projection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectedBindingKind {
    /// A direct local-name pin, retaining its optional source kind.
    LocalName {
        source_kind: Option<BindingSourceKind>,
    },
    /// A direct import claim. Imports are handled as re-exports and do not
    /// claim a local declaration source unit.
    ImportSpecifier,
    /// A non-name selector resolved by the canonical source resolver.
    SourceResolved,
}

/// One member's typed projection. `None` resolution is reserved for imports;
/// local claims always retain either resolved source units or an explicit mismatch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectedMemberClaim {
    pub binding_name: String,
    pub kind: ProjectedBindingKind,
    pub resolution: Option<LocalBindingResolution>,
}

/// A local name claim that could not be assigned to a unique declaration
/// identity using the dependency graph's declaration and co-declaration evidence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedLocalBindingClaim {
    pub module_path: PathBuf,
    pub claim_name: String,
    pub binding_name: String,
    pub source_kind: Option<BindingSourceKind>,
    pub resolution: LocalBindingResolution,
}

/// A typed direct binding claim that does not match exactly one source chunk
/// with the claimed declaration/import kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedDirectBindingSourceClaim {
    pub module_path: PathBuf,
    pub claim_name: String,
    pub binding_name: String,
    pub check: DirectBindingSourceCheck,
}

/// Typed claim projection shared by the normal realizability gate and the
/// complete source unit-assignment check used by `modules refine`.
#[derive(Debug, Clone, Default)]
pub struct ModuleClaimProjection {
    /// In the same order as `PostEditModule::claims.member_selectors`.
    pub members: Vec<ProjectedMemberClaim>,
    /// Local binding names contributed by this module, including source-backed
    /// claims, with declaration-site identity preserved.
    pub local_bindings: BTreeMap<String, LocalBindingResolution>,
    /// Every local source unit contributed by the module's resolved claims.
    pub source_units: BTreeSet<SourceUnitId>,
    /// Anonymous statement source units resolved by source location.
    pub anonymous_source_units: BTreeSet<SourceUnitId>,
}

#[derive(Debug, Clone, Default)]
pub struct SourceUnitClaimProjection {
    /// Per-module claims remain aligned with `PostEditSpec::modules`.
    pub modules: Vec<ModuleClaimProjection>,
    /// Local direct claims whose source unit identity is missing or ambiguous.
    /// The first projection consumer records these; the strict CLI edit check
    /// decides whether to reject them.
    pub unresolved_local_bindings: Vec<UnresolvedLocalBindingClaim>,
    /// Typed direct claims whose kind/name pair is missing, mismatched, or
    /// ambiguous in parsed source.
    pub unresolved_direct_binding_sources: Vec<UnresolvedDirectBindingSourceClaim>,
}

/// Per-projection index for mapping local binding spellings to declaration
/// source units. Connectivity is read from the canonical graph; report metadata
/// supplies the declaration and edge binding names.
struct LocalBindingIndex<'a> {
    input: &'a DependencyGraphInput,
    source_units_by_binding: BTreeMap<String, BTreeSet<SourceUnitId>>,
}

impl<'a> LocalBindingIndex<'a> {
    fn new(input: &'a DependencyGraphInput) -> Self {
        let mut source_units_by_binding = BTreeMap::<String, BTreeSet<SourceUnitId>>::new();
        for node in input.graph().iter_nodes() {
            for binding in &input.source_unit_report(node.id).declared_bindings {
                source_units_by_binding
                    .entry(binding.binding.to_string())
                    .or_default()
                    .insert(node.id);
            }
        }

        Self {
            input,
            source_units_by_binding,
        }
    }

    fn resolve(&self, binding_name: &str) -> LocalBindingResolution {
        let Some(candidates) = self.source_units_by_binding.get(binding_name) else {
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
        let graph = self.input.graph();
        while let Some(source_unit) = pending.pop() {
            // Co-declaration is an equality relation, including when the saved
            // report retains only one direction of an edge.
            for edge_id in graph
                .out_edges_of(source_unit)
                .iter()
                .chain(graph.in_edges_of(source_unit))
            {
                let edge = graph.edge(*edge_id);
                if edge.reason.kind != DepKind::CoDeclaration
                    || self.input.edge_report(*edge_id).binding.as_deref() != Some(binding_name)
                {
                    continue;
                }
                let next = if edge.from == source_unit {
                    edge.to
                } else {
                    edge.from
                };
                if candidates.contains(&next) && reached.insert(next) {
                    pending.push(next);
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

/// Resolve typed binding claims to source unit identities. Direct imports remain
/// distinct from local declarations, and repeated local declarations are
/// joined only when `dependency_graph.json.gz` carries matching `CoDeclaration` edges.
pub fn project_source_unit_claims(
    input: &DependencyGraphInput,
    post_spec: &PostEditSpec,
    resolved_claims: &[ResolvedSourceClaims],
) -> Result<SourceUnitClaimProjection> {
    anyhow::ensure!(
        post_spec.modules.len() == resolved_claims.len(),
        "resolved selector results do not match the post-edit module list"
    );
    let binding_index = LocalBindingIndex::new(input);
    let mut projection = SourceUnitClaimProjection::default();
    for (module, resolved) in post_spec.modules.iter().zip(resolved_claims) {
        let mut module_projection = ModuleClaimProjection::default();
        for (member_index, (claim_name, selector)) in
            module.claims.member_selectors.iter().enumerate()
        {
            match selector {
                MemberSelectorSpec::Binding(binding)
                    if binding.kind == Some(BindingSourceKind::ImportSpecifier) =>
                {
                    record_direct_binding_source_check(
                        &mut projection,
                        &module.path,
                        claim_name,
                        binding,
                        resolved.direct_binding_checks.get(member_index),
                    );
                    module_projection.members.push(ProjectedMemberClaim {
                        binding_name: binding.name.clone(),
                        kind: ProjectedBindingKind::ImportSpecifier,
                        resolution: None,
                    });
                }
                MemberSelectorSpec::Binding(binding) => {
                    record_direct_binding_source_check(
                        &mut projection,
                        &module.path,
                        claim_name,
                        binding,
                        resolved.direct_binding_checks.get(member_index),
                    );
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
        module_projection.anonymous_source_units = resolved.anonymous_source_units.clone();
        module_projection
            .source_units
            .extend(module_projection.anonymous_source_units.iter().copied());
        projection.modules.push(module_projection);
    }
    Ok(projection)
}

fn record_direct_binding_source_check(
    projection: &mut SourceUnitClaimProjection,
    module_path: &Path,
    claim_name: &str,
    binding: &spec::BindingSelector,
    check: Option<&Option<DirectBindingSourceCheck>>,
) {
    if binding.kind.is_none() {
        return;
    }
    let check =
        check
            .and_then(Option::as_ref)
            .cloned()
            .unwrap_or_else(|| DirectBindingSourceCheck {
                expected_kind: binding.kind.expect("checked above"),
                resolution: DirectBindingSourceResolution::Missing,
            });
    if !matches!(
        check.resolution,
        DirectBindingSourceResolution::Matched { .. }
    ) {
        projection
            .unresolved_direct_binding_sources
            .push(UnresolvedDirectBindingSourceClaim {
                module_path: module_path.to_path_buf(),
                claim_name: claim_name.to_string(),
                binding_name: binding.name.clone(),
                check,
            });
    }
}

fn record_local_binding_claim(
    projection: &mut SourceUnitClaimProjection,
    module_projection: &mut ModuleClaimProjection,
    module_path: &Path,
    claim_name: &str,
    binding_name: &str,
    source_kind: Option<BindingSourceKind>,
    resolution: LocalBindingResolution,
) {
    match &resolution {
        LocalBindingResolution::Resolved(source_units) => {
            module_projection
                .source_units
                .extend(source_units.iter().copied());
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

/// Build the complete source unit assignment represented by a typed claim
/// projection. Unresolved local claims remain visible in the projection and
/// are rejected by the strict edit check.
pub fn source_unit_module_assignments_from_projection(
    dependency_graph_report: &DependencyGraphReport,
    modules_root: &Path,
    post_spec: &PostEditSpec,
    projection: &SourceUnitClaimProjection,
    scope: Option<&ModuleTreeScope>,
) -> Result<SourceUnitModuleAssignments> {
    anyhow::ensure!(
        post_spec.modules.len() == projection.modules.len(),
        "projected selector results do not match the post-edit module list"
    );
    let mut assignments = BTreeMap::new();
    for node in &dependency_graph_report.nodes {
        anyhow::ensure!(
            assignments.insert(node.id.clone(), None).is_none(),
            "dependency_graph.json.gz repeats source unit ID {:?}",
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
        for source_unit in &projected.source_units {
            assign_source_unit_module(
                &mut assignments,
                dependency_graph_report,
                *source_unit,
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
                "resolved source unit index {} is absent from dependency_graph.json.gz",
                source_unit.0
            )
        })?;
    let destination = assignments.get_mut(&node.id).with_context(|| {
        format!(
            "resolved source unit {:?} is absent from dependency_graph.json.gz",
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

/// Parse a spec module YAML and return the source unit claims its
/// `members:` and `anonymous_statements:` entries declare.
pub fn read_gate_claims(path: &Path) -> Result<ModuleClaims> {
    if !is_module_yaml(path) {
        return Ok(ModuleClaims::default());
    }
    read_module_claims(path).with_context(|| format!("reading module {}", path.display()))
}

fn check_post_edit_claims(
    dependency_graph_path: &Path,
    input: &DependencyGraphInput,
    modules_root: &Path,
    source_lookup: EditSourceLookup<'_>,
    post_spec: &PostEditSpec,
    expected: ExpectedPostEditClaims<'_>,
) -> Result<()> {
    ensure_unaliased_module_tree(modules_root)?;
    let dependency_graph_report = input.report();

    // Source-backed claims resolve to the chunk-top bindings and anonymous
    // source units they claim. A claim the gate cannot resolve (missing chunk
    // source, unmatched/ambiguous selector) is a hard error — the claimed
    // source unit must never silently fall to residual.
    let claim_sets = source_claim_sets_from_post_spec(post_spec);
    let resolved_claims = resolve_source_claims(
        dependency_graph_report,
        dependency_graph_path,
        modules_root,
        source_lookup.root,
        source_lookup.verified,
        &claim_sets,
    )?;
    let claim_projection = project_source_unit_claims(input, post_spec, &resolved_claims)?;
    reject_unresolved_projected_claims(&claim_projection)?;
    match expected {
        ExpectedPostEditClaims::SourceUnits {
            assignments: expected_source_unit_modules,
            scope,
        } => {
            let actual_source_unit_modules = source_unit_module_assignments_from_projection(
                dependency_graph_report,
                modules_root,
                post_spec,
                &claim_projection,
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
                        "post-edit selector resolution changed the reviewed source unit assignment for {source_unit_id:?}: requested {:?}, resolved {:?}",
                        expected_source_unit_modules.get(source_unit_id),
                        actual_source_unit_modules.get(source_unit_id)
                    );
                }
                bail!(
                    "post-edit source unit assignment does not match the reviewed complete candidate"
                );
            }
        }
        ExpectedPostEditClaims::Members(expected) => {
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
        }
    }

    Ok(())
}

fn reject_unresolved_projected_claims(projection: &SourceUnitClaimProjection) -> Result<()> {
    let mut mismatches = Vec::new();
    for unresolved in &projection.unresolved_local_bindings {
        let resolution = match &unresolved.resolution {
            LocalBindingResolution::Missing => {
                "no matching local declaration source unit exists".to_string()
            }
            LocalBindingResolution::Ambiguous(source_units) => {
                format!(
                    "local name resolves to ambiguous declaration source units {source_units:?}"
                )
            }
            LocalBindingResolution::Resolved(_) => continue,
        };
        mismatches.push(format!(
            "- module {} member {:?} claims local binding {:?}: {resolution}",
            unresolved.module_path.display(),
            unresolved.claim_name,
            unresolved.binding_name
        ));
    }
    for unresolved in &projection.unresolved_direct_binding_sources {
        let reason = match &unresolved.check.resolution {
            DirectBindingSourceResolution::Matched { .. } => continue,
            DirectBindingSourceResolution::Missing => {
                "no source declaration or import with this kind was found".to_string()
            }
            DirectBindingSourceResolution::WrongKind { observed } => {
                format!("source occurrences have incompatible kinds: {observed:?}")
            }
            DirectBindingSourceResolution::Ambiguous { source_paths } => {
                format!("the typed claim occurs in multiple source chunks: {source_paths:?}")
            }
        };
        mismatches.push(format!(
            "- module {} member {:?} claims binding {:?} with kind {:?}: {reason}",
            unresolved.module_path.display(),
            unresolved.claim_name,
            unresolved.binding_name,
            unresolved.check.expected_kind
        ));
    }
    if mismatches.is_empty() {
        return Ok(());
    }
    bail!(
        "post-edit binding claims are unresolved or do not match source:\n{}",
        mismatches.join("\n")
    )
}
