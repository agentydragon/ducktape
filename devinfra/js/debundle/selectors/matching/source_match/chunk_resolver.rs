//! The shape matcher's per-chunk candidate generator (`ChunkResolver`): for a
//! parsed chunk and a JS-template selector, every top-level statement the
//! selector matches and the binding(s) it declares there. It uses the fact
//! matcher (`selector_match::matches` over `chunk_facts`) as the per-statement
//! match oracle, then extracts the claimed binding(s) with the shared
//! `declared_bindings` / `selector_binding_location` helpers. Materialization
//! projects the candidates into the selector IR, and the global solve picks one
//! target per selector (<docs/selector_resolution.md>). The per-statement match
//! semantics are pinned by `selector_match_test`.
//!
//! Fail-closed: a construct the matcher does not faithfully handle (an
//! `Unsupported` needle) surfaces as an error rather than a wrong claim — it never
//! under-resolves silently.

use super::*;

/// Facts for a single top-level statement. Extracts from the borrowed item (one
/// item being one root) — no per-call clone into a one-item `Module`, which on the
/// per-selector hot path was the resolver's main overhead over production.
pub(crate) fn item_facts(item: &ModuleItem) -> Option<chunk_facts::ChunkFacts> {
    chunk_facts::extract_facts_items(std::slice::from_ref(item)).ok()
}

/// Facts for a needle statement: one that does not project to facts is an error,
/// not an empty match set.
fn needle_item_facts(item: &ModuleItem) -> Result<chunk_facts::ChunkFacts> {
    item_facts(item)
        .ok_or_else(|| anyhow::anyhow!("source_match resolver: needle did not project to facts"))
}

fn needle_index(item: &ModuleItem) -> Result<selector_match::Index> {
    Ok(selector_match::Index::build(&needle_item_facts(item)?))
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
    /// never queries an `Ident` token, so its candidate set is unchanged. Regex
    /// predicates add no candidate tokens; needles without other tokens use the
    /// root-kind-prefiltered full scan before the matcher checks the full regex.
    body_tokens: std::collections::HashMap<selector_match::Token, Vec<usize>>,
    /// Like `body_tokens`, but keyed to indices into `var_declarator_subjects`:
    /// a single-declarator member/group needle only matches a declarator that
    /// carries its tokens, so the declarator scan visits just the rarest token's
    /// postings. Exact-mode `const NAME = …;` needles now prune by identifier
    /// spelling; regex predicates are checked by the full matcher after this
    /// candidate step.
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
    /// match — the intersection of every required token posting, or all
    /// declarators when the needle pins no candidate constraint. Mirrors
    /// [`Self::candidate_bodies`]
    /// for the declarator scan. `mode` / `allow_exact_ident` gate the exact-mode
    /// identifier discriminator (see [`selector_match::needle_required_tokens`]).
    fn candidate_declarators(
        &self,
        needle_index: &selector_match::Index,
        mode: selector_match::Mode,
        allow_exact_ident: bool,
    ) -> Vec<usize> {
        intersect_candidate_postings(
            &self.declarator_tokens,
            &selector_match::needle_required_tokens(needle_index, mode, allow_exact_ident),
            self.var_declarator_subjects.len(),
        )
    }

    /// Body indices a needle could match: the **intersection** of every required
    /// token's postings, or every body index when the needle pins no candidate
    /// constraint. Intersecting (not just taking the rarest list) is what shrinks
    /// the candidate set for needles whose constraints are individually common
    /// but jointly rare. `mode` / `allow_exact_ident` gate the exact-mode
    /// identifier discriminator (see [`selector_match::needle_required_tokens`]);
    /// a prebind path must pass `allow_exact_ident = false`.
    fn candidate_bodies(
        &self,
        needle_index: &selector_match::Index,
        mode: selector_match::Mode,
        allow_exact_ident: bool,
    ) -> Vec<usize> {
        intersect_candidate_postings(
            &self.body_tokens,
            &selector_match::needle_required_tokens(needle_index, mode, allow_exact_ident),
            self.body_indices.len(),
        )
    }
}

/// Intersect token postings into the candidate set. Empty constraints ⟹ scan
/// everything (`0..total`); an absent token ⟹ no candidate can match. Postings
/// are intersected smallest-first via binary search, so the cost is bounded by
/// the rarest constraint. Regex predicates are checked by the full matcher after
/// candidate generation, avoiding an independently interpreted regex prefilter.
fn intersect_candidate_postings(
    index: &std::collections::HashMap<selector_match::Token, Vec<usize>>,
    tokens: &[selector_match::Token],
    total: usize,
) -> Vec<usize> {
    let mut postings: Vec<&[usize]> = Vec::with_capacity(tokens.len());
    for token in tokens {
        match index.get(token) {
            Some(list) => postings.push(list),
            None => return Vec::new(),
        }
    }
    if postings.is_empty() {
        return (0..total).collect();
    }
    postings.sort_by_key(|list| list.len());
    let (rarest, rest) = postings.split_first().expect("postings is non-empty");
    let mut candidates = Vec::new();
    for candidate in rarest.iter().copied() {
        if rest
            .iter()
            .all(|list| list.binary_search(&candidate).is_ok())
        {
            candidates.push(candidate);
        }
    }
    candidates
}

/// Top-level body indices whose statement the needle matches under the fact
/// matcher, over the chunk's shared EDB, each with its free-identifier bindings.
/// Fails closed if the needle itself is `Unsupported`.
fn matching_body_indices(
    chunk: &ChunkResolver,
    needle_facts: &chunk_facts::ChunkFacts,
    mode: selector_match::Mode,
) -> Result<Vec<selector_match::Matched<usize>>> {
    // Build the needle index once; probe it (an unsupported construct errors
    // uniformly), then match it against each cached body index.
    let needle_index = selector_match::Index::build(needle_facts);
    let free = free_identifiers([&needle_index]);
    selector_match::matches_indexed(&needle_index, &needle_index, mode, &free)
        .map_err(unsupported_error("source_match resolver"))?;
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
        if let Some(free_bindings) = selector_match::matches_prepared(
            &needle_index,
            &chunk.body_indices[body_idx],
            mode,
            &free,
        ) {
            indices.push(selector_match::Matched {
                site: body_idx,
                free_bindings,
            });
        }
    }
    Ok(indices)
}

/// A synthetic single-declarator version of a var-decl `item` keeping only
/// `declarator`, cloned from the real item so span/context stay valid. Matching
/// the single-declarator needle against this *is* the per-declarator match: a
/// needle that matches one declarator of a multi-declarator owner is a real match
/// a whole-statement match would miss.
pub(super) fn single_declarator_item(item: &ModuleItem, declarator: &VarDeclarator) -> ModuleItem {
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
/// path). Returns one `MemberBindingMatch` per matched declarator, body-index
/// ascending (the cached `var_declarator_subjects` are built in body order).
fn member_matches_var_declarator(
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
        .map_err(unsupported_error("source_match resolver"))?;
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
                    "logical_module {request_id}: source_matches[].bindings local \
                     `{target_binding}` is not declared by the selector source"
                ),
                _ => bail!(
                    "logical_module {request_id}: source_matches[].bindings local \
                     `{target_binding}` is ambiguous within the selector source"
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
        let Some(free_bindings) =
            selector_match::matches_prepared(&needle_index, &subject.index, mode, &free)
        else {
            continue;
        };
        let item = &chunk.module.body[subject.body_idx];
        let declarator = &item_var_decl(item)
            .expect("cached var-declarator subject owner is a var-decl")
            .decls[subject.declarator_idx];
        let declared = declared_bindings_for_var_declarator(declarator);
        if selector.target_binding.is_none() && declared.len() != 1 {
            bail!(
                "logical_module {request_id}: source_match matched a declarator binding {} \
                 names; needs a single-binding declarator or source_matches[].bindings \
                 projection",
                declared.len(),
            );
        }
        let Some(binding) = declared.into_iter().nth(target_binding_idx) else {
            bail!(
                "source_match resolver: target binding index out of range for matched declarator"
            );
        };
        matches.push(MemberBindingMatch {
            body_idx: subject.body_idx,
            binding,
            free_bindings,
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
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let target_binding = selector.target_binding.as_deref().ok_or_else(|| {
        anyhow::anyhow!(
            "source_match resolver: declarator-hole member selector needs target_binding"
        )
    })?;
    let needle_var = item_var_decl(needle).ok_or_else(|| {
        anyhow::anyhow!(
            "source_match resolver: declarator-hole needle is not a variable declaration"
        )
    })?;
    let (target_decl_idx, target_binding_idx) =
        selector_var_declarator_binding_location(needle_var, request_id, selector, target_binding)?;
    let needle_index = needle_index(needle)?;
    let free = free_identifiers([&needle_index]);
    let mode = selector_mode(selector);
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
    .map_err(unsupported_error("source_match resolver"))?;
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
                    &[(target_binding, &candidate_binding)],
                    &free,
                )
            });
        let Some(alignment) = alignment else {
            continue;
        };
        let Some(Some(candidate_decl_idx)) = alignment.site.get(target_decl_idx) else {
            bail!(
                "logical_module {request_id}: source_matches[].bindings[`{target_binding}`] was \
                 matched by a DECLARATORS hole, not a pinned declarator"
            );
        };
        let Some(candidate_declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
            bail!(
                "source_match resolver: target binding aligned to a missing candidate declarator"
            );
        };
        let Some(binding) = declared_bindings_for_var_declarator(candidate_declarator)
            .into_iter()
            .nth(target_binding_idx)
        else {
            bail!(
                "source_match resolver: target binding index out of range for matched declarator"
            );
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
    .map_err(unsupported_error("source_match resolver"))?;
    let mut matches: Vec<MemberBindingMatch> = Vec::new();
    for window in windows {
        let (target_body_idx, subject_decl_idx) = window.site;
        let Some(candidate_var) = item_var_decl(&chunk.module.body[target_body_idx]) else {
            bail!("source_match resolver: matched window target is not a variable declaration");
        };
        let Some(declarator) = candidate_var.decls.get(subject_decl_idx) else {
            bail!("source_match resolver: matched declarator index out of range");
        };
        let Some(binding) = declared_bindings_for_var_declarator(declarator)
            .into_iter()
            .nth(target_binding_idx)
        else {
            bail!(
                "source_match resolver: target binding index out of range for matched declarator"
            );
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
fn member_matches_multi(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let target_binding = selector.target_binding.as_deref().ok_or_else(|| {
        anyhow::anyhow!(
            "source_match resolver: multi-statement member selector needs target_binding"
        )
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
    .map_err(unsupported_error("source_match resolver"))?;
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
            "source_match resolver: declarator-hole group needle is not a variable declaration"
        )
    })?;
    let target_locations: BTreeMap<String, (usize, usize)> = exports_by_target
        .keys()
        .map(|target| {
            selector_var_declarator_binding_location(needle_var, request_id, selector, target)
                .map(|location| (target.clone(), location))
        })
        .collect::<Result<_>>()?;
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
    .map_err(unsupported_error("source_match resolver"))?;
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
                bail!(
                    "logical_module {request_id}: source_matches[].bindings[`{target}`] was \
                     matched by a DECLARATORS hole, not a pinned declarator"
                );
            };
            let Some(declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
                bail!(
                    "source_match resolver: target `{target}` aligned to a missing candidate declarator"
                );
            };
            let Some(binding) = declared_bindings_for_var_declarator(declarator)
                .into_iter()
                .nth(*target_binding_idx)
            else {
                bail!("source_match resolver: target `{target}` binding index out of range");
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
                    "logical_module {request_id}: source_matches[].bindings[`{target}`] is not \
                     declared by the selector source"
                ),
                _ => bail!(
                    "logical_module {request_id}: source_matches[].bindings[`{target}`] is \
                     ambiguous within the selector source"
                ),
            }
        })
        .collect::<Result<_>>()?;
    let needle_facts = needle_item_facts(needle)?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let free = free_identifiers([&needle_index]);
    let init_prefilter = selector_match::needle_var_declarator_init_kind_prefilter(&needle_facts);
    let mode = selector_mode(selector);
    selector_match::matches_indexed(&needle_index, &needle_index, mode, &free)
        .map_err(unsupported_error("source_match resolver"))?;
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
        let Some(free_bindings) =
            selector_match::matches_prepared(&needle_index, &subject.index, mode, &free)
        else {
            continue;
        };
        let item = &chunk.module.body[subject.body_idx];
        let declarator = &item_var_decl(item)
            .expect("cached var-declarator subject owner is a var-decl")
            .decls[subject.declarator_idx];
        let declarator_bindings = declared_bindings_for_var_declarator(declarator);
        let mut resolved = BTreeMap::new();
        for (target, target_binding_idx) in &target_binding_indices {
            let Some(binding) = declarator_bindings.get(*target_binding_idx) else {
                bail!("source_match resolver: target `{target}` binding index out of range");
            };
            resolved.insert(
                target.clone(),
                MatchedBinding {
                    body_idx: subject.body_idx,
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
fn candidate_target_binding(
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
) -> Result<ResolvedMemberBinding> {
    let item = &chunk.module.body[body_idx];
    let (Some(needle_var), Some(candidate_var)) = (item_var_decl(needle), item_var_decl(item))
    else {
        return declared_bindings(item)
            .into_iter()
            .nth(target_binding_idx)
            .with_context(|| {
                format!(
                    "logical_module {request_id}: source_matches[].bindings[`{target}`] matched \
                     top-level statement at body index {body_idx}, but that statement declares \
                     too few bindings"
                )
            });
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
    .with_context(|| {
        format!(
            "logical_module {request_id}: source_matches[].bindings[`{target}`] matched body \
             index {body_idx}, but its declarators do not align with the selector's"
        )
    })?;
    let Some(Some(candidate_decl_idx)) = alignment.site.get(target_decl_idx) else {
        bail!(
            "logical_module {request_id}: source_matches[].bindings[`{target}`] was matched by a \
             DECLARATORS hole, not a pinned declarator"
        );
    };
    declared_bindings_for_var_declarator(&candidate_var.decls[*candidate_decl_idx])
        .into_iter()
        .nth(declarator_binding_idx)
        .with_context(|| {
            format!("source_match resolver: target `{target}` binding index out of range")
        })
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
    .map_err(unsupported_error("source_match resolver"))?;
    let mut matches = Vec::new();
    for alignment in alignments {
        let mut resolved = BTreeMap::new();
        for (target, (target_item_idx, target_binding_idx)) in &target_locations {
            let Some(Some(body_idx)) = alignment.site.get(*target_item_idx) else {
                bail!(
                    "logical_module {request_id}: source_matches[].bindings[`{target}`] was \
                     matched by a STMT_LIST hole, not a pinned statement"
                );
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
fn member_matches_single_statement(
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
                    multiple => bail!(
                        "logical_module {request_id}: source_matches[] matched \
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
    /// Every place a `source_match` member selector matches, each carrying its
    /// `body_idx` and claimed binding; the caller (the global selector solve, the
    /// minimizer's uniqueness measure) decides what the matches mean.
    pub fn member_candidates(
        &self,
        request_id: &str,
        parsed: &ParsedSourceMatchSelector,
    ) -> Result<Vec<MemberBindingMatch>> {
        let selector = parsed.selector();
        let needles = parsed.body();
        let [needle] = needles else {
            return member_matches_multi(self, needles, request_id, selector);
        };
        if selector_var_decl_has_declarator_holes(needle) {
            return member_matches_declarator_hole(self, needle, request_id, selector);
        }
        if selector_single_var_declarator(needle).is_some() {
            return member_matches_var_declarator(self, needle, request_id, selector);
        }
        member_matches_single_statement(self, needle, request_id, selector)
    }

    /// Every candidate alignment of a binding-group `source_match` selector,
    /// each carrying per-target body indices so the global selector solve can
    /// claim the actual owner of every exported binding.
    pub fn member_group_candidates(
        &self,
        request_id: &str,
        parsed: &ParsedSourceMatchSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<Vec<MemberBindingGroupMatch>> {
        let selector = parsed.selector();
        if selector.target_binding.is_some() {
            bail!(
                "source_match resolver: binding-group selector for {request_id} unexpectedly has \
                 target_binding set"
            );
        }
        let needles = parsed.body();
        let [first, ..] = needles else {
            bail!("logical_module {request_id}: source_matches[] parsed to zero statements");
        };
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
        group_matches_general(self, needles, request_id, selector, exports_by_target)
    }

    /// Candidate top-level body-index groups for an anonymous statement selector.
    ///
    /// The current public anonymous selector form validates to one parsed statement,
    /// so each group contains one body index. The grouped shape keeps this reusable
    /// if anonymous ranges grow a multi-statement form later.
    pub fn anonymous_group_candidates(
        &self,
        request_id: &str,
        parsed: &ParsedSourceMatchSelector,
    ) -> Result<Vec<AnonymousGroupMatch>> {
        let selector = parsed.selector();
        let needles = parsed.body();
        anonymous_selector_statement_indices(request_id, selector, needles)?;
        let [needle] = needles else {
            unreachable!("anonymous selector validation requires one parsed statement")
        };
        let needle_facts = needle_item_facts(needle)?;
        Ok(
            matching_body_indices(self, &needle_facts, selector_mode(selector))?
                .into_iter()
                .map(|matched| AnonymousGroupMatch {
                    body_indices: vec![matched.site],
                    free_bindings: matched.free_bindings,
                })
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
        }
    }

    fn exports(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(target, export)| (target.to_string(), export.to_string()))
            .collect()
    }

    fn parse(selector: &AnonymousStatementSelector) -> ParsedSourceMatchSelector {
        ParsedSourceMatchSelector::parse("test", "<test>".to_string(), selector, "source_match")
            .expect("selector parses")
    }

    /// The claimed binding of every member candidate, body-index ascending.
    fn member_names(chunk: &Module, selector: &AnonymousStatementSelector) -> Vec<String> {
        ChunkResolver::new(chunk)
            .member_candidates("test", &parse(selector))
            .expect("member candidates")
            .into_iter()
            .map(|matched| matched.binding.binding_name)
            .collect()
    }

    fn group_candidates(
        chunk: &Module,
        selector: &AnonymousStatementSelector,
        exports: &BTreeMap<String, String>,
    ) -> Vec<MemberBindingGroupMatch> {
        ChunkResolver::new(chunk)
            .member_group_candidates("test", &parse(selector), exports)
            .expect("group candidates")
    }

    fn anonymous_groups(
        chunk: &Module,
        selector: &AnonymousStatementSelector,
    ) -> Vec<AnonymousGroupMatch> {
        ChunkResolver::new(chunk)
            .anonymous_group_candidates("test", &parse(selector))
            .expect("anonymous candidates")
    }

    #[test]
    fn chunk_resolver_declarator_hole_group_candidate() {
        js_ast::with_swc_globals(|| {
            // The `*-module_*` binding-group shape: holes around several pinned,
            // string-predicate declarators; one alignment supplies every target.
            let chunk = module("const p = 1, aClass = \"abc\", bClass = \"xyz\", q = 2;\n");
            let selector = group(
                "const DECLARATORS_BEFORE = null, a = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
                 DECLARATORS_GAP = null, b = STR_LITERAL_MATCHING_RE(\"^xyz$\"), \
                 DECLARATORS_AFTER = null;",
            );
            let candidates = group_candidates(
                &chunk,
                &selector,
                &exports(&[("a", "ExportA"), ("b", "ExportB")]),
            );
            assert_eq!(candidates.len(), 1);
            assert_eq!(candidates[0].bindings["a"].binding.binding_name, "aClass");
            assert_eq!(candidates[0].bindings["b"].binding.binding_name, "bClass");
        });
    }

    #[test]
    fn chunk_resolver_group_referencing_a_renamed_target_by_chunk_name() {
        js_ast::with_swc_globals(|| {
            // `wrap` references the chunk's `q` by its chunk spelling while the
            // group renames that same declarator to `readable`.
            let chunk = module("const z = 0, w = () => use(q), q = () => 1;\n");
            let selector =
                group("const DECLARATORS_BEFORE = null, wrap = () => use(q), readable = () => 1;");
            let candidates = group_candidates(
                &chunk,
                &selector,
                &exports(&[("wrap", "Wrap"), ("readable", "Readable")]),
            );
            assert_eq!(candidates.len(), 1);
            assert_eq!(candidates[0].bindings["wrap"].binding.binding_name, "w");
            assert_eq!(candidates[0].bindings["readable"].binding.binding_name, "q");
            assert_eq!(
                candidates[0].free_bindings,
                BTreeMap::from([
                    ("q".to_string(), "q".to_string()),
                    ("use".to_string(), "use".to_string()),
                ])
            );
        });
    }

    #[test]
    fn chunk_resolver_general_group_candidate() {
        js_ast::with_swc_globals(|| {
            // A multi-statement (general-path) group: a leading anonymous statement
            // then two single-declarator targets, matched as a contiguous window.
            let chunk = module("init();\nconst alpha = makeA();\nconst beta = makeB();\n");
            let selector = group("init();\nconst a = makeA();\nconst b = makeB();");
            let candidates = group_candidates(
                &chunk,
                &selector,
                &exports(&[("a", "ExportA"), ("b", "ExportB")]),
            );
            assert_eq!(candidates.len(), 1);
            assert_eq!(candidates[0].bindings["a"].binding.binding_name, "alpha");
            assert_eq!(candidates[0].bindings["b"].binding.binding_name, "beta");
        });
    }

    #[test]
    fn chunk_resolver_member_candidate() {
        js_ast::with_swc_globals(|| {
            let chunk = module("function alpha(n) { return n + 1; }\nconst beta = alpha(2);\n");
            // A function with a body the alpha selector matches structurally.
            let selector = member("function f(x) { return x + 1; }", Some("f"));
            assert_eq!(member_names(&chunk, &selector), ["alpha"]);
        });
    }

    #[test]
    fn chunk_resolver_declarator_inside_multi_declarator_owner() {
        js_ast::with_swc_globals(|| {
            // The target init lives in the second declarator of a multi-declarator
            // statement — only declarator-level matching finds it.
            let chunk = module("const a = 1, target = compute();\nconst other = 2;\n");
            let selector = member("const x = compute();", Some("x"));
            assert_eq!(member_names(&chunk, &selector), ["target"]);
        });
    }

    #[test]
    fn chunk_resolver_var_declarator_candidates_include_multi_declarator_owners() {
        js_ast::with_swc_globals(|| {
            // The same init appears in two declarators (one inside a
            // multi-declarator owner): both are candidates. A whole-statement match
            // would miss the multi-declarator one and report a single candidate.
            let chunk = module("const a = 1, b = compute();\nconst c = compute();\n");
            let selector = member("const x = compute();", Some("x"));
            assert_eq!(member_names(&chunk, &selector), ["b", "c"]);
        });
    }

    #[test]
    fn chunk_resolver_declarator_hole_member_candidate() {
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
            assert_eq!(member_names(&chunk, &selector), ["theClass"]);
        });
    }

    #[test]
    fn chunk_resolver_declarator_hole_member_candidate_per_owner() {
        js_ast::with_swc_globals(|| {
            // Two separate var-decl owners each match the hole needle: each is a
            // candidate.
            let chunk = module("const a = \"abc\";\nconst b = \"abc\";\n");
            let selector = member(
                "const DECLARATORS_BEFORE = null, c = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
                 DECLARATORS_AFTER = null;",
                Some("c"),
            );
            assert_eq!(member_names(&chunk, &selector), ["a", "b"]);
        });
    }

    #[test]
    fn chunk_resolver_single_declarator_target_window() {
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
            assert_eq!(member_names(&chunk, &selector), ["theTarget"]);
        });
    }

    #[test]
    fn chunk_resolver_single_declarator_target_window_candidate_per_window() {
        js_ast::with_swc_globals(|| {
            // The window shape appears twice: each window is a candidate.
            let chunk = module(
                "function h1(n) { return n + 1; }\nconst a = makeThing();\n\
                 function h2(m) { return m + 1; }\nconst b = makeThing();\n",
            );
            let selector = member(
                "function f(x) { return x + 1; }\nconst t = makeThing();",
                Some("t"),
            );
            assert_eq!(member_names(&chunk, &selector), ["a", "b"]);
        });
    }

    #[test]
    fn chunk_resolver_multi_statement_declarator_hole_target() {
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
            assert_eq!(member_names(&chunk, &selector), ["theTarget"]);
        });
    }

    #[test]
    fn chunk_resolver_anonymous_statement_candidate() {
        js_ast::with_swc_globals(|| {
            let chunk = module("init();\nregister(widget);\nteardown();\n");
            let selector = member("register(ANYTHING);", None);
            // matches exactly the `register(widget);` statement at body index 1.
            assert_eq!(
                anonymous_groups(&chunk, &selector),
                vec![AnonymousGroupMatch {
                    body_indices: vec![1],
                    free_bindings: BTreeMap::from([(
                        "register".to_string(),
                        "register".to_string()
                    )]),
                }]
            );
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
    // match. These tests exercise candidate lists that go through the discriminator
    // (exact mode, identifier-bearing needles) and assert the correct owner is
    // still found — i.e. the discriminator never prunes a real match.

    #[test]
    fn chunk_resolver_exact_ident_discriminator_finds_identifier_only_decl() {
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
            assert_eq!(member_names(&chunk, &selector), ["target"]);
        });
    }

    #[test]
    fn chunk_resolver_exact_ident_discriminator_keeps_match_when_referenced_name_is_decisive() {
        js_ast::with_swc_globals(|| {
            // Two declarators share the binding name shape but differ only in the
            // referenced identifier; in exact mode the reference is compared
            // byte-for-byte. The discriminator must keep the declarator whose
            // reference matches (`dep_b`) — a wrongful prune would surface as no
            // candidate, not a wrong owner.
            let chunk = module("const m = wrap(dep_a);\nconst m2 = wrap(dep_b);\n");
            let selector = exact_member("const m2 = wrap(dep_b);", Some("m2"));
            assert_eq!(member_names(&chunk, &selector), ["m2"]);
        });
    }

    #[test]
    fn chunk_resolver_exact_ident_anonymous_statement_candidate() {
        js_ast::with_swc_globals(|| {
            // The no-prebind single-statement scan (anonymous) also uses the
            // exact-mode identifier discriminator: `register(widget)` must reach
            // the statement carrying both `register` and `widget`.
            let chunk = module("init();\nregister(other);\nregister(widget);\n");
            let selector = exact_member("register(widget);", None);
            let body_indices: Vec<Vec<usize>> = anonymous_groups(&chunk, &selector)
                .into_iter()
                .map(|group| group.body_indices)
                .collect();
            assert_eq!(body_indices, vec![vec![2]]);
        });
    }

    #[test]
    fn chunk_resolver_exact_declarator_hole_candidate_despite_target_rename() {
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
            assert_eq!(member_names(&chunk, &selector), ["theClass"]);
        });
    }
}
