use super::*;

pub fn source_match_declared_binding_names(
    request_id: &str,
    source_match: &SourceMatch,
) -> Result<Vec<String>> {
    let parsed = parse_selector_module_with_capability_check(
        request_id,
        "binding_groups[].source_match",
        format!("<binding group source_match in {request_id}>"),
        &source_match.match_source,
        "binding_groups[].source_match",
    )?;
    Ok(parsed
        .body
        .iter()
        .flat_map(declared_bindings)
        .map(|binding| binding.binding_name)
        .collect())
}

pub fn binding_group_member_selectors(
    request_id: &str,
    group: &BindingGroup,
) -> Result<Vec<BindingGroupMemberSelector>> {
    if group.source_match.target_binding.is_some() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match must not include \
             `target_binding`; use the `exports` keys to choose selector-local bindings"
        );
    }
    let exports = effective_binding_group_exports(group, request_id)?;
    let unknown_comments = group
        .comments
        .keys()
        .filter(|name| !exports.contains_key(*name))
        .cloned()
        .collect::<Vec<_>>();
    if !unknown_comments.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].comments names bindings that \
             are not exported by the group: {}",
            unknown_comments.join(", ")
        );
    }
    Ok(exports
        .into_iter()
        .map(|(target_binding, export_name)| {
            let mut selector = group.source_match.selector();
            selector.target_binding = Some(target_binding.clone());
            selector.target_statement = None;
            selector.target_statements = None;
            let comment = group.comments.get(&target_binding).cloned();
            BindingGroupMemberSelector {
                export_name,
                selector,
                comment,
            }
        })
        .collect())
}

pub fn binding_group_anonymous_statement_selector(
    group: &BindingGroup,
) -> Option<AnonymousStatementSelector> {
    if group.source_match.target_statement.is_none()
        && group.source_match.target_statements.is_none()
    {
        return None;
    }
    let mut selector = group.source_match.selector();
    selector.target_binding = None;
    Some(selector)
}

pub(crate) fn effective_binding_group_exports(
    group: &BindingGroup,
    request_id: &str,
) -> Result<BTreeMap<String, String>> {
    let mut exports = match &group.adopt_names {
        BindingGroupAdoptNames::None | BindingGroupAdoptNames::All(false) => BTreeMap::new(),
        BindingGroupAdoptNames::All(true) => {
            let names = declared_selector_binding_names(group, request_id)?;
            names
                .into_iter()
                .map(|name| (name.clone(), name))
                .collect::<BTreeMap<_, _>>()
        }
        BindingGroupAdoptNames::Names(names) => {
            let declared = declared_selector_binding_names(group, request_id)?;
            let declared_set = declared.into_iter().collect::<BTreeSet<_>>();
            let mut adopted = BTreeMap::new();
            for name in names {
                if !declared_set.contains(name) {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names entry \
                         `{name}` is not declared by source_match.match"
                    );
                }
                if adopted.insert(name.clone(), name.clone()).is_some() {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names repeats \
                         `{name}`"
                    );
                }
            }
            adopted
        }
    };
    exports.extend(group.exports.clone());
    if exports.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[] must include non-empty `exports` \
             or `adopt_names`"
        );
    }
    Ok(exports)
}

pub(crate) fn declared_selector_binding_names(
    group: &BindingGroup,
    request_id: &str,
) -> Result<Vec<String>> {
    let names = source_match_declared_binding_names(request_id, &group.source_match)?;
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for name in &names {
        if !seen.insert(name.clone()) {
            duplicates.insert(name.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match declares duplicate \
             selector-local binding names: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    if names.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].adopt_names found no declared \
             bindings in source_match.match"
        );
    }
    Ok(names)
}

/// All member-binding candidates a member-form selector matches in
/// `runtime_module`, without the exactly-one arbitration
/// [`resolve_member_binding`] applies. Used by callers that
/// aggregate matches across several source files (the CLI edit
/// gate) before deciding uniqueness.
pub fn member_binding_candidates(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<ResolvedMemberBinding>> {
    Ok(
        member_binding_candidate_matches(runtime_module, request_id, selector)?
            .into_iter()
            .map(|matched| matched.binding)
            .collect(),
    )
}

pub fn member_binding_candidate_matches(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    member_binding_candidate_matches_within(
        runtime_module,
        request_id,
        selector,
        BodyIndexFilter::All,
    )
}

/// Like [`member_binding_candidate_matches`], but only inspects top-level body
/// indices the `filter` admits. The reported matches are identical to the
/// unfiltered scan whenever `filter` is a sound superset of the matchable
/// indices (see [`BodyIndexFilter`]).
pub fn member_binding_candidate_matches_within(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    filter: BodyIndexFilter<'_>,
) -> Result<Vec<MemberBindingMatch>> {
    trace_source_match(
        "members[].selector.source_match candidates",
        request_id,
        selector,
        || find_member_binding_matches(runtime_module, request_id, selector, filter),
        |matches: &Vec<MemberBindingMatch>| {
            format!(
                "matches={} body_indices={} bindings={}",
                matches.len(),
                render_timing_body_indices(matches.iter().map(|matched| &matched.body_idx)),
                render_timing_names(
                    matches
                        .iter()
                        .map(|matched| matched.binding.binding_name.as_str())
                )
            )
        },
    )
}

pub fn resolve_anonymous_statement_body_index(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<usize> {
    let matches = resolve_anonymous_statement_body_indices(runtime_module, request_id, selector)?;
    match matches.as_slice() {
        [single] => Ok(*single),
        multiple => bail!(
            "logical_module {request_id}: anonymous_statements[].source_match resolved to {} \
             top-level statements at body indices {:?}; expected exactly one. Use the plural \
             resolver for selectors with `target_statements`.",
            multiple.len(),
            multiple,
        ),
    }
}

pub fn resolve_anonymous_statement_body_indices(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<usize>> {
    let matches = find_anonymous_statement_body_index_groups(runtime_module, request_id, selector)?;
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: anonymous_statements[].match did not match any \
             top-level statement group in the chunk. Selector:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: anonymous_statements[].match is ambiguous — \
             matched {} top-level statement groups at body indices {:?}. Refine the selector. \
             Source:\n{match_source}",
            multiple.len(),
            multiple,
            match_source = selector.match_source,
        ),
    }
}

pub fn find_anonymous_statement_body_indices(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<usize>> {
    Ok(
        find_anonymous_statement_body_index_groups(runtime_module, request_id, selector)?
            .into_iter()
            .flatten()
            .collect(),
    )
}

pub fn find_anonymous_statement_body_index_groups(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<Vec<usize>>> {
    trace_source_match(
        "anonymous_statements[].source_match",
        request_id,
        selector,
        || {
            let parsed = parse_selector_module_with_capability_check(
                request_id,
                "anonymous_statements[].source_match",
                format!("<anonymous_statement match in {request_id}>"),
                &selector.match_source,
                "anonymous_statements[].match",
            )?;
            let target_indices =
                anonymous_selector_target_statement_indices(request_id, selector, &parsed.body)?;
            let mut groups = Vec::new();
            for alignment in
                find_matching_body_group_alignments(runtime_module, &parsed.body, selector)
            {
                let mut group = Vec::with_capacity(target_indices.len());
                for target_idx in &target_indices {
                    let Some(Some(body_idx)) = alignment.get(*target_idx) else {
                        bail!(
                            "logical_module {request_id}: anonymous_statements[].source_match \
                             target statement {target_idx} was matched by a STMT_LIST hole instead \
                             of a pinned selector statement. Refine the selector:\n{match_source}",
                            match_source = selector.match_source,
                        );
                    };
                    group.push(*body_idx);
                }
                groups.push(group);
            }
            Ok(groups)
        },
        |groups: &Vec<Vec<usize>>| {
            format!(
                "matches={} body_indices={}",
                groups.len(),
                render_timing_groups(groups)
            )
        },
    )
}

/// Source-aware fragility signal for selector-debt reporting.
///
/// This intentionally does not change selector semantics: it reuses the
/// normal exact matcher, then lists high-scoring non-matching top-level
/// items that look structurally close enough to become ambiguous after a
/// small source drift. The first slice only scores selectors whose source
/// parses to one pinned top-level item; multi-statement windows still use
/// the exact match count but do not get near-miss rows yet.
pub fn source_match_body_debt(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    min_score: usize,
    limit: usize,
) -> Result<SourceMatchBodyDebt> {
    let parsed = parse_selector_module_with_capability_check(
        request_id,
        "source_match",
        format!("<source_match debt in {request_id}>"),
        &selector.match_source,
        "source_match",
    )?;
    let exact_groups = find_matching_body_group_alignments(runtime_module, &parsed.body, selector);
    let [needle] = parsed.body.as_slice() else {
        return Ok(SourceMatchBodyDebt {
            exact_groups,
            near_misses: Vec::new(),
        });
    };
    if module_item_list_hole_name(needle).is_some() {
        return Ok(SourceMatchBodyDebt {
            exact_groups,
            near_misses: Vec::new(),
        });
    }
    let exact_body_indices = exact_groups
        .iter()
        .flat_map(|group| group.iter().flatten().copied())
        .collect::<BTreeSet<_>>();
    let wildcard_idents = wildcard_ident_names(needle);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    let mut near_misses = SyntaxContext::within_ignored_ctxt(|| {
        runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(body_idx, candidate)| {
                if exact_body_indices.contains(&body_idx) {
                    return None;
                }
                let reason =
                    first_mismatch_reason(needle, candidate, selector, &wildcard_idents, alpha)?;
                if reason.score < min_score {
                    return None;
                }
                let declared_bindings = declared_bindings(candidate)
                    .into_iter()
                    .map(|binding| binding.binding_name)
                    .collect::<Vec<_>>();
                Some(SourceMatchNearMiss {
                    body_idx,
                    declared_bindings,
                    score: reason.score,
                    reason: reason.reason,
                })
            })
            .collect::<Vec<_>>()
    });
    near_misses.sort_by(|left, right| {
        right
            .score
            .cmp(&left.score)
            .then_with(|| left.body_idx.cmp(&right.body_idx))
    });
    if limit > 0 {
        near_misses.truncate(limit);
    }
    Ok(SourceMatchBodyDebt {
        exact_groups,
        near_misses,
    })
}

pub fn resolve_member_binding(
    runtime_module: &Module,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<ResolvedMemberBinding> {
    let kind = format!("members[].selector.source_match export=`{export_name}`");
    let matched = trace_source_match(
        &kind,
        request_id,
        selector,
        || {
            let matches = find_member_binding_matches(
                runtime_module,
                request_id,
                selector,
                BodyIndexFilter::All,
            )?;
            let target_binding_hint = selector
                .target_binding
                .as_deref()
                .map(|target| format!(" target_binding `{target}`"))
                .unwrap_or_default();
            match matches.as_slice() {
                [single] => Ok(single.clone()),
                [] => {
                    let hint = source_match_no_match_hint(runtime_module, selector);
                    bail!(
                        "logical_module {request_id}: members[].selector.source_match for export \
                         `{export_name}`{target_binding_hint} did not match any top-level declaration in the chunk. \
                         Selector:\n{match_source}{hint}",
                        match_source = selector.match_source,
                        hint = hint.unwrap_or_default(),
                    )
                }
                multiple => bail!(
                    "logical_module {request_id}: members[].selector.source_match for export \
                     `{export_name}`{target_binding_hint} is ambiguous — matched {} top-level statements at body \
                     indices {:?} (bindings: {}). Refine the selector. Source:\n{match_source}",
                    multiple.len(),
                    multiple
                        .iter()
                        .map(|matched| matched.body_idx)
                        .collect::<Vec<_>>(),
                    multiple
                        .iter()
                        .map(|matched| matched.binding.binding_name.as_str())
                        .collect::<Vec<_>>()
                        .join(", "),
                    match_source = selector.match_source,
                ),
            }
        },
        |matched| {
            format!(
                "body_indices=[{}] binding={}",
                matched.body_idx, matched.binding.binding_name
            )
        },
    )?;
    Ok(matched.binding)
}

pub fn resolve_member_binding_group(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<BTreeMap<String, ResolvedMemberBinding>> {
    Ok(
        resolve_member_binding_group_match(
            runtime_module,
            request_id,
            selector,
            exports_by_target,
        )?
        .bindings,
    )
}

pub fn resolve_member_binding_group_match(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<ResolvedMemberBindingGroup> {
    trace_source_match(
        "binding_groups[].source_match",
        request_id,
        selector,
        || {
            resolve_member_binding_group_impl(
                runtime_module,
                request_id,
                selector,
                exports_by_target,
            )
        },
        |resolved| {
            format!(
                "targets={} bindings={}",
                resolved.bindings.len(),
                render_timing_names(
                    resolved
                        .bindings
                        .values()
                        .map(|matched| matched.binding_name.as_str())
                )
            )
        },
    )
}

pub(crate) fn resolve_member_binding_group_impl(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<ResolvedMemberBindingGroup> {
    if selector.target_binding.is_some() {
        bail!(
            "logical_module {request_id}: binding group resolver received a selector with \
             target_binding already set"
        );
    }
    let parsed = parse_selector_module_with_capability_check(
        request_id,
        "binding_groups[].source_match",
        format!("<binding group source_match in {request_id}>"),
        &selector.match_source,
        "binding_groups[].source_match",
    )?;
    if parsed.body.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match parsed to zero \
             statements; selector source must contain at least one top-level statement:\n{match_source}",
            match_source = selector.match_source,
        );
    }
    if parsed.body.len() == 1 && selector_single_var_declarator(&parsed.body[0]).is_some() {
        let mut resolved = BTreeMap::new();
        let mut matched_body_idx = None;
        for (target_binding, export_name) in exports_by_target {
            let mut selector = selector.clone();
            selector.target_binding = Some(target_binding.clone());
            let matches = member_binding_candidate_matches(runtime_module, request_id, &selector)?;
            let [matched] = matches.as_slice() else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match target_binding \
                     `{target_binding}` for export `{export_name}` resolved to {} candidate \
                     binding(s). Refine the selector. Source:\n{match_source}",
                    matches.len(),
                    match_source = selector.match_source,
                );
            };
            if matched_body_idx
                .replace(matched.body_idx)
                .is_some_and(|prior| prior != matched.body_idx)
            {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match targets \
                     resolved to different body indices. Refine the selector. Source:\n{match_source}",
                    match_source = selector.match_source,
                );
            }
            resolved.insert(target_binding.clone(), matched.binding.clone());
        }
        return Ok(ResolvedMemberBindingGroup {
            body_idx: matched_body_idx.unwrap_or(0),
            bindings: resolved,
        });
    }
    if parsed.body.len() == 1 && selector_var_decl_has_declarator_holes(&parsed.body[0]) {
        return resolve_member_binding_group_with_declarator_holes(
            runtime_module,
            request_id,
            &parsed.body[0],
            selector,
            exports_by_target,
        );
    }

    let mut target_locations = BTreeMap::new();
    for target_binding in exports_by_target.keys() {
        target_locations.insert(
            target_binding.clone(),
            selector_binding_location(&parsed.body, request_id, selector, target_binding)?,
        );
    }

    let target_hint = exports_by_target
        .iter()
        .map(|(target_binding, export_name)| {
            format!("target_binding `{target_binding}` for export `{export_name}`")
        })
        .collect::<Vec<_>>()
        .join(", ");
    let alignments = find_matching_body_group_alignments(runtime_module, &parsed.body, selector);
    let alignment = match alignments.as_slice() {
        [single] => single,
        [] => {
            let hint = source_match_no_match_hint(runtime_module, selector);
            bail!(
                "logical_module {request_id}: binding_groups[].source_match for targets \
                 `{target_hint}` did not match any top-level declaration range in the chunk. \
                 Selector:\n{match_source}{hint}",
                match_source = selector.match_source,
                hint = hint.unwrap_or_default(),
            )
        }
        multiple => bail!(
            "logical_module {request_id}: binding_groups[].source_match for targets \
             `{target_hint}` is ambiguous — matched {} top-level declaration ranges at body \
             indices {:?}. Refine the selector. Source:\n{match_source}",
            multiple.len(),
            multiple
                .iter()
                .map(|alignment| alignment.iter().flatten().copied().collect::<Vec<_>>())
                .collect::<Vec<_>>(),
            match_source = selector.match_source,
        ),
    };

    let mut resolved = BTreeMap::new();
    for (target_binding, (target_item_idx, target_binding_idx)) in target_locations {
        let Some(Some(matched_body_idx)) = alignment.get(target_item_idx) else {
            bail!(
                "logical_module {request_id}: binding_groups[].source_match target_binding \
                 `{target_binding}` was matched by a STMT_LIST hole instead of a pinned \
                 selector statement. Refine the selector:\n{match_source}",
                match_source = selector.match_source,
            );
        };
        let matched_body_idx = *matched_body_idx;
        let item = require_body_item(runtime_module, matched_body_idx)?;
        let declared = declared_bindings(item);
        let Some(binding) = declared.get(target_binding_idx) else {
            bail!(
                "logical_module {request_id}: binding_groups[].source_match target_binding \
                 `{target_binding}` matched top-level statement at body index {matched_body_idx}, but \
                 the matched statement declares only {} bindings. Source:\n{match_source}",
                declared.len(),
                match_source = selector.match_source,
            );
        };
        resolved.insert(target_binding, binding.clone());
    }
    Ok(ResolvedMemberBindingGroup {
        body_idx: alignment.iter().flatten().copied().next().unwrap_or(0),
        bindings: resolved,
    })
}
