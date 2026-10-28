//! `ChunkPlanBuilder` owns the per-chunk mutable state that
//! `materialize_logical_chunk` threads across its eight phases. Each phase is a
//! method on the builder. `bindings_catalogue` is the authoritative binding
//! destination state; owned-only lookup maps are derived when a downstream
//! analysis needs that projection.

use super::outcome_sink::OutcomeSink;
use crate::anonymous::ResolvedAnonymousStatement;
use crate::chunk_ast::{TopLevelDecl, binding_declaration};
use crate::imports::{
    ArtifactSourceImportResolutionCache, RuntimeImportFacts, resolve_imported_binding,
};
use crate::plans::ModulePlan;
use crate::rebind_fold::RebindFold;
use crate::util::target_file_for_request;
use analysis::{
    BindingCatalogue, BindingKind, ChunkId, DependencyGraphAndAtomicGroups, ModuleId, ModuleRole,
    SourceUnitClass, SourceUnitId, top_level_id,
};
use anyhow::{Context, Result, bail};
use js_ast::statement_for_source_unit_ordinal;
use selector_outcome::{
    Declaration, Entity, EntityRef, Outcome, ResolvedBy, SelectorOutcome, SelectorOutcomeReport,
};
use selector_resolve::{EntityIndex, EntityOutcome, MemberSelector, Resolution};
use source_identity::{SourceUnitOrdinal, StatementId};
use spec_resolution::{LogicalRequest, MemberRequest, reject_duplicate_member_bindings};
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
    pub(super) unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
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
    /// Authoritative destination of every claimed binding, including imported
    /// bindings claimed only for re-export. Module plans retain export/name
    /// decisions; syntax placement reads this catalogue.
    bindings_catalogue: HashMap<Id, BindingKind>,
    /// Index into `module_plans` of the "catchall" plan that
    /// unclaimed bindings sweep into, when one exists. `None` when
    /// the chunk has no residual landing site (default
    /// `InlineInEntry` with no fallback request, or `PerAtomicGroup`).
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
    /// Deferred binding selector names that were duplicate claims during
    /// request construction. Every member for such a binding stays out of the
    /// solver program so the existing duplicate-claim report remains the
    /// primary diagnostic.
    duplicate_deferred_binding_names: BTreeSet<String>,
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
            bindings_catalogue: HashMap::new(),
            residual_plan_index: None,
            unmatched_spec_claims: Vec::new(),
            catalogue_index_by_name: HashMap::new(),
            deferred_binding_claims_by_name: HashMap::new(),
            duplicate_deferred_binding_names: BTreeSet::new(),
            outcomes: OutcomeSink::new(fail_fast, list_template_identifiers),
        }
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
                    if member.resolves_after_chunk_analysis() {
                        self.duplicate_deferred_binding_names
                            .insert(member.binding.clone());
                    }
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
                    self.duplicate_deferred_binding_names
                        .insert(member.binding.clone());
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

    /// The entities of the explicit requests the selector resolve decides:
    /// every solver-resolved member and anonymous statement. A name pin whose
    /// binding no top-level declaration carries was already recorded as an
    /// unmatched claim, and a duplicate claim as its outcome, so neither is
    /// resolved.
    pub(super) fn selector_member_indices(
        &self,
        explicit_requests: &[LogicalRequest],
        chunk_top_level_mark: swc_common::Mark,
        declaration_by_name: &HashMap<Id, Vec<StatementId>>,
    ) -> Vec<Vec<usize>> {
        explicit_requests
            .iter()
            .map(|request| {
                request
                    .members
                    .iter()
                    .enumerate()
                    .filter(|(_, member)| {
                        member.resolves_after_chunk_analysis()
                            && !self
                                .duplicate_deferred_binding_names
                                .contains(&member.binding)
                            && (!matches!(member.selector, MemberSelector::Binding(_))
                                || declaration_by_name.contains_key(&top_level_id(
                                    &member.binding,
                                    chunk_top_level_mark,
                                )))
                    })
                    .map(|(index, _)| index)
                    .collect()
            })
            .collect()
    }

    /// Claims what `resolution` resolved of `selectors`, and records every
    /// other outcome.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn claim_resolution(
        &mut self,
        explicit_requests: &[LogicalRequest],
        resolution: Resolution,
        resolved_members: Vec<Vec<usize>>,
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
                    self.claim_anonymous_statement(
                        index,
                        &request.id,
                        &ResolvedAnonymousStatement {
                            body_index: StatementId(body_index),
                            comment: request.anonymous_statements[statement_index]
                                .comment
                                .clone(),
                        },
                    )?;
                    true
                }
                EntityIndex::Member(member_index) => {
                    let member = &request.members[resolved_members[index][member_index]];
                    let binding = binding.with_context(|| {
                        format!(
                            "logical_module {}: global selector solver resolved member `{}` to \
                             body item {body_index} without a single declared binding",
                            request.id, member.export_name,
                        )
                    })?;
                    self.claim_binding_after_chunk_analysis(
                        request,
                        member,
                        &binding,
                        index,
                        chunk_top_level_mark,
                        chunk_id,
                        body,
                        declaration_by_name,
                    )?
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

    pub(super) fn resolved_binding_for_member(
        &self,
        request_index: usize,
        member: &MemberRequest,
    ) -> Option<String> {
        if !member.binding.is_empty() {
            return Some(member.binding.clone());
        }
        let plan = self.module_plans.get(request_index)?;
        if let Some((binding, _)) = plan
            .bindings
            .iter()
            .find(|(_, export_name)| *export_name == &member.export_name)
        {
            return Some(binding.clone());
        }
        self.bindings_catalogue
            .iter()
            .find_map(|(binding, kind)| match kind {
                BindingKind::Imported {
                    re_exporter: ModuleId(index),
                    public_name,
                    ..
                } if *index == request_index && public_name.as_ref() == member.export_name => {
                    Some(binding.0.as_str().to_string())
                }
                _ => None,
            })
    }

    pub(super) fn fail_fast(&self) -> bool {
        self.outcomes.fail_fast()
    }

    /// Drop the name-keyed catalogue scratch index now that the
    /// explicit-requests loop is finished. Destructure siblings and
    /// the residual sweep don't consult this index.
    pub(super) fn drop_explicit_request_scratch(&mut self) {
        self.catalogue_index_by_name = HashMap::new();
        self.deferred_binding_claims_by_name = HashMap::new();
    }

    /// Destructure-atomicity: a destructuring declarator like
    /// `const { x, y } = obj` binds multiple names from a single
    /// pattern that the lowerer's `split_var_decl` moves as one
    /// unit. If the spec claims any one binding from such a pattern,
    /// every sibling binding must travel to the same module —
    /// otherwise the residual's export list would list a name whose
    /// declarator has already moved away, and `node` would reject the
    /// resulting module with `SyntaxError: Export 'y' is not defined
    /// in module`.
    ///
    /// Implicitly-pulled siblings join the claimed module with their
    /// own binding name as the export name. They aren't separately
    /// spec'd, but the destructure pattern must keep its full name
    /// set together regardless. Conflicting claims (two siblings
    /// claimed by different modules) are rejected.
    pub(super) fn pull_destructure_siblings(
        &mut self,
        destructure_siblings: &BTreeMap<String, BTreeSet<String>>,
        chunk_top_level_mark: swc_common::Mark,
    ) -> Result<()> {
        for (claimed_name, sibling_set) in destructure_siblings {
            let claimed_id = top_level_id(claimed_name, chunk_top_level_mark);
            let Some(owner_index) = owned_destination(&self.bindings_catalogue, &claimed_id) else {
                continue;
            };
            let owner_module_id = ModuleId(owner_index);
            for sibling in sibling_set {
                if sibling == claimed_name {
                    continue;
                }
                let sibling_id = top_level_id(sibling, chunk_top_level_mark);
                match owned_destination(&self.bindings_catalogue, &sibling_id) {
                    None => {
                        self.bindings_catalogue.insert(
                            sibling_id,
                            BindingKind::Owned {
                                module: owner_module_id,
                            },
                        );
                        let plan = &mut self.module_plans[owner_index];
                        plan.bindings.insert(sibling.clone(), sibling.clone());
                    }
                    Some(other_index) if Some(other_index) == self.residual_plan_index => {
                        self.bindings_catalogue.insert(
                            sibling_id,
                            BindingKind::Owned {
                                module: owner_module_id,
                            },
                        );
                        self.module_plans[other_index].bindings.remove(sibling);
                        let plan = &mut self.module_plans[owner_index];
                        plan.bindings.insert(sibling.clone(), sibling.clone());
                    }
                    Some(other_index) if other_index != owner_index => {
                        let owner_plan_id = self.module_plans[owner_index].id.clone();
                        let other_plan_id = self.module_plans[other_index].id.clone();
                        bail!(
                            "destructure declarator binds {claimed_name} (claimed by module \
                             {owner_plan_id}) and {sibling} (claimed by module {other_plan_id}); \
                             destructuring declarators must move atomically — claim both \
                             bindings from the same module or claim neither.",
                        );
                    }
                    Some(_) => {}
                }
            }
        }
        Ok(())
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
    /// statement. Unlike a direct `var` declaration, that statement cannot be
    /// split out of the entry body while leaving the surrounding control flow
    /// behind. Route it whole only when all of its bindings and any anonymous
    /// statement claim already agree on one destination.
    pub(super) fn route_nested_var_statements(
        &mut self,
        body: &[ModuleItem],
        declarations: &[TopLevelDecl],
    ) -> Result<()> {
        for decl in declarations {
            let Some(ModuleItem::Stmt(statement)) = body.get(decl.statement.0) else {
                continue;
            };
            // Direct variable declarations can be split by declarator in the
            // emitter. `TopLevelDecl`s for other direct declaration kinds do
            // not represent hoisted nested vars either.
            if matches!(statement, Stmt::Decl(_)) {
                continue;
            }

            let assigned_destinations = decl
                .bindings
                .iter()
                .filter_map(|(_, id)| owned_destination(&self.bindings_catalogue, id))
                .chain(
                    self.whole_statement_assignment
                        .get(&decl.statement)
                        .copied(),
                )
                .collect::<BTreeSet<_>>();
            if assigned_destinations.is_empty() {
                continue;
            }
            let missing_bindings = decl
                .bindings
                .iter()
                .filter(|(_, id)| owned_destination(&self.bindings_catalogue, id).is_none())
                .map(|(name, _)| name.as_str())
                .collect::<Vec<_>>();
            if !missing_bindings.is_empty() {
                bail!(
                    "top-level control-flow statement at body index {} also declares unassigned \
                     bindings ({}); assign them to the same module before extracting the statement",
                    decl.statement,
                    missing_bindings.join(", "),
                );
            }
            if assigned_destinations.len() > 1 {
                let destinations = assigned_destinations
                    .iter()
                    .map(|index| self.module_plans[*index].id.as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                bail!(
                    "top-level control-flow statement at body index {} declares bindings assigned \
                     to different modules ({destinations}); assign all nested var bindings to one module",
                    decl.statement,
                );
            }

            let destination = *assigned_destinations
                .first()
                .expect("assigned_destinations is nonempty");
            self.whole_statement_assignment
                .insert(decl.statement, destination);
            let plan = &mut self.module_plans[destination];
            plan.anonymous_statements.push(decl.statement);
            plan.anonymous_statements.sort_unstable();
            plan.anonymous_statements.dedup();
        }
        Ok(())
    }

    /// Residual sweep: route every chunk top-level binding the spec
    /// did not claim to the chunk's catchall destination.
    ///
    /// Two shapes:
    ///
    /// 1. A memberless residual request was synthesized (or supplied
    ///    by the spec) — build a new residual plan, append it, and
    ///    point `residual_plan_index` at it.
    /// 2. A `logical_modules` entry already pins itself at the
    ///    catchall target — repurpose that plan: mark it `Unassigned`
    ///    and append unclaimed bindings to its members.
    ///
    /// `None` for both `residual_request` and `catchall_target`
    /// leaves `residual_plan_index` unset, which is the
    /// `InlineInEntry` / `PerAtomicGroup` shape.
    pub(super) fn add_residual_sweep(
        &mut self,
        residual_request: Option<&LogicalRequest>,
        catchall_target_for_overflow: Option<&str>,
        declarations: &[TopLevelDecl],
        target_dir: &str,
    ) -> Result<()> {
        if let Some(residual) = residual_request {
            let residual_index = self.module_plans.len();
            let residual_module_id = ModuleId(residual_index);
            let mut residual_bindings = HashMap::<String, String>::new();
            for decl in declarations {
                for (name, id) in &decl.bindings {
                    if owned_destination(&self.bindings_catalogue, id).is_none() {
                        residual_bindings.insert(name.clone(), name.clone());
                        self.bindings_catalogue.insert(
                            id.clone(),
                            BindingKind::Owned {
                                module: residual_module_id,
                            },
                        );
                    }
                }
            }
            if !residual_bindings.is_empty() {
                self.module_plans.push(ModulePlan {
                    id: residual.id.clone(),
                    target_file: target_file_for_request(target_dir, &residual.target_path)?,
                    target_path: residual.target_path.clone(),
                    role: ModuleRole::Unassigned,
                    bindings: residual_bindings,
                    spec_named_bindings: BTreeSet::new(),
                    anonymous_statements: Vec::new(),
                    anonymous_statement_comments: BTreeMap::new(),
                    comment: None,
                    binding_comments: BTreeMap::new(),
                    binding_claim_origins: BTreeMap::new(),
                });
                self.residual_plan_index = Some(residual_index);
            }
        } else if let Some(catchall_target) = catchall_target_for_overflow {
            // No memberless residual request was synthesized — an
            // explicit `logical_modules` entry already pinned itself at
            // the catchall target. Append unclaimed bindings to that
            // plan so the residual sweep still has a home, and mark
            // it Unassigned. The role preserves the legacy residual
            // report/artifact projection for default-assigned outputs;
            // it does not select the physical DFS root.
            let owner_index = self
                .module_plans
                .iter()
                .position(|plan| plan.target_path == catchall_target);
            if let Some(owner_index) = owner_index {
                let owner_module_id = ModuleId(owner_index);
                let owner_plan = &mut self.module_plans[owner_index];
                owner_plan.role = ModuleRole::Unassigned;
                for decl in declarations {
                    for (name, id) in &decl.bindings {
                        if owned_destination(&self.bindings_catalogue, id).is_none() {
                            owner_plan
                                .bindings
                                .entry(name.clone())
                                .or_insert_with(|| name.clone());
                            self.bindings_catalogue.insert(
                                id.clone(),
                                BindingKind::Owned {
                                    module: owner_module_id,
                                },
                            );
                        }
                    }
                }
                self.residual_plan_index = Some(owner_index);
            }
        }
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
        body: &[ModuleItem],
    ) {
        let Some(index) = self.residual_plan_index else {
            return;
        };
        let graph = &precomputed.dependency_graph;
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
                && let Some(body_index) =
                    statement_for_source_unit_ordinal(body, node.statement_ordinal)
                && self.whole_statement_assignment.get(&body_index) == Some(&index)
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
                    let Some(body_index) =
                        statement_for_source_unit_ordinal(body, node.statement_ordinal)
                    else {
                        return false;
                    };
                    return self
                        .whole_statement_assignment
                        .get(&body_index)
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
                    && let Some(body_index) =
                        statement_for_source_unit_ordinal(body, node.statement_ordinal)
                    && self
                        .whole_statement_assignment
                        .get(&body_index)
                        .is_none_or(|&assigned| assigned == index)
                {
                    body_indices.insert(body_index);
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
                let Some(body_index) =
                    statement_for_source_unit_ordinal(body, node.statement_ordinal)
                else {
                    continue;
                };
                if self
                    .whole_statement_assignment
                    .get(&body_index)
                    .is_some_and(|&assigned| assigned != index)
                {
                    continue;
                }
                body_indices.insert(body_index);
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
                if let Some(body_index) =
                    statement_for_source_unit_ordinal(body, node.statement_ordinal)
                {
                    body_indices.insert(body_index);
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
        body: &[ModuleItem],
        target_dir: &str,
    ) -> Result<()> {
        let dependency_graph = &precomputed.dependency_graph;
        let atomic_groups = &precomputed.atomic_groups;
        let mut source_unit_declared_names: HashMap<SourceUnitId, Vec<Id>> = HashMap::new();
        let mut source_unit_statement_ordinal: HashMap<SourceUnitId, SourceUnitOrdinal> =
            HashMap::new();
        for node in dependency_graph.iter_nodes() {
            let ids: Vec<Id> = node.declared.iter().cloned().collect();
            source_unit_declared_names.insert(node.id, ids);
            source_unit_statement_ordinal.insert(node.id, node.statement_ordinal);
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
                let Some(stmt_ord) = source_unit_statement_ordinal.get(&source_unit).copied()
                else {
                    return true;
                };
                let Some(body_idx) = statement_for_source_unit_ordinal(body, stmt_ord) else {
                    return true;
                };
                match whole_statement_assignment.get(&body_idx).copied() {
                    None => return true,
                    Some(idx) if Some(idx) == residual_plan_index => return true,
                    Some(_) => return false,
                }
            }
            true
        };

        let mut unclaimed_atomic_groups: Vec<&BTreeSet<SourceUnitId>> = atomic_groups
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

        for (idx, members) in unclaimed_atomic_groups.into_iter().enumerate() {
            let synthetic_idx = self.module_plans.len();
            let synthetic_module_id = ModuleId(synthetic_idx);
            let target_path = format!("__auto/atomic_groups/{idx:04}");
            let target_file = target_file_for_request(target_dir, &target_path)?;
            let mut bindings = HashMap::<String, String>::new();
            let mut anonymous_statements = Vec::<StatementId>::new();
            for source_unit in members {
                let names = source_unit_declared_names
                    .get(source_unit)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                if names.is_empty() {
                    let Some(stmt_ord) = source_unit_statement_ordinal.get(source_unit).copied()
                    else {
                        continue;
                    };
                    let Some(body_idx) = statement_for_source_unit_ordinal(body, stmt_ord) else {
                        continue;
                    };
                    self.whole_statement_assignment
                        .insert(body_idx, synthetic_idx);
                    anonymous_statements.push(body_idx);
                    continue;
                }
                for name in names {
                    let name_str = name.0.to_string();
                    bindings.insert(name_str.clone(), name_str.clone());
                    // Move the binding out of the residual plan (if it
                    // was staged there by the sweep above) into the
                    // synthesized plan. The residual plan's
                    // bindings/anonymous-ordinal maps are pruned so it
                    // doesn't double-claim members.
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
