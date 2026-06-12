//! `ChunkPlanBuilder` owns the per-chunk mutable state that
//! `materialize_logical_chunk` used to thread as five loose `&mut`
//! maps through eight phases. Each phase becomes a method on the
//! builder; the same lookup (`bindings_catalogue` + `binding_assignment`)
//! that previously appeared in eight subtly different forms now lives
//! behind the builder's encapsulation.
//!
//! See `ARCHITECTURE_BACKLOG.md` § "`materialize_logical_chunk` is a
//! 750-line god function with parallel mutable state" for the
//! original motivation.

use super::super::ordinal::body_index_for_statement_ordinal;
use super::*;

/// Output of `ChunkPlanBuilder::finalize`: everything downstream
/// `lower_chunk` + the chunk-report builder need from the plan
/// construction phase.
pub(super) struct ChunkPlan {
    pub(super) module_plans: Vec<ModulePlan>,
    pub(super) binding_assignment: HashMap<Id, usize>,
    pub(super) bindings_catalogue: HashMap<Id, BindingKind>,
    pub(super) anonymous_ordinal_assignment: BTreeMap<usize, usize>,
    pub(super) unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
}

/// Per-explicit-request inputs the builder reads but does not own.
pub(super) struct ExplicitRequestContext<'a> {
    pub(super) runtime_module: &'a Module,
    pub(super) declaration_by_name: &'a HashMap<Id, usize>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
    pub(super) target_dir: &'a str,
    pub(super) chunk_id: &'a str,
    pub(super) target_file: &'a str,
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
}

fn resolve_request_source_matches(
    request: &mut LogicalRequest,
    runtime_module: &Module,
) -> Result<()> {
    let mut grouped_by_selector: BTreeMap<spec::AnonymousStatementSelector, Vec<usize>> =
        BTreeMap::new();
    for (idx, member) in request.members.iter().enumerate() {
        let Some(selector) = &member.source_match else {
            continue;
        };
        if selector.target_binding.is_none() {
            continue;
        }
        let mut group_selector = selector.clone();
        group_selector.target_binding = None;
        grouped_by_selector
            .entry(group_selector)
            .or_default()
            .push(idx);
    }

    let mut resolved_member_indices = BTreeSet::new();
    for (selector, member_indices) in grouped_by_selector {
        if member_indices.len() < 2 {
            continue;
        }
        let mut exports_by_target = BTreeMap::new();
        let mut has_duplicate_target = false;
        for idx in &member_indices {
            let member = &request.members[*idx];
            let target_binding = member
                .source_match
                .as_ref()
                .and_then(|selector| selector.target_binding.clone())
                .expect("grouped selectors always have target_binding");
            if exports_by_target
                .insert(target_binding, member.export_name.clone())
                .is_some()
            {
                has_duplicate_target = true;
            }
        }
        if has_duplicate_target {
            continue;
        }
        let resolved = source_match::resolve_member_binding_group(
            runtime_module,
            &request.id,
            &selector,
            &exports_by_target,
        )?;
        for idx in member_indices {
            let member = &mut request.members[idx];
            let target_binding = member
                .source_match
                .as_ref()
                .and_then(|selector| selector.target_binding.as_ref())
                .expect("grouped selectors always have target_binding");
            let resolved = resolved.get(target_binding).with_context(|| {
                format!("binding group resolver did not return target_binding `{target_binding}`")
            })?;
            apply_resolved_member_binding(member, resolved.clone());
            resolved_member_indices.insert(idx);
        }
    }

    let mut source_match_cache = BTreeMap::new();
    for (idx, member) in request.members.iter_mut().enumerate() {
        if resolved_member_indices.contains(&idx) {
            continue;
        }
        member.resolve_source_match(runtime_module, &request.id, &mut source_match_cache)?;
    }
    Ok(())
}

fn apply_resolved_member_binding(
    member: &mut MemberRequest,
    resolved: source_match::ResolvedMemberBinding,
) {
    member.binding = resolved.binding_name;
    member.is_import_specifier = matches!(resolved.kind, Some(BindingSourceKind::ImportSpecifier));
    member.source_match = None;
}

/// Builds a `ChunkPlan` from spec requests and chunk AST analysis.
///
/// Owns the five mutable maps (`binding_assignment`,
/// `bindings_catalogue`, `anonymous_ordinal_assignment`, `module_plans`,
/// `residual_plan_index`) that the previous shape passed through every
/// helper as `&mut` arguments. All duplicate-claim / cross-claim
/// invariants on the canonical state live behind the builder's
/// methods.
pub(super) struct ChunkPlanBuilder {
    /// Per-binding-`Id` index into `module_plans`. Authoritative
    /// source of "which logical module owns this binding".
    binding_assignment: HashMap<Id, usize>,
    /// Per-source-body-index → `module_plans` index, for anonymous
    /// (non-declared) top-level statements claimed by spec
    /// `anonymous_statements` entries.
    anonymous_ordinal_assignment: BTreeMap<usize, usize>,
    /// The plans being constructed, in append order. Final indices
    /// stable: `binding_assignment` and `anonymous_ordinal_assignment`
    /// hold positional references.
    module_plans: Vec<ModulePlan>,
    /// `BindingKind` view of every claimed binding (Owned vs Imported).
    /// Owned entries duplicate `binding_assignment`'s mapping in a
    /// different shape; Imported entries are exclusive to this map.
    bindings_catalogue: HashMap<Id, BindingKind>,
    /// Index into `module_plans` of the "catchall" plan that
    /// unclaimed bindings sweep into, when one exists. `None` when
    /// the chunk has no residual landing site (default
    /// `InlineInEntry` with no fallback request, or `MiniFactors`).
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
    /// inserts into this index and look up by `&str` to keep
    /// duplicate detection O(1) per member.
    ///
    /// Only consulted during `add_explicit_request`; later phases
    /// (destructure siblings, residual sweep) append without
    /// name-collision checks. The map is dropped by
    /// `drop_explicit_request_scratch`.
    catalogue_index_by_name: HashMap<String, BindingKind>,
}

impl ChunkPlanBuilder {
    pub(super) fn new() -> Self {
        Self {
            binding_assignment: HashMap::new(),
            anonymous_ordinal_assignment: BTreeMap::new(),
            module_plans: Vec::new(),
            bindings_catalogue: HashMap::new(),
            residual_plan_index: None,
            unmatched_spec_claims: Vec::new(),
            catalogue_index_by_name: HashMap::new(),
        }
    }

    /// Process one explicit (non-residual) logical-module request:
    /// resolve its anonymous-statement matches, claim each named
    /// member, and append a `ModulePlan`. Duplicate-claim detection
    /// across all explicit requests is keyed by binding-name via the
    /// builder's `catalogue_index_by_name` scratch index.
    pub(super) fn add_explicit_request(
        &mut self,
        index: usize,
        request: &mut LogicalRequest,
        ctx: &ExplicitRequestContext<'_>,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
    ) -> Result<()> {
        resolve_request_source_matches(request, ctx.runtime_module)?;
        reject_duplicate_member_bindings("logical_module", &request.id, &request.members)?;
        let mut bindings = HashMap::<String, String>::new();
        let anonymous_statement_claims =
            resolve_anonymous_statement_ordinals(request, ctx.runtime_module)?;
        for claim in &anonymous_statement_claims {
            if let Some(existing) = self
                .anonymous_ordinal_assignment
                .get(&claim.ordinal)
                .copied()
            {
                let existing_id: String = self
                    .module_plans
                    .get(existing)
                    .map(|plan: &ModulePlan| plan.id.clone())
                    .unwrap_or_else(|| format!("<plan#{existing}>"));
                bail!(
                    "anonymous_statements[].match in module {} also matches the \
                     top-level statement at ordinal {} already claimed by module {}; \
                     each anonymous statement may belong to at most one logical \
                     module.",
                    request.id,
                    claim.ordinal,
                    existing_id,
                );
            }
            self.anonymous_ordinal_assignment
                .insert(claim.ordinal, index);
        }
        let anonymous_statement_ordinals: Vec<usize> = anonymous_statement_claims
            .iter()
            .map(|claim| claim.ordinal)
            .collect();
        let anonymous_statement_comments: BTreeMap<usize, String> = anonymous_statement_claims
            .iter()
            .filter_map(|claim| {
                claim
                    .comment
                    .as_ref()
                    .map(|comment| (claim.ordinal, comment.clone()))
            })
            .collect();
        let dest_target_file = target_file_for_request(ctx.target_dir, &request.target_path)?;
        let module_id = ModuleId(LogicalModuleIndex(index));
        for member in &request.members {
            if let Some(existing_kind) = self.catalogue_index_by_name.get(member.binding.as_str()) {
                let existing_id = match existing_kind {
                    BindingKind::Owned {
                        module: ModuleId(LogicalModuleIndex(owner_index)),
                    } => self
                        .module_plans
                        .get(*owner_index)
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{owner_index}>")),
                    BindingKind::Imported {
                        re_exporter: ModuleId(LogicalModuleIndex(re_index)),
                        ..
                    } => self
                        .module_plans
                        .get(*re_index)
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{re_index}>")),
                };
                bail!(
                    "Duplicate binding claim for {:?} in chunk {:?}: already \
                     claimed by module {existing_id} and now also claimed by module \
                     {}. Each binding may belong to exactly one logical module. \
                     Different selector forms (`{{name: foo}}` vs \
                     `{{name: foo, kind: class_declaration}}`) that resolve to the \
                     same source declaration still count as duplicates. To expose a \
                     binding under multiple readable names, list all the renames in \
                     one module.",
                    member.binding,
                    ctx.chunk_id,
                    request.id,
                );
            }
            if member.is_import_specifier {
                let (imported_name, imported_from) = resolve_imported_binding(
                    imported_binding_resolver,
                    ctx.runtime_import_facts,
                    ctx.chunk_id,
                    ctx.target_file,
                    &member.binding,
                    imported_from_by_src,
                )?;
                let kind = BindingKind::Imported {
                    imported_name: imported_name.into(),
                    imported_from,
                    re_exporter: module_id,
                    public_name: member.export_name.as_str().into(),
                };
                self.catalogue_index_by_name
                    .insert(member.binding.clone(), kind.clone());
                self.bindings_catalogue.insert(
                    top_level_id(&member.binding, ctx.chunk_top_level_mark),
                    kind,
                );
            } else {
                bindings.insert(member.binding.clone(), member.export_name.clone());
            }
        }
        for (binding, export_name) in &bindings {
            let binding_id = top_level_id(binding, ctx.chunk_top_level_mark);
            if ctx.declaration_by_name.contains_key(&binding_id) {
                self.binding_assignment.insert(binding_id.clone(), index);
                let kind = BindingKind::Owned { module: module_id };
                self.catalogue_index_by_name
                    .insert(binding.clone(), kind.clone());
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
            .filter_map(|member| {
                member
                    .comment
                    .as_ref()
                    .map(|c| (member.binding.clone(), c.clone()))
            })
            .collect();
        self.module_plans.push(ModulePlan {
            id: request.id.clone(),
            target_file: dest_target_file,
            target_path: request.target_path.clone(),
            explicit: true,
            bindings,
            anonymous_statement_ordinals,
            anonymous_statement_comments,
            comment: request.comment.clone(),
            binding_comments,
        });
        Ok(())
    }

    /// Drop the name-keyed catalogue scratch index now that the
    /// explicit-requests loop is finished. Destructure siblings and
    /// the residual sweep don't consult this index.
    pub(super) fn drop_explicit_request_scratch(&mut self) {
        self.catalogue_index_by_name = HashMap::new();
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
            let Some(&owner_index) = self.binding_assignment.get(&claimed_id) else {
                continue;
            };
            let owner_id = ModuleId(LogicalModuleIndex(owner_index));
            for sibling in sibling_set {
                if sibling == claimed_name {
                    continue;
                }
                let sibling_id = top_level_id(sibling, chunk_top_level_mark);
                match self.binding_assignment.get(&sibling_id).copied() {
                    None => {
                        self.binding_assignment
                            .insert(sibling_id.clone(), owner_index);
                        self.bindings_catalogue
                            .insert(sibling_id, BindingKind::Owned { module: owner_id });
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
    /// cross-module import wiring must follow it. Runs before the
    /// residual sweep so the sweep doesn't route these bindings to
    /// the catchall while their declaring statement lives elsewhere
    /// (which emitted an `export { name }` whose declaration is in a
    /// different file — a SyntaxError at load).
    pub(super) fn adopt_bindings_of_claimed_anonymous_statements(
        &mut self,
        declarations: &[TopLevelDecl],
    ) {
        for decl in declarations {
            let Some(&plan_index) = self.anonymous_ordinal_assignment.get(&decl.ordinal) else {
                continue;
            };
            let module = ModuleId(LogicalModuleIndex(plan_index));
            for (name, id) in &decl.bindings {
                if self.binding_assignment.contains_key(id) {
                    continue;
                }
                self.binding_assignment.insert(id.clone(), plan_index);
                self.module_plans[plan_index]
                    .bindings
                    .entry(name.clone())
                    .or_insert_with(|| name.clone());
                self.bindings_catalogue
                    .insert(id.clone(), BindingKind::Owned { module });
            }
        }
    }

    /// Residual sweep: route every chunk top-level binding the spec
    /// did not claim to the chunk's catchall destination.
    ///
    /// Two shapes:
    ///
    /// 1. A memberless residual request was synthesized (or supplied
    ///    by the spec) — build a new residual plan, append it, and
    ///    point `residual_plan_index` at it.
    /// 2. An explicit `logical_modules` entry already pins itself at
    ///    the catchall target — repurpose that plan: flip its
    ///    `explicit` flag and append unclaimed bindings to its
    ///    members.
    ///
    /// `None` for both `residual_request` and `catchall_target`
    /// leaves `residual_plan_index` unset, which is the
    /// `InlineInEntry` / `MiniFactors` shape.
    pub(super) fn add_residual_sweep(
        &mut self,
        residual_request: Option<&LogicalRequest>,
        catchall_target_for_overflow: Option<&str>,
        declarations: &[TopLevelDecl],
        target_dir: &str,
    ) -> Result<()> {
        if let Some(residual) = residual_request {
            let residual_index = self.module_plans.len();
            let residual_module_id = ModuleId(LogicalModuleIndex(residual_index));
            let mut residual_bindings = HashMap::<String, String>::new();
            for decl in declarations {
                for (name, id) in &decl.bindings {
                    if !self.binding_assignment.contains_key(id) {
                        self.binding_assignment.insert(id.clone(), residual_index);
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
                    explicit: false,
                    bindings: residual_bindings,
                    anonymous_statement_ordinals: Vec::new(),
                    anonymous_statement_comments: BTreeMap::new(),
                    comment: None,
                    binding_comments: BTreeMap::new(),
                });
                self.residual_plan_index = Some(residual_index);
            }
        } else if let Some(catchall_target) = catchall_target_for_overflow {
            // No memberless residual request was synthesized — an
            // explicit `logical_modules` entry already pinned itself at
            // the catchall target. Append unclaimed bindings to that
            // plan so the residual sweep still has a home, and flip
            // its `explicit` flag so downstream consumers see it as
            // the residual destination (residual flag on the factorization
            // module, OutputRole::ResidualModule in artifact metadata, and
            // `residual: true` in modules.json).
            let owner_index = self
                .module_plans
                .iter()
                .position(|plan| plan.target_path == catchall_target);
            if let Some(owner_index) = owner_index {
                let owner_id = ModuleId(LogicalModuleIndex(owner_index));
                let owner_plan = &mut self.module_plans[owner_index];
                owner_plan.explicit = false;
                for decl in declarations {
                    for (name, id) in &decl.bindings {
                        if !self.binding_assignment.contains_key(id) {
                            self.binding_assignment.insert(id.clone(), owner_index);
                            owner_plan
                                .bindings
                                .entry(name.clone())
                                .or_insert_with(|| name.clone());
                            self.bindings_catalogue
                                .insert(id.clone(), BindingKind::Owned { module: owner_id });
                        }
                    }
                }
                self.residual_plan_index = Some(owner_index);
            }
        }
        Ok(())
    }

    /// `MiniFactors` mode: every unclaimed atomic factor unit gets
    /// its own synthesized plan at `__auto/mini/NNNN`. Run after
    /// Stage A.5 (`apply_rebind_folds`) so the residual sweep +
    /// rebind folding have already settled which units are still
    /// unclaimed. Bindings
    /// previously parked at the residual plan are moved out into the
    /// synthesized plan; the residual plan's `bindings` map is
    /// pruned to match.
    pub(super) fn synthesize_mini_factors(
        &mut self,
        precomputed: &OwnerGraphAndUnits,
        body: &[ModuleItem],
        target_dir: &str,
    ) -> Result<()> {
        let owner_graph = &precomputed.owner_graph;
        let atomic_units = &precomputed.atomic_units;
        let mut owner_declared_names: HashMap<OwnerId, Vec<Id>> = HashMap::new();
        let mut owner_statement_ordinal: HashMap<OwnerId, usize> = HashMap::new();
        for node in owner_graph.iter_nodes() {
            let ids: Vec<Id> = node.declared.iter().cloned().collect();
            owner_declared_names.insert(node.id, ids);
            owner_statement_ordinal.insert(node.id, node.statement_ordinal.0);
        }

        let residual_plan_index = self.residual_plan_index;
        let binding_assignment = &self.binding_assignment;
        let anonymous_ordinal_assignment = &self.anonymous_ordinal_assignment;
        // A unit member counts as unclaimed iff every declared binding
        // is either absent from `binding_assignment` or assigned to
        // the residual plan (if any); anonymous owners must similarly
        // be unassigned or routed via residual. If any member is
        // claimed by an explicit (non-residual) plan, the spec author
        // already named the unit's destination — leave the existing
        // claim intact (and let downstream validation flag an
        // atomic-unit conflict if the claims disagree).
        let is_owner_unclaimed = |owner: OwnerId| -> bool {
            let names = owner_declared_names
                .get(&owner)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            for id in names {
                match binding_assignment.get(id).copied() {
                    None => continue,
                    Some(idx) if Some(idx) == residual_plan_index => continue,
                    Some(_) => return false,
                }
            }
            if names.is_empty() {
                let Some(stmt_ord) = owner_statement_ordinal.get(&owner).copied() else {
                    return true;
                };
                let Some(body_idx) = body_index_for_statement_ordinal(body, stmt_ord) else {
                    return true;
                };
                match anonymous_ordinal_assignment.get(&body_idx).copied() {
                    None => return true,
                    Some(idx) if Some(idx) == residual_plan_index => return true,
                    Some(_) => return false,
                }
            }
            true
        };

        let mut unclaimed_units: Vec<&BTreeSet<OwnerId>> = atomic_units
            .iter()
            .filter(|unit| unit.members.iter().copied().all(is_owner_unclaimed))
            .map(|unit| &unit.members)
            .collect();
        // Stable iteration order: smallest OwnerId first.
        unclaimed_units.sort_by_key(|members| members.iter().next().copied());

        for (idx, members) in unclaimed_units.into_iter().enumerate() {
            let synthetic_idx = self.module_plans.len();
            let synthetic_module_id = ModuleId(LogicalModuleIndex(synthetic_idx));
            let target_path = format!("__auto/mini/{idx:04}");
            let target_file = target_file_for_request(target_dir, &target_path)?;
            let mut bindings = HashMap::<String, String>::new();
            let mut anonymous_statement_ordinals = Vec::<usize>::new();
            for owner in members {
                let names = owner_declared_names
                    .get(owner)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                if names.is_empty() {
                    let Some(stmt_ord) = owner_statement_ordinal.get(owner).copied() else {
                        continue;
                    };
                    let Some(body_idx) = body_index_for_statement_ordinal(body, stmt_ord) else {
                        continue;
                    };
                    self.anonymous_ordinal_assignment
                        .insert(body_idx, synthetic_idx);
                    anonymous_statement_ordinals.push(body_idx);
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
                    if let Some(prev) = self.binding_assignment.get(name).copied()
                        && Some(prev) == residual_plan_index
                        && let Some(residual_idx) = residual_plan_index
                    {
                        self.module_plans[residual_idx].bindings.remove(&name_str);
                    }
                    self.binding_assignment.insert(name.clone(), synthetic_idx);
                    self.bindings_catalogue.insert(
                        name.clone(),
                        BindingKind::Owned {
                            module: synthetic_module_id,
                        },
                    );
                }
            }
            anonymous_statement_ordinals.sort_unstable();
            self.module_plans.push(ModulePlan {
                id: target_path.clone(),
                target_file,
                target_path,
                explicit: false,
                bindings,
                anonymous_statement_ordinals,
                anonymous_statement_comments: BTreeMap::new(),
                comment: None,
                binding_comments: BTreeMap::new(),
            });
        }
        Ok(())
    }

    /// Apply a batch of rebind-fold decisions produced by the
    /// Stage A.5 composer (`stage_one::compute_rebind_folds`).
    ///
    /// Each fold reroutes a single binding from its previous plan
    /// (if any) to the cycle's explicit destination. The
    /// `bindings_catalogue` mirrors the new owner, and bindings
    /// that were previously parked at the residual plan are pruned
    /// from that plan's binding list so the residual doesn't
    /// double-claim them.
    ///
    /// Folds are produced in atomic-unit iteration order; we do not
    /// reorder them here. The mutations are idempotent in the sense
    /// that `module_plans[dest].bindings.entry(name).or_insert_with`
    /// preserves any existing entry under that name.
    pub(super) fn apply_rebind_folds(&mut self, folds: Vec<RebindFold>) {
        let residual_plan_index = self.residual_plan_index;
        for fold in folds {
            let RebindFold {
                binding,
                name,
                dest,
                owned_kind,
                previous,
            } = fold;
            self.binding_assignment.insert(binding.clone(), dest);
            self.bindings_catalogue.insert(binding, owned_kind);
            self.module_plans[dest]
                .bindings
                .entry(name.clone())
                .or_insert_with(|| name.clone());
            if let Some(prev_idx) = previous
                && Some(prev_idx) == residual_plan_index
            {
                self.module_plans[prev_idx].bindings.remove(&name);
            }
        }
    }

    /// Borrow access to the current binding assignment so the
    /// Stage A.5 composer can compute folds against it without
    /// mutating the builder.
    pub(super) fn binding_assignment(&self) -> &HashMap<Id, usize> {
        &self.binding_assignment
    }

    /// The residual landing-site plan index, if one was created by
    /// the residual sweep. Needed by Stage A.5 to know which
    /// existing claims count as "swept" (and hence still foldable).
    pub(super) fn residual_plan_index(&self) -> Option<usize> {
        self.residual_plan_index
    }

    pub(super) fn finalize(self) -> ChunkPlan {
        ChunkPlan {
            module_plans: self.module_plans,
            binding_assignment: self.binding_assignment,
            bindings_catalogue: self.bindings_catalogue,
            anonymous_ordinal_assignment: self.anonymous_ordinal_assignment,
            unmatched_spec_claims: self.unmatched_spec_claims,
        }
    }
}
