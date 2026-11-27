//! Single-target member matches: per-declarator, multi-statement and
//! single-statement needles.

use super::*;

/// Collect every single-declarator var-decl member match: the needle's
/// declarator against every declarator of every var-decl source_unit (the per-declarator
/// path). Returns one `MemberBindingMatch` per matched declarator, body-index
/// ascending (the cached `var_declarator_subjects` are built in body order).
pub(super) fn member_matches_var_declarator(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_facts = needle_item_facts(needle)?;
    let mode = selector_mode(selector);
    let needle_index = selector_match::Index::build(&needle_facts);
    let free = free_identifiers([&needle_index]);
    // Probe the needle once: an unsupported construct errors uniformly.
    selector_match::matches_indexed(&needle_index, &needle_index, mode, &free)
        .map_err(unsupported_error)?;
    let target_binding_idx = match &selector.target_binding {
        Some(target) => {
            declared_target_index(&declared_bindings(needle), request_id, selector, target)?
        }
        None => 0,
    };
    let init_prefilter = selector_match::needle_var_declarator_init_kind_prefilter(&needle_facts);
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    for DeclaratorMatch {
        body_idx,
        declared,
        free_bindings,
    } in chunk.matching_declarators(&needle_index, mode, &free, init_prefilter)
    {
        if selector.target_binding.is_none() && declared.len() != 1 {
            return Err(SourceMatchError::MultipleBindings {
                logical_module: request_id.to_string(),
                body_index: body_idx,
                count: declared.len(),
            }
            .into());
        }

        let Some(binding) = declared.into_iter().nth(target_binding_idx) else {
            return Err(MatcherError::invariant(MatcherInvariant::MissingBinding));
        };
        matches.push(MemberBindingMatch {
            body_idx,
            binding,
            free_bindings,
        });
    }
    Ok(matches)
}

/// Collect every declarator-hole member match (`const DECLARATORS, x = …,
/// DECLARATORS;` with a `target_binding`). Alpha mode uses the one-target group
/// matcher without a prebind, preserving the distinction between a declared
/// target and a free sibling reference that resolves to the same source binding.
/// Exact mode prebinds the target name to preserve its intentional alpha-coupling.
/// Both paths return one `MemberBindingMatch` per matched source_unit, body-index
/// ascending.
pub(super) fn member_matches_declarator_hole(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let target_binding = selector
        .target_binding
        .as_deref()
        .ok_or_else(|| MatcherError::invariant(MatcherInvariant::MissingTarget))?;
    let mode = selector_mode(selector);
    if mode == selector_match::Mode::AlphaAll {
        // A raw free sibling name may resolve to the claimed binding in the
        // subject. Prebinding the declared target would merge those two template
        // roles, so use the group's un-prebound alignment even for one target.
        let candidates = group_matches_declarator_holes(
            chunk,
            needle,
            request_id,
            selector,
            &BTreeMap::from([(target_binding.to_string(), target_binding.to_string())]),
        )?;
        return candidates
            .into_iter()
            .map(|candidate| {
                let matched = candidate.bindings.get(target_binding).ok_or_else(|| {
                    MatcherError::invariant(MatcherInvariant::MissingGroupTarget {
                        target: target_binding.to_string(),
                    })
                })?;
                Ok(MemberBindingMatch {
                    body_idx: matched.body_idx,
                    binding: matched.binding.clone(),
                    free_bindings: candidate.free_bindings,
                })
            })
            .collect();
    }

    let needle_var = item_var_decl(needle)
        .ok_or_else(|| MatcherError::invariant(MatcherInvariant::ExpectedVariableDeclaration))?;
    let (target_decl_idx, target_binding_idx) =
        selector_var_declarator_binding_location(needle_var, request_id, selector, target_binding)?;
    let needle_index = needle_index(needle)?;
    let free = free_identifiers([&needle_index]);
    // Probe the needle once: an unsupported construct errors uniformly (and makes
    // the per-subject `.expect` below sound — the only `Unsupported` source is the
    // needle construct, invariant across subjects and prebindings).
    selector_match::var_declarator_alignment_indexed(
        &needle_index,
        &needle_index,
        mode,
        &[],
        &free,
    )
    .map_err(unsupported_error)?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    // The fixed (non-hole) declarators pin invariant tokens any matching source_unit
    // must carry, so the token index narrows the source_unit scan (the giant
    // `initBundle` var-decl is the worst case). The `DECLARATORS` holes pin none.
    // This path prebinds the `target_binding` (alpha-coupling the target name to a
    // candidate's binding), so the needle's identifier spellings are **not** required
    // of the subject — `allow_exact_ident = false` keeps the prefilter sound.
    for body_idx in chunk.candidate_bodies(&needle_index, mode, false) {
        let item = &chunk.module.body[body_idx];
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        // Reuse the cached body index across the candidate-binding inner loop:
        // every alignment attempt for this source_unit shares one prebuilt subject index.
        let subject_index = &chunk.body_indices[body_idx];
        let alignment = target_binding_candidate_names(candidate_var, target_binding_idx)
            .into_iter()
            .find_map(|candidate_binding| {
                selector_match::var_declarator_alignment_prepared(
                    &needle_index,
                    subject_index,
                    mode,
                    &[(target_binding, &candidate_binding)],
                    &free,
                )
            });
        let Some(alignment) = alignment else {
            continue;
        };
        let Some(Some(candidate_decl_idx)) = alignment.site.get(target_decl_idx) else {
            return Err(SourceMatchError::binding(
                request_id,
                selector,
                target_binding,
                BindingProblem::MatchedHole {
                    hole: ProjectionHole::Declarators,
                },
            )
            .into());
        };
        let Some(candidate_declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
            return Err(MatcherError::invariant(MatcherInvariant::MissingDeclarator));
        };
        let Some(binding) = declared_bindings_for_var_declarator(candidate_declarator)
            .into_iter()
            .nth(target_binding_idx)
        else {
            return Err(MatcherError::invariant(MatcherInvariant::MissingBinding));
        };
        matches.push(MemberBindingMatch {
            body_idx,
            binding,
            free_bindings: alignment.free_bindings,
        });
    }
    Ok(matches)
}

/// Collect every multi-statement member match whose target item is a
/// single-declarator var-decl: per-declarator across contiguous windows (the
/// target may live in a declarator of a multi-declarator source_unit), reading the
/// matched declarator's binding at `target_binding_idx`. One `MemberBindingMatch`
/// per matched window (`body_idx` is the target item's position, ascending).
pub(super) fn member_matches_single_declarator_target_window(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
    target_item_idx: usize,
    target_binding_idx: usize,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_indices = needles
        .iter()
        .map(needle_index)
        .collect::<Result<Vec<_>>>()?;
    let windows = selector_match::match_single_declarator_target_windows_indexed(
        &needle_indices,
        &chunk.body_indices,
        target_item_idx,
        selector_mode(selector),
        &free_identifiers(&needle_indices),
    )
    .map_err(unsupported_error)?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    for window in windows {
        let (target_body_idx, subject_decl_idx) = window.site;
        let Some(candidate_var) = item_var_decl(&chunk.module.body[target_body_idx]) else {
            return Err(MatcherError::invariant(
                MatcherInvariant::ExpectedVariableDeclaration,
            ));
        };
        let Some(declarator) = candidate_var.decls.get(subject_decl_idx) else {
            return Err(MatcherError::invariant(MatcherInvariant::MissingDeclarator));
        };
        let Some(binding) = declared_bindings_for_var_declarator(declarator)
            .into_iter()
            .nth(target_binding_idx)
        else {
            return Err(MatcherError::invariant(MatcherInvariant::MissingBinding));
        };
        matches.push(MemberBindingMatch {
            body_idx: target_body_idx,
            binding,
            free_bindings: window.free_bindings,
        });
    }
    Ok(matches)
}

/// Collect every multi-statement member match: align the needle statements
/// against the chunk body as a fixed contiguous window, then read the target
/// binding from the body statement at the window's target offset. One
/// `MemberBindingMatch` per matched window (`body_idx` is the target item's
/// position, ascending). The multi-statement member path is **fixed-window** —
/// **not** the gapped module-level `STMT_LIST` subsequence the anonymous/group
/// paths use.
pub(super) fn member_matches_multi(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let target_binding = selector
        .target_binding
        .as_deref()
        .ok_or_else(|| MatcherError::invariant(MatcherInvariant::MissingTarget))?;
    let (target_item_idx, target_binding_idx) =
        selector_binding_location(needles, request_id, selector, target_binding)?;
    // A single-declarator var-decl target is matched per-declarator within the
    // contiguous window (so a target inside a multi-declarator source_unit is found).
    if selector_has_single_var_declarator(&needles[target_item_idx]) {
        return member_matches_single_declarator_target_window(
            chunk,
            needles,
            selector,
            target_item_idx,
            target_binding_idx,
        );
    }
    // A module-level run-hole statement (a bare `STMT_LIST;`) is not a valid
    // list-position carrier in a fixed contiguous window: a `STMT_LIST;` matches no
    // real statement positionally, so such a window has no match. Return an empty
    // candidate list (`match_fixed_window_sequence_indexed` would instead fail
    // closed on the run-hole keyword).
    if needles
        .iter()
        .any(|item| module_item_list_hole_name(item).is_some())
    {
        return Ok(Vec::new());
    }
    // A declarator-**hole** target inside a multi-statement window takes the same
    // general path (no single-declarator special-case); `candidate_target_binding`
    // aligns its declarators.
    let needle_indices = needles
        .iter()
        .map(needle_index)
        .collect::<Result<Vec<_>>>()?;
    let free = free_identifiers(&needle_indices);
    let starts = selector_match::match_fixed_window_sequence_indexed(
        &needle_indices,
        &chunk.body_indices,
        selector_mode(selector),
        &free,
    )
    .map_err(unsupported_error)?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    for start in starts {
        let body_idx = start.site + target_item_idx;
        let binding = candidate_target_binding(
            chunk,
            &needles[target_item_idx],
            &needle_indices[target_item_idx],
            &free,
            body_idx,
            selector_mode(selector),
            request_id,
            selector,
            target_binding,
            target_binding_idx,
        )?;
        matches.push(MemberBindingMatch {
            body_idx,
            binding,
            free_bindings: start.free_bindings,
        });
    }
    Ok(matches)
}

/// Collect every single-statement member match: scan the chunk body for
/// statements the needle matches, then read the claimed binding per matched item:
/// - **with `target_binding`** (a non-var-declarator needle): read
///   `declared_bindings[target_binding_idx]` per match, erroring only when that
///   index is out of range. Does **not** require a single declared binding.
/// - **without `target_binding`**: push the lone declared binding, skip a
///   statement that declares nothing, and bail when one declares more than one.
///
/// One `MemberBindingMatch` per matched statement, body-index ascending
/// (`matching_body_indices` returns the postings intersection in ascending order).
pub(super) fn member_matches_single_statement(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_facts = needle_item_facts(needle)?;
    let indices = matching_body_indices(chunk, &needle_facts, selector_mode(selector))?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    match &selector.target_binding {
        Some(target_binding) => {
            let (target_item_idx, target_binding_idx) = selector_binding_location(
                std::slice::from_ref(needle),
                request_id,
                selector,
                target_binding,
            )?;
            debug_assert_eq!(target_item_idx, 0, "single-statement needle");
            let needle_index = selector_match::Index::build(&needle_facts);
            let free = free_identifiers([&needle_index]);
            for matched in indices {
                let binding = candidate_target_binding(
                    chunk,
                    needle,
                    &needle_index,
                    &free,
                    matched.site,
                    selector_mode(selector),
                    request_id,
                    selector,
                    target_binding,
                    target_binding_idx,
                )?;
                matches.push(MemberBindingMatch {
                    body_idx: matched.site,
                    binding,
                    free_bindings: matched.free_bindings,
                });
            }
        }
        None => {
            for matched in indices {
                let body_idx = matched.site;
                let declared = declared_bindings(&chunk.module.body[body_idx]);
                match declared.as_slice() {
                    [single] => matches.push(MemberBindingMatch {
                        body_idx,
                        binding: single.clone(),
                        free_bindings: matched.free_bindings,
                    }),
                    [] => {}
                    multiple => {
                        return Err(SourceMatchError::MultipleBindings {
                            logical_module: request_id.to_string(),
                            body_index: body_idx,
                            count: multiple.len(),
                        }
                        .into());
                    }
                }
            }
        }
    }
    Ok(matches)
}
