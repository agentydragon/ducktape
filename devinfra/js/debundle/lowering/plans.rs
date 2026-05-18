//! Spec-derived request + plan structures plus the helpers that
//! convert spec entries into `LogicalRequest`s and synthesize
//! mini-factor plans for unclaimed atomic units.

use super::util::{body_index_for_statement_ordinal, target_file_for_request};
use super::*;

#[derive(Debug, Clone)]
pub(super) struct LogicalRequest {
    pub(super) id: String,
    pub(super) target_path: String,
    pub(super) residual: bool,
    pub(super) members: Vec<MemberRequest>,
    /// Verbatim source of each anonymous-statement member the spec
    /// asked to co-move into this module. Resolved later (after AST
    /// analysis) into [`ModulePlan::anonymous_statement_ordinals`].
    pub(super) anonymous_match_sources: Vec<String>,
}

#[derive(Debug, Clone)]
pub(super) struct MemberRequest {
    pub(super) binding: String,
    pub(super) export_name: String,
    /// When `true`, the member's source is an import specifier in the
    /// source chunk (not a top-level decl). The materializer looks up
    /// the import statement by `binding` in the chunk body and rewrites
    /// it to a re-import in the destination module.
    pub(super) is_import_specifier: bool,
    /// Spec-level purity annotation. `Pure` asserts that calls to the
    /// bound function have no observable side effects — the validator
    /// trusts the annotation and drops S edges for `<binding>(...)`
    /// call sites. `Default` means "not annotated, fall back to
    /// inferred classification". An author-trust contract; see
    /// AGENTS.md "Declared purity" and DESIGN.md A9.
    pub(super) purity: MemberPurity,
    /// Spec-level local-effect annotation. `TypescriptDecorateHelper`
    /// asserts that recognized calls to the bound helper mutate only
    /// their target class/prototype, so the analyzer can model a local
    /// effect edge instead of a global side-effect-order edge.
    pub(super) effect: MemberEffect,
    /// Property names on the bound value whose member calls
    /// (`<binding>.<prop>(args)` / `<binding>?.<prop>(args)`) the author
    /// asserts have no observable side effects beyond evaluating their
    /// arguments. Same author-trust contract as `purity: pure` — see
    /// AGENTS.md "Declared purity". Empty when the spec doesn't carry a
    /// `pure_members` entry for this member.
    pub(super) pure_members: Vec<String>,
}

#[derive(Debug, Clone)]
pub(super) struct ModulePlan {
    pub(super) id: String,
    pub(super) target_file: String,
    /// Logical module path the spec asked for (e.g. `"ai/mcp/foo"`).
    /// Distinct from `target_file`, which is the chunk-relative
    /// emitted file path (e.g. `"modules/foo.js"`).
    pub(super) target_path: String,
    pub(super) explicit: bool,
    /// Local-name → public-export-name for every owned binding this
    /// plan claims (i.e. members whose `selector.binding.kind` is
    /// _not_ `ImportSpecifier`). ImportSpecifier-bound members live
    /// in `Schedule.bindings` as `BindingKind::Imported` and their
    /// emit is driven from there. Iteration order is undefined;
    /// emit / report sites sort by local name before consuming so
    /// the emitted source and JSON shapes stay deterministic.
    pub(super) bindings: HashMap<String, String>,
    /// Source-chunk statement ordinals of anonymous-statement members
    /// claimed by this module. These owners have empty
    /// `declared_bindings`, so they can't be addressed by name —
    /// the spec resolves them by AST shape (see
    /// [`spec::LogicalModule::anonymous_statements`]). The
    /// materializer routes each such statement into this module's
    /// body in source order, alongside the named members.
    pub(super) anonymous_statement_ordinals: Vec<usize>,
}

pub(super) fn logical_requests_for_chunk(
    chunk_logical_modules: Option<&BTreeMap<String, LogicalModule>>,
    chunk_unassigned_mode: &UnassignedMode,
    chunk_renames_present: bool,
    chunk_id: &str,
    target_dir: &str,
) -> Result<Vec<LogicalRequest>> {
    let mut requests = Vec::new();
    let catchall_target = chunk_unassigned_mode
        .catchall_file_target()
        .map(str::to_string);
    let mut explicit_module_at_catchall = false;
    if let Some(by_target_path) = chunk_logical_modules {
        for (target_path, module) in by_target_path {
            let id = format!("{chunk_id}::{target_path}");
            let members = build_members(&module.members);
            reject_duplicate_export_names("logical_module", &id, &members)?;
            reject_duplicate_member_bindings("logical_module", &id, &members)?;
            let anonymous_match_sources = module
                .anonymous_statements
                .iter()
                .map(|stmt| stmt.match_source.clone())
                .collect();
            if catchall_target.as_deref() == Some(target_path.as_str()) {
                explicit_module_at_catchall = true;
            }
            requests.push(LogicalRequest {
                id,
                target_path: target_path.clone(),
                residual: false,
                members,
                anonymous_match_sources,
            });
        }
    }
    // Synthesize a memberless catchall-file request when the chunk's
    // `unassigned_mode` is `CatchallFile` and no explicit logical
    // module already claims the catchall target. When an explicit
    // module *is* at the catchall target, the residual sweep in
    // `materialize_logical_chunk` will append unclaimed bindings to
    // that explicit plan instead.
    if let Some(target_path) = catchall_target
        && !explicit_module_at_catchall
    {
        requests.push(LogicalRequest {
            id: format!("{chunk_id}::residual"),
            target_path,
            residual: true,
            members: Vec::new(),
            anonymous_match_sources: Vec::new(),
        });
    }
    // Fallback: when the spec is silent about this chunk (no
    // `logical_modules`, default `InlineInEntry` mode, no
    // `chunk_renames`), inject a memberless residual so the
    // materializer has at least one module to point unowned decls
    // at. Skipped when the spec has any `chunk_renames` for the
    // chunk — that signals the spec wants bindings to stay in
    // `ResidualEntry`-land (no `Logical(R)` module, no separate
    // residual file emitted), with renames applied in-place by the
    // lowerer. Skipped when `MiniFactors` is active — the
    // synthesizer takes care of placing unclaimed code into
    // mini-factor modules.
    if requests.is_empty()
        && !chunk_renames_present
        && !matches!(chunk_unassigned_mode, UnassignedMode::MiniFactors)
    {
        requests.push(LogicalRequest {
            id: format!("{chunk_id}::residual"),
            target_path: join_module_path(&[target_dir, "unhandled"]),
            residual: true,
            members: Vec::new(),
            anonymous_match_sources: Vec::new(),
        });
    }
    Ok(requests)
}

#[allow(clippy::too_many_arguments)]
pub(super) fn synthesize_mini_factor_plans(
    precomputed: &OwnerGraphAndUnits,
    body: &[ModuleItem],
    residual_plan_index: Option<usize>,
    module_plans: &mut Vec<ModulePlan>,
    binding_assignment: &mut HashMap<Id, usize>,
    bindings_catalogue: &mut HashMap<Id, BindingKind>,
    anonymous_ordinal_assignment: &mut BTreeMap<usize, usize>,
    chunk_top_level_mark: swc_common::Mark,
    target_dir: &str,
) -> Result<()> {
    let owner_graph = &precomputed.owner_graph;
    let atomic_units = &precomputed.atomic_units;
    let mut owner_declared_names: HashMap<OwnerId, Vec<BindingName>> = HashMap::new();
    let mut owner_statement_ordinal: HashMap<OwnerId, usize> = HashMap::new();
    for node in owner_graph.iter_nodes() {
        let names: Vec<BindingName> = node
            .declared
            .iter()
            .filter_map(|bid| owner_graph.binding_table.name(*bid).cloned())
            .collect();
        owner_declared_names.insert(node.id, names);
        owner_statement_ordinal.insert(node.id, node.statement_ordinal.0);
    }

    // A unit member counts as unclaimed iff every declared binding is
    // either absent from `binding_assignment` or assigned to the
    // residual plan (if any); anonymous owners must similarly be
    // unassigned or routed via residual. If any member is claimed by
    // an explicit (non-residual) plan, the spec author already named
    // the unit's destination — leave the existing claim intact (and
    // let downstream validation flag an atomic-unit conflict if the
    // claims disagree).
    let is_owner_unclaimed = |owner: OwnerId| -> bool {
        let names = owner_declared_names
            .get(&owner)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        for name in names {
            let id = top_level_id(name, chunk_top_level_mark);
            match binding_assignment.get(&id).copied() {
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
        let synthetic_idx = module_plans.len();
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
                anonymous_ordinal_assignment.insert(body_idx, synthetic_idx);
                anonymous_statement_ordinals.push(body_idx);
                continue;
            }
            for name in names {
                bindings.insert(name.clone(), name.clone());
                let id = top_level_id(name, chunk_top_level_mark);
                // Move the binding out of the residual plan (if it was
                // staged there by the sweep above) into the synthesized
                // plan. The residual plan's bindings/anonymous-ordinal
                // maps are pruned so it doesn't double-claim members.
                if let Some(prev) = binding_assignment.get(&id).copied()
                    && Some(prev) == residual_plan_index
                    && let Some(residual_idx) = residual_plan_index
                {
                    module_plans[residual_idx].bindings.remove(name);
                }
                binding_assignment.insert(id.clone(), synthetic_idx);
                bindings_catalogue.insert(
                    id,
                    BindingKind::Owned {
                        owner: synthetic_module_id,
                    },
                );
            }
        }
        anonymous_statement_ordinals.sort_unstable();
        module_plans.push(ModulePlan {
            id: target_path.clone(),
            target_file,
            target_path,
            explicit: false,
            bindings,
            anonymous_statement_ordinals,
        });
    }
    Ok(())
}

pub(super) fn build_members(members: &[spec::Member]) -> Vec<MemberRequest> {
    members
        .iter()
        .map(|m| {
            let binding = m.selector.binding.name.clone();
            let export_name = m.name.clone().unwrap_or_else(|| binding.clone());
            MemberRequest {
                is_import_specifier: matches!(
                    m.selector.binding.kind,
                    Some(BindingSourceKind::ImportSpecifier)
                ),
                binding,
                export_name,
                purity: m.purity,
                effect: m.effect,
                pure_members: m.pure_members.clone(),
            }
        })
        .collect()
}

pub(super) fn known_effect_from_member_effect(effect: MemberEffect) -> Option<KnownEffect> {
    match effect {
        MemberEffect::Default => None,
        MemberEffect::TypescriptDecorateHelper => Some(KnownEffect::TypescriptDecorateHelper),
    }
}
