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
type Result<T> = std::result::Result<T, MatcherError>;

mod group;
mod member;
#[cfg(test)]
mod tests;

use group::*;
use member::*;

/// Facts for a single top-level statement. Extracts from the borrowed item (one
/// item being one root) — no per-call clone into a one-item `Module`, which on the
/// per-selector hot path was the resolver's main overhead over production.
pub fn item_facts(item: &ModuleItem) -> Option<chunk_facts::ChunkFacts> {
    chunk_facts::extract_facts_items(std::slice::from_ref(item)).ok()
}

/// Facts for a needle statement: one that does not project to facts is an error,
/// not an empty match set.
fn needle_item_facts(item: &ModuleItem) -> Result<chunk_facts::ChunkFacts> {
    chunk_facts::extract_facts_items(std::slice::from_ref(item))
        .map_err(|source| MatcherError::Invalid(SourceMatchError::Facts { source }))
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
    /// var-decl source_unit, built once. The single-declarator var-decl member path
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

/// A var-decl source_unit's single declarator, projected once to a synthetic
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

struct DeclaratorMatch {
    body_idx: usize,
    declared: Vec<String>,
    free_bindings: BTreeMap<String, String>,
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
                if let Ok(facts) = chunk_facts::extract_single_declarator_facts(item, declarator) {
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

    /// Match a supported needle against individual declarators in source order.
    /// There is no prebind on this path, so exact mode can filter on identifier
    /// spellings before the full match.
    fn matching_declarators(
        &self,
        needle: &selector_match::Index,
        mode: selector_match::Mode,
        free: &BTreeSet<String>,
        init_kind: Option<&'static str>,
    ) -> impl Iterator<Item = DeclaratorMatch> {
        let candidates = intersect_candidate_postings(
            &self.declarator_tokens,
            &selector_match::needle_required_tokens(needle, mode, true),
            self.var_declarator_subjects.len(),
        );
        candidates.into_iter().filter_map(move |index| {
            let subject = &self.var_declarator_subjects[index];
            if init_kind.is_some() && subject.init_kind != init_kind {
                return None;
            }
            let free_bindings =
                selector_match::matches_prepared(needle, &subject.index, mode, free)?;
            let declarator = &item_var_decl(&self.module.body[subject.body_idx])
                .expect("cached var-declarator subject source_unit is a var-decl")
                .decls[subject.declarator_idx];
            Some(DeclaratorMatch {
                body_idx: subject.body_idx,
                declared: declared_bindings_for_var_declarator(declarator),
                free_bindings,
            })
        })
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
        .map_err(unsupported_error)?;
    // Sound root-kind prefilter: when the needle root is a concrete kind, a
    // subject whose root kind differs is a guaranteed non-match (the `nkind !=
    // subject kind` gate in the matcher), so skip it without matching.
    let prefilter = selector_match::needle_root_kind_prefilter(needle_facts);
    let mut indices = Vec::new();
    // Token index narrows the scan to statements that carry the needle's required
    // tokens (a sound superset); the root-kind prefilter and full match then
    // filter. This path matches without a prebind, so an exact-mode needle may also
    // require its identifier spellings (`allow_exact_ident = true`). A non-extractable
    // statement projects to empty facts (no root, no tokens) and so matches nothing.
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
/// needle that matches one declarator of a multi-declarator source_unit is a real match
/// a whole-statement match would miss.
pub fn single_declarator_item(item: &ModuleItem, declarator: &VarDeclarator) -> ModuleItem {
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

impl ChunkResolver<'_> {
    /// Candidate free-name mappings for a template that claims a binding by
    /// its use site. A single-declarator needle must match each candidate
    /// declarator, even when its source statement declares other bindings.
    /// Anonymous statement selectors retain whole-statement semantics.
    pub fn free_reference_candidates(
        &self,
        request_id: &str,
        parsed: &ParsedSourceMatchSelector,
    ) -> Result<Vec<BTreeMap<String, String>>> {
        if let [needle] = parsed.body()
            && selector_has_single_var_declarator(needle)
        {
            let declared = parsed.declared_binding_names();
            if !declared.is_empty() {
                let targets = declared
                    .into_iter()
                    .map(|name| (name.clone(), name))
                    .collect();
                return group_matches_single_declarator(
                    self,
                    needle,
                    request_id,
                    parsed.selector(),
                    &targets,
                )
                .map(|candidates| {
                    candidates
                        .into_iter()
                        .map(|candidate| candidate.free_bindings)
                        .collect()
                });
            }
        }
        self.anonymous_group_candidates(request_id, parsed)
            .map(|candidates| {
                candidates
                    .into_iter()
                    .map(|candidate| candidate.free_bindings)
                    .collect()
            })
    }

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
        if selector_has_single_var_declarator(needle) {
            return member_matches_var_declarator(self, needle, request_id, selector);
        }
        member_matches_single_statement(self, needle, request_id, selector)
    }

    /// Every candidate alignment of a binding-group `source_match` selector,
    /// each carrying per-target body indices so the global selector solve can
    /// claim the actual source_unit of every exported binding.
    pub fn member_group_candidates(
        &self,
        request_id: &str,
        parsed: &ParsedSourceMatchSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<Vec<MemberBindingGroupMatch>> {
        let selector = parsed.selector();
        if selector.target_binding.is_some() {
            return Err(MatcherError::invariant(
                MatcherInvariant::UnexpectedGroupTarget,
            ));
        }
        let needles = parsed.body();
        let [first, ..] = needles else {
            return Err(SourceMatchError::claim(request_id, ClaimProblem::EmptySource).into());
        };
        // A sole declared claim keeps the member matcher's contextual
        // declarator projection. Its fixed-window path also preserves the
        // existing candidate set; a STMT_LIST hole needs group alignment.
        if exports_by_target.len() == 1
            && !needles
                .iter()
                .any(|item| module_item_list_hole_name(item).is_some())
        {
            let target = exports_by_target
                .keys()
                .next()
                .expect("single target was checked");
            let member = parsed.with_target_binding(Some(target.clone()));
            return Ok(self
                .member_candidates(request_id, &member)?
                .into_iter()
                .map(|matched| MemberBindingGroupMatch {
                    bindings: BTreeMap::from([(
                        target.clone(),
                        MatchedBinding {
                            body_idx: matched.body_idx,
                            binding: matched.binding,
                        },
                    )]),
                    free_bindings: matched.free_bindings,
                })
                .collect());
        }
        if needles.len() == 1 && selector_has_single_var_declarator(first) {
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
    /// A multi-statement `source_match` uses a contiguous context window; its
    /// final body index is the only statement the caller claims.
    pub fn anonymous_group_candidates(
        &self,
        request_id: &str,
        parsed: &ParsedSourceMatchSelector,
    ) -> Result<Vec<AnonymousGroupMatch>> {
        let selector = parsed.selector();
        let needles = parsed.body();
        let target = anonymous_selector_target_index(request_id, selector, needles)?;
        if target == 0 {
            let needle_facts = needle_item_facts(&needles[0])?;
            return Ok(
                matching_body_indices(self, &needle_facts, selector_mode(selector))?
                    .into_iter()
                    .map(|matched| AnonymousGroupMatch {
                        body_indices: vec![matched.site],
                        free_bindings: matched.free_bindings,
                    })
                    .collect(),
            );
        }
        let needle_indices = needles
            .iter()
            .map(needle_index)
            .collect::<Result<Vec<_>>>()?;
        let free = free_identifiers(&needle_indices);
        Ok(selector_match::match_fixed_window_sequence_indexed(
            &needle_indices,
            &self.body_indices,
            selector_mode(selector),
            &free,
        )
        .map_err(unsupported_error)?
        .into_iter()
        .map(|matched| AnonymousGroupMatch {
            body_indices: (matched.site..=matched.site + target).collect(),
            free_bindings: matched.free_bindings,
        })
        .collect())
    }
}
