//! `ChunkPlanBuilder` owns the per-chunk mutable state that
//! `materialize_logical_chunk` threads across its planning phases. Each phase is a
//! method on the builder. `bindings_catalogue` is the authoritative binding
//! destination state; owned-only lookup maps are derived when a downstream
//! analysis needs that projection.

use super::outcome_sink::OutcomeSink;
use crate::anonymous::ResolvedAnonymousStatement;
use crate::exports::PublicExportSurface;
use crate::imports::{ArtifactSourceImportResolutionCache, resolve_imported_binding};
use crate::ownership_subset::SelectedModule;
use crate::plans::ModulePlan;
use crate::rebind_fold::RebindFold;
use crate::util::target_file_for_request;
use analysis::assignment_assembly::assemble_assignment_with_source_unit_claims;
use analysis::{
    BindingCatalogue, BindingKind, DependencyGraphAndAtomicGroups, DestructuringDeclaratorGroup,
    DestructuringGroupOwnerClassification, EntryDestination, ModuleId, ModuleRole,
    NestedVarDestinationClassification, NestedVarStatement, SourceUnitClass, SourceUnitLayout,
    chunk_constraining_module_edges, classify_destructuring_group_owners,
    classify_nested_var_placement, top_level_id,
};
use anyhow::{Context, Result, bail, ensure};
use frontend_elaboration::MemberClaimProjection;
use frontend_elaboration::chunk_ast::{SourceExportIdentity, TopLevelDecl, binding_declaration};
use frontend_elaboration::source_ast::RuntimeImportFacts;
use gate::simulated_evaluation_post_order;
use selector_outcome::{
    Declaration, Entity, EntityRef, Outcome, ResolvedBy, SelectorOutcome, SelectorOutcomeReport,
};
use selector_resolve::{EntityIndex, EntityOutcome, MemberSelector, Resolution};
use source_identity::ChunkId;
use source_identity::SourceUnitId;
use source_identity::StatementId;
use spec_resolution::{LogicalRequest, MemberRequest, reject_duplicate_member_bindings};
use std::collections::VecDeque;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_ecma_ast::*;

/// The module path of a `<chunk>::<path>` logical module id.
fn logical_module_path(request_id: &str) -> String {
    request_id
        .split_once("::")
        .map(|(_, path)| path.to_string())
        .unwrap_or_else(|| panic!("logical module id {request_id:?} is not `<chunk>::<path>`"))
}

fn member_outcome(
    chunk_id: &str,
    request: &LogicalRequest,
    member: &MemberRequest,
    outcome: Outcome,
) -> SelectorOutcome {
    selector_resolve::member_outcome(
        chunk_id,
        &request.target_path,
        &member.export_name,
        &member.selector,
        outcome,
    )
}

/// Output of `ChunkPlanBuilder::finalize`: everything downstream
/// `lower_chunk` + the chunk-report builder need from the plan
/// construction phase.
pub(super) struct ChunkPlan {
    pub(super) module_plans: Vec<ModulePlan>,
    pub(super) bindings_catalogue: BindingCatalogue,
    pub(super) whole_statement_assignment: BTreeMap<StatementId, usize>,
    /// Positional IDs of plans constructed specifically by coarse residual
    /// synthesis. This provenance is never inferred from a module path.
    pub(super) generated_residual_plans: BTreeSet<usize>,
    /// Source units adopted by an explicitly opted-in authored module to
    /// complete a partially claimed atomic group.
    pub(super) atomic_group_owner_completions: Vec<AtomicGroupOwnerCompletion>,
    pub(super) unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
}

pub(super) struct AtomicGroupOwnerCompletion {
    pub(super) owner_path: String,
    pub(super) atomic_group_source_unit_ids: Vec<SourceUnitId>,
    pub(super) adopted_source_unit_ids: Vec<SourceUnitId>,
}

struct CoarseResidualBucket {
    source_units: BTreeSet<SourceUnitId>,
    export_surface: PublicExportSurface,
}

/// Per-explicit-request inputs the builder reads but does not own.
pub(super) struct ExplicitRequestContext<'a> {
    pub(super) body: &'a [ModuleItem],
    pub(super) declaration_by_name: &'a HashMap<Id, Vec<StatementId>>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
    pub(super) target_dir: &'a str,
    pub(super) chunk_id: &'a str,
    pub(super) target_file: &'a str,
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
}

/// Builds a `ChunkPlan` from spec requests and chunk AST analysis.
///
/// Owns canonical binding destinations, anonymous statement destinations, and
/// module export/name plans. All duplicate-claim / cross-claim invariants live
/// behind the builder's methods. Owned binding destinations are never maintained
/// in a second mutable map.
pub(super) struct ChunkPlanBuilder {
    /// Per-source-body-index → `module_plans` index, for anonymous
    /// (non-declared) top-level statements claimed by spec
    /// `anonymous_statements` entries.
    whole_statement_assignment: BTreeMap<StatementId, usize>,
    /// The plans being constructed, in append order. Final indices
    /// stable: binding destinations and `whole_statement_assignment`
    /// hold positional references.
    module_plans: Vec<ModulePlan>,
    /// Positional IDs of plans constructed by coarse residual synthesis.
    generated_residual_plans: BTreeSet<usize>,
    atomic_group_owner_completions: Vec<AtomicGroupOwnerCompletion>,
    /// Authoritative destination of every claimed binding, including imported
    /// bindings claimed only for re-export. Module plans retain export/name
    /// decisions; syntax placement reads this catalogue.
    bindings_catalogue: HashMap<Id, BindingKind>,
    /// Index into `module_plans` of the "catchall" plan that
    /// unclaimed bindings sweep into, when one exists. `None` when
    /// the chunk has no residual landing site (default `InlineInEntry` with
    /// no fallback request, `PerAtomicGroup`, or `CoarseAtomicGroups`).
    residual_plan_index: Option<usize>,
    /// Spec claims that named a binding for which no top-level
    /// declaration exists in this chunk. Materialization keeps
    /// running with the missing claim treated as if absent; the
    /// caller fails the pipeline at the end with the rolled-up list.
    unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
    /// Name-keyed index into `bindings_catalogue` for the
    /// duplicate-claim check inside `add_explicit_request`. The
    /// previous shape — a linear scan of the entire `bindings_catalogue`
    /// HashMap on every member of every request — was the dominant
    /// cost in `build_module_plans` on chunks with thousands of spec
    /// modules (O(N^2) over the growing catalogue). Every catalogue
    /// key is constructed via `top_level_id(name,
    /// chunk_top_level_mark)`, so the `name` alone uniquely
    /// identifies the catalogue entry within a chunk; we mirror
    /// binding identities in this index and look up by `&str` to keep
    /// duplicate detection O(1) per member. Destinations are always read from
    /// the canonical catalogue, never copied into this spelling index.
    ///
    /// Only consulted during `add_explicit_request`; later phases
    /// (destructure siblings, residual sweep) append without
    /// name-collision checks. The map is dropped by
    /// `drop_explicit_request_scratch`.
    catalogue_index_by_name: HashMap<String, Id>,
    /// Name-keyed duplicate-check scratch for binding selectors that have a
    /// known source binding spelling but whose ownership is claimed after chunk
    /// analysis through the global solver.
    deferred_binding_claims_by_name: HashMap<String, EntityRef>,
    /// Every selector outcome worth reporting. In keep-going mode a failed
    /// selector is left out of canonical ownership, so later modules in the
    /// chunk can still be checked; the chunk fails in `finalize` if any
    /// outcome is an error.
    outcomes: OutcomeSink,
}

impl ChunkPlanBuilder {
    pub(super) fn new(fail_fast: bool, list_template_identifiers: bool) -> Self {
        Self {
            whole_statement_assignment: BTreeMap::new(),
            module_plans: Vec::new(),
            generated_residual_plans: BTreeSet::new(),
            atomic_group_owner_completions: Vec::new(),
            bindings_catalogue: HashMap::new(),
            residual_plan_index: None,
            unmatched_spec_claims: Vec::new(),
            catalogue_index_by_name: HashMap::new(),
            deferred_binding_claims_by_name: HashMap::new(),
            outcomes: OutcomeSink::new(fail_fast, list_template_identifiers),
        }
    }

    /// Restrict ownership to selected explicit modules after the full selector
    /// solve, duplicate checks, analysis-assumption collection, and resolved
    /// member-site collection have completed, but before residual routing,
    /// rebind folds and assignment analysis run.
    ///
    /// The selector outcomes, unmatched claims, and no-reentry assertions stay
    /// attached to the builder so filtering cannot hide an invalid full spec.
    /// Partial selections remove derived sibling/anonymous binding owners;
    /// the caller reapplies the existing placement rules after filtering.
    pub(super) fn retain_modules(&mut self, selected: &BTreeSet<SelectedModule>) -> Result<()> {
        ensure!(
            self.residual_plan_index.is_none(),
            "cannot retain an ownership subset while a residual plan exists"
        );
        if let Some(plan) = self
            .module_plans
            .iter()
            .find(|plan| plan.role != ModuleRole::Specified)
        {
            bail!(
                "cannot retain an ownership subset with non-Specified module plan {:?}",
                plan.id
            );
        }

        let choices = selected
            .iter()
            .map(|entry| (&entry.module, &entry.bindings))
            .collect::<BTreeMap<_, _>>();
        let mut remapped_indices = vec![None; self.module_plans.len()];
        let mut matched_selected = BTreeSet::new();
        let mut next_index = 0;
        for (old_index, plan) in self.module_plans.iter().enumerate() {
            let path = spec::ModulePath::parse(&plan.target_path, "")
                .with_context(|| format!("module plan {:?} has an invalid target path", plan.id))?;
            if choices.contains_key(&path) {
                ensure!(
                    matched_selected.insert(path.clone()),
                    "multiple module plans have selected target path {path}"
                );
                remapped_indices[old_index] = Some(next_index);
                next_index += 1;
            }
        }

        let missing_selected = selected
            .iter()
            .map(|entry| &entry.module)
            .filter(|path| !matched_selected.contains(*path))
            .map(ToString::to_string)
            .collect::<Vec<_>>();
        ensure!(
            missing_selected.is_empty(),
            "selected modules have no explicit plan: {}",
            missing_selected.join(", ")
        );

        let remap_index = |old_index: usize| -> Result<Option<usize>> {
            remapped_indices.get(old_index).copied().with_context(|| {
                format!("ownership claim references missing module plan {old_index}")
            })
        };

        let mut bindings_catalogue = HashMap::new();
        // Metadata is keyed by source name for both owned bindings and imported
        // re-exports, even though imports have no entry in plan.bindings.
        let mut retained_claim_names = vec![BTreeSet::<String>::new(); self.module_plans.len()];
        for (binding, kind) in &self.bindings_catalogue {
            let mut retained_kind = kind.clone();
            let referenced_module = match &mut retained_kind {
                BindingKind::Owned { module } => module,
                BindingKind::Imported { re_exporter, .. } => re_exporter,
            };
            let old_index = referenced_module.0;
            let Some(new_index) = remap_index(old_index)? else {
                continue;
            };
            let path = spec::ModulePath::parse(&self.module_plans[old_index].target_path, "")?;
            let keep = match choices[&path] {
                None => true,
                Some(names) => match kind {
                    BindingKind::Owned { .. } => {
                        let source_name = binding.0.as_ref();
                        self.module_plans[old_index]
                            .binding_claim_origins
                            .contains_key(source_name)
                            && self.module_plans[old_index]
                                .bindings
                                .get(source_name)
                                .is_some_and(|name| names.contains(name))
                    }
                    BindingKind::Imported { public_name, .. } => {
                        names.contains(public_name.as_ref())
                    }
                },
            };
            if keep {
                referenced_module.0 = new_index;
                retained_claim_names[old_index].insert(binding.0.to_string());
                bindings_catalogue.insert(binding.clone(), retained_kind);
            }
        }

        let mut whole_statement_assignment = BTreeMap::new();
        for (statement, old_index) in &self.whole_statement_assignment {
            if let Some(new_index) = remap_index(*old_index)? {
                whole_statement_assignment.insert(*statement, new_index);
            }
        }

        let old_module_plans = std::mem::take(&mut self.module_plans);
        self.module_plans = old_module_plans
            .into_iter()
            .enumerate()
            .filter_map(|(old_index, mut plan)| {
                remapped_indices[old_index].map(|_| {
                    let retained = &retained_claim_names[old_index];
                    plan.bindings.retain(|name, _| retained.contains(name));
                    plan.spec_named_bindings
                        .retain(|name| retained.contains(name));
                    plan.binding_comments
                        .retain(|name, _| retained.contains(name));
                    plan.binding_claim_origins
                        .retain(|name, _| retained.contains(name));
                    plan
                })
            })
            .collect();
        self.bindings_catalogue = bindings_catalogue;
        self.whole_statement_assignment = whole_statement_assignment;
        Ok(())
    }

    /// Process one explicit (non-residual) logical-module request:
    /// claim each member that does not need chunk-analysis facts, and append a
    /// `ModulePlan`.
    /// Solver-resolved members and anonymous statement selectors are left
    /// unclaimed until `resolve_and_claim_global_selectors` runs over chunk
    /// analysis facts. Duplicate-claim detection across all explicit requests is
    /// keyed by binding-name via the builder's scratch indexes.
    pub(super) fn add_explicit_request(
        &mut self,
        index: usize,
        request: &mut LogicalRequest,
        ctx: &ExplicitRequestContext<'_>,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
    ) -> Result<()> {
        reject_duplicate_member_bindings("logical_module", &request.id, &request.members)?;
        let mut bindings = HashMap::<String, String>::new();
        let dest_target_file = target_file_for_request(ctx.target_dir, &request.target_path)?;
        let module_id = ModuleId(index);
        let mut duplicate_bindings = BTreeSet::<String>::new();
        for member in &request.members {
            if !member.binding.is_empty() {
                if let Some(existing_kind) = self
                    .catalogue_index_by_name
                    .get(member.binding.as_str())
                    .map(|id| &self.bindings_catalogue[id])
                {
                    let duplicate = self.duplicate_claim_for(
                        existing_kind,
                        &member.binding,
                        binding_declaration(
                            ctx.body,
                            ctx.declaration_by_name,
                            &top_level_id(&member.binding, ctx.chunk_top_level_mark),
                        )?,
                        ctx.chunk_id,
                        request,
                        member,
                    );
                    self.outcomes.record(duplicate)?;
                    duplicate_bindings.insert(member.binding.clone());
                    continue;
                }
                if let Some(existing) = self
                    .deferred_binding_claims_by_name
                    .get(member.binding.as_str())
                {
                    self.outcomes.record(member_outcome(
                        ctx.chunk_id,
                        request,
                        member,
                        Outcome::DuplicateClaim {
                            binding: member.binding.clone(),
                            declaration: binding_declaration(
                                ctx.body,
                                ctx.declaration_by_name,
                                &top_level_id(&member.binding, ctx.chunk_top_level_mark),
                            )?,
                            claimed_by: existing.clone(),
                        },
                    ))?;
                    duplicate_bindings.insert(member.binding.clone());
                    continue;
                }
            }
            if member.resolves_after_chunk_analysis() {
                if matches!(member.selector, MemberSelector::Binding(_)) {
                    let binding_id = top_level_id(&member.binding, ctx.chunk_top_level_mark);
                    if !ctx.declaration_by_name.contains_key(&binding_id) {
                        self.outcomes.record_selector_result(
                            ctx.chunk_id,
                            &request.target_path,
                            Entity::Export(member.export_name.clone()),
                            false,
                        );
                        self.unmatched_spec_claims.push(crate::UnmatchedSpecClaim {
                            chunk_id: ctx.chunk_id.to_string(),
                            module_path: spec::ModulePath::parse(&request.target_path, "")
                                .expect("request target_path is a canonical module path"),
                            binding_name: member.binding.clone(),
                            export_name: member.export_name.clone(),
                        });
                        continue;
                    }
                }
                if !member.binding.is_empty() {
                    self.deferred_binding_claims_by_name.insert(
                        member.binding.clone(),
                        EntityRef {
                            logical_module: request.target_path.clone(),
                            entity: Some(Entity::Export(member.export_name.clone())),
                        },
                    );
                }
                continue;
            }
            if member.selector.is_import_specifier() {
                let (imported_name, imported_from) = resolve_imported_binding(
                    imported_binding_resolver,
                    ctx.runtime_import_facts,
                    ctx.chunk_id,
                    ctx.target_file,
                    &member.binding,
                    imported_from_by_src,
                )?;
                self.outcomes.record_selector_result(
                    ctx.chunk_id,
                    &request.target_path,
                    Entity::Export(member.export_name.clone()),
                    true,
                );
                let kind = BindingKind::Imported {
                    imported_name: imported_name.into(),
                    imported_from,
                    re_exporter: module_id,
                    public_name: member.export_name.as_str().into(),
                };
                let binding_id = top_level_id(&member.binding, ctx.chunk_top_level_mark);
                self.catalogue_index_by_name
                    .insert(member.binding.clone(), binding_id.clone());
                self.bindings_catalogue.insert(binding_id, kind);
            } else {
                bindings.insert(member.binding.clone(), member.export_name.clone());
            }
        }
        for (binding, export_name) in &bindings {
            let binding_id = top_level_id(binding, ctx.chunk_top_level_mark);
            if ctx.declaration_by_name.contains_key(&binding_id) {
                let kind = BindingKind::Owned { module: module_id };
                self.catalogue_index_by_name
                    .insert(binding.clone(), binding_id.clone());
                self.bindings_catalogue.insert(binding_id, kind);
            } else {
                // The spec claimed a binding name that does not
                // appear as a top-level declaration in this chunk —
                // the previous behavior silently dropped the claim,
                // leaving the destination module short one export
                // and the binding falling into the residual sweep.
                // Record it so the pipeline can fail at the end
                // with the full list across every chunk; meanwhile
                // keep lowering as if the spec had not claimed the
                // name (lower_chunk only touches binding ids it can
                // resolve, so the missing claim is a no-op here).
                self.unmatched_spec_claims.push(crate::UnmatchedSpecClaim {
                    chunk_id: ctx.chunk_id.to_string(),
                    module_path: spec::ModulePath::parse(&request.target_path, "")
                        .expect("request target_path is a canonical module path"),
                    binding_name: binding.clone(),
                    export_name: export_name.clone(),
                });
            }
        }
        let binding_comments: BTreeMap<String, String> = request
            .members
            .iter()
            .filter(|member| !member.resolves_after_chunk_analysis())
            .filter(|member| !duplicate_bindings.contains(member.binding.as_str()))
            .filter_map(|member| {
                member
                    .comment
                    .as_ref()
                    .map(|c| (member.binding.clone(), c.clone()))
            })
            .collect();
        let binding_claim_origins: BTreeMap<String, String> = request
            .members
            .iter()
            .filter(|member| !member.resolves_after_chunk_analysis())
            .filter(|member| !duplicate_bindings.contains(member.binding.as_str()))
            .map(|member| (member.binding.clone(), member.claim_origin.clone()))
            .collect();
        self.module_plans.push(ModulePlan {
            id: request.id.clone(),
            target_file: dest_target_file,
            target_path: request.target_path.clone(),
            role: ModuleRole::Specified,
            bindings,
            spec_named_bindings: request
                .members
                .iter()
                .filter(|member| member.explicit_export_name && !member.binding.is_empty())
                .map(|member| member.binding.clone())
                .collect(),
            anonymous_statements: Vec::new(),
            anonymous_statement_comments: BTreeMap::new(),
            comment: request.comment.clone(),
            binding_comments,
            binding_claim_origins,
        });
        Ok(())
    }

    fn claim_anonymous_statement(
        &mut self,
        module_index: usize,
        request_id: &str,
        claim: &ResolvedAnonymousStatement,
    ) -> Result<()> {
        if let Some(existing) = self
            .whole_statement_assignment
            .get(&claim.body_index)
            .copied()
        {
            let existing_id: String = self
                .module_plans
                .get(existing)
                .map(|plan: &ModulePlan| plan.id.clone())
                .unwrap_or_else(|| format!("<plan#{existing}>"));
            bail!(
                "anonymous_statements[].match in module {} also matches the top-level \
                 statement at body index {} already claimed by module {}; each anonymous \
                 statement may belong to at most one logical module.",
                request_id,
                claim.body_index,
                existing_id,
            );
        }
        self.whole_statement_assignment
            .insert(claim.body_index, module_index);
        let plan = self.module_plans.get_mut(module_index).with_context(|| {
            format!(
                "logical_module {request_id}: anonymous statement resolved before its module plan existed"
            )
        })?;
        plan.anonymous_statements.push(claim.body_index);
        plan.anonymous_statements.sort_unstable();
        plan.anonymous_statements.dedup();
        if let Some(comment) = &claim.comment {
            plan.anonymous_statement_comments
                .insert(claim.body_index, comment.clone());
        }
        Ok(())
    }

    fn record(&mut self, outcome: SelectorOutcome) -> Result<()> {
        self.outcomes.record(outcome)
    }

    /// Claims what `resolution` resolved of `selectors`, and records every
    /// other outcome.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn claim_resolution(
        &mut self,
        explicit_requests: &[LogicalRequest],
        resolution: Resolution,
        resolved_members: Vec<Vec<usize>>,
        member_claims: &MemberClaimProjection,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        body: &[ModuleItem],
        declaration_by_name: &HashMap<Id, Vec<StatementId>>,
    ) -> Result<()> {
        self.outcomes.list_templates(resolution.templates);
        // Recorded last, once every claim is in.
        let mut eliminated = Vec::new();
        for EntityOutcome {
            module: index,
            entity,
            outcome,
        } in resolution.outcomes
        {
            let request = &explicit_requests[index];
            let Outcome::Resolved {
                body_index,
                binding,
                resolved_by,
            } = outcome.outcome.clone()
            else {
                self.record(outcome)?;
                continue;
            };
            let claimed = match entity {
                EntityIndex::AnonymousStatement(statement_index) => {
                    let statement = StatementId(body_index);
                    ensure!(
                        member_claims
                            .claimed_anonymous_statements
                            .get(&(index, statement_index))
                            == Some(&statement),
                        "frontend anonymous-statement claim projection disagrees for logical_module {} statement {} at body index {}",
                        request.id,
                        statement_index,
                        body_index,
                    );
                    self.claim_anonymous_statement(
                        index,
                        &request.id,
                        &ResolvedAnonymousStatement {
                            body_index: statement,
                            comment: request.anonymous_statements[statement_index]
                                .comment
                                .clone(),
                        },
                    )?;
                    true
                }
                EntityIndex::Member(member_index) => {
                    let request_member_index = resolved_members[index][member_index];
                    let member = &request.members[request_member_index];
                    let binding = binding.with_context(|| {
                        format!(
                            "logical_module {}: global selector solver resolved member `{}` to \
                             body item {body_index} without a single declared binding",
                            request.id, member.export_name,
                        )
                    })?;
                    let applied = self.claim_binding_after_chunk_analysis(
                        request,
                        member,
                        &binding,
                        index,
                        chunk_top_level_mark,
                        chunk_id,
                        body,
                        declaration_by_name,
                    )?;
                    let projected = member_claims
                        .claimed_members
                        .get(&(index, request_member_index))
                        == Some(&binding);
                    ensure!(
                        applied == projected,
                        "frontend member-claim projection disagrees for logical_module {} member {} (binding `{binding}`): projected {}, applied {}",
                        request.id,
                        member.export_name,
                        projected,
                        applied,
                    );
                    applied
                }
            };
            if matches!(
                resolved_by,
                ResolvedBy::Elimination { .. } | ResolvedBy::ReferencedBy { .. }
            ) {
                eliminated.push(outcome);
            } else if let Some(placement) = &outcome.placement
                && let Some(entity) = &placement.entity
            {
                self.outcomes.record_selector_result(
                    &outcome.chunk,
                    &placement.logical_module,
                    entity.clone(),
                    claimed,
                );
            }
        }
        eliminated
            .into_iter()
            .try_for_each(|outcome| self.record(outcome))
    }

    /// The outcome of `member` resolving to `binding`, which `existing_kind`
    /// already claims.
    fn duplicate_claim_for(
        &self,
        existing_kind: &BindingKind,
        binding: &str,
        declaration: Declaration,
        chunk_id: &str,
        request: &LogicalRequest,
        member: &MemberRequest,
    ) -> SelectorOutcome {
        let (plan_index, export_name) = match existing_kind {
            BindingKind::Owned {
                module: ModuleId(owner_index),
            } => (
                *owner_index,
                self.module_plans[*owner_index]
                    .bindings
                    .get(binding)
                    .cloned(),
            ),
            BindingKind::Imported {
                re_exporter: ModuleId(re_index),
                public_name,
                ..
            } => (*re_index, Some(public_name.to_string())),
        };
        member_outcome(
            chunk_id,
            request,
            member,
            Outcome::DuplicateClaim {
                binding: binding.to_string(),
                declaration,
                claimed_by: EntityRef {
                    logical_module: logical_module_path(&self.module_plans[plan_index].id),
                    entity: export_name.map(Entity::Export),
                },
            },
        )
    }

    fn same_module_duplicate_source_binding_report(
        &self,
        existing_kind: &BindingKind,
        binding: &str,
        index: usize,
        request: &LogicalRequest,
        member: &MemberRequest,
    ) -> Option<String> {
        member.selector.source_match()?;
        let BindingKind::Owned {
            module: ModuleId(owner_index),
        } = existing_kind
        else {
            return None;
        };
        if *owner_index != index {
            return None;
        }
        let plan = self.module_plans.get(index)?;
        let existing_export = plan.bindings.get(binding)?;
        let existing_origin = plan
            .binding_claim_origins
            .get(binding)
            .map(String::as_str)
            .unwrap_or("<unknown origin>");
        Some(format!(
            "logical_module {} has duplicate source binding claims:\n- source binding `{}` \
             claimed 2 times:\n  - export `{}` ({})\n  - export `{}` ({})",
            request.id,
            binding,
            existing_export,
            existing_origin,
            member.export_name,
            member.claim_origin,
        ))
    }

    /// Claim a binding the global selector solver resolved to: record an
    /// unmatched-claim if the binding has no top-level declaration; error / record a
    /// duplicate if already claimed; otherwise move it out of the residual sweep and
    /// into module `index`, registering its export name, claim origin, and comment.
    /// The binding was parked at the residual plan when the pre-analysis sweep ran,
    /// before the solver knew its identity.
    #[allow(clippy::too_many_arguments)]
    fn claim_binding_after_chunk_analysis(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        binding: &str,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        body: &[ModuleItem],
        declaration_by_name: &HashMap<Id, Vec<StatementId>>,
    ) -> Result<bool> {
        let binding_id = top_level_id(binding, chunk_top_level_mark);
        if !declaration_by_name.contains_key(&binding_id) {
            // Mirror the named-member path: a resolved binding with no top-level
            // declaration in this chunk is recorded and the pipeline fails at the
            // end with the full list, rather than half-claiming it here.
            self.unmatched_spec_claims.push(crate::UnmatchedSpecClaim {
                chunk_id: chunk_id.to_string(),
                module_path: spec::ModulePath::parse(&request.target_path, "")
                    .expect("request target_path is a canonical module path"),
                binding_name: binding.to_string(),
                export_name: member.export_name.clone(),
            });
            return Ok(false);
        }
        let module_id = ModuleId(index);
        if let Some(existing_kind) = self
            .catalogue_index_by_name
            .get(binding)
            .map(|id| &self.bindings_catalogue[id])
        {
            if let Some(report) = self.same_module_duplicate_source_binding_report(
                existing_kind,
                binding,
                index,
                request,
                member,
            ) {
                bail!("{report}");
            }
            let duplicate = self.duplicate_claim_for(
                existing_kind,
                binding,
                binding_declaration(body, declaration_by_name, &binding_id)?,
                chunk_id,
                request,
                member,
            );
            self.record(duplicate)?;
            return Ok(false);
        }
        // The residual sweep ran before chunk analysis (before this pass), so the target
        // binding — unclaimed at sweep time — was parked at the residual plan. Move
        // it out: overwrite its assignment and prune the residual plan's export
        // list, so the residual module doesn't keep `export { <binding> }` for a
        // declaration that now lives in the explicit module.
        if let Some(prev) = owned_destination(&self.bindings_catalogue, &binding_id)
            && Some(prev) == self.residual_plan_index
            && let Some(residual_idx) = self.residual_plan_index
        {
            self.module_plans[residual_idx].bindings.remove(binding);
        }
        let kind = BindingKind::Owned { module: module_id };
        self.catalogue_index_by_name
            .insert(binding.to_string(), binding_id.clone());
        self.bindings_catalogue.insert(binding_id, kind);
        let plan = &mut self.module_plans[index];
        plan.bindings
            .insert(binding.to_string(), member.export_name.clone());
        if member.explicit_export_name {
            plan.spec_named_bindings.insert(binding.to_string());
        }
        plan.binding_claim_origins
            .insert(binding.to_string(), member.claim_origin.clone());
        if let Some(comment) = &member.comment {
            plan.binding_comments
                .insert(binding.to_string(), comment.clone());
        }
        Ok(true)
    }

    /// Drop the name-keyed catalogue scratch index now that the
    /// explicit-requests loop is finished. Destructure siblings and
    /// the residual sweep don't consult this index.
    pub(super) fn drop_explicit_request_scratch(&mut self) {
        self.catalogue_index_by_name = HashMap::new();
        self.deferred_binding_claims_by_name = HashMap::new();
    }

    /// Apply the shared placement-kernel decision to each retained
    /// destructuring declarator. The kernel owns the colocation rule; this
    /// builder applies only its one-owner pull recommendation to the canonical
    /// binding catalogue and export plans. Conflicting requests stay intact
    /// for the final projection to report through the shared candidate path.
    pub(super) fn apply_destructuring_group_placement(
        &mut self,
        destructuring_groups: &[DestructuringDeclaratorGroup],
    ) {
        // Overlapping `var` declarators can share a hygienic binding. Iterate
        // until no more siblings move, so a pull through a later group also
        // reaches earlier groups that share the newly placed binding.
        loop {
            let mut moved_sibling = false;
            for group in destructuring_groups {
                // The request-planning check intentionally sees explicit owners
                // only. A residual-owned sibling is still movable and is pulled
                // together with an unclaimed sibling below, matching the existing
                // plan-construction behavior.
                let explicit_owners = group.bindings.iter().filter_map(|binding| {
                    owned_destination(&self.bindings_catalogue, binding)
                        .filter(|destination| Some(*destination) != self.residual_plan_index)
                        .map(|destination| (binding.clone(), ModuleId(destination)))
                });
                let decision = classify_destructuring_group_owners(group, explicit_owners);
                match &decision.classification {
                    DestructuringGroupOwnerClassification::Unclaimed => {}
                    // Keep every authored owner request for final projection.
                    // Do not turn the planning classifier into a second veto.
                    DestructuringGroupOwnerClassification::Split { .. } => {}
                    DestructuringGroupOwnerClassification::OneOwner { destination } => {
                        let already_owned: BTreeSet<_> = decision
                            .owners
                            .iter()
                            .filter(|(_, owner)| owner == destination)
                            .map(|(binding, _)| binding.clone())
                            .collect();
                        let bindings: Vec<_> = group
                            .bindings
                            .iter()
                            .filter(|binding| !already_owned.contains(*binding))
                            .collect();
                        let owner_index = destination.0;
                        for sibling_id in bindings {
                            let sibling = sibling_id.0.to_string();
                            match owned_destination(&self.bindings_catalogue, sibling_id) {
                                None => {
                                    self.bindings_catalogue.insert(
                                        sibling_id.clone(),
                                        BindingKind::Owned {
                                            module: *destination,
                                        },
                                    );
                                    self.module_plans[owner_index]
                                        .bindings
                                        .insert(sibling.clone(), sibling);
                                    moved_sibling = true;
                                }
                                Some(other_index)
                                    if Some(other_index) == self.residual_plan_index =>
                                {
                                    self.bindings_catalogue.insert(
                                        sibling_id.clone(),
                                        BindingKind::Owned {
                                            module: *destination,
                                        },
                                    );
                                    self.module_plans[other_index].bindings.remove(&sibling);
                                    self.module_plans[owner_index]
                                        .bindings
                                        .insert(sibling.clone(), sibling);
                                    moved_sibling = true;
                                }
                                Some(other_index) if other_index == owner_index => {}
                                Some(other_index) => {
                                    unreachable!(
                                        "shared destructuring decision missed explicit destination {} for binding {sibling}",
                                        other_index
                                    );
                                }
                            }
                        }
                    }
                }
            }
            if !moved_sibling {
                break;
            }
        }
    }

    /// Bindings declared by an anonymously-claimed statement (e.g. a
    /// block-hoisted `var` inside a claimed `try` statement) belong
    /// to the module that claims the statement: the declaration is
    /// emitted there, so binding ownership, exports, and
    /// cross-module import wiring must follow it. Runs after global
    /// selector resolution; bindings already swept into residual are
    /// moved out so the residual file does not export declarations
    /// emitted in another module.
    pub(super) fn adopt_bindings_of_claimed_anonymous_statements(
        &mut self,
        declarations: &[TopLevelDecl],
    ) {
        for decl in declarations {
            let Some(&plan_index) = self.whole_statement_assignment.get(&decl.statement) else {
                continue;
            };
            let module = ModuleId(plan_index);
            for (name, id) in &decl.bindings {
                if let Some(existing) = owned_destination(&self.bindings_catalogue, id) {
                    if Some(existing) != self.residual_plan_index {
                        continue;
                    }
                    if let Some(residual_index) = self.residual_plan_index {
                        self.module_plans[residual_index].bindings.remove(name);
                    }
                }
                self.module_plans[plan_index]
                    .bindings
                    .entry(name.clone())
                    .or_insert_with(|| name.clone());
                self.bindings_catalogue
                    .insert(id.clone(), BindingKind::Owned { module });
            }
        }
    }

    /// A nested `var` is declared by its enclosing top-level control-flow
    /// statement. The shared placement kernel decides whether retained binding
    /// owners and any explicit whole-statement request agree. Planning applies
    /// that destination to the indivisible statement; an unclaimed statement
    /// remains available to entry/residual routing.
    pub(super) fn route_nested_var_statements(
        &mut self,
        nested_var_statements: &[NestedVarStatement],
    ) {
        for nested in nested_var_statements {
            let owners = nested
                .bindings
                .iter()
                .filter_map(|binding| {
                    owned_destination(&self.bindings_catalogue, binding)
                        .map(|destination| (binding.clone(), ModuleId(destination)))
                })
                .collect();
            let whole_statement_request = self
                .whole_statement_assignment
                .get(&nested.statement)
                .copied()
                .map(ModuleId);
            let check = classify_nested_var_placement(nested, owners, whole_statement_request);
            if check.destinations().is_empty() {
                continue;
            }
            if !check.missing_bindings.is_empty() {
                // Preserve partial ownership for the final projection's typed
                // refusal instead of manufacturing a statement destination.
                continue;
            }

            let destination = match &check.classification {
                NestedVarDestinationClassification::Unclaimed => continue,
                NestedVarDestinationClassification::OneDestination { destination } => *destination,
                NestedVarDestinationClassification::SplitDestinations { .. } => continue,
            };
            self.whole_statement_assignment
                .insert(nested.statement, destination.0);
            let plan = &mut self.module_plans[destination.0];
            plan.anonymous_statements.push(nested.statement);
            plan.anonymous_statements.sort_unstable();
            plan.anonymous_statements.dedup();
        }
    }

    /// Route unclaimed bindings only after all explicit claims and optional
    /// ownership filtering. A retained explicit catchall plan is reused; when
    /// omitted, the same request supplies only its residual destination.
    pub(super) fn add_residual_sweep(
        &mut self,
        catchall_request: Option<&LogicalRequest>,
        declarations: &[TopLevelDecl],
        target_dir: &str,
    ) -> Result<()> {
        let Some(request) = catchall_request else {
            return Ok(());
        };
        let unclaimed: Vec<_> = declarations
            .iter()
            .flat_map(|declaration| &declaration.bindings)
            .filter(|(_, binding)| owned_destination(&self.bindings_catalogue, binding).is_none())
            .collect();
        let owner_index = match self
            .module_plans
            .iter()
            .position(|plan| plan.target_path == request.target_path)
        {
            Some(index) => index,
            None => {
                if unclaimed.is_empty() {
                    return Ok(());
                }
                let index = self.module_plans.len();
                self.module_plans.push(ModulePlan {
                    id: request.id.clone(),
                    target_file: target_file_for_request(target_dir, &request.target_path)?,
                    target_path: request.target_path.clone(),
                    role: ModuleRole::Unassigned,
                    bindings: HashMap::new(),
                    spec_named_bindings: BTreeSet::new(),
                    anonymous_statements: Vec::new(),
                    anonymous_statement_comments: BTreeMap::new(),
                    comment: None,
                    binding_comments: BTreeMap::new(),
                    binding_claim_origins: BTreeMap::new(),
                });
                index
            }
        };
        let owner_plan = &mut self.module_plans[owner_index];
        owner_plan.role = ModuleRole::Unassigned;
        for (name, binding) in unclaimed {
            owner_plan
                .bindings
                .entry(name.clone())
                .or_insert_with(|| name.clone());
            self.bindings_catalogue.insert(
                binding.clone(),
                BindingKind::Owned {
                    module: ModuleId(owner_index),
                },
            );
        }
        self.residual_plan_index = Some(owner_index);
        Ok(())
    }

    /// Move anonymous source units into the catchall when their atomic group already
    /// belongs there, or when a catchall source unit must run after them according
    /// to the source-order side-effect graph. Other anonymous statements may
    /// stay in the entry and run after peeled modules. Explicit claims win;
    /// the realizability gate rejects an order that cannot be represented.
    pub(super) fn route_unclaimed_anonymous_to_catchall(
        &mut self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
    ) -> Result<()> {
        let Some(index) = self.residual_plan_index else {
            return Ok(());
        };
        let graph = &precomputed.dependency_graph;
        let source_unit_statements: HashMap<SourceUnitId, StatementId> = graph
            .iter_nodes()
            .map(|node| {
                let location = layout.by_source_unit(node.id).with_context(|| {
                    format!("source unit {} has no checked syntax location", node.id.0)
                })?;
                Ok((node.id, location.statement))
            })
            .collect::<Result<_>>()?;
        let mut body_indices = BTreeSet::new();
        let mut catchall_source_units = BTreeSet::new();
        for node in graph.iter_nodes() {
            if node.class() == SourceUnitClass::BindingDeclaration
                && node.declared.iter().all(|binding| {
                    owned_destination(&self.bindings_catalogue, binding) == Some(index)
                })
            {
                catchall_source_units.insert(node.id);
            } else if node.class() == SourceUnitClass::AnonymousStatement
                && let Some(body_index) = source_unit_statements.get(&node.id)
                && self.whole_statement_assignment.get(body_index) == Some(&index)
            {
                catchall_source_units.insert(node.id);
            }
        }
        for atomic_group in &precomputed.atomic_groups {
            let mut has_catchall_binding = false;
            let all_members_fit = atomic_group.members.iter().all(|&source_unit_id| {
                let Some(node) = graph.node(source_unit_id) else {
                    return false;
                };
                if node.class() == SourceUnitClass::AnonymousStatement {
                    let Some(body_index) = source_unit_statements.get(&source_unit_id) else {
                        return false;
                    };
                    return self
                        .whole_statement_assignment
                        .get(body_index)
                        .is_none_or(|&assigned| assigned == index);
                }
                let in_catchall = node.declared.iter().all(|binding| {
                    owned_destination(&self.bindings_catalogue, binding) == Some(index)
                });
                has_catchall_binding |= in_catchall;
                in_catchall
            });
            if !has_catchall_binding || !all_members_fit {
                continue;
            }
            for &source_unit_id in &atomic_group.members {
                let Some(node) = graph.node(source_unit_id) else {
                    continue;
                };
                if node.class() == SourceUnitClass::AnonymousStatement
                    && let Some(body_index) = source_unit_statements.get(&source_unit_id)
                    && self
                        .whole_statement_assignment
                        .get(body_index)
                        .is_none_or(|&assigned| assigned == index)
                {
                    body_indices.insert(*body_index);
                    catchall_source_units.insert(source_unit_id);
                }
            }
        }
        // A Sequenced edge points from a later effect to an earlier one.
        // Follow those edges backwards so no required predecessor remains in
        // the entry, which always evaluates after the catchall module.
        let mut pending: Vec<SourceUnitId> = catchall_source_units.iter().copied().collect();
        while let Some(source_unit_id) = pending.pop() {
            for &edge_id in graph.out_edges_of(source_unit_id) {
                let edge = graph.edge(edge_id);
                if !edge.reason.is_sequenced() {
                    continue;
                }
                let Some(node) = graph.node(edge.to) else {
                    continue;
                };
                if node.class() == SourceUnitClass::BindingDeclaration {
                    continue;
                }
                let body_index = source_unit_statements
                    .get(&node.id)
                    .expect("every graph node was checked against the source-unit layout");
                if self
                    .whole_statement_assignment
                    .get(body_index)
                    .is_some_and(|&assigned| assigned != index)
                {
                    continue;
                }
                body_indices.insert(*body_index);
                if catchall_source_units.insert(node.id) {
                    pending.push(node.id);
                }
            }
        }
        // With no other module to order against, keep the complete chunk in
        // its catchall file, including independent trailing statements.
        if self.module_plans.len() == 1 {
            for node in graph
                .iter_nodes()
                .filter(|node| node.class() == SourceUnitClass::AnonymousStatement)
            {
                if let Some(body_index) = source_unit_statements.get(&node.id) {
                    body_indices.insert(*body_index);
                }
            }
        }
        for body_index in body_indices {
            if self.whole_statement_assignment.contains_key(&body_index) {
                continue;
            }
            self.whole_statement_assignment.insert(body_index, index);
            self.module_plans[index]
                .anonymous_statements
                .push(body_index);
        }
        Ok(())
    }

    /// `PerAtomicGroup` mode: every unclaimed atomic factor group gets
    /// its own synthesized plan at `__auto/atomic_groups/NNNN`. Run after rebind folding
    /// so the residual sweep and fold decisions have already settled which
    /// atomic groups are still unclaimed. Bindings
    /// previously parked at the residual plan are moved out into the
    /// synthesized plan; the residual plan's `bindings` map is
    /// pruned to match.
    pub(super) fn synthesize_per_atomic_group(
        &mut self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        target_dir: &str,
    ) -> Result<()> {
        let groups = self.wholly_unclaimed_atomic_groups(precomputed, layout)?;
        self.synthesize_source_unit_groups(
            groups,
            precomputed,
            layout,
            target_dir,
            "__auto/atomic_groups",
            false,
        )
    }

    /// Complete a partially claimed atomic group only for an explicitly
    /// opted-in authored owner. This runs after rebind folding and ownership
    /// subset filtering. Groups with zero or multiple current owners, and
    /// groups whose sole owner is not listed, remain untouched for ordinary
    /// residual routing and the complete-assignment gate.
    pub(super) fn route_partial_atomic_groups_to_named_owners(
        &mut self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        opted_in_owner_paths: &[String],
        single_authored_owner_groups: &BTreeMap<Vec<SourceUnitId>, String>,
    ) -> Result<()> {
        if opted_in_owner_paths.is_empty() {
            return Ok(());
        }

        let dependency_graph = &precomputed.dependency_graph;
        let mut source_unit_declared_names: HashMap<SourceUnitId, Vec<Id>> = HashMap::new();
        let mut source_unit_statements = HashMap::<SourceUnitId, StatementId>::new();
        for node in dependency_graph.iter_nodes() {
            source_unit_declared_names.insert(node.id, node.declared.iter().cloned().collect());
            let location = layout.by_source_unit(node.id).with_context(|| {
                format!("source unit {} has no checked syntax location", node.id.0)
            })?;
            source_unit_statements.insert(node.id, location.statement);
        }

        let opted_in_owner_paths: BTreeSet<String> = opted_in_owner_paths.iter().cloned().collect();
        let mut atomic_groups: Vec<&BTreeSet<SourceUnitId>> = precomputed
            .atomic_groups
            .iter()
            .map(|atomic_group| &atomic_group.members)
            .collect();
        atomic_groups.sort_by_key(|members| members.iter().next().copied());

        for members in atomic_groups {
            let member_key: Vec<_> = members.iter().copied().collect();
            // Ownership subsets must not turn a two-owner authored atom into
            // an eligible one-owner atom by removing one of its owners.
            if !single_authored_owner_groups.contains_key(&member_key) {
                continue;
            }
            let mut current_owners = BTreeSet::<usize>::new();
            let mut unowned_bindings = Vec::<(SourceUnitId, Id)>::new();
            let mut unowned_anonymous = Vec::<(SourceUnitId, StatementId)>::new();
            let mut has_imported_binding = false;

            for &source_unit_id in members {
                let names = source_unit_declared_names
                    .get(&source_unit_id)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                if names.is_empty() {
                    let body_index = *source_unit_statements
                        .get(&source_unit_id)
                        .expect("every graph node was checked against the source-unit layout");
                    match self.whole_statement_assignment.get(&body_index).copied() {
                        Some(owner) => {
                            current_owners.insert(owner);
                        }
                        None => unowned_anonymous.push((source_unit_id, body_index)),
                    }
                    continue;
                }

                for name in names {
                    match self.bindings_catalogue.get(name) {
                        Some(BindingKind::Owned { module }) => {
                            current_owners.insert(module.0);
                        }
                        Some(BindingKind::Imported { .. }) => {
                            // Imported re-exports are not declarations that
                            // this chunk can adopt into an authored owner.
                            has_imported_binding = true;
                        }
                        None => unowned_bindings.push((source_unit_id, name.clone())),
                    }
                }
            }

            if has_imported_binding || current_owners.len() != 1 {
                continue;
            }
            let owner_index = *current_owners
                .iter()
                .next()
                .expect("the owner count was checked above");
            let Some((owner_role, owner_path)) = self
                .module_plans
                .get(owner_index)
                .map(|plan| (plan.role, plan.target_path.clone()))
            else {
                bail!("atomic group owner index {owner_index} is outside the module plan");
            };
            if owner_role != ModuleRole::Specified
                || !opted_in_owner_paths.contains(&owner_path)
                || single_authored_owner_groups.get(&member_key) != Some(&owner_path)
            {
                continue;
            }
            if unowned_bindings.is_empty() && unowned_anonymous.is_empty() {
                continue;
            }

            let owner_module = ModuleId(owner_index);
            let mut adopted_source_unit_ids = BTreeSet::new();
            for (source_unit_id, binding) in unowned_bindings {
                let name = binding.0.to_string();
                let owner_plan = &mut self.module_plans[owner_index];
                owner_plan
                    .bindings
                    .entry(name.clone())
                    .or_insert_with(|| name.clone());
                self.bindings_catalogue.insert(
                    binding,
                    BindingKind::Owned {
                        module: owner_module,
                    },
                );
                adopted_source_unit_ids.insert(source_unit_id);
            }
            for (source_unit_id, body_index) in unowned_anonymous {
                self.whole_statement_assignment
                    .insert(body_index, owner_index);
                self.module_plans[owner_index]
                    .anonymous_statements
                    .push(body_index);
                adopted_source_unit_ids.insert(source_unit_id);
            }
            let anonymous_statements = &mut self.module_plans[owner_index].anonymous_statements;
            anonymous_statements.sort_unstable();
            anonymous_statements.dedup();

            self.atomic_group_owner_completions
                .push(AtomicGroupOwnerCompletion {
                    owner_path,
                    atomic_group_source_unit_ids: members.iter().copied().collect(),
                    adopted_source_unit_ids: adopted_source_unit_ids.into_iter().collect(),
                });
        }
        Ok(())
    }

    /// Snapshot which atoms had exactly one opted-in authored owner before an
    /// ownership subset can remove plans or claims. Completion itself runs
    /// later, after subset filtering and rebind folds.
    pub(super) fn single_authored_owner_atomic_groups(
        &self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        opted_in_owner_paths: &[String],
    ) -> Result<BTreeMap<Vec<SourceUnitId>, String>> {
        if opted_in_owner_paths.is_empty() {
            return Ok(BTreeMap::new());
        }

        let dependency_graph = &precomputed.dependency_graph;
        let mut source_unit_declared_names: HashMap<SourceUnitId, Vec<Id>> = HashMap::new();
        let mut source_unit_statements = HashMap::<SourceUnitId, StatementId>::new();
        for node in dependency_graph.iter_nodes() {
            source_unit_declared_names.insert(node.id, node.declared.iter().cloned().collect());
            let location = layout.by_source_unit(node.id).with_context(|| {
                format!("source unit {} has no checked syntax location", node.id.0)
            })?;
            source_unit_statements.insert(node.id, location.statement);
        }

        let opted_in_owner_paths: BTreeSet<&str> =
            opted_in_owner_paths.iter().map(String::as_str).collect();
        let mut atomic_groups: Vec<&BTreeSet<SourceUnitId>> = precomputed
            .atomic_groups
            .iter()
            .map(|atomic_group| &atomic_group.members)
            .collect();
        atomic_groups.sort_by_key(|members| members.iter().next().copied());

        let mut eligible_groups = BTreeMap::new();
        for members in atomic_groups {
            let mut current_owners = BTreeSet::<usize>::new();
            let mut has_imported_binding = false;
            for &source_unit_id in members {
                let names = source_unit_declared_names
                    .get(&source_unit_id)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                if names.is_empty() {
                    let body_index = *source_unit_statements
                        .get(&source_unit_id)
                        .expect("every graph node was checked against the source-unit layout");
                    if let Some(owner) = self.whole_statement_assignment.get(&body_index) {
                        current_owners.insert(*owner);
                    }
                    continue;
                }
                for name in names {
                    match self.bindings_catalogue.get(name) {
                        Some(BindingKind::Owned { module }) => {
                            current_owners.insert(module.0);
                        }
                        Some(BindingKind::Imported { .. }) => has_imported_binding = true,
                        None => {}
                    }
                }
            }

            if has_imported_binding || current_owners.len() != 1 {
                continue;
            }
            let owner_index = *current_owners
                .iter()
                .next()
                .expect("the owner count was checked above");
            let Some(owner_plan) = self.module_plans.get(owner_index) else {
                bail!("atomic group owner index {owner_index} is outside the module plan");
            };
            if owner_plan.role == ModuleRole::Specified
                && opted_in_owner_paths.contains(owner_plan.target_path.as_str())
            {
                eligible_groups.insert(
                    members.iter().copied().collect(),
                    owner_plan.target_path.clone(),
                );
            }
        }
        Ok(eligible_groups)
    }

    /// Coarse residual routing merges wholly-unclaimed atomic groups when
    /// their strict constraining-edge reachability to existing owners agrees
    /// within one interval between fixed owners' fine-plan evaluations. This
    /// signature/interval partition is only a candidate heuristic: alternating
    /// source-order edges across distinct signatures can still form a cycle.
    /// The materializer checks the resulting actual gate SCCs and may contract
    /// a whole SCC only when every member has generated-residual provenance.
    pub(super) fn synthesize_coarse_atomic_groups(
        &mut self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        source_exports_by_statement: &HashMap<StatementId, Vec<SourceExportIdentity>>,
        target_dir: &str,
    ) -> Result<()> {
        let atomic_groups = self.wholly_unclaimed_atomic_groups(precomputed, layout)?;
        if atomic_groups.is_empty() {
            return Ok(());
        }
        let groups = self.coarse_residual_groups(
            precomputed,
            layout,
            &atomic_groups,
            source_exports_by_statement,
        )?;
        self.synthesize_source_unit_groups(
            groups.iter(),
            precomputed,
            layout,
            target_dir,
            "__auto/residual_groups",
            true,
        )
    }

    fn wholly_unclaimed_atomic_groups<'a>(
        &self,
        precomputed: &'a DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
    ) -> Result<Vec<&'a BTreeSet<SourceUnitId>>> {
        let dependency_graph = &precomputed.dependency_graph;
        let mut source_unit_declared_names: HashMap<SourceUnitId, Vec<Id>> = HashMap::new();
        let mut source_unit_statements = HashMap::<SourceUnitId, StatementId>::new();
        for node in dependency_graph.iter_nodes() {
            let ids: Vec<Id> = node.declared.iter().cloned().collect();
            source_unit_declared_names.insert(node.id, ids);
            let location = layout.by_source_unit(node.id).with_context(|| {
                format!("source unit {} has no checked syntax location", node.id.0)
            })?;
            source_unit_statements.insert(node.id, location.statement);
        }

        let residual_plan_index = self.residual_plan_index;
        let bindings_catalogue = &self.bindings_catalogue;
        let whole_statement_assignment = &self.whole_statement_assignment;
        // An atomic-group member counts as unclaimed iff every declared binding
        // has no owned destination in `bindings_catalogue` or is assigned to
        // the residual plan (if any); anonymous statements must similarly
        // be unassigned or routed via residual. If any member is
        // claimed by an explicit (non-residual) plan, the spec author
        // already named the atomic group's destination — leave the existing
        // claim intact (and let downstream validation flag an
        // atomic group conflict if the claims disagree).
        let is_source_unit_unclaimed = |source_unit: SourceUnitId| -> bool {
            let names = source_unit_declared_names
                .get(&source_unit)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            for id in names {
                match owned_destination(bindings_catalogue, id) {
                    None => continue,
                    Some(idx) if Some(idx) == residual_plan_index => continue,
                    Some(_) => return false,
                }
            }
            if names.is_empty() {
                let body_idx = source_unit_statements
                    .get(&source_unit)
                    .expect("every graph node was checked against the source-unit layout");
                match whole_statement_assignment.get(body_idx).copied() {
                    None => return true,
                    Some(idx) if Some(idx) == residual_plan_index => return true,
                    Some(_) => return false,
                }
            }
            true
        };

        let mut unclaimed_atomic_groups: Vec<&BTreeSet<SourceUnitId>> = precomputed
            .atomic_groups
            .iter()
            .filter(|atomic_group| {
                atomic_group
                    .members
                    .iter()
                    .copied()
                    .all(is_source_unit_unclaimed)
            })
            .map(|atomic_group| &atomic_group.members)
            .collect();
        // Stable iteration order: smallest SourceUnitId first.
        unclaimed_atomic_groups.sort_by_key(|members| members.iter().next().copied());
        Ok(unclaimed_atomic_groups)
    }

    fn coarse_residual_groups(
        &self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        atomic_groups: &[&BTreeSet<SourceUnitId>],
        source_exports_by_statement: &HashMap<StatementId, Vec<SourceExportIdentity>>,
    ) -> Result<Vec<BTreeSet<SourceUnitId>>> {
        let dependency_graph = &precomputed.dependency_graph;
        // Match fine lowering's ID domain, including its tie breaks: generated
        // plans follow explicit plans, and entry follows every generated plan.
        let first_virtual_module = self.module_plans.len();
        let entry_module = ModuleId(
            self.module_plans
                .len()
                .checked_add(atomic_groups.len())
                .context("residual entry module ID overflowed")?,
        );
        let mut source_unit_claims = Vec::<(SourceUnitId, ModuleId)>::new();
        for (statement, destination) in &self.whole_statement_assignment {
            for location in layout.for_statement(*statement) {
                source_unit_claims.push((location.source_unit, ModuleId(*destination)));
            }
        }

        let mut source_unit_export_surfaces = HashMap::<SourceUnitId, PublicExportSurface>::new();
        for node in dependency_graph.iter_nodes() {
            let location = layout.by_source_unit(node.id).with_context(|| {
                format!("source unit {} has no checked syntax location", node.id.0)
            })?;
            let mut surface = PublicExportSurface::default();
            for name in &node.declared {
                surface.add_binding(name.0.to_string(), name.clone());
            }
            if let Some(exports) = source_exports_by_statement.get(&location.statement) {
                surface.add_source_identities(exports);
            }
            source_unit_export_surfaces.insert(node.id, surface);
        }

        let mut virtual_modules = BTreeSet::new();
        let mut candidate_modules =
            Vec::<(ModuleId, &BTreeSet<SourceUnitId>, PublicExportSurface)>::new();
        for (idx, members) in atomic_groups.iter().enumerate() {
            let module_id = ModuleId(
                first_virtual_module
                    .checked_add(idx)
                    .context("temporary residual module IDs overflowed")?,
            );
            virtual_modules.insert(module_id);
            let mut export_surface = PublicExportSurface::default();
            for source_unit in members.iter() {
                let unit_surface =
                    source_unit_export_surfaces
                        .get(source_unit)
                        .with_context(|| {
                            format!("source unit {} has no export surface", source_unit.0)
                        })?;
                export_surface.merge(unit_surface);
            }
            candidate_modules.push((module_id, *members, export_surface));
            for source_unit in members.iter() {
                source_unit_claims.push((*source_unit, module_id));
            }
        }

        let assignment = assemble_assignment_with_source_unit_claims(
            dependency_graph,
            &precomputed.atomic_groups,
            &self.bindings_catalogue,
            &source_unit_claims,
            EntryDestination::from_module_id(entry_module),
        )?
        .assignment;
        let canonical_edges = chunk_constraining_module_edges(dependency_graph, &assignment);
        let evaluation_order =
            simulated_evaluation_post_order(&canonical_edges, dependency_graph, &assignment);
        let mut successors = BTreeMap::<ModuleId, BTreeSet<ModuleId>>::new();
        for &(from, to) in canonical_edges.edges.keys() {
            successors.entry(from).or_default().insert(to);
        }
        drop(canonical_edges);

        // A destination is fixed when it already owns at least one source
        // unit outside the candidate atomic groups. This includes a retained
        // residual placeholder and includes entry only when entry actually
        // owns units. Empty planned destinations cannot influence reachability.
        let fixed_destinations: BTreeSet<ModuleId> = assignment
            .iter()
            .map(|(_, destination)| destination)
            .filter(|destination| !virtual_modules.contains(destination))
            .collect();
        let mut fixed_positions: Vec<usize> = fixed_destinations
            .iter()
            .filter_map(|destination| evaluation_order.get(destination).copied())
            .collect();
        fixed_positions.sort_unstable();
        let mut groups_by_signature =
            BTreeMap::<(usize, BTreeSet<ModuleId>), Vec<CoarseResidualBucket>>::new();
        let mut groups = Vec::new();
        for (virtual_module, members, export_surface) in candidate_modules {
            // Entry imports every assigned destination, so source-aware
            // simulation includes isolated candidate modules in the same
            // evaluation order as the emitted tree. Every virtual module has
            // at least one assigned source unit and must therefore appear.
            let position = *evaluation_order
                .get(&virtual_module)
                .expect("assigned virtual residual module is imported from entry");
            let interval = fixed_positions.partition_point(|fixed| *fixed < position);
            let mut reachable = BTreeSet::new();
            let mut visited = BTreeSet::from([virtual_module]);
            let mut pending = VecDeque::from([virtual_module]);
            while let Some(from) = pending.pop_front() {
                for to in successors.get(&from).into_iter().flatten() {
                    if fixed_destinations.contains(to) {
                        reachable.insert(*to);
                    }
                    if visited.insert(*to) {
                        pending.push_back(*to);
                    }
                }
            }
            let buckets = groups_by_signature
                .entry((interval, reachable))
                .or_default();
            let existing_bucket = buckets
                .iter_mut()
                .find(|bucket| bucket.export_surface.is_compatible_with(&export_surface));
            if let Some(bucket) = existing_bucket {
                bucket.source_units.extend(members.iter().copied());
                bucket.export_surface.merge(&export_surface);
            } else {
                buckets.push(CoarseResidualBucket {
                    source_units: members.iter().copied().collect(),
                    export_surface,
                });
            }
        }

        groups.extend(
            groups_by_signature
                .into_values()
                .flatten()
                .map(|bucket| bucket.source_units),
        );
        groups.sort_by_key(|members| members.iter().next().copied());
        Ok(groups)
    }

    fn synthesize_source_unit_groups<'a>(
        &mut self,
        groups: impl IntoIterator<Item = &'a BTreeSet<SourceUnitId>>,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        target_dir: &str,
        path_prefix: &str,
        generated_residual: bool,
    ) -> Result<()> {
        let dependency_graph = &precomputed.dependency_graph;
        let mut source_unit_declared_names: HashMap<SourceUnitId, Vec<Id>> = HashMap::new();
        let mut source_unit_statements = HashMap::<SourceUnitId, StatementId>::new();
        for node in dependency_graph.iter_nodes() {
            source_unit_declared_names.insert(node.id, node.declared.iter().cloned().collect());
            let location = layout.by_source_unit(node.id).with_context(|| {
                format!("source unit {} has no checked syntax location", node.id.0)
            })?;
            source_unit_statements.insert(node.id, location.statement);
        }

        let residual_plan_index = self.residual_plan_index;
        for (idx, members) in groups.into_iter().enumerate() {
            let synthetic_idx = self.module_plans.len();
            let synthetic_module_id = ModuleId(synthetic_idx);
            let target_path = format!("{path_prefix}/{idx:04}");
            let target_file = target_file_for_request(target_dir, &target_path)?;
            let mut bindings = HashMap::<String, String>::new();
            let mut anonymous_statements = Vec::<StatementId>::new();
            for source_unit in members {
                let names = source_unit_declared_names
                    .get(source_unit)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                if names.is_empty() {
                    let body_idx = source_unit_statements
                        .get(source_unit)
                        .expect("every graph node was checked against the source-unit layout");
                    self.whole_statement_assignment
                        .insert(*body_idx, synthetic_idx);
                    anonymous_statements.push(*body_idx);
                    continue;
                }
                for name in names {
                    let name_str = name.0.to_string();
                    bindings.insert(name_str.clone(), name_str.clone());
                    // Move the binding out of the residual plan (if it
                    // was staged there by the sweep above) into the
                    // synthesized plan. The residual plan's bindings are
                    // pruned so it doesn't double-claim members.
                    if let Some(prev) = owned_destination(&self.bindings_catalogue, name)
                        && Some(prev) == residual_plan_index
                        && let Some(residual_idx) = residual_plan_index
                    {
                        self.module_plans[residual_idx].bindings.remove(&name_str);
                    }
                    self.bindings_catalogue.insert(
                        name.clone(),
                        BindingKind::Owned {
                            module: synthetic_module_id,
                        },
                    );
                }
            }
            anonymous_statements.sort_unstable();
            self.module_plans.push(ModulePlan {
                id: target_path.clone(),
                target_file,
                target_path,
                role: ModuleRole::Unassigned,
                bindings,
                spec_named_bindings: BTreeSet::new(),
                anonymous_statements,
                anonymous_statement_comments: BTreeMap::new(),
                comment: None,
                binding_comments: BTreeMap::new(),
                binding_claim_origins: BTreeMap::new(),
            });
            if generated_residual {
                self.generated_residual_plans.insert(synthetic_idx);
            }
        }
        Ok(())
    }

    /// Apply a batch of rebind-fold decisions produced by
    /// `lowering/rebind_fold.rs`.
    ///
    /// Each fold reroutes a single binding from its previous plan
    /// (if any) to the cycle's explicit destination. The
    /// `bindings_catalogue` records the new destination, and bindings
    /// that were previously parked at the residual plan are pruned
    /// from that plan's binding list so the residual doesn't
    /// double-claim them.
    ///
    /// Folds are produced in atomic-group iteration order; we do not
    /// reorder them here. The mutations are idempotent in the sense
    /// that `module_plans[dest].bindings.entry(name).or_insert_with`
    /// preserves any existing entry under that name.
    pub(super) fn apply_rebind_folds(&mut self, folds: Vec<RebindFold>) {
        let residual_plan_index = self.residual_plan_index;
        for RebindFold {
            binding,
            dest,
            from_residual,
        } in folds
        {
            let name = binding.0.as_ref().to_string();
            self.bindings_catalogue.insert(
                binding,
                BindingKind::Owned {
                    module: ModuleId(dest),
                },
            );
            self.module_plans[dest]
                .bindings
                .entry(name.clone())
                .or_insert_with(|| name.clone());
            if from_residual && let Some(residual) = residual_plan_index {
                self.module_plans[residual].bindings.remove(&name);
            }
        }
    }

    /// Derive the owned-only view required by rebind analysis.
    /// This is a snapshot, never independently mutated construction state.
    pub(super) fn owned_binding_destinations(&self) -> HashMap<Id, usize> {
        self.bindings_catalogue
            .iter()
            .filter_map(|(id, kind)| match kind {
                BindingKind::Owned { module } => Some((id.clone(), module.0)),
                BindingKind::Imported { .. } => None,
            })
            .collect()
    }

    /// The residual landing-site plan index, if one was created by the residual
    /// sweep. Needed by rebind folding to know which existing claims count as
    /// "swept" (and hence still foldable).
    pub(super) fn residual_plan_index(&self) -> Option<usize> {
        self.residual_plan_index
    }

    pub(super) fn selector_outcome_report(&self) -> Option<SelectorOutcomeReport> {
        self.outcomes.report()
    }

    pub(super) fn finalize(self, chunk_id: ChunkId) -> Result<ChunkPlan> {
        self.outcomes.finish()?;
        Ok(ChunkPlan {
            module_plans: self.module_plans,
            bindings_catalogue: BindingCatalogue::new(chunk_id, self.bindings_catalogue),
            whole_statement_assignment: self.whole_statement_assignment,
            generated_residual_plans: self.generated_residual_plans,
            atomic_group_owner_completions: self.atomic_group_owner_completions,
            unmatched_spec_claims: self.unmatched_spec_claims,
        })
    }
}

/// Read owned destinations from the same catalogue that records imported
/// re-export claims. Borrow the field rather than the whole builder, allowing
/// callers to update a disjoint module export plan while consulting placement.
fn owned_destination(bindings: &HashMap<Id, BindingKind>, id: &Id) -> Option<usize> {
    match bindings.get(id) {
        Some(BindingKind::Owned { module }) => Some(module.0),
        Some(BindingKind::Imported { .. }) | None => None,
    }
}
