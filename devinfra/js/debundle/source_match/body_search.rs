use super::*;

/// Fetch the runtime-module top-level item a matcher resolved to. The
/// index always comes from a window/alignment computed against
/// `runtime_module.body` itself, so a miss is an internal invariant
/// break, not a user error.
pub(crate) fn require_body_item(runtime_module: &Module, body_idx: usize) -> Result<&ModuleItem> {
    runtime_module
        .body
        .get(body_idx)
        .with_context(|| format!("body index {body_idx} disappeared while resolving source_match"))
}

pub(crate) fn find_matching_body_indices(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    filter: BodyIndexFilter<'_>,
) -> Vec<usize> {
    let prepared = PreparedNeedle::new(needle, selector);
    let declarator_hole_prefilter = item_var_decl(needle)
        .filter(|var| {
            var.decls
                .iter()
                .any(|declarator| declarator_list_hole_name(declarator).is_some())
        })
        .map(|var| VarDeclWithDeclaratorHolesPrefilter::new(var, &prepared));
    runtime_module
        .body
        .iter()
        .enumerate()
        .filter(|(body_idx, _)| filter.allows(*body_idx))
        .filter_map(|(body_idx, item)| {
            if let Some(prefilter) = &declarator_hole_prefilter {
                let candidate_var = item_var_decl(item)?;
                if !prefilter.var_decl_can_match(candidate_var) {
                    return None;
                }
            }
            prepared.matches(item).then_some(body_idx)
        })
        .collect()
}

/// `target_item_idx` is the offset of the matched/target statement within a
/// multi-statement window; the `filter` is applied to that absolute target
/// index (`window_start + target_item_idx`), not the window start.
pub(crate) fn find_matching_body_ranges(
    runtime_module: &Module,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
    target_item_idx: usize,
    filter: BodyIndexFilter<'_>,
) -> Vec<usize> {
    if needles.is_empty() || needles.len() > runtime_module.body.len() {
        return Vec::new();
    }
    if let [needle] = needles {
        let prepared = PreparedNeedle::new(needle, selector);
        let declarator_hole_prefilter = item_var_decl(needle)
            .filter(|var| {
                var.decls
                    .iter()
                    .any(|declarator| declarator_list_hole_name(declarator).is_some())
            })
            .map(|var| VarDeclWithDeclaratorHolesPrefilter::new(var, &prepared));
        return runtime_module
            .body
            .iter()
            .enumerate()
            .filter(|(body_idx, _)| filter.allows(*body_idx + target_item_idx))
            .filter_map(|(body_idx, candidate)| {
                if let Some(prefilter) = &declarator_hole_prefilter {
                    let candidate_var = item_var_decl(candidate)?;
                    if !prefilter.var_decl_can_match(candidate_var) {
                        return None;
                    }
                }
                prepared.matches(candidate).then_some(body_idx)
            })
            .collect();
    }
    let wildcard_idents = wildcard_ident_names_for_module_items(needles);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    runtime_module
        .body
        .windows(needles.len())
        .enumerate()
        .filter(|(body_idx, _)| filter.allows(*body_idx + target_item_idx))
        .filter_map(|(body_idx, candidates)| {
            SyntaxContext::within_ignored_ctxt(|| {
                let mut matcher = AstWildcardMatcher::new(selector, &wildcard_idents, alpha);
                needles
                    .iter()
                    .zip(candidates)
                    .all(|(needle, candidate)| matcher.match_module_item(needle, candidate))
            })
            .then_some(body_idx)
        })
        .collect()
}

pub(crate) fn find_matching_body_group_alignments(
    runtime_module: &Module,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
) -> Vec<Vec<Option<usize>>> {
    if needles.is_empty() {
        return Vec::new();
    }
    let mut segments: Vec<(usize, usize)> = Vec::new();
    let mut idx = 0;
    while idx < needles.len() {
        if module_item_list_hole_name(&needles[idx]).is_some() {
            idx += 1;
            continue;
        }
        let start = idx;
        while idx < needles.len() && module_item_list_hole_name(&needles[idx]).is_none() {
            idx += 1;
        }
        segments.push((start, idx - start));
    }
    if segments.is_empty() {
        return Vec::new();
    }

    let wildcard_idents = wildcard_ident_names_for_module_items(needles);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    SyntaxContext::within_ignored_ctxt(|| {
        let mut matcher = AstWildcardMatcher::new(selector, &wildcard_idents, alpha);
        let search = SegmentSearch {
            needle: needles,
            candidate: &runtime_module.body,
            segments: &segments,
            anchored_left: false,
            anchored_right: false,
        };
        let mut alignment = vec![None; needles.len()];
        let mut matches = Vec::new();
        place_module_item_segments(&mut matcher, &search, 0, 0, &mut alignment, &mut matches);
        matches
    })
}
