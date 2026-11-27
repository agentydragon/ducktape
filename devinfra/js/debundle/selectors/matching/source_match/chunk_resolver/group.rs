//! Binding-group matches: declarator-hole, single-declarator and general
//! needles.

use super::*;

/// Binding-group branch 2 — a single-statement declarator-hole needle (the
/// `*-module_*` group, e.g. tooltip's `const DECLARATORS_BEFORE = null, a = …,
/// DECLARATORS_GAP = null, b = …, DECLARATORS_AFTER = null;`): one plain
/// (un-prebound) declarator alignment per candidate source_unit yields every target's
/// declarator at once.
pub(super) fn group_matches_declarator_holes(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<Vec<MemberBindingGroupMatch>> {
    let needle_var = item_var_decl(needle)
        .ok_or_else(|| MatcherError::invariant(MatcherInvariant::ExpectedVariableDeclaration))?;
    let target_locations: BTreeMap<String, (usize, usize)> = exports_by_target
        .keys()
        .map(|target| {
            selector_var_declarator_binding_location(needle_var, request_id, selector, target)
                .map(|location| (target.clone(), location))
        })
        .collect::<MatchResult<_>>()?;
    let needle_index = needle_index(needle)?;
    let free = free_identifiers([&needle_index]);
    let mode = selector_mode(selector);
    selector_match::var_declarator_alignment_indexed(
        &needle_index,
        &needle_index,
        mode,
        &[],
        &free,
    )
    .map_err(unsupported_error)?;
    let mut matches: Vec<MemberBindingGroupMatch> = Vec::new();
    // No prebind here (the alignment runs un-prebound), so an exact-mode needle may
    // require its pinned-declarator identifier spellings; `DECLARATORS`-hole
    // declarator idents are absorbed and excluded by `needle_required_tokens`.
    for body_idx in chunk.candidate_bodies(&needle_index, mode, true) {
        let item = &chunk.module.body[body_idx];
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        let Some(alignment) = selector_match::var_declarator_alignment_prepared(
            &needle_index,
            &chunk.body_indices[body_idx],
            mode,
            &[],
            &free,
        ) else {
            continue;
        };
        let mut resolved = BTreeMap::new();
        for (target, (target_decl_idx, target_binding_idx)) in &target_locations {
            let Some(Some(candidate_decl_idx)) = alignment.site.get(*target_decl_idx) else {
                return Err(SourceMatchError::matched_hole(
                    request_id,
                    selector,
                    target,
                    ProjectionHole::Declarators,
                )
                .into());
            };
            let Some(declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
                return Err(MatcherError::invariant(MatcherInvariant::MissingDeclarator));
            };
            let Some(binding) = declared_bindings_for_var_declarator(declarator)
                .into_iter()
                .nth(*target_binding_idx)
            else {
                return Err(MatcherError::invariant(MatcherInvariant::MissingBinding));
            };
            resolved.insert(target.clone(), MatchedBinding { body_idx, binding });
        }
        matches.push(MemberBindingGroupMatch {
            bindings: resolved,
            free_bindings: alignment.free_bindings,
        });
    }
    Ok(matches)
}

/// Binding-group branch 1 — a single-statement single-declarator needle: match
/// the declarator across every source_unit's declarators (a target inside a
/// multi-declarator source_unit is found), reading each target's binding from the one
/// matched declarator.
pub(super) fn group_matches_single_declarator(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<Vec<MemberBindingGroupMatch>> {
    let declared = declared_bindings(needle);
    let target_binding_indices = exports_by_target
        .keys()
        .map(|target| {
            declared_target_index(&declared, request_id, selector, target)
                .map(|index| (target, index))
        })
        .collect::<MatchResult<Vec<_>>>()?;
    let needle_facts = needle_item_facts(needle)?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let free = free_identifiers([&needle_index]);
    let init_prefilter = selector_match::needle_var_declarator_init_kind_prefilter(&needle_facts);
    let mode = selector_mode(selector);
    selector_match::matches_indexed(&needle_index, &needle_index, mode, &free)
        .map_err(unsupported_error)?;
    let mut matches: Vec<MemberBindingGroupMatch> = Vec::new();
    for DeclaratorMatch {
        body_idx,
        declared,
        free_bindings,
    } in chunk.matching_declarators(&needle_index, mode, &free, init_prefilter)
    {
        let mut resolved = BTreeMap::new();
        for (target, target_binding_idx) in &target_binding_indices {
            let Some(binding) = declared.get(*target_binding_idx) else {
                return Err(MatcherError::invariant(MatcherInvariant::MissingBinding));
            };
            resolved.insert(
                (*target).clone(),
                MatchedBinding {
                    body_idx,
                    binding: binding.clone(),
                },
            );
        }
        matches.push(MemberBindingGroupMatch {
            bindings: resolved,
            free_bindings,
        });
    }
    Ok(matches)
}

/// The binding `target` names in the candidate statement at `body_idx` that
/// matched `needle`. A var-decl needle aligns declarator by declarator: its
/// `DECLARATORS` runs and floating `ANYTHING = <init>` declarators declare
/// nothing in the needle, but the candidate declarators they absorb do, so a
/// flat binding index would name the wrong declarator.
#[allow(clippy::too_many_arguments)]
pub(super) fn candidate_target_binding(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    needle_index: &selector_match::Index,
    free: &BTreeSet<String>,
    body_idx: usize,
    mode: selector_match::Mode,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target: &str,
    target_binding_idx: usize,
) -> Result<String> {
    let item = &chunk.module.body[body_idx];
    let (Some(needle_var), Some(candidate_var)) = (item_var_decl(needle), item_var_decl(item))
    else {
        return declared_bindings(item)
            .into_iter()
            .nth(target_binding_idx)
            .ok_or_else(|| MatcherError::invariant(MatcherInvariant::MissingBinding));
    };
    let (target_decl_idx, declarator_binding_idx) =
        selector_var_declarator_binding_location(needle_var, request_id, selector, target)?;
    let alignment = selector_match::var_declarator_alignment_prepared(
        needle_index,
        &chunk.body_indices[body_idx],
        mode,
        &[],
        free,
    )
    .ok_or_else(|| MatcherError::invariant(MatcherInvariant::DeclaratorAlignment))?;
    let Some(Some(candidate_decl_idx)) = alignment.site.get(target_decl_idx) else {
        return Err(SourceMatchError::matched_hole(
            request_id,
            selector,
            target,
            ProjectionHole::Declarators,
        )
        .into());
    };
    declared_bindings_for_var_declarator(&candidate_var.decls[*candidate_decl_idx])
        .into_iter()
        .nth(declarator_binding_idx)
        .ok_or_else(|| MatcherError::invariant(MatcherInvariant::MissingBinding))
}

/// Binding-group branch 3 — a general (multi-statement or non-var) needle: a
/// single top-level sequence alignment supplies every target's source_unit statement,
/// read by declared-binding index.
pub(super) fn group_matches_general(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<Vec<MemberBindingGroupMatch>> {
    let target_locations: BTreeMap<String, (usize, usize)> = exports_by_target
        .keys()
        .map(|target| {
            selector_binding_location(needles, request_id, selector, target)
                .map(|location| (target.clone(), location))
        })
        .collect::<MatchResult<_>>()?;
    let needle_indices = needles
        .iter()
        .map(needle_index)
        .collect::<Result<Vec<_>>>()?;
    let free = free_identifiers(&needle_indices);
    let alignments = selector_match::match_top_level_sequence_indexed(
        &needle_indices,
        &chunk.body_indices,
        selector_mode(selector),
        &free,
    )
    .map_err(unsupported_error)?;
    let mut matches = Vec::new();
    for alignment in alignments {
        let mut resolved = BTreeMap::new();
        for (target, (target_item_idx, target_binding_idx)) in &target_locations {
            let Some(Some(body_idx)) = alignment.site.get(*target_item_idx) else {
                return Err(SourceMatchError::matched_hole(
                    request_id,
                    selector,
                    target,
                    ProjectionHole::StatementList,
                )
                .into());
            };
            let binding = candidate_target_binding(
                chunk,
                &needles[*target_item_idx],
                &needle_indices[*target_item_idx],
                &free,
                *body_idx,
                selector_mode(selector),
                request_id,
                selector,
                target,
                *target_binding_idx,
            )?;
            resolved.insert(
                target.clone(),
                MatchedBinding {
                    body_idx: *body_idx,
                    binding,
                },
            );
        }
        matches.push(MemberBindingGroupMatch {
            bindings: resolved,
            free_bindings: alignment.free_bindings,
        });
    }
    Ok(matches)
}
