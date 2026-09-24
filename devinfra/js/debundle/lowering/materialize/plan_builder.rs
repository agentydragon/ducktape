//! `ChunkPlanBuilder` owns the per-chunk mutable state that
//! `materialize_logical_chunk` threads across its eight phases. Each phase is a
//! method on the builder, so the shared lookup (`bindings_catalogue` +
//! `binding_assignment`) lives behind the builder's encapsulation rather than
//! being open-coded per phase.

use super::super::ordinal::body_index_for_statement_ordinal;
use super::outcome_sink::OutcomeSink;
use super::*;
use crate::plans::{AnonymousStatementRequest, RelationalSelector};
use analysis::{DepKind, OwnerId, StatementOrdinal};

#[derive(Debug, Clone)]
struct AnonymousStatementTargetInfo {
    target: SelectorTargetId,
    request_index: usize,
    statement_index: usize,
    statement: AnonymousStatementRequest,
}

/// A `source_match` selector projected into the solve: its targets (one, or
/// every binding of a `source_matches[]` group) and its candidate rows, each
/// row the `(owner, binding)` places it would claim.
struct ProjectedEntity {
    targets: Vec<SelectorTargetId>,
    rows: Vec<Vec<(OwnerId, Option<String>)>>,
    subject: ProjectedEntitySubject,
}

enum ProjectedEntitySubject {
    Member {
        request_index: usize,
        member_index: usize,
    },
    Group {
        request_index: usize,
        group: SourceMatchGroupAssignment,
    },
}

/// The targets whose solved claims made `entity` unique: `Some` when it had
/// several candidate rows and exactly one survives dropping every row whose
/// owner or binding another exclusive target's solved value holds. `exclusive`
/// are the targets the solve keeps on distinct owners; a claim by any other
/// target never took a row away.
fn elimination_claimers(
    entity: &ProjectedEntity,
    result: &selector_ir::SolverResult,
    exclusive: &BTreeSet<SelectorTargetId>,
) -> Option<BTreeSet<SelectorTargetId>> {
    if entity.rows.len() < 2
        || !entity.targets.iter().all(|target| {
            matches!(
                result.outcome_for(*target),
                Some(ClaimOutcome::Unique { .. })
            )
        })
    {
        return None;
    }
    let other_claims = exclusive
        .iter()
        .filter(|target| !entity.targets.contains(target))
        .filter_map(|target| match result.outcome_for(*target) {
            Some(ClaimOutcome::Unique { claim }) => Some((*target, claim)),
            _ => None,
        })
        .collect::<Vec<_>>();
    let mut survivors = 0;
    let mut claimers = BTreeSet::new();
    for row in &entity.rows {
        let takers = other_claims
            .iter()
            .filter(|(_, claim)| {
                row.iter().any(|(owner, binding)| {
                    *owner == claim.owner || (binding.is_some() && *binding == claim.binding)
                })
            })
            .map(|(target, _)| *target)
            .collect::<Vec<_>>();
        if takers.is_empty() {
            survivors += 1;
        }
        claimers.extend(takers);
    }
    (survivors == 1).then_some(claimers)
}

struct SelectorFactCoverage<'a> {
    owner_kind_by_owner: BTreeMap<OwnerId, &'a str>,
    owners_by_binding: BTreeMap<&'a str, BTreeSet<OwnerId>>,
}

use selector_ir::{
    ClaimOutcome, ResolvedClaim, SelectorAtom, SelectorFact, SelectorFactStore, SelectorProgram,
    SelectorSourceMatchProjectionEvent, SelectorSourceMatchProjectionOutcome, SelectorTargetId,
};
use selector_ir_lowering::{
    MemberSelectorLoweringContext, MemberSelectorProgramBuilder, MemberSelectorSpecRef,
};
use selector_outcome::{
    Candidate, Entity, EntityRef, MAX_CANDIDATES_PER_SELECTOR, Outcome, Placement, ResolvedBy,
    SelectorKind, SelectorOutcome, SelectorOutcomeReport, Severity,
};
use selector_runtime::solve_global_selector_program;

/// The module path of a `<chunk>::<path>` logical module id.
fn logical_module_path(request_id: &str) -> String {
    request_id
        .split_once("::")
        .map(|(_, path)| path.to_string())
        .unwrap_or_else(|| panic!("logical module id {request_id:?} is not `<chunk>::<path>`"))
}

/// Names a solver target the way its own outcome names it.
fn target_entity_ref(target: &selector_ir::SelectorTarget) -> EntityRef {
    EntityRef {
        logical_module: logical_module_path(&target.logical_module),
        entity: match (&target.claim, &target.origin) {
            (
                selector_ir::ClaimKind::Binding {
                    export_name: Some(export_name),
                },
                _,
            )
            | (selector_ir::ClaimKind::BindingGroupMember { export_name, .. }, _) => {
                Some(Entity::Export(export_name.clone()))
            }
            (_, selector_ir::ClaimOrigin::AnonymousStatement { index }) => {
                Some(Entity::AnonymousStatement(*index))
            }
            _ => None,
        },
    }
}

fn target_entity_refs<'a>(
    program: &SelectorProgram,
    targets: impl IntoIterator<Item = &'a SelectorTargetId>,
) -> Vec<EntityRef> {
    targets
        .into_iter()
        .map(|target| target_entity_ref(&program.targets[target.0]))
        .collect()
}

fn member_outcome(
    chunk_id: &str,
    request: &LogicalRequest,
    member: &MemberRequest,
    outcome: Outcome,
) -> SelectorOutcome {
    SelectorOutcome {
        chunk: chunk_id.to_string(),
        placement: Some(Placement {
            logical_module: request.target_path.clone(),
            entity: Some(Entity::Export(member.export_name.clone())),
            selector_kind: member_selector_kind(member),
        }),
        target_binding: member
            .source_match
            .as_ref()
            .and_then(|selector| selector.target_binding.clone()),
        selector_preview: match (
            &member.source_match,
            &member.relational,
            &member.binding_selector,
        ) {
            (Some(selector), _, _) => {
                Some(source_match::source_match_preview(&selector.match_source))
            }
            (None, Some(relational), _) => Some(format!("{relational:?}")),
            (None, None, Some(binding)) => Some(format!("{binding:?}")),
            (None, None, None) => None,
        },
        outcome,
    }
}

fn anonymous_statement_outcome(
    chunk_id: &str,
    request: &LogicalRequest,
    statement_index: usize,
    statement: &AnonymousStatementRequest,
    outcome: Outcome,
) -> SelectorOutcome {
    SelectorOutcome {
        chunk: chunk_id.to_string(),
        placement: Some(Placement {
            logical_module: request.target_path.clone(),
            entity: Some(Entity::AnonymousStatement(statement_index)),
            selector_kind: SelectorKind::AnonymousStatement,
        }),
        target_binding: None,
        selector_preview: Some(source_match::source_match_preview(
            &statement.selector.match_source,
        )),
        outcome,
    }
}

/// A solver claim as a candidate place: its source body index and binding.
fn claim_candidate(module: &swc_ecma_ast::Module, claim: &ResolvedClaim) -> Result<Candidate> {
    Ok(Candidate {
        owner: body_index_for_statement_ordinal(&module.body, claim.statement_ordinal.0)
            .with_context(|| {
                format!(
                    "global selector solver claimed post-split ordinal {} which has no source \
                     body item",
                    claim.statement_ordinal.0
                )
            })?,
        binding: claim.binding.clone(),
    })
}

fn ambiguous_outcome(
    module: &swc_ecma_ast::Module,
    candidates: &[ResolvedClaim],
    truncated: bool,
) -> Result<Outcome> {
    Ok(Outcome::ambiguous(
        candidates
            .iter()
            .map(|claim| claim_candidate(module, claim))
            .collect::<Result<_>>()?,
        truncated,
    ))
}

/// The outcome of a selector the candidate projection rejected before the
/// solve, from its projection-event `reason_category`.
fn unprojected_outcome(
    reason_category: &str,
    reason: &str,
    projected_row_count: Option<usize>,
) -> Outcome {
    match (reason_category, projected_row_count) {
        ("shape_matcher_no_candidates", _) => Outcome::NoMatch,
        ("too_broad", Some(row_count)) => Outcome::too_broad(row_count),
        _ => Outcome::Invalid {
            error: reason.to_string(),
        },
    }
}

fn too_broad_reason(row_count: usize) -> String {
    format!("{row_count} candidate rows exceed the cap of {MAX_CANDIDATES_PER_SELECTOR}")
}

fn member_selector_ref_for_global_solver(
    member: &MemberRequest,
) -> Option<MemberSelectorSpecRef<'_>> {
    if let Some(binding) = &member.binding_selector
        && !member.is_import_specifier
    {
        return Some(MemberSelectorSpecRef::Binding(binding));
    }

    if let Some(selector) = &member.source_match {
        return Some(MemberSelectorSpecRef::SourceMatch(selector));
    }

    if let Some(relational) = &member.relational {
        return Some(match relational {
            RelationalSelector::CrossRef(target) => MemberSelectorSpecRef::CrossRef(target),
            RelationalSelector::ReadsMember(target) => MemberSelectorSpecRef::ReadsMember(target),
            RelationalSelector::MemberOfModule(target) => {
                MemberSelectorSpecRef::MemberOfModule(target)
            }
            RelationalSelector::PassedToCall(target) => MemberSelectorSpecRef::PassedToCall(target),
            RelationalSelector::MakesDecorateCall(target) => {
                MemberSelectorSpecRef::MakesDecorateCall(target)
            }
            RelationalSelector::IntrinsicAlias(target) => {
                MemberSelectorSpecRef::IntrinsicAlias(target)
            }
        });
    }

    if member.resolves_after_chunk_analysis() || member.is_import_specifier {
        return None;
    }
    member
        .binding_selector
        .as_ref()
        .map(MemberSelectorSpecRef::Binding)
}

fn selector_fact_coverage(facts: &SelectorFactStore) -> SelectorFactCoverage<'_> {
    let mut owner_kind_by_owner = BTreeMap::new();
    let mut owners_by_binding = BTreeMap::<&str, BTreeSet<OwnerId>>::new();
    for fact in &facts.facts {
        match fact {
            SelectorFact::Owner {
                owner,
                statement_kind,
                ..
            } => {
                owner_kind_by_owner.insert(*owner, statement_kind.as_str());
            }
            SelectorFact::DeclaredBinding { owner, binding, .. } => {
                owners_by_binding
                    .entry(binding.as_str())
                    .or_default()
                    .insert(*owner);
            }
            _ => {}
        }
    }
    SelectorFactCoverage {
        owner_kind_by_owner,
        owners_by_binding,
    }
}

fn binding_source_kind_statement_kind(kind: spec::BindingSourceKind) -> &'static str {
    match kind {
        spec::BindingSourceKind::ImportSpecifier => "import",
        spec::BindingSourceKind::VariableDeclarator => "var_decl",
        spec::BindingSourceKind::FunctionDeclaration => "fn_decl",
        spec::BindingSourceKind::ClassDeclaration => "class_decl",
    }
}

fn binding_selector_has_fact_candidate(
    coverage: &SelectorFactCoverage<'_>,
    member: &MemberRequest,
) -> bool {
    let Some(selector) = &member.binding_selector else {
        return true;
    };
    if member.is_import_specifier {
        return true;
    }
    let Some(owners) = coverage.owners_by_binding.get(selector.name.as_str()) else {
        return false;
    };
    let Some(kind) = selector.kind else {
        return !owners.is_empty();
    };
    let expected = binding_source_kind_statement_kind(kind);
    owners.iter().any(|owner| {
        coverage
            .owner_kind_by_owner
            .get(owner)
            .is_some_and(|actual| *actual == expected)
    })
}

fn selector_fact_store_for_chunk(
    program: &SelectorProgram,
    chunk_id: ChunkId,
    structural: &analysis::facts::StructuralChunkAnalysis<'_>,
    module: &swc_ecma_ast::Module,
    import_sources: &HashMap<String, String>,
) -> SelectorFactStore {
    let mut store = SelectorFactStore::default();
    let binding_owner = structural_binding_owner(structural);
    let statement_by_owner: HashMap<OwnerId, &analysis::facts::StructuralStatementFacts> =
        structural
            .per_statement
            .iter()
            .map(|statement| (OwnerId(statement.ordinal.0), statement))
            .collect();
    let target_is_hoisted = |id: &swc_ecma_ast::Id| -> bool {
        binding_owner
            .get(id)
            .and_then(|owner| statement_by_owner.get(owner))
            .is_some_and(|statement| statement.kind == analysis::StatementKind::FnDecl)
    };

    for statement in &structural.per_statement {
        let owner = OwnerId(statement.ordinal.0);
        store.push(SelectorFact::Owner {
            chunk_id,
            owner,
            statement_ordinal: statement.ordinal,
            statement_kind: statement.kind.to_string(),
        });
        for binding in &statement.declared {
            store.push(SelectorFact::DeclaredBinding {
                chunk_id,
                owner,
                binding: binding.0.as_str().to_string(),
                export_name: None,
            });
        }
    }

    for statement in &structural.per_statement {
        let owner = OwnerId(statement.ordinal.0);
        for binding in &statement.reads.eager {
            if !target_is_hoisted(binding) {
                push_structural_selector_reference(
                    &mut store,
                    chunk_id,
                    owner,
                    binding,
                    DepKind::EagerUse,
                    &binding_owner,
                );
            }
        }
        for binding in &statement.reads.lazy {
            push_structural_selector_reference(
                &mut store,
                chunk_id,
                owner,
                binding,
                DepKind::LazyUse,
                &binding_owner,
            );
        }
        for binding in &statement.rebinds.eager {
            push_structural_selector_reference(
                &mut store,
                chunk_id,
                owner,
                binding,
                DepKind::EagerRebind,
                &binding_owner,
            );
        }
        for binding in &statement.rebinds.first_order_lazy {
            push_structural_selector_reference(
                &mut store,
                chunk_id,
                owner,
                binding,
                DepKind::LazyRebind,
                &binding_owner,
            );
        }
        for binding in &statement.rebinds.lazy {
            if statement.rebinds.first_order_lazy.contains(binding) {
                continue;
            }
            push_structural_selector_reference(
                &mut store,
                chunk_id,
                owner,
                binding,
                DepKind::DeferredRebind,
                &binding_owner,
            );
        }
    }

    if selector_program_needs_member_reads(program) {
        for (ordinal, reads) in chunk_facts::member_reads_by_ordinal(module) {
            for read in reads {
                store.push(SelectorFact::MemberRead {
                    chunk_id,
                    statement_ordinal: StatementOrdinal(ordinal),
                    object: read.object,
                    member: read.member,
                });
            }
        }
    }
    if selector_program_needs_module_member_uses(program) {
        for (ordinal, uses) in chunk_facts::module_member_uses_by_ordinal(module, import_sources) {
            for use_site in uses {
                store.push(SelectorFact::ModuleMemberUse {
                    chunk_id,
                    statement_ordinal: StatementOrdinal(ordinal),
                    module: use_site.module,
                    member: use_site.member,
                });
            }
        }
    }
    if selector_program_needs_call_argument_uses(program) {
        for call in chunk_facts::call_argument_uses(module) {
            store.push(SelectorFact::CallArgumentUse {
                chunk_id,
                argument: call.argument,
                callee_object: call.callee_object,
                callee_member: call.callee_member,
                arg_index: call.arg_index,
            });
        }
    }
    if selector_program_needs_decorate_call_uses(program) {
        for call in chunk_facts::decorate_call_uses(module) {
            store.push(SelectorFact::DecorateCallUse {
                chunk_id,
                callee: call.callee,
                class_anchor: call.class_anchor,
                member: call.member,
            });
        }
    }
    if selector_program_needs_intrinsic_alias_uses(program) {
        for alias in chunk_facts::intrinsic_alias_uses(module) {
            store.push(SelectorFact::IntrinsicAliasUse {
                chunk_id,
                binding: alias.binding,
                property: alias.property,
            });
        }
    }
    store
}

fn structural_binding_owner(
    structural: &analysis::facts::StructuralChunkAnalysis<'_>,
) -> HashMap<swc_ecma_ast::Id, OwnerId> {
    let mut binding_owner = HashMap::new();
    for statement in &structural.per_statement {
        let owner = OwnerId(statement.ordinal.0);
        for binding in &statement.declared {
            binding_owner.insert(binding.clone(), owner);
        }
    }
    binding_owner
}

fn push_structural_selector_reference(
    store: &mut SelectorFactStore,
    chunk_id: ChunkId,
    owner: OwnerId,
    binding: &swc_ecma_ast::Id,
    edge_kind: DepKind,
    binding_owner: &HashMap<swc_ecma_ast::Id, OwnerId>,
) {
    let Some(target_owner) = binding_owner.get(binding) else {
        return;
    };
    if owner == *target_owner {
        return;
    }
    store.push(SelectorFact::OwnerReferencesBinding {
        chunk_id,
        owner,
        binding: binding.0.as_str().to_string(),
        edge_kind: edge_kind.to_string(),
    });
}

fn selector_program_needs_member_reads(program: &SelectorProgram) -> bool {
    program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::ReadsMember { .. } | SelectorAtom::ReadsMemberOfOwner { .. }
        )
    })
}

fn selector_program_needs_module_member_uses(program: &SelectorProgram) -> bool {
    program
        .atoms
        .iter()
        .any(|atom| matches!(atom, SelectorAtom::ConsumesModuleMember { .. }))
}

fn selector_program_needs_call_argument_uses(program: &SelectorProgram) -> bool {
    program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::PassedToCall { .. } | SelectorAtom::PassedToCallOfOwner { .. }
        )
    })
}

fn selector_program_needs_decorate_call_uses(program: &SelectorProgram) -> bool {
    program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::MakesDecorateCall { .. } | SelectorAtom::MakesDecorateCallForOwner { .. }
        )
    })
}

fn selector_program_needs_intrinsic_alias_uses(program: &SelectorProgram) -> bool {
    program
        .atoms
        .iter()
        .any(|atom| matches!(atom, SelectorAtom::IntrinsicAlias { .. }))
}

fn solver_claim_is_import_specifier(facts: &SelectorFactStore, claim: &ResolvedClaim) -> bool {
    facts.facts.iter().any(|fact| {
        matches!(
            fact,
            SelectorFact::Owner {
                owner,
                statement_ordinal,
                statement_kind,
                ..
            } if *owner == claim.owner
                && *statement_ordinal == claim.statement_ordinal
                && statement_kind == "import"
        )
    })
}

fn source_match_projection_kind(claim_origin: &str) -> &'static str {
    if claim_origin.starts_with("source_matches[]") {
        "source_matches"
    } else {
        "members.source_match"
    }
}

fn member_selector_kind(member: &MemberRequest) -> SelectorKind {
    if member.source_match.is_some() {
        return if member.claim_origin.starts_with("source_matches[]") {
            SelectorKind::SourceMatches
        } else {
            SelectorKind::MemberSourceMatch
        };
    }
    match &member.relational {
        Some(RelationalSelector::CrossRef(_)) => SelectorKind::CrossRef,
        Some(RelationalSelector::ReadsMember(_)) => SelectorKind::ReadsMember,
        Some(RelationalSelector::MemberOfModule(_)) => SelectorKind::MemberOfModule,
        Some(RelationalSelector::PassedToCall(_)) => SelectorKind::PassedToCall,
        Some(RelationalSelector::MakesDecorateCall(_)) => SelectorKind::MakesDecorateCall,
        Some(RelationalSelector::IntrinsicAlias(_)) => SelectorKind::IntrinsicAlias,
        None => SelectorKind::Binding,
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct SourceMatchGroupCacheKey {
    selector: spec::AnonymousStatementSelector,
    target_bindings: Vec<String>,
}

impl SourceMatchGroupCacheKey {
    fn new(
        selector: spec::AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Self {
        let target_bindings = exports_by_target.keys().cloned().collect();
        Self {
            selector,
            target_bindings,
        }
    }
}

#[derive(Debug, Clone)]
struct SourceMatchGroupAssignment {
    selector: spec::AnonymousStatementSelector,
    parsed_selector: source_match::ParsedSourceMatchSelector,
    exports_by_target: BTreeMap<String, String>,
    members_by_target: BTreeMap<String, usize>,
    selector_kind: &'static str,
}

fn source_match_group_assignments(
    request: &LogicalRequest,
) -> BTreeMap<usize, SourceMatchGroupAssignment> {
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

    let mut assignments = BTreeMap::new();
    for (selector, member_indices) in grouped_by_selector {
        if member_indices.len() < 2 {
            continue;
        }
        let mut exports_by_target = BTreeMap::new();
        let mut members_by_target = BTreeMap::new();
        let mut selector_kind = None;
        let mut has_duplicate_target = false;
        for idx in &member_indices {
            let member = &request.members[*idx];
            selector_kind.get_or_insert_with(|| source_match_projection_kind(&member.claim_origin));
            let target_binding = member
                .source_match
                .as_ref()
                .and_then(|selector| selector.target_binding.clone())
                .expect("grouped selectors always have target_binding");
            if exports_by_target
                .insert(target_binding.clone(), member.export_name.clone())
                .is_some()
                || members_by_target.insert(target_binding, *idx).is_some()
            {
                has_duplicate_target = true;
            }
        }
        if has_duplicate_target {
            continue;
        }
        let parsed_selector = request.members[member_indices[0]]
            .source_match_parsed
            .as_ref()
            .expect("grouped source_match member should carry parsed selector")
            .with_target_binding(None);
        let assignment = SourceMatchGroupAssignment {
            selector,
            parsed_selector,
            exports_by_target,
            members_by_target,
            selector_kind: selector_kind.unwrap_or("members.source_match"),
        };
        for idx in member_indices {
            assignments.insert(idx, assignment.clone());
        }
    }
    assignments
}

fn declare_source_match_group_targets(
    builder: &mut MemberSelectorProgramBuilder,
    request_index: usize,
    request: &LogicalRequest,
    group: &SourceMatchGroupAssignment,
    deferred_targets: &mut BTreeMap<SelectorTargetId, (usize, usize)>,
) -> Result<Vec<SelectorTargetId>> {
    let mut targets = Vec::new();
    for (target_binding, member_index) in &group.members_by_target {
        let member = &request.members[*member_index];
        let selector = member_selector_ref_for_global_solver(member)
            .expect("binding group member should still have a selector");
        let target = builder.declare_binding_group_member_target_in_module_ref(
            &request.id,
            &member.export_name,
            target_binding,
            selector,
        )?;
        if member.resolves_after_chunk_analysis() {
            deferred_targets.insert(target, (request_index, *member_index));
        }
        targets.push(target);
    }
    Ok(targets)
}

/// One outcome per binding of a `source_matches[]` group that failed as a whole.
fn group_member_outcomes(
    chunk_id: &str,
    request: &LogicalRequest,
    group: &SourceMatchGroupAssignment,
    outcome: &Outcome,
) -> Vec<SelectorOutcome> {
    group
        .members_by_target
        .values()
        .map(|member_index| {
            member_outcome(
                chunk_id,
                request,
                &request.members[*member_index],
                outcome.clone(),
            )
        })
        .collect()
}

fn owner_by_body_index_and_binding(
    structural: &analysis::facts::StructuralChunkAnalysis<'_>,
    module: &swc_ecma_ast::Module,
) -> BTreeMap<(usize, String), OwnerId> {
    let mut owners = BTreeMap::new();
    for statement in &structural.per_statement {
        let Some(body_idx) = body_index_for_statement_ordinal(&module.body, statement.ordinal.0)
        else {
            continue;
        };
        for binding in &statement.declared {
            owners.insert(
                (body_idx, binding.0.as_str().to_string()),
                OwnerId(statement.ordinal.0),
            );
        }
    }
    owners
}

fn anonymous_owner_by_body_index(
    structural: &analysis::facts::StructuralChunkAnalysis<'_>,
    module: &swc_ecma_ast::Module,
) -> BTreeMap<usize, OwnerId> {
    let mut owners = BTreeMap::new();
    for statement in &structural.per_statement {
        let Some(body_idx) = body_index_for_statement_ordinal(&module.body, statement.ordinal.0)
        else {
            continue;
        };
        owners.insert(body_idx, OwnerId(statement.ordinal.0));
    }
    owners
}

fn projected_source_match_candidate(
    owner_by_binding: &BTreeMap<(usize, String), OwnerId>,
    candidate: &source_match::MemberBindingMatch,
) -> Result<(OwnerId, String)> {
    let binding = candidate.binding.binding_name.clone();
    let owner = owner_by_binding
        .get(&(candidate.body_idx, binding.clone()))
        .copied()
        .with_context(|| {
            format!(
                "source_match candidate at body index {} binding `{}` \
                 does not map to an owner-graph node",
                candidate.body_idx, binding
            )
        })?;
    Ok((owner, binding))
}

fn projected_source_match_candidate_rows(
    owner_by_binding: &BTreeMap<(usize, String), OwnerId>,
    candidates: Vec<source_match::MemberBindingMatch>,
) -> Result<Vec<(OwnerId, String)>> {
    candidates
        .iter()
        .map(|candidate| projected_source_match_candidate(owner_by_binding, candidate))
        .collect()
}

fn projected_anonymous_statement_candidate_rows(
    owner_by_body_index: &BTreeMap<usize, OwnerId>,
    candidate_groups: Vec<Vec<usize>>,
) -> Result<Vec<OwnerId>> {
    candidate_groups
        .into_iter()
        .map(|group| {
            let [body_idx] = group.as_slice() else {
                anyhow::bail!(
                    "anonymous source_match candidate group has {} statements; projected lowering \
                     currently supports one statement per anonymous claim",
                    group.len()
                );
            };
            owner_by_body_index.get(body_idx).copied().with_context(|| {
                format!(
                    "anonymous source_match candidate at body index {body_idx} does not map \
                         to an owner-graph node",
                )
            })
        })
        .collect()
}

fn projected_source_match_group_candidate_rows(
    owner_by_binding: &BTreeMap<(usize, String), OwnerId>,
    candidates: Vec<source_match::MemberBindingGroupMatch>,
) -> Result<Vec<BTreeMap<String, (OwnerId, String)>>> {
    candidates
        .into_iter()
        .map(|candidate| {
            candidate
                .bindings
                .iter()
                .map(|(target_binding, binding_match)| {
                    projected_source_match_candidate(owner_by_binding, binding_match)
                        .map(|row| (target_binding.clone(), row))
                })
                .collect()
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn source_match_projection_event(
    logical_module: &str,
    selector_kind: &str,
    export_name: Option<&str>,
    exports_by_target: BTreeMap<String, String>,
    selector: &spec::AnonymousStatementSelector,
    outcome: SelectorSourceMatchProjectionOutcome,
    reason_category: &str,
    reason: String,
    candidate_count: Option<usize>,
    projected_row_count: Option<usize>,
) -> SelectorSourceMatchProjectionEvent {
    SelectorSourceMatchProjectionEvent {
        selector_kind: selector_kind.to_string(),
        logical_module: logical_module.to_string(),
        export_name: export_name.map(ToString::to_string),
        target_binding: selector.target_binding.clone(),
        exports_by_target,
        outcome,
        reason_category: reason_category.to_string(),
        reason,
        candidate_count,
        projected_row_count,
        selector_preview: source_match::source_match_preview(&selector.match_source),
        selector_hash: source_match::selector_key(selector),
        selector_body_hash: source_match::selector_body_key(selector),
    }
}

fn source_match_projection_error_reason(category: &str, error: &anyhow::Error) -> String {
    let message = error.to_string();
    let first_line = message
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or(&message);
    format!("{category}: {first_line}")
}

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
    pub(super) declaration_by_name: &'a HashMap<Id, usize>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
    pub(super) target_dir: &'a str,
    pub(super) chunk_id: &'a str,
    pub(super) target_file: &'a str,
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
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

#[allow(dead_code)]
impl ChunkPlanBuilder {
    pub(super) fn new(fail_fast: bool) -> Self {
        Self {
            binding_assignment: HashMap::new(),
            anonymous_ordinal_assignment: BTreeMap::new(),
            module_plans: Vec::new(),
            bindings_catalogue: HashMap::new(),
            residual_plan_index: None,
            unmatched_spec_claims: Vec::new(),
            catalogue_index_by_name: HashMap::new(),
            deferred_binding_claims_by_name: HashMap::new(),
            duplicate_deferred_binding_names: BTreeSet::new(),
            outcomes: OutcomeSink::new(fail_fast),
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
        let module_id = ModuleId(LogicalModuleIndex(index));
        let mut duplicate_bindings = BTreeSet::<String>::new();
        for member in &request.members {
            if !member.binding.is_empty() {
                if let Some(existing_kind) =
                    self.catalogue_index_by_name.get(member.binding.as_str())
                {
                    let duplicate = self.duplicate_claim_for(
                        existing_kind,
                        &member.binding,
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
                if member.binding_selector.is_some() && !member.is_import_specifier {
                    let binding_id = top_level_id(&member.binding, ctx.chunk_top_level_mark);
                    if !ctx.declaration_by_name.contains_key(&binding_id) {
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
            explicit: true,
            bindings,
            anonymous_statement_ordinals: Vec::new(),
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
                "anonymous_statements[].match in module {} also matches the top-level \
                 statement at body index {} already claimed by module {}; each anonymous \
                 statement may belong to at most one logical module.",
                request_id,
                claim.ordinal,
                existing_id,
            );
        }
        self.anonymous_ordinal_assignment
            .insert(claim.ordinal, module_index);
        let plan = self.module_plans.get_mut(module_index).with_context(|| {
            format!(
                "logical_module {request_id}: anonymous statement resolved before its module plan existed"
            )
        })?;
        plan.anonymous_statement_ordinals.push(claim.ordinal);
        plan.anonymous_statement_ordinals.sort_unstable();
        plan.anonymous_statement_ordinals.dedup();
        if let Some(comment) = &claim.comment {
            plan.anonymous_statement_comments
                .insert(claim.ordinal, comment.clone());
        }
        Ok(())
    }

    fn claim_anonymous_statement_from_solver(
        &mut self,
        module: &swc_ecma_ast::Module,
        module_index: usize,
        request_id: &str,
        statement: &AnonymousStatementRequest,
        claim: &ResolvedClaim,
    ) -> Result<()> {
        let body_index = body_index_for_statement_ordinal(&module.body, claim.statement_ordinal.0)
            .with_context(|| {
                format!(
                    "logical_module {request_id}: global selector solver resolved anonymous \
                     statement to post-split ordinal {} which has no source body item",
                    claim.statement_ordinal.0,
                )
            })?;
        self.claim_anonymous_statement(
            module_index,
            request_id,
            &ResolvedAnonymousStatement {
                ordinal: body_index,
                comment: statement.comment.clone(),
            },
        )
    }

    fn record(&mut self, outcome: SelectorOutcome) -> Result<()> {
        self.outcomes.record(outcome)
    }

    fn record_all(&mut self, outcomes: Vec<SelectorOutcome>) -> Result<()> {
        outcomes
            .into_iter()
            .try_for_each(|outcome| self.record(outcome))
    }

    fn has_recorded_anonymous_statement_failure(&self, request: &LogicalRequest) -> bool {
        self.outcomes.outcomes().iter().any(|outcome| {
            outcome.severity() == Severity::Error
                && matches!(
                    &outcome.placement,
                    Some(Placement {
                        logical_module,
                        entity: Some(Entity::AnonymousStatement(_)),
                        ..
                    }) if *logical_module == request.target_path
                )
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn resolve_and_claim_global_selectors(
        &mut self,
        explicit_requests: &[LogicalRequest],
        structural: &analysis::facts::StructuralChunkAnalysis<'_>,
        module: &swc_ecma_ast::Module,
        import_sources: &HashMap<String, String>,
        runtime_import_facts: &RuntimeImportFacts,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        target_file: &str,
        chunk_id_interned: ChunkId,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        let has_deferred_members = explicit_requests
            .iter()
            .flat_map(|request| &request.members)
            .any(MemberRequest::resolves_after_chunk_analysis);
        let has_anonymous_statements = explicit_requests
            .iter()
            .any(|request| !request.anonymous_statements.is_empty());
        if !has_deferred_members && !has_anonymous_statements {
            return Ok(());
        }

        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            chunk_id_interned,
            chunk_id,
        ));
        let mut deferred_targets = BTreeMap::<SelectorTargetId, (usize, usize)>::new();
        let mut anonymous_statement_targets = Vec::<AnonymousStatementTargetInfo>::new();
        let mut pending_constraints = Vec::<(usize, usize)>::new();
        let mut pending_source_match_members = Vec::<(usize, usize)>::new();
        let mut pending_source_match_groups = Vec::<(usize, SourceMatchGroupAssignment)>::new();
        let mut pending_source_match_group_keys =
            BTreeSet::<(String, SourceMatchGroupCacheKey)>::new();
        let mut projected_entities = Vec::<ProjectedEntity>::new();
        if has_deferred_members {
            for (index, request) in explicit_requests.iter().enumerate() {
                let group_assignments = source_match_group_assignments(request);
                for (member_index, member) in request.members.iter().enumerate() {
                    if member.resolves_after_chunk_analysis()
                        && !member.binding.is_empty()
                        && self
                            .duplicate_deferred_binding_names
                            .contains(&member.binding)
                    {
                        continue;
                    }
                    if member.binding_selector.is_some() && !member.is_import_specifier {
                        let binding_id = top_level_id(&member.binding, chunk_top_level_mark);
                        if !declaration_by_name.contains_key(&binding_id) {
                            continue;
                        }
                    }
                    let group_assignment = group_assignments.get(&member_index).cloned();
                    if let Some(group) = &group_assignment {
                        let key = (
                            request.id.clone(),
                            SourceMatchGroupCacheKey::new(
                                group.selector.clone(),
                                &group.exports_by_target,
                            ),
                        );
                        if pending_source_match_group_keys.insert(key) {
                            pending_source_match_groups.push((index, group.clone()));
                        }
                        continue;
                    }
                    if member.source_match_parsed.is_some() {
                        pending_source_match_members.push((index, member_index));
                        continue;
                    }
                    let Some(selector) = member_selector_ref_for_global_solver(member) else {
                        continue;
                    };
                    let target = builder.declare_member_target_in_module_ref(
                        &request.id,
                        &member.export_name,
                        selector,
                    )?;
                    pending_constraints.push((index, member_index));
                    if member.resolves_after_chunk_analysis() {
                        deferred_targets.insert(target, (index, member_index));
                    }
                }
            }
        }
        let has_pending_source_match =
            !pending_source_match_groups.is_empty() || !pending_source_match_members.is_empty();
        // Shape (`source_match`) selectors resolve in two stages: `ChunkResolver`
        // enumerates the top-level statements each JS-template-with-holes matches,
        // and those candidates are projected into the selector IR as a small
        // `ProjectedAllowedTuples` domain per target. The global solve then picks
        // one target per selector under `all_different`. A selector the matcher
        // places nowhere never reaches the solver: an empty candidate table would
        // make the whole chunk's program unsatisfiable. It is reported unmatched
        // here instead.
        let source_match_projection =
            (has_pending_source_match || has_anonymous_statements).then(|| {
                (
                    source_match::chunk_resolver::ChunkResolver::new(module),
                    owner_by_body_index_and_binding(structural, module),
                    anonymous_owner_by_body_index(structural, module),
                )
            });
        for (request_index, request) in explicit_requests.iter().enumerate() {
            for (statement_index, statement) in request.anonymous_statements.iter().enumerate() {
                let mut projected = false;
                let mut candidate_count = None;
                let mut projected_row_count = None;
                let mut reason_category = "projection_not_attempted";
                let mut reason = "anonymous source_match projection was not attempted".to_string();
                if let Some((resolver, _, owner_by_body_index)) = &source_match_projection {
                    match resolver
                        .anonymous_group_candidates_parsed(&request.id, &statement.parsed_selector)
                    {
                        Ok(candidates) => {
                            candidate_count = Some(candidates.len());
                            match projected_anonymous_statement_candidate_rows(
                                owner_by_body_index,
                                candidates,
                            ) {
                                Ok(candidate_rows)
                                    if candidate_rows.len() > MAX_CANDIDATES_PER_SELECTOR =>
                                {
                                    projected_row_count = Some(candidate_rows.len());
                                    reason_category = "too_broad";
                                    reason = too_broad_reason(candidate_rows.len());
                                }
                                Ok(candidate_rows) if !candidate_rows.is_empty() => {
                                    projected_row_count = Some(candidate_rows.len());
                                    builder.record_source_match_projection_event(
                                        source_match_projection_event(
                                            &request.id,
                                            "anonymous_statements.source_match",
                                            None,
                                            BTreeMap::new(),
                                            &statement.selector,
                                            SelectorSourceMatchProjectionOutcome::Projected,
                                            "projected_candidates",
                                            "projected anonymous statement candidates into owner rows"
                                                .to_string(),
                                            candidate_count,
                                            projected_row_count,
                                        ),
                                    );
                                    let target = builder
                                        .declare_projected_anonymous_statement_target_in_module(
                                            &request.id,
                                            statement_index,
                                            candidate_rows,
                                        );
                                    anonymous_statement_targets.push(
                                        AnonymousStatementTargetInfo {
                                            target,
                                            request_index,
                                            statement_index,
                                            statement: statement.clone(),
                                        },
                                    );
                                    projected = true;
                                }
                                Ok(_) => {
                                    projected_row_count = Some(0);
                                    reason_category = "shape_matcher_no_candidates";
                                    reason = "shape matcher returned no anonymous candidates"
                                        .to_string();
                                }
                                Err(error) => {
                                    reason_category = "projection_owner_mapping_error";
                                    reason = source_match_projection_error_reason(
                                        reason_category,
                                        &error,
                                    );
                                }
                            }
                        }
                        Err(error) => {
                            reason_category = "shape_matcher_error";
                            reason = source_match_projection_error_reason(reason_category, &error);
                        }
                    }
                }
                if projected {
                    continue;
                }
                builder.record_source_match_projection_event(source_match_projection_event(
                    &request.id,
                    "anonymous_statements.source_match",
                    None,
                    BTreeMap::new(),
                    &statement.selector,
                    SelectorSourceMatchProjectionOutcome::NotProjected,
                    reason_category,
                    reason.clone(),
                    candidate_count,
                    projected_row_count,
                ));
                self.record(anonymous_statement_outcome(
                    chunk_id,
                    request,
                    statement_index,
                    statement,
                    unprojected_outcome(reason_category, &reason, projected_row_count),
                ))?;
            }
        }
        for (request_index, group) in pending_source_match_groups {
            let request = &explicit_requests[request_index];
            let logical_module = &request.id;
            let Some((resolver, owner_by_binding, _)) = &source_match_projection else {
                continue;
            };
            let mut candidate_count = None;
            let mut projected_row_count = None;
            let reason_category;
            let reason;
            match resolver.member_group_candidates_parsed(
                logical_module,
                &group.parsed_selector,
                &group.exports_by_target,
            ) {
                Ok(candidates) => {
                    let candidate_len = candidates.len();
                    candidate_count = Some(candidate_len);
                    match projected_source_match_group_candidate_rows(owner_by_binding, candidates)
                    {
                        Ok(rows) if rows.len() > MAX_CANDIDATES_PER_SELECTOR => {
                            projected_row_count = Some(rows.len());
                            reason_category = "too_broad";
                            reason = too_broad_reason(rows.len());
                        }
                        Ok(rows) => {
                            projected_row_count = Some(rows.len());
                            if !rows.is_empty() {
                                let targets = declare_source_match_group_targets(
                                    &mut builder,
                                    request_index,
                                    request,
                                    &group,
                                    &mut deferred_targets,
                                )?;
                                builder.record_source_match_projection_event(
                                    source_match_projection_event(
                                        logical_module,
                                        group.selector_kind,
                                        None,
                                        group.exports_by_target.clone(),
                                        &group.selector,
                                        SelectorSourceMatchProjectionOutcome::Projected,
                                        "projected_candidates",
                                        format!(
                                            "projected {} shape-matcher candidate group(s) to {} \
                                             owner/binding row(s)",
                                            candidate_len,
                                            rows.len()
                                        ),
                                        candidate_count,
                                        projected_row_count,
                                    ),
                                );
                                projected_entities.push(ProjectedEntity {
                                    targets,
                                    rows: rows
                                        .iter()
                                        .map(|row| {
                                            row.values()
                                                .map(|(owner, binding)| {
                                                    (*owner, Some(binding.clone()))
                                                })
                                                .collect()
                                        })
                                        .collect(),
                                    subject: ProjectedEntitySubject::Group {
                                        request_index,
                                        group: group.clone(),
                                    },
                                });
                                builder.lower_projected_source_match_group_candidates(
                                    logical_module,
                                    &group.exports_by_target,
                                    rows,
                                );
                                continue;
                            }
                            reason_category = "shape_matcher_no_candidates";
                            reason = "shape matcher returned no candidate groups".to_string();
                        }
                        Err(error) => {
                            reason_category = "projection_owner_mapping_error";
                            reason = source_match_projection_error_reason(reason_category, &error);
                        }
                    }
                }
                Err(error) => {
                    reason_category = "shape_matcher_error";
                    reason = source_match_projection_error_reason(reason_category, &error);
                }
            }
            builder.record_source_match_projection_event(source_match_projection_event(
                logical_module,
                group.selector_kind,
                None,
                group.exports_by_target.clone(),
                &group.selector,
                SelectorSourceMatchProjectionOutcome::NotProjected,
                reason_category,
                reason.clone(),
                candidate_count,
                projected_row_count,
            ));
            self.record_all(group_member_outcomes(
                chunk_id,
                request,
                &group,
                &unprojected_outcome(reason_category, &reason, projected_row_count),
            ))?;
        }
        for (request_index, member_index) in pending_source_match_members {
            let request = &explicit_requests[request_index];
            let member = &request.members[member_index];
            let (Some(parsed_selector), Some((resolver, owner_by_binding, _))) =
                (&member.source_match_parsed, &source_match_projection)
            else {
                unreachable!("pending source_match members have a parsed selector and a resolver");
            };
            let (reason_category, reason, candidate_count, projected_row_count) = match resolver
                .member_candidates_parsed(&request.id, parsed_selector)
            {
                Ok(candidates) => {
                    let candidate_len = candidates.len();
                    match projected_source_match_candidate_rows(owner_by_binding, candidates) {
                        Ok(rows) if rows.len() > MAX_CANDIDATES_PER_SELECTOR => (
                            "too_broad",
                            too_broad_reason(rows.len()),
                            Some(candidate_len),
                            rows.len(),
                        ),
                        Ok(rows) if !rows.is_empty() => {
                            builder.record_source_match_projection_event(
                                source_match_projection_event(
                                    &request.id,
                                    source_match_projection_kind(&member.claim_origin),
                                    Some(&member.export_name),
                                    BTreeMap::new(),
                                    parsed_selector.selector(),
                                    SelectorSourceMatchProjectionOutcome::Projected,
                                    "projected_candidates",
                                    format!(
                                        "projected {} shape-matcher candidate(s) to {} \
                                             owner/binding row(s)",
                                        candidate_len,
                                        rows.len()
                                    ),
                                    Some(candidate_len),
                                    Some(rows.len()),
                                ),
                            );
                            let target = builder.declare_member_target_in_module_ref(
                                &request.id,
                                &member.export_name,
                                member_selector_ref_for_global_solver(member)
                                    .expect("a source_match member has a solver selector"),
                            )?;
                            projected_entities.push(ProjectedEntity {
                                targets: vec![target],
                                rows: rows
                                    .iter()
                                    .map(|(owner, binding)| vec![(*owner, Some(binding.clone()))])
                                    .collect(),
                                subject: ProjectedEntitySubject::Member {
                                    request_index,
                                    member_index,
                                },
                            });
                            builder.lower_projected_source_match_candidates(
                                &request.id,
                                &member.export_name,
                                rows,
                            );
                            deferred_targets.insert(target, (request_index, member_index));
                            continue;
                        }
                        Ok(_) => (
                            "shape_matcher_no_candidates",
                            "shape matcher returned no candidates".to_string(),
                            Some(candidate_len),
                            0,
                        ),
                        Err(error) => (
                            "projection_owner_mapping_error",
                            source_match_projection_error_reason(
                                "projection_owner_mapping_error",
                                &error,
                            ),
                            Some(candidate_len),
                            0,
                        ),
                    }
                }
                Err(error) => (
                    "shape_matcher_error",
                    source_match_projection_error_reason("shape_matcher_error", &error),
                    None,
                    0,
                ),
            };
            builder.record_source_match_projection_event(source_match_projection_event(
                &request.id,
                source_match_projection_kind(&member.claim_origin),
                Some(&member.export_name),
                BTreeMap::new(),
                parsed_selector.selector(),
                SelectorSourceMatchProjectionOutcome::NotProjected,
                reason_category,
                reason.clone(),
                candidate_count,
                Some(projected_row_count),
            ));
            self.record(member_outcome(
                chunk_id,
                request,
                member,
                unprojected_outcome(reason_category, &reason, Some(projected_row_count)),
            ))?;
        }
        for (request_index, member_index) in pending_constraints {
            let request = &explicit_requests[request_index];
            let member = &request.members[member_index];
            let selector = member_selector_ref_for_global_solver(member)
                .expect("pending selector constraint should still be lowerable");
            builder.lower_member_constraints_in_module_ref(
                &request.id,
                &member.export_name,
                selector,
            )?;
        }
        let program = builder.into_program()?;
        if program.targets.is_empty() {
            return Ok(());
        }
        let facts = selector_fact_store_for_chunk(
            &program,
            chunk_id_interned,
            structural,
            module,
            import_sources,
        );
        let fact_coverage = selector_fact_coverage(&facts);
        let mut presolve_no_match = BTreeSet::new();
        for (target, (index, member_index)) in &deferred_targets {
            let request = &explicit_requests[*index];
            if !request.anonymous_statements.is_empty() {
                continue;
            }
            let member = &request.members[*member_index];
            if !binding_selector_has_fact_candidate(&fact_coverage, member) {
                self.record(member_outcome(chunk_id, request, member, Outcome::NoMatch))?;
                presolve_no_match.insert(*target);
            }
        }
        let result = solve_global_selector_program(&program, &facts)?;

        for info in anonymous_statement_targets {
            let request = &explicit_requests[info.request_index];
            match result.outcome_for(info.target) {
                Some(ClaimOutcome::Unique { claim }) => {
                    self.claim_anonymous_statement_from_solver(
                        module,
                        info.request_index,
                        &request.id,
                        &info.statement,
                        claim,
                    )?;
                }
                Some(ClaimOutcome::NoMatch) => {
                    self.record(anonymous_statement_outcome(
                        chunk_id,
                        request,
                        info.statement_index,
                        &info.statement,
                        Outcome::NoMatch,
                    ))?;
                }
                Some(ClaimOutcome::Conflict { with }) => {
                    self.record(anonymous_statement_outcome(
                        chunk_id,
                        request,
                        info.statement_index,
                        &info.statement,
                        Outcome::Conflict {
                            with: target_entity_refs(&program, with),
                        },
                    ))?;
                }
                Some(ClaimOutcome::Ambiguous {
                    candidates,
                    candidates_truncated,
                }) => {
                    self.record(anonymous_statement_outcome(
                        chunk_id,
                        request,
                        info.statement_index,
                        &info.statement,
                        ambiguous_outcome(module, candidates, *candidates_truncated)?,
                    ))?;
                }
                Some(ClaimOutcome::Duplicate {
                    owner,
                    conflicting_targets,
                }) => {
                    bail!(
                        "logical_module {}: global selector solver assigned anonymous statement \
                         to duplicate owner {:?} shared by targets {:?}",
                        request.id,
                        owner,
                        conflicting_targets,
                    );
                }
                Some(ClaimOutcome::Undecided { reason }) => {
                    self.record(anonymous_statement_outcome(
                        chunk_id,
                        request,
                        info.statement_index,
                        &info.statement,
                        Outcome::Undecided {
                            reason: reason.clone(),
                        },
                    ))?;
                }
                None => {
                    bail!(
                        "logical_module {}: global selector solver returned no outcome for \
                         anonymous statement selector",
                        request.id,
                    );
                }
            }
        }

        for (target, (index, member_index)) in deferred_targets {
            let request = &explicit_requests[index];
            let member = &request.members[member_index];
            match result.outcome_for(target) {
                Some(ClaimOutcome::Unique { claim }) => {
                    let binding = claim.binding.as_deref().with_context(|| {
                        format!(
                            "logical_module {}: global selector solver resolved member `{}` to \
                             owner {:?} without a single declared binding",
                            request.id, member.export_name, claim.owner,
                        )
                    })?;
                    let selects_import = member.source_match.is_some()
                        && solver_claim_is_import_specifier(&facts, claim);
                    if selects_import {
                        self.claim_imported_binding_after_chunk_analysis(
                            request,
                            member,
                            binding,
                            index,
                            chunk_top_level_mark,
                            chunk_id,
                            target_file,
                            runtime_import_facts,
                            imported_binding_resolver,
                            imported_from_by_src,
                        )?;
                        continue;
                    }
                    self.claim_binding_after_chunk_analysis(
                        request,
                        member,
                        binding,
                        index,
                        chunk_top_level_mark,
                        chunk_id,
                        declaration_by_name,
                    )?;
                }
                Some(ClaimOutcome::NoMatch) => {
                    if presolve_no_match.contains(&target)
                        || (member.binding_selector.is_some()
                            && member.source_match.is_none()
                            && member.relational.is_none()
                            && self.has_recorded_anonymous_statement_failure(request))
                    {
                        continue;
                    }
                    self.record(member_outcome(chunk_id, request, member, Outcome::NoMatch))?;
                }
                Some(ClaimOutcome::Conflict { with }) => {
                    self.record(member_outcome(
                        chunk_id,
                        request,
                        member,
                        Outcome::Conflict {
                            with: target_entity_refs(&program, with),
                        },
                    ))?;
                }
                Some(ClaimOutcome::Ambiguous {
                    candidates,
                    candidates_truncated,
                }) => {
                    self.record(member_outcome(
                        chunk_id,
                        request,
                        member,
                        ambiguous_outcome(module, candidates, *candidates_truncated)?,
                    ))?;
                }
                Some(ClaimOutcome::Duplicate {
                    owner,
                    conflicting_targets,
                }) => {
                    bail!(
                        "logical_module {}: global selector solver assigned selector member `{}` \
                         to duplicate owner {:?} shared by targets {:?}",
                        request.id,
                        member.export_name,
                        owner,
                        conflicting_targets,
                    );
                }
                Some(ClaimOutcome::Undecided { reason }) => {
                    self.record(member_outcome(
                        chunk_id,
                        request,
                        member,
                        Outcome::Undecided {
                            reason: reason.clone(),
                        },
                    ))?;
                }
                None => {
                    bail!(
                        "logical_module {}: global selector solver returned no outcome for \
                         selector member `{}` ({})",
                        request.id,
                        member.export_name,
                        member.claim_origin,
                    );
                }
            }
        }
        self.record_elimination_warnings(
            explicit_requests,
            module,
            chunk_id,
            &program,
            &result,
            &projected_entities,
        )
    }

    fn record_elimination_warnings(
        &mut self,
        explicit_requests: &[LogicalRequest],
        module: &swc_ecma_ast::Module,
        chunk_id: &str,
        program: &SelectorProgram,
        result: &selector_ir::SolverResult,
        projected_entities: &[ProjectedEntity],
    ) -> Result<()> {
        let mut exclusive = program
            .all_different
            .iter()
            .flatten()
            .copied()
            .collect::<BTreeSet<_>>();
        // `all_different` names one representative per `source_matches[]`
        // group; the group's other bindings are claimed with it.
        for entity in projected_entities {
            if entity
                .targets
                .iter()
                .any(|target| exclusive.contains(target))
            {
                exclusive.extend(entity.targets.iter().copied());
            }
        }
        for entity in projected_entities {
            let Some(claimers) = elimination_claimers(entity, result, &exclusive) else {
                continue;
            };
            let claimers = target_entity_refs(program, &claimers);
            let (request, member_indices) = match &entity.subject {
                ProjectedEntitySubject::Member {
                    request_index,
                    member_index,
                } => (&explicit_requests[*request_index], vec![*member_index]),
                ProjectedEntitySubject::Group {
                    request_index,
                    group,
                } => (
                    &explicit_requests[*request_index],
                    group.members_by_target.values().copied().collect(),
                ),
            };
            // A group's targets are declared in `members_by_target` order.
            for (target, member_index) in entity.targets.iter().zip(member_indices) {
                let Some(ClaimOutcome::Unique { claim }) = result.outcome_for(*target) else {
                    unreachable!("an entity resolved by elimination has a unique claim per target");
                };
                let Candidate { owner, binding } = claim_candidate(module, claim)?;
                self.record(member_outcome(
                    chunk_id,
                    request,
                    &request.members[member_index],
                    Outcome::Resolved {
                        owner,
                        binding,
                        resolved_by: ResolvedBy::Elimination {
                            claimers: claimers.clone(),
                        },
                    },
                ))?;
            }
        }
        Ok(())
    }

    /// The outcome of `member` resolving to `binding`, which `existing_kind`
    /// already claims.
    fn duplicate_claim_for(
        &self,
        existing_kind: &BindingKind,
        binding: &str,
        chunk_id: &str,
        request: &LogicalRequest,
        member: &MemberRequest,
    ) -> SelectorOutcome {
        let (plan_index, export_name) = match existing_kind {
            BindingKind::Owned {
                module: ModuleId(LogicalModuleIndex(owner_index)),
            } => (
                *owner_index,
                self.module_plans[*owner_index]
                    .bindings
                    .get(binding)
                    .cloned(),
            ),
            BindingKind::Imported {
                re_exporter: ModuleId(LogicalModuleIndex(re_index)),
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
                claimed_by: EntityRef {
                    logical_module: logical_module_path(&self.module_plans[plan_index].id),
                    entity: export_name.map(Entity::Export),
                },
            },
        )
    }

    /// Claim a binding the global selector solver resolved to: record an
    /// unmatched-claim if the binding has no top-level declaration; error / record a
    /// duplicate if already claimed; otherwise move it out of the residual sweep and
    /// into module `index`, registering its export name, claim origin, and comment.
    /// The binding was parked at the residual plan when the pre-analysis sweep ran,
    /// before the solver knew its identity.
    #[allow(clippy::too_many_arguments)]
    fn claim_imported_binding_after_chunk_analysis(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        binding: &str,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        target_file: &str,
        runtime_import_facts: &RuntimeImportFacts,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
    ) -> Result<()> {
        if let Some(existing_kind) = self.catalogue_index_by_name.get(binding) {
            let duplicate =
                self.duplicate_claim_for(existing_kind, binding, chunk_id, request, member);
            return self.record(duplicate);
        }
        let (imported_name, imported_from) = resolve_imported_binding(
            imported_binding_resolver,
            runtime_import_facts,
            chunk_id,
            target_file,
            binding,
            imported_from_by_src,
        )?;
        let kind = BindingKind::Imported {
            imported_name: imported_name.into(),
            imported_from,
            re_exporter: ModuleId(LogicalModuleIndex(index)),
            public_name: member.export_name.as_str().into(),
        };
        self.catalogue_index_by_name
            .insert(binding.to_string(), kind.clone());
        self.bindings_catalogue
            .insert(top_level_id(binding, chunk_top_level_mark), kind);
        Ok(())
    }

    fn same_module_duplicate_source_binding_report(
        &self,
        existing_kind: &BindingKind,
        binding: &str,
        index: usize,
        request: &LogicalRequest,
        member: &MemberRequest,
    ) -> Option<String> {
        member.source_match.as_ref()?;
        let BindingKind::Owned {
            module: ModuleId(LogicalModuleIndex(owner_index)),
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

    #[allow(clippy::too_many_arguments)]
    fn claim_binding_after_chunk_analysis(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        binding: &str,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
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
            return Ok(());
        }
        let module_id = ModuleId(LogicalModuleIndex(index));
        if let Some(existing_kind) = self.catalogue_index_by_name.get(binding) {
            if let Some(report) = self.same_module_duplicate_source_binding_report(
                existing_kind,
                binding,
                index,
                request,
                member,
            ) {
                bail!("{report}");
            }
            let duplicate =
                self.duplicate_claim_for(existing_kind, binding, chunk_id, request, member);
            return self.record(duplicate);
        }
        // The residual sweep ran before chunk analysis (before this pass), so the target
        // binding — unclaimed at sweep time — was parked at the residual plan. Move
        // it out: overwrite its assignment and prune the residual plan's export
        // list, so the residual module doesn't keep `export { <binding> }` for a
        // declaration that now lives in the explicit module.
        if let Some(prev) = self.binding_assignment.get(&binding_id).copied()
            && Some(prev) == self.residual_plan_index
            && let Some(residual_idx) = self.residual_plan_index
        {
            self.module_plans[residual_idx].bindings.remove(binding);
        }
        self.binding_assignment.insert(binding_id.clone(), index);
        let kind = BindingKind::Owned { module: module_id };
        self.catalogue_index_by_name
            .insert(binding.to_string(), kind.clone());
        self.bindings_catalogue.insert(binding_id, kind);
        let plan = &mut self.module_plans[index];
        plan.bindings
            .insert(binding.to_string(), member.export_name.clone());
        plan.binding_claim_origins
            .insert(binding.to_string(), member.claim_origin.clone());
        if let Some(comment) = &member.comment {
            plan.binding_comments
                .insert(binding.to_string(), comment.clone());
        }
        Ok(())
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
                    re_exporter: ModuleId(LogicalModuleIndex(index)),
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
                    Some(other_index) if Some(other_index) == self.residual_plan_index => {
                        self.binding_assignment
                            .insert(sibling_id.clone(), owner_index);
                        self.bindings_catalogue
                            .insert(sibling_id, BindingKind::Owned { module: owner_id });
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
            let Some(&plan_index) = self.anonymous_ordinal_assignment.get(&decl.ordinal) else {
                continue;
            };
            let module = ModuleId(LogicalModuleIndex(plan_index));
            for (name, id) in &decl.bindings {
                if let Some(existing) = self.binding_assignment.get(id).copied() {
                    if Some(existing) != self.residual_plan_index {
                        continue;
                    }
                    if let Some(residual_index) = self.residual_plan_index {
                        self.module_plans[residual_index].bindings.remove(name);
                    }
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
                    binding_claim_origins: BTreeMap::new(),
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

    /// With no explicit modules, the catchall must preserve the whole
    /// statement sequence, including anonymous effects between declarations.
    pub(super) fn add_unclaimed_chunk_statements(
        &mut self,
        precomputed: &OwnerGraphAndUnits,
        body: &[ModuleItem],
    ) {
        let Some(index) = self.residual_plan_index else {
            return;
        };
        for node in precomputed.owner_graph.iter_nodes() {
            if node.declared.is_empty()
                && let Some(body_index) =
                    body_index_for_statement_ordinal(body, node.statement_ordinal.0)
            {
                self.anonymous_ordinal_assignment.insert(body_index, index);
                self.module_plans[index]
                    .anonymous_statement_ordinals
                    .push(body_index);
            }
        }
    }

    /// `MiniFactors` mode: every unclaimed atomic factor unit gets
    /// its own synthesized plan at `__auto/mini/NNNN`. Run after rebind folding
    /// so the residual sweep and fold decisions have already settled which
    /// units are still unclaimed. Bindings
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
                binding_claim_origins: BTreeMap::new(),
            });
        }
        Ok(())
    }

    /// Apply a batch of rebind-fold decisions produced from chunk analysis
    /// (`stage_one::compute_rebind_folds`).
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

    /// Borrow access to the current binding assignment so the rebind-fold
    /// composer can compute folds without mutating the builder.
    pub(super) fn binding_assignment(&self) -> &HashMap<Id, usize> {
        &self.binding_assignment
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

    pub(super) fn finalize(self) -> Result<ChunkPlan> {
        self.outcomes.finish()?;
        Ok(ChunkPlan {
            module_plans: self.module_plans,
            binding_assignment: self.binding_assignment,
            bindings_catalogue: self.bindings_catalogue,
            anonymous_ordinal_assignment: self.anonymous_ordinal_assignment,
            unmatched_spec_claims: self.unmatched_spec_claims,
        })
    }
}
