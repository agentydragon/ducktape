//! The fact-based `SelectorResolver` (`ChunkResolver`): resolve a selector to its
//! claimed owner/binding using the fact matcher (`selector_match::matches` over
//! `chunk_facts`) as the per-statement match oracle, then extract the claimed
//! binding(s) with the shared `declared_bindings` / `selector_binding_location`
//! helpers. The per-statement match semantics are pinned by
//! `selector_match_differential_test`.
//!
//! Fail-closed: a construct the matcher does not faithfully handle (an
//! `Unsupported` needle) surfaces as an error rather than a wrong claim — it never
//! under-resolves silently.

use super::*;

/// Facts for a single top-level statement. Extracts from the borrowed item (one
/// item being one root) — no per-call clone into a one-item `Module`, which on the
/// per-selector hot path was the resolver's main overhead over production.
fn item_facts(item: &ModuleItem) -> Option<chunk_facts::ChunkFacts> {
    chunk_facts::extract_facts_items(std::slice::from_ref(item)).ok()
}

/// Facts for a single **needle** statement, honoring the selector's
/// `wildcard_string_literals` (a `StrLit` whose value is a wildcard projects to a
/// `str_wildcard` fact the matcher matches against any string value). Subject
/// (chunk-body) facts use [`item_facts`] — real source carries no wildcards.
fn needle_item_facts(
    item: &ModuleItem,
    selector: &AnonymousStatementSelector,
) -> Option<chunk_facts::ChunkFacts> {
    chunk_facts::extract_facts_needle(
        std::slice::from_ref(item),
        &selector.wildcard_string_literals,
    )
    .ok()
}

/// One chunk's relational model, built once: the AST plus its top-level body
/// projected to per-statement facts (the EDB). Every selector resolves against
/// this **shared** model — the single pass that, for today's cross-reference-free
/// corpus, the global solve factors into independent per-selector matches. (A
/// non-extractable statement projects to empty facts, which has no root and so
/// matches nothing — the same outcome as skipping it.) Building the EDB once,
/// rather than re-projecting it per selector, is what makes a corpus-wide
/// resolver differential tractable.
pub struct ChunkResolver<'m> {
    module: &'m Module,
    /// Root node kind of each body item, cached once for the single-statement
    /// scan's sound root-kind prefilter (see `matching_body_indices`).
    body_root_kinds: Vec<Option<&'static str>>,
    /// Each body item's match `Index`, built once. The matcher rebuilds a
    /// subject's index on every call otherwise, so caching it here turns the
    /// per-`(selector, subject)` index construction into one build per subject
    /// for the whole chunk — the dominant cost of a corpus-wide resolve.
    body_indices: Vec<selector_match::Index>,
    /// One synthetic single-declarator subject index per declarator of every
    /// var-decl owner, built once. The single-declarator var-decl member path
    /// matches its needle against each declarator; without this it re-synthesizes
    /// the item and re-extracts facts for every declarator on every such selector
    /// (the second-worst per-selector cost after declarator holes).
    var_declarator_subjects: Vec<VarDeclaratorSubject>,
    /// Inverted index: a [`selector_match::subject_tokens`] token (literal /
    /// property / regex, **plus** every identifier spelling) → the (ascending) body
    /// indices whose statement carries it. A needle can only match a statement that
    /// carries every token the needle requires, so the single-statement scan visits
    /// just the rarest required token's postings instead of every body item.
    /// Indexing identifiers lets an exact-mode needle prune by its identifier
    /// spellings (the discriminator that removes the `O(selectors × statements)`
    /// scan for identifier-only `const NAME = …;` selectors); an alpha-mode needle
    /// never queries an `Ident` token, so its candidate set is unchanged. Needles
    /// that require no token (pure-structural / alpha with no literal, or a bare
    /// regex predicate) fall back to the root-kind-prefiltered full scan.
    body_tokens: std::collections::HashMap<selector_match::Token, Vec<usize>>,
    /// Like `body_tokens`, but keyed to indices into `var_declarator_subjects`:
    /// a single-declarator member/group needle only matches a declarator that
    /// carries its tokens, so the declarator scan visits just the rarest token's
    /// postings. Exact-mode `const NAME = …;` needles now prune by identifier
    /// spelling; CSS-module regex-predicate (alpha) needles pin no token and fall
    /// back to the init-kind-prefiltered scan (cheap now that the regex is precompiled).
    declarator_tokens: std::collections::HashMap<selector_match::Token, Vec<usize>>,
}

/// A var-decl owner's single declarator, projected once to a synthetic
/// single-declarator subject index. `body_idx`/`declarator_idx` locate the
/// declarator back in `module.body` for binding extraction on a match.
struct VarDeclaratorSubject {
    body_idx: usize,
    declarator_idx: usize,
    index: selector_match::Index,
    /// The declarator's init-expression kind, cached for the sound init-kind
    /// prefilter (most var-decl member scans reject on init kind without a match).
    init_kind: Option<&'static str>,
}

impl<'m> ChunkResolver<'m> {
    pub fn new(module: &'m Module) -> Self {
        let body_facts: Vec<_> = module
            .body
            .iter()
            .map(|item| item_facts(item).unwrap_or_default())
            .collect();
        let body_root_kinds = body_facts
            .iter()
            .map(selector_match::subject_root_kind)
            .collect();
        let body_indices: Vec<selector_match::Index> = body_facts
            .iter()
            .map(selector_match::Index::build)
            .collect();
        let mut body_tokens: std::collections::HashMap<selector_match::Token, Vec<usize>> =
            std::collections::HashMap::new();
        for (body_idx, index) in body_indices.iter().enumerate() {
            for token in selector_match::subject_tokens(index) {
                body_tokens.entry(token).or_default().push(body_idx);
            }
        }
        let mut var_declarator_subjects = Vec::new();
        let mut declarator_tokens: std::collections::HashMap<selector_match::Token, Vec<usize>> =
            std::collections::HashMap::new();
        for (body_idx, item) in module.body.iter().enumerate() {
            let Some(var) = item_var_decl(item) else {
                continue;
            };
            for (declarator_idx, declarator) in var.decls.iter().enumerate() {
                if let Some(facts) = item_facts(&single_declarator_item(item, declarator)) {
                    let index = selector_match::Index::build(&facts);
                    let subject_idx = var_declarator_subjects.len();
                    for token in selector_match::subject_tokens(&index) {
                        declarator_tokens
                            .entry(token)
                            .or_default()
                            .push(subject_idx);
                    }
                    var_declarator_subjects.push(VarDeclaratorSubject {
                        body_idx,
                        declarator_idx,
                        init_kind: selector_match::var_declarator_init_kind(&facts),
                        index,
                    });
                }
            }
        }
        Self {
            module,
            body_root_kinds,
            body_indices,
            var_declarator_subjects,
            body_tokens,
            declarator_tokens,
        }
    }

    /// Indices into `var_declarator_subjects` a single-declarator needle could
    /// match — the intersection of every required token's postings (sound
    /// superset), or all declarators when the needle pins no required token.
    /// Mirrors [`Self::candidate_bodies`] for the declarator scan. `mode` /
    /// `allow_exact_ident` gate the exact-mode identifier discriminator (see
    /// [`selector_match::needle_required_tokens`]).
    fn candidate_declarators(
        &self,
        needle_index: &selector_match::Index,
        mode: selector_match::Mode,
        allow_exact_ident: bool,
    ) -> Vec<usize> {
        intersect_postings(
            &self.declarator_tokens,
            &selector_match::needle_required_tokens(needle_index, mode, allow_exact_ident),
            self.var_declarator_subjects.len(),
        )
    }

    /// Body indices a needle could match: the **intersection** of every required
    /// token's postings (a sound superset — every match carries every token), or
    /// every body index when the needle pins no required token. Intersecting
    /// (not just taking the rarest list) is what shrinks the candidate set for
    /// needles whose tokens are individually common but jointly rare. `mode` /
    /// `allow_exact_ident` gate the exact-mode identifier discriminator (see
    /// [`selector_match::needle_required_tokens`]); a prebind path must pass
    /// `allow_exact_ident = false`.
    fn candidate_bodies(
        &self,
        needle_index: &selector_match::Index,
        mode: selector_match::Mode,
        allow_exact_ident: bool,
    ) -> Vec<usize> {
        intersect_postings(
            &self.body_tokens,
            &selector_match::needle_required_tokens(needle_index, mode, allow_exact_ident),
            self.body_indices.len(),
        )
    }
}

/// Intersect the postings lists of every `token` (each a sorted, ascending index
/// list) into the candidate set. Empty `tokens` ⟹ scan everything (`0..total`);
/// a token absent from `index` ⟹ no candidate can match. Postings are intersected
/// smallest-first via binary search, so the cost is bounded by the rarest token.
fn intersect_postings(
    index: &std::collections::HashMap<selector_match::Token, Vec<usize>>,
    tokens: &[selector_match::Token],
    total: usize,
) -> Vec<usize> {
    if tokens.is_empty() {
        return (0..total).collect();
    }
    let mut postings: Vec<&Vec<usize>> = Vec::with_capacity(tokens.len());
    for token in tokens {
        match index.get(token) {
            Some(list) => postings.push(list),
            None => return Vec::new(),
        }
    }
    postings.sort_by_key(|list| list.len());
    let mut candidates = postings[0].clone();
    for list in &postings[1..] {
        candidates.retain(|candidate| list.binary_search(candidate).is_ok());
        if candidates.is_empty() {
            break;
        }
    }
    candidates
}

/// Top-level body indices whose statement the needle matches under the fact
/// matcher, over the chunk's shared EDB. Fails closed if the needle itself is
/// `Unsupported`.
fn matching_body_indices(
    chunk: &ChunkResolver,
    needle_facts: &chunk_facts::ChunkFacts,
    mode: selector_match::Mode,
) -> Result<Vec<usize>> {
    // Build the needle index once; probe it (an unsupported construct errors
    // uniformly), then match it against each cached body index.
    let needle_index = selector_match::Index::build(needle_facts);
    selector_match::matches_indexed(&needle_index, &needle_index, mode)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    // Sound root-kind prefilter: when the needle root is a concrete kind, a
    // subject whose root kind differs is a guaranteed non-match (the `nkind !=
    // subject kind` gate in the matcher), so skip it without matching.
    let prefilter = selector_match::needle_root_kind_prefilter(needle_facts);
    let mut indices = Vec::new();
    // Token index narrows the scan to statements that carry the needle's required
    // tokens (a sound superset); the root-kind prefilter and full match then
    // filter. This path matches without a prebind, so an exact-mode needle may also
    // require its identifier spellings (`allow_exact_ident = true`). A non-extractable
    // statement projects to empty facts (no root, no tokens) and so matches nothing —
    // the same outcome as the old skip.
    for body_idx in chunk.candidate_bodies(&needle_index, mode, true) {
        if let Some(kind) = prefilter
            && chunk.body_root_kinds[body_idx] != Some(kind)
        {
            continue;
        }
        // The needle was probed-supported above, so this per-candidate match skips
        // the redundant needle-only faithful-subset re-check (the dominant self-cost).
        if selector_match::matches_prepared(&needle_index, &chunk.body_indices[body_idx], mode) {
            indices.push(body_idx);
        }
    }
    Ok(indices)
}

/// A synthetic single-declarator version of a var-decl `item` keeping only
/// `declarator`, cloned from the real item so span/context stay valid. Matching
/// the single-declarator needle against this *is* the per-declarator match the
/// production resolver does. Counting matches across every declarator of every
/// owner keeps categoricity faithful: a single-declarator needle that matches
/// one declarator of a multi-declarator owner is a real match a whole-statement
/// match would miss — which would mis-count and turn a production-ambiguous case
/// into a spurious unique resolution.
fn single_declarator_item(item: &ModuleItem, declarator: &VarDeclarator) -> ModuleItem {
    let mut cloned = item.clone();
    match &mut cloned {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => var.decls = vec![declarator.clone()],
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            if let Decl::Var(var) = &mut export.decl {
                var.decls = vec![declarator.clone()];
            }
        }
        _ => {}
    }
    cloned
}

/// Collect every single-declarator var-decl member match: the needle's
/// declarator against every declarator of every var-decl owner (the per-declarator
/// path), so the match count — hence categoricity — is faithful. Returns one
/// `MemberBindingMatch` per matched declarator, body-index ascending (the cached
/// `var_declarator_subjects` are built in body order).
fn member_matches_var_declarator(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_facts = needle_item_facts(needle, selector)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let mode = selector_mode(selector);
    // Probe the needle once: an unsupported construct errors uniformly.
    selector_match::matches(&needle_facts, &needle_facts, mode)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    // Which of the needle declarator's declared bindings is the target.
    let target_binding_idx = match &selector.target_binding {
        Some(target_binding) => {
            let declared = declared_bindings(needle);
            let indices: Vec<usize> = declared
                .iter()
                .enumerate()
                .filter_map(|(idx, binding)| {
                    (binding.binding_name == *target_binding).then_some(idx)
                })
                .collect();
            match indices.as_slice() {
                [single] => *single,
                [] => bail!(
                    "logical_module {request_id}: target_binding `{target_binding}` is not \
                     declared by the selector source"
                ),
                _ => bail!(
                    "logical_module {request_id}: target_binding `{target_binding}` is \
                     ambiguous within the selector source"
                ),
            }
        }
        None => 0,
    };
    // Match the needle against each candidate var-decl declarator via the cached
    // synthetic single-declarator indices (built once for the chunk), so this
    // scan is index-build-free. The token index narrows to declarators carrying
    // the needle's required tokens; this path matches without a prebind, so an
    // exact-mode needle also requires its identifier spellings (the binding name
    // plus any referenced names) — for `const NAME = …;` selectors that alone
    // shrinks the scan from every declarator to the few sharing those names. A
    // sound init-kind prefilter then skips declarators whose initializer kind
    // cannot match, before the full match.
    let needle_index = selector_match::Index::build(&needle_facts);
    let init_prefilter = selector_match::needle_var_declarator_init_kind_prefilter(&needle_facts);
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    for subject_idx in chunk.candidate_declarators(&needle_index, mode, true) {
        let subject = &chunk.var_declarator_subjects[subject_idx];
        if let Some(kind) = init_prefilter
            && subject.init_kind != Some(kind)
        {
            continue;
        }
        // Needle probed-supported above; skip the per-candidate needle re-check.
        if !selector_match::matches_prepared(&needle_index, &subject.index, mode) {
            continue;
        }
        let item = &chunk.module.body[subject.body_idx];
        let declarator = &item_var_decl(item)
            .expect("cached var-declarator subject owner is a var-decl")
            .decls[subject.declarator_idx];
        let declared = declared_bindings_for_var_declarator(declarator);
        if selector.target_binding.is_none() && declared.len() != 1 {
            bail!(
                "datalog resolver: export `{export_name}` matched a declarator binding {} \
                 names; needs a single-binding declarator or target_binding",
                declared.len(),
            );
        }
        let Some(binding) = declared.into_iter().nth(target_binding_idx) else {
            bail!("datalog resolver: target binding index out of range for matched declarator");
        };
        matches.push(MemberBindingMatch {
            body_idx: subject.body_idx,
            binding,
        });
    }
    Ok(matches)
}

/// Collect every declarator-hole member match (`const DECLARATORS, x = …,
/// DECLARATORS;` with a `target_binding`): for each var-decl owner in the chunk,
/// try each candidate binding name at the target's declarator position —
/// prebinding it so the alpha bijection pins the target's identity — and take
/// the greedy-leftmost declarator alignment. `alignment[target_decl_idx]` names
/// the subject declarator the target pinned, whose binding is the resolved
/// owner. One `MemberBindingMatch` per matched owner, body-index ascending.
fn member_matches_declarator_hole(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let target_binding = selector.target_binding.as_deref().ok_or_else(|| {
        anyhow::anyhow!("datalog resolver: declarator-hole member selector needs target_binding")
    })?;
    let needle_var = item_var_decl(needle).ok_or_else(|| {
        anyhow::anyhow!("datalog resolver: declarator-hole needle is not a variable declaration")
    })?;
    let (target_decl_idx, target_binding_idx) =
        selector_var_declarator_binding_location(needle_var, request_id, selector, target_binding)?;
    let needle_facts = needle_item_facts(needle, selector)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let mode = selector_mode(selector);
    // Probe the needle once: an unsupported construct errors uniformly (and makes
    // the per-subject `.expect` below sound — the only `Unsupported` source is the
    // needle construct, invariant across subjects and prebindings).
    selector_match::var_declarator_alignment_indexed(&needle_index, &needle_index, mode, None)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    // The fixed (non-hole) declarators pin invariant tokens any matching owner
    // must carry, so the token index narrows the owner scan (the giant
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
        // every alignment attempt for this owner shares one prebuilt subject index.
        let subject_index = &chunk.body_indices[body_idx];
        let alignment = target_binding_candidate_names(candidate_var, target_binding_idx)
            .into_iter()
            .find_map(|candidate_binding| {
                selector_match::var_declarator_alignment_prepared(
                    &needle_index,
                    subject_index,
                    mode,
                    Some((target_binding, &candidate_binding)),
                )
            });
        let Some(alignment) = alignment else {
            continue;
        };
        let Some(Some(candidate_decl_idx)) = alignment.get(target_decl_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match for export \
                 `{export_name}` target_binding `{target_binding}` was matched by a DECLARATORS \
                 hole, not a pinned declarator"
            );
        };
        let Some(candidate_declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
            bail!("datalog resolver: target binding aligned to a missing candidate declarator");
        };
        let Some(binding) = declared_bindings_for_var_declarator(candidate_declarator)
            .into_iter()
            .nth(target_binding_idx)
        else {
            bail!("datalog resolver: target binding index out of range for matched declarator");
        };
        matches.push(MemberBindingMatch { body_idx, binding });
    }
    Ok(matches)
}

/// Parse a selector's `match_source` to its top-level statements (any count),
/// **fail-closed** on an unsupported selector construct: for example, a misplaced
/// `ANYTHING` hole in an object property *key* errors here before any match is
/// attempted, so the diagnostic names the bad construct rather than a generic
/// "did not match" (mirrors the deleted matcher's selector capability gate).
fn parse_needles(
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<ModuleItem>> {
    Ok(parse_selector_module_with_capability_check(
        request_id,
        "source_match",
        format!("<datalog needle in {request_id}>"),
        &selector.match_source,
        "source_match",
    )?
    .body)
}

/// Collect every multi-statement member match whose target item is a
/// single-declarator var-decl: per-declarator across contiguous windows (the
/// target may live in a declarator of a multi-declarator owner), reading the
/// matched declarator's binding at `target_binding_idx`. One `MemberBindingMatch`
/// per matched window (`body_idx` is the target item's position, ascending).
fn member_matches_single_declarator_target_window(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
    target_item_idx: usize,
    target_binding_idx: usize,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_facts = needles
        .iter()
        .map(|item| needle_item_facts(item, selector))
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_indices: Vec<_> = needle_facts
        .iter()
        .map(selector_match::Index::build)
        .collect();
    let windows = selector_match::match_single_declarator_target_windows_indexed(
        &needle_indices,
        &chunk.body_indices,
        target_item_idx,
        selector_mode(selector),
    )
    .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    for (target_body_idx, subject_decl_idx) in windows {
        let Some(candidate_var) = item_var_decl(&chunk.module.body[target_body_idx]) else {
            bail!("datalog resolver: matched window target is not a variable declaration");
        };
        let Some(declarator) = candidate_var.decls.get(subject_decl_idx) else {
            bail!("datalog resolver: matched declarator index out of range");
        };
        let Some(binding) = declared_bindings_for_var_declarator(declarator)
            .into_iter()
            .nth(target_binding_idx)
        else {
            bail!("datalog resolver: target binding index out of range for matched declarator");
        };
        matches.push(MemberBindingMatch {
            body_idx: target_body_idx,
            binding,
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
fn member_matches_multi(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let target_binding = selector.target_binding.as_deref().ok_or_else(|| {
        anyhow::anyhow!("datalog resolver: multi-statement member selector needs target_binding")
    })?;
    let (target_item_idx, target_binding_idx) =
        selector_binding_location(needles, request_id, selector, target_binding)?;
    // A single-declarator var-decl target is matched per-declarator within the
    // contiguous window (so a target inside a multi-declarator owner is found).
    if selector_single_var_declarator(&needles[target_item_idx]).is_some() {
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
    // closed on the run-hole keyword); the categorical `resolve_member` still
    // rejects (empty → bail).
    if needles
        .iter()
        .any(|item| module_item_list_hole_name(item).is_some())
    {
        return Ok(Vec::new());
    }
    // A declarator-**hole** target inside a multi-statement window takes the same
    // general path (no single-declarator special-case): the whole-statement match
    // absorbs the DECLARATORS hole, and `declared_bindings[target_binding_idx]`
    // lines up because the hole declares nothing.
    let needle_facts = needles
        .iter()
        .map(|item| needle_item_facts(item, selector))
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_indices: Vec<_> = needle_facts
        .iter()
        .map(selector_match::Index::build)
        .collect();
    let starts = selector_match::match_fixed_window_sequence_indexed(
        &needle_indices,
        &chunk.body_indices,
        selector_mode(selector),
    )
    .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    for start in starts {
        let body_idx = start + target_item_idx;
        let Some(binding) = declared_bindings(&chunk.module.body[body_idx])
            .into_iter()
            .nth(target_binding_idx)
        else {
            bail!("datalog resolver: target binding index out of range");
        };
        matches.push(MemberBindingMatch { body_idx, binding });
    }
    Ok(matches)
}

/// Binding-group branch 2 — a single-statement declarator-hole needle (the
/// `*-module_*` group, e.g. tooltip's `const DECLARATORS_BEFORE = null, a = …,
/// DECLARATORS_GAP = null, b = …, DECLARATORS_AFTER = null;`): one plain
/// (un-prebound) declarator alignment per candidate owner yields every target's
/// declarator at once.
fn group_matches_declarator_holes(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<Vec<MemberBindingGroupMatch>> {
    let needle_var = item_var_decl(needle).ok_or_else(|| {
        anyhow::anyhow!(
            "datalog resolver: declarator-hole group needle is not a variable declaration"
        )
    })?;
    let target_locations: BTreeMap<String, (usize, usize)> = exports_by_target
        .keys()
        .map(|target| {
            selector_var_declarator_binding_location(needle_var, request_id, selector, target)
                .map(|location| (target.clone(), location))
        })
        .collect::<Result<_>>()?;
    let needle_facts = needle_item_facts(needle, selector)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let mode = selector_mode(selector);
    selector_match::var_declarator_alignment_indexed(&needle_index, &needle_index, mode, None)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
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
            None,
        ) else {
            continue;
        };
        let mut resolved = BTreeMap::new();
        for (target, (target_decl_idx, target_binding_idx)) in &target_locations {
            let Some(Some(candidate_decl_idx)) = alignment.get(*target_decl_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match target_binding \
                     `{target}` was matched by a DECLARATORS hole, not a pinned declarator"
                );
            };
            let Some(declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
                bail!(
                    "datalog resolver: target `{target}` aligned to a missing candidate declarator"
                );
            };
            let Some(binding) = declared_bindings_for_var_declarator(declarator)
                .into_iter()
                .nth(*target_binding_idx)
            else {
                bail!("datalog resolver: target `{target}` binding index out of range");
            };
            resolved.insert(target.clone(), MemberBindingMatch { body_idx, binding });
        }
        matches.push(MemberBindingGroupMatch { bindings: resolved });
    }
    Ok(matches)
}

/// Binding-group branch 1 — a single-statement single-declarator needle: match
/// the declarator across every owner's declarators (a target inside a
/// multi-declarator owner is found), reading each target's binding from the one
/// matched declarator.
fn group_matches_single_declarator(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<Vec<MemberBindingGroupMatch>> {
    let declared = declared_bindings(needle);
    let target_binding_indices: BTreeMap<String, usize> = exports_by_target
        .keys()
        .map(|target| {
            let indices: Vec<usize> = declared
                .iter()
                .enumerate()
                .filter_map(|(idx, binding)| (binding.binding_name == *target).then_some(idx))
                .collect();
            match indices.as_slice() {
                [single] => Ok((target.clone(), *single)),
                [] => bail!(
                    "logical_module {request_id}: binding_groups[].source_match target_binding \
                     `{target}` is not declared by the selector source"
                ),
                _ => bail!(
                    "logical_module {request_id}: binding_groups[].source_match target_binding \
                     `{target}` is ambiguous within the selector source"
                ),
            }
        })
        .collect::<Result<_>>()?;
    let needle_facts = needle_item_facts(needle, selector)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let init_prefilter = selector_match::needle_var_declarator_init_kind_prefilter(&needle_facts);
    let mode = selector_mode(selector);
    selector_match::matches_indexed(&needle_index, &needle_index, mode)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<MemberBindingGroupMatch> = Vec::new();
    // No prebind (the declarator match is un-prebound); an exact-mode needle may
    // require its identifier spellings.
    for subject_idx in chunk.candidate_declarators(&needle_index, mode, true) {
        let subject = &chunk.var_declarator_subjects[subject_idx];
        if let Some(kind) = init_prefilter
            && subject.init_kind != Some(kind)
        {
            continue;
        }
        // Needle probed-supported above; skip the per-candidate needle re-check.
        if !selector_match::matches_prepared(&needle_index, &subject.index, mode) {
            continue;
        }
        let item = &chunk.module.body[subject.body_idx];
        let declarator = &item_var_decl(item)
            .expect("cached var-declarator subject owner is a var-decl")
            .decls[subject.declarator_idx];
        let declarator_bindings = declared_bindings_for_var_declarator(declarator);
        let mut resolved = BTreeMap::new();
        for (target, target_binding_idx) in &target_binding_indices {
            let Some(binding) = declarator_bindings.get(*target_binding_idx) else {
                bail!("datalog resolver: target `{target}` binding index out of range");
            };
            resolved.insert(
                target.clone(),
                MemberBindingMatch {
                    body_idx: subject.body_idx,
                    binding: binding.clone(),
                },
            );
        }
        matches.push(MemberBindingGroupMatch { bindings: resolved });
    }
    Ok(matches)
}

/// Binding-group branch 3 — a general (multi-statement or non-var) needle: a
/// single top-level sequence alignment supplies every target's owner statement,
/// read by declared-binding index.
fn group_matches_general(
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
        .collect::<Result<_>>()?;
    let needle_facts = needles
        .iter()
        .map(|item| needle_item_facts(item, selector))
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_indices: Vec<_> = needle_facts
        .iter()
        .map(selector_match::Index::build)
        .collect();
    let alignments = selector_match::match_top_level_sequence_indexed(
        &needle_indices,
        &chunk.body_indices,
        selector_mode(selector),
    )
    .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches = Vec::new();
    for alignment in alignments {
        let mut resolved = BTreeMap::new();
        for (target, (target_item_idx, target_binding_idx)) in &target_locations {
            let Some(Some(body_idx)) = alignment.get(*target_item_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match target_binding \
                     `{target}` was matched by a STMT_LIST hole, not a pinned statement"
                );
            };
            let Some(binding) = declared_bindings(&chunk.module.body[*body_idx])
                .into_iter()
                .nth(*target_binding_idx)
            else {
                bail!("datalog resolver: target `{target}` binding index out of range");
            };
            resolved.insert(
                target.clone(),
                MemberBindingMatch {
                    body_idx: *body_idx,
                    binding,
                },
            );
        }
        matches.push(MemberBindingGroupMatch { bindings: resolved });
    }
    Ok(matches)
}

/// Owner-level categoricity for the per-owner group branches: exactly one owner
/// must match (production rejects zero or several).
fn one_group_match(
    mut matches: Vec<MemberBindingGroupMatch>,
    request_id: &str,
) -> Result<ResolvedMemberBindingGroup> {
    match matches.len() {
        1 => {
            let match_ = matches.remove(0);
            let body_idx = match_
                .bindings
                .values()
                .map(|binding| binding.body_idx)
                .min()
                .unwrap_or(0);
            let bindings = match_
                .bindings
                .into_iter()
                .map(|(target, matched)| (target, matched.binding))
                .collect();
            Ok(ResolvedMemberBindingGroup { body_idx, bindings })
        }
        0 => bail!(
            "logical_module {request_id}: binding_groups[].source_match did not match any owner"
        ),
        n => bail!(
            "logical_module {request_id}: binding_groups[].source_match is ambiguous — {n} owners"
        ),
    }
}

/// Collect every single-statement member match: scan the chunk body for
/// statements the needle matches, then read the claimed binding per matched item:
/// - **with `target_binding`** (a non-var-declarator needle): read
///   `declared_bindings[target_binding_idx]` per match, erroring only when that
///   index is out of range. Does **not** require a single declared binding.
/// - **without `target_binding`**: push the lone declared binding, skip a
///   statement that declares nothing, and bail when one declares more than one
///   (the categorical-position bail every candidate accessor preserves).
///
/// One `MemberBindingMatch` per matched statement, body-index ascending
/// (`matching_body_indices` returns the postings intersection in ascending order).
fn member_matches_single_statement(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_facts = needle_item_facts(needle, selector)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
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
            for body_idx in indices {
                let declared = declared_bindings(&chunk.module.body[body_idx]);
                let Some(binding) = declared.into_iter().nth(target_binding_idx) else {
                    bail!(
                        "logical_module {request_id}: members[].selector.source_match \
                         target_binding `{target_binding}` matched top-level statement at body \
                         index {body_idx}, but that statement declares too few bindings"
                    );
                };
                matches.push(MemberBindingMatch { body_idx, binding });
            }
        }
        None => {
            for body_idx in indices {
                let declared = declared_bindings(&chunk.module.body[body_idx]);
                match declared.as_slice() {
                    [single] => matches.push(MemberBindingMatch {
                        body_idx,
                        binding: single.clone(),
                    }),
                    [] => {}
                    multiple => bail!(
                        "logical_module {request_id}: members[].selector.source_match matched \
                         top-level statement at body index {body_idx}, but that statement \
                         declares {} bindings. Use a single-declarator selector or refine the \
                         match.",
                        multiple.len(),
                    ),
                }
            }
        }
    }
    Ok(matches)
}

impl ChunkResolver<'_> {
    fn collect_member_candidates(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<MemberBindingMatch>> {
        // Diagnostics-only: the matcher free fn takes no export name either.
        let export_name = "candidate";
        let needles = parse_needles(request_id, selector)?;
        let [needle] = needles.as_slice() else {
            return member_matches_multi(self, &needles, request_id, selector);
        };
        if selector_var_decl_has_declarator_holes(needle) {
            return member_matches_declarator_hole(self, needle, request_id, export_name, selector);
        }
        if selector_single_var_declarator(needle).is_some() {
            return member_matches_var_declarator(self, needle, request_id, export_name, selector);
        }
        member_matches_single_statement(self, needle, request_id, selector)
    }

    /// The non-categorical member match list. Shares the per-path match collection
    /// with [`SelectorResolver::resolve_member`]; the only difference is this
    /// returns the whole list (each carrying `body_idx` + binding) instead of
    /// collapsing to the unique winner. Used by cross-source consumers that
    /// aggregate matches before deciding uniqueness (the CLI edit gate).
    pub fn member_candidates(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<MemberBindingMatch>> {
        self.collect_member_candidates(request_id, selector)
    }

    fn member_candidates_with_timing(
        &self,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<MemberBindingMatch>> {
        let (matches, elapsed) =
            time_source_match(|| self.collect_member_candidates(request_id, selector));
        let matches = matches?;
        if source_match_timings_enabled() {
            let body_indices: Vec<usize> = matches.iter().map(|m| m.body_idx).collect();
            let binding = match matches.as_slice() {
                [single] => single.binding.binding_name.as_str(),
                _ => "<unresolved>",
            };
            emit_source_match_timing(
                &format!("members[].selector.source_match export=`{export_name}`"),
                request_id,
                selector,
                elapsed,
                &format!("body_indices={body_indices:?} binding={binding}"),
            );
        }
        Ok(matches)
    }

    fn member_group_candidates_impl(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<Vec<MemberBindingGroupMatch>> {
        if selector.target_binding.is_some() {
            bail!(
                "datalog resolver: binding-group selector for {request_id} unexpectedly has \
                 target_binding set"
            );
        }
        let needles = parse_needles(request_id, selector)?;
        let [first, ..] = needles.as_slice() else {
            bail!(
                "logical_module {request_id}: binding_groups[].source_match parsed to zero \
                 statements"
            );
        };
        // Branch order mirrors `resolve_member_group`: single-declarator before
        // declarator-holes, then the general sequence path.
        if needles.len() == 1 && selector_single_var_declarator(first).is_some() {
            return group_matches_single_declarator(
                self,
                first,
                request_id,
                selector,
                exports_by_target,
            );
        }
        if needles.len() == 1 && selector_var_decl_has_declarator_holes(first) {
            return group_matches_declarator_holes(
                self,
                first,
                request_id,
                selector,
                exports_by_target,
            );
        }
        group_matches_general(self, &needles, request_id, selector, exports_by_target)
    }

    fn collapse_member_match(
        &self,
        matches: Vec<MemberBindingMatch>,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
        selector_label: &'static str,
    ) -> Result<ResolvedMemberBinding> {
        let target_binding_hint = selector
            .target_binding
            .as_deref()
            .map(|target| format!(" target_binding `{target}`"))
            .unwrap_or_default();
        let match_source = &selector.match_source;
        match matches.as_slice() {
            [single] => Ok(single.binding.clone()),
            [] => {
                let hint =
                    fact_source_match_no_match_hint(self.module, selector).unwrap_or_default();
                bail!(
                    "logical_module {request_id}: {selector_label} for export `{export_name}`\
                     {target_binding_hint} did not match any top-level declaration in the chunk. \
                     Selector:\n{match_source}{hint}"
                )
            }
            multiple => bail!(
                "logical_module {request_id}: {selector_label} for export `{export_name}`\
                 {target_binding_hint} is ambiguous — matched {} top-level statements at body \
                 indices {:?} (bindings: {}). Refine the selector. Source:\n{match_source}",
                multiple.len(),
                multiple.iter().map(|m| m.body_idx).collect::<Vec<_>>(),
                multiple
                    .iter()
                    .map(|m| m.binding.binding_name.as_str())
                    .collect::<Vec<_>>()
                    .join(", "),
            ),
        }
    }
}

impl SelectorResolver for ChunkResolver<'_> {
    fn resolve_member_with_label(
        &self,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
        selector_label: &'static str,
    ) -> Result<ResolvedMemberBinding> {
        let needles = parse_needles(request_id, selector)?;
        let (matches, elapsed) = time_source_match(|| match needles.as_slice() {
            [needle] if selector_var_decl_has_declarator_holes(needle) => {
                // Declarator-hole var-decls need alignment-aware extraction: a
                // DECLARATORS run hole absorbs declarators, so the owner's binding
                // index no longer lines up with the needle's. Resolve via the
                // greedy-leftmost declarator alignment.
                member_matches_declarator_hole(self, needle, request_id, export_name, selector)
            }
            // A single-declarator var-decl resolves at declarator granularity (so a
            // match inside a multi-declarator owner is counted).
            [needle] if selector_single_var_declarator(needle).is_some() => {
                member_matches_var_declarator(self, needle, request_id, export_name, selector)
            }
            [needle] => member_matches_single_statement(self, needle, request_id, selector),
            _ => member_matches_multi(self, &needles, request_id, selector),
        });
        let matches = matches?;
        if source_match_timings_enabled() {
            let body_indices: Vec<usize> = matches.iter().map(|m| m.body_idx).collect();
            let binding = match matches.as_slice() {
                [single] => single.binding.binding_name.as_str(),
                _ => "<unresolved>",
            };
            emit_source_match_timing(
                &format!("{selector_label} export=`{export_name}`"),
                request_id,
                selector,
                elapsed,
                &format!("body_indices={body_indices:?} binding={binding}"),
            );
        }
        self.collapse_member_match(matches, request_id, export_name, selector, selector_label)
    }

    fn member_candidates(
        &self,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<MemberBindingMatch>> {
        self.member_candidates_with_timing(request_id, export_name, selector)
    }

    fn member_group_candidates(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<Vec<MemberBindingGroupMatch>> {
        self.member_group_candidates_impl(request_id, selector, exports_by_target)
    }

    fn resolve_member_group(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<ResolvedMemberBindingGroup> {
        if selector.target_binding.is_some() {
            bail!(
                "datalog resolver: binding-group selector for {request_id} unexpectedly has \
                 target_binding set"
            );
        }
        let needles = parse_needles(request_id, selector)?;
        let [first, ..] = needles.as_slice() else {
            bail!(
                "logical_module {request_id}: binding_groups[].source_match parsed to zero \
                 statements"
            );
        };
        // Branch order: single-declarator before declarator-holes (a lone hole
        // declarator is single-declarator too), then the general sequence path.
        if needles.len() == 1 && selector_single_var_declarator(first).is_some() {
            return one_group_match(
                group_matches_single_declarator(
                    self,
                    first,
                    request_id,
                    selector,
                    exports_by_target,
                )?,
                request_id,
            );
        }
        if needles.len() == 1 && selector_var_decl_has_declarator_holes(first) {
            return one_group_match(
                group_matches_declarator_holes(
                    self,
                    first,
                    request_id,
                    selector,
                    exports_by_target,
                )?,
                request_id,
            );
        }
        one_group_match(
            group_matches_general(self, &needles, request_id, selector, exports_by_target)?,
            request_id,
        )
    }

    fn resolve_anonymous_groups(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<Vec<usize>>> {
        let needles = parse_needles(request_id, selector)?;
        anonymous_selector_statement_indices(request_id, selector, &needles)?;
        let [needle] = needles.as_slice() else {
            unreachable!("anonymous selector validation requires one parsed statement")
        };
        let needle_facts = needle_item_facts(needle, selector)
            .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
        Ok(
            matching_body_indices(self, &needle_facts, selector_mode(selector))?
                .into_iter()
                .map(|body_idx| vec![body_idx])
                .collect(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn member(match_source: &str, target_binding: Option<&str>) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: target_binding.map(str::to_string),
            wildcard_string_literals: BTreeSet::new(),
        }
    }

    fn module(src: &str) -> Module {
        js_ast::parse_js_module_ast("<test>", src).unwrap()
    }

    fn group(match_source: &str) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: None,
            wildcard_string_literals: BTreeSet::new(),
        }
    }

    fn exports(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(target, export)| (target.to_string(), export.to_string()))
            .collect()
    }

    #[test]
    fn datalog_resolver_resolves_declarator_hole_group_like_production() {
        js_ast::with_swc_globals(|| {
            // The `*-module_*` binding-group shape: holes around several pinned,
            // string-predicate declarators; one alignment supplies every target.
            let chunk = module("const p = 1, aClass = \"abc\", bClass = \"xyz\", q = 2;\n");
            let selector = group(
                "const DECLARATORS_BEFORE = null, a = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
                 DECLARATORS_GAP = null, b = STR_LITERAL_MATCHING_RE(\"^xyz$\"), \
                 DECLARATORS_AFTER = null;",
            );
            let exports = exports(&[("a", "ExportA"), ("b", "ExportB")]);
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member_group("test", &selector, &exports)
                .expect("datalog resolves the group");
            assert_eq!(datalog.bindings["a"].binding_name, "aClass");
            assert_eq!(datalog.bindings["b"].binding_name, "bClass");
        });
    }

    #[test]
    fn datalog_resolver_resolves_general_group() {
        js_ast::with_swc_globals(|| {
            // A multi-statement (general-path) group: a leading anonymous statement
            // then two single-declarator targets, matched as a contiguous window.
            let chunk = module("init();\nconst alpha = makeA();\nconst beta = makeB();\n");
            let selector = group("init();\nconst a = makeA();\nconst b = makeB();");
            let exports = exports(&[("a", "ExportA"), ("b", "ExportB")]);
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member_group("test", &selector, &exports)
                .expect("datalog resolves the general group");
            assert_eq!(datalog.bindings["a"].binding_name, "alpha");
            assert_eq!(datalog.bindings["b"].binding_name, "beta");
        });
    }

    #[test]
    fn datalog_resolver_resolves_member() {
        js_ast::with_swc_globals(|| {
            let chunk = module("function alpha(n) { return n + 1; }\nconst beta = alpha(2);\n");
            // A function with a body the alpha selector matches structurally.
            let selector = member("function f(x) { return x + 1; }", Some("f"));
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "Alpha", &selector)
                .expect("datalog resolves the function");
            assert_eq!(datalog.binding_name, "alpha");
        });
    }

    #[test]
    fn datalog_resolver_resolves_declarator_inside_multi_declarator_owner() {
        js_ast::with_swc_globals(|| {
            // The target init lives in the second declarator of a multi-declarator
            // statement — only declarator-level matching finds it.
            let chunk = module("const a = 1, target = compute();\nconst other = 2;\n");
            let selector = member("const x = compute();", Some("x"));
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "X", &selector)
                .expect("datalog resolves the inner declarator");
            assert_eq!(datalog.binding_name, "target");
        });
    }

    #[test]
    fn datalog_resolver_var_declarator_categoricity_rejects_ambiguous() {
        js_ast::with_swc_globals(|| {
            // The same init appears in two declarators (one inside a
            // multi-declarator owner) — ambiguous. The resolver must count both
            // (declarator-level) and reject; a whole-statement match would miss the
            // multi-declarator one and spuriously resolve unique.
            let chunk = module("const a = 1, b = compute();\nconst c = compute();\n");
            let selector = member("const x = compute();", Some("x"));
            assert!(
                ChunkResolver::new(&chunk)
                    .resolve_member("test", "X", &selector)
                    .is_err(),
                "two declarators with the same init are ambiguous",
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_declarator_hole_member() {
        js_ast::with_swc_globals(|| {
            // A `DECLARATORS`-hole needle pins one declarator by a string-literal
            // predicate; the holes absorb the surrounding declarators. Only the
            // greedy-leftmost declarator alignment finds the target's owner.
            let chunk = module("const p = 1, theClass = \"abc\", q = 2;\nconst other = 5;\n");
            let selector = member(
                "const DECLARATORS_BEFORE = null, c = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
                 DECLARATORS_AFTER = null;",
                Some("c"),
            );
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "C", &selector)
                .expect("datalog resolves the declarator-hole target");
            assert_eq!(datalog.binding_name, "theClass");
        });
    }

    #[test]
    fn datalog_resolver_declarator_hole_categoricity_rejects_ambiguous() {
        js_ast::with_swc_globals(|| {
            // Two separate var-decl owners each match the hole needle → ambiguous
            // at the owner level. The resolver must reject (no spurious unique
            // resolution).
            let chunk = module("const a = \"abc\";\nconst b = \"abc\";\n");
            let selector = member(
                "const DECLARATORS_BEFORE = null, c = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
                 DECLARATORS_AFTER = null;",
                Some("c"),
            );
            assert!(
                ChunkResolver::new(&chunk)
                    .resolve_member("test", "C", &selector)
                    .is_err(),
                "two matching owners are ambiguous",
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_single_declarator_target_window() {
        js_ast::with_swc_globals(|| {
            // A contiguous two-statement window: a helper function then a
            // single-declarator var-decl target living inside a multi-declarator
            // owner. Only per-declarator matching within the window finds it.
            let chunk = module(
                "function helper(n) { return n + 1; }\n\
                 const lead = 0, theTarget = makeThing(), tail = 1;\n\
                 const other = 9;\n",
            );
            let selector = member(
                "function f(x) { return x + 1; }\nconst t = makeThing();",
                Some("t"),
            );
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "T", &selector)
                .expect("datalog resolves the windowed single-declarator target");
            assert_eq!(datalog.binding_name, "theTarget");
        });
    }

    #[test]
    fn datalog_resolver_single_declarator_target_window_categoricity_rejects_ambiguous() {
        js_ast::with_swc_globals(|| {
            // The window shape appears twice → ambiguous; the resolver rejects.
            let chunk = module(
                "function h1(n) { return n + 1; }\nconst a = makeThing();\n\
                 function h2(m) { return m + 1; }\nconst b = makeThing();\n",
            );
            let selector = member(
                "function f(x) { return x + 1; }\nconst t = makeThing();",
                Some("t"),
            );
            assert!(
                ChunkResolver::new(&chunk)
                    .resolve_member("test", "T", &selector)
                    .is_err(),
                "two matching windows are ambiguous",
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_multi_statement_declarator_hole_target() {
        js_ast::with_swc_globals(|| {
            // A two-statement window whose target item is a declarator-hole var-decl
            // with the pinned target declarator first (the corpus `const mR =
            // ANYTHING, DECLARATORS = null;` shape). The general multi-statement
            // path applies: the matcher absorbs the hole, and `declared_bindings[0]`
            // names the anchored-left target owner.
            let chunk = module(
                "function helper(n) { return n; }\n\
                 const theTarget = compute(), extra = 1, more = 2;\n",
            );
            let selector = member(
                "function f(x) { return x; }\nconst m = ANYTHING, DECLARATORS = null;",
                Some("m"),
            );
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "M", &selector)
                .expect("datalog resolves the windowed declarator-hole target");
            assert_eq!(datalog.binding_name, "theTarget");
        });
    }

    #[test]
    fn datalog_resolver_resolves_anonymous_statement() {
        js_ast::with_swc_globals(|| {
            let chunk = module("init();\nregister(widget);\nteardown();\n");
            let selector = member("register(ANYTHING);", None);
            let groups = ChunkResolver::new(&chunk)
                .resolve_anonymous_groups("test", &selector)
                .expect("datalog resolves the anonymous statement");
            // matches exactly the `register(widget);` statement at body index 1.
            assert_eq!(groups, vec![vec![1]]);
        });
    }

    fn exact_member(
        match_source: &str,
        target_binding: Option<&str>,
    ) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            identifiers: SourceMatchIdentifierMode::Exact,
            ..member(match_source, target_binding)
        }
    }

    // The exact-mode identifier candidate discriminator must stay a *sound*
    // prefilter: it may prune only declarators/statements that provably cannot
    // match. These tests exercise resolutions that go through the discriminator
    // (exact mode, identifier-bearing needles) and assert the correct owner is
    // still found — i.e. the discriminator never prunes a real match.

    #[test]
    fn datalog_resolver_exact_ident_discriminator_resolves_identifier_only_decl() {
        js_ast::with_swc_globals(|| {
            // The perf-corpus shape: identifier-only inits with no literal/prop token
            // to pin, in exact mode, so *only* the exact-mode identifier discriminator
            // narrows the declarator scan. The needle (binding `target` + referenced
            // `dep_b`) must reach exactly the matching declarator, not be pruned away.
            // (Exact mode compares the binding name too, so the needle names the real
            // binding, exactly as the source_match rewrite does.)
            let chunk = module(
                "const dep_a = base();\nconst dep_b = base();\n\
                 const target = wrap(dep_b);\nconst other = wrap(dep_a);\n",
            );
            let selector = exact_member("const target = wrap(dep_b);", Some("target"));
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "T", &selector)
                .expect("exact-mode identifier-only needle resolves");
            assert_eq!(datalog.binding_name, "target");
        });
    }

    #[test]
    fn datalog_resolver_exact_ident_discriminator_keeps_match_when_referenced_name_is_decisive() {
        js_ast::with_swc_globals(|| {
            // Two declarators share the binding name shape but differ only in the
            // referenced identifier; in exact mode the reference is compared
            // byte-for-byte. The discriminator must keep the declarator whose
            // reference matches (`dep_b`) and the resolution must be the unique one —
            // a wrongful prune would surface as a no-match, not a wrong owner.
            let chunk = module("const m = wrap(dep_a);\nconst m2 = wrap(dep_b);\n");
            let selector = exact_member("const m2 = wrap(dep_b);", Some("m2"));
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "M2", &selector)
                .expect("exact-mode needle resolves uniquely by referenced name");
            assert_eq!(datalog.binding_name, "m2");
        });
    }

    #[test]
    fn datalog_resolver_exact_ident_anonymous_statement_resolves() {
        js_ast::with_swc_globals(|| {
            // The no-prebind single-statement scan (anonymous) also uses the
            // exact-mode identifier discriminator: `register(widget)` must reach
            // the statement carrying both `register` and `widget`.
            let chunk = module("init();\nregister(other);\nregister(widget);\n");
            let selector = exact_member("register(widget);", None);
            let groups = ChunkResolver::new(&chunk)
                .resolve_anonymous_groups("test", &selector)
                .expect("exact-mode anonymous needle resolves");
            assert_eq!(groups, vec![vec![2]]);
        });
    }

    #[test]
    fn datalog_resolver_exact_declarator_hole_resolves_despite_target_rename() {
        js_ast::with_swc_globals(|| {
            // The declarator-hole path *prebinds* the target name, alpha-coupling it
            // to the candidate's binding — so the exact-mode discriminator must NOT
            // require the needle's target identifier (`c`) of the subject. The owner
            // binds `theClass`, not `c`; the prebind path passes
            // `allow_exact_ident = false`, so the match is still found.
            let chunk = module("const p = 1, theClass = \"abc\", q = 2;\nconst other = 5;\n");
            let selector = exact_member(
                "const DECLARATORS_BEFORE = null, c = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
                 DECLARATORS_AFTER = null;",
                Some("c"),
            );
            let datalog = ChunkResolver::new(&chunk)
                .resolve_member("test", "C", &selector)
                .expect("exact-mode declarator-hole needle resolves despite target rename");
            assert_eq!(datalog.binding_name, "theClass");
        });
    }
}
