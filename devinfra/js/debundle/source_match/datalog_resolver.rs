//! The Datalog-side `SelectorResolver`: resolve a selector to its claimed
//! owner/binding using the fact-based matcher (`selector_match::matches` over
//! `chunk_facts`) as the per-statement match oracle, then reuse the **same**
//! production binding-extraction (`declared_bindings`, `selector_binding_location`)
//! that `AstWildcardResolver` uses. Only the match decision is swapped, so wherever
//! both resolvers handle a selector they agree by construction with the matcher
//! differential (`selector_match_differential_test`, `corpus_match_differential`)
//! that already proves the per-statement verdicts equal.
//!
//! Fail-closed: a construct this resolver does not yet handle (a multi-statement
//! needle, a var-declarator/declarator-hole target, a binding group, an
//! `Unsupported` needle) returns an error rather than a wrong claim — it never
//! under-resolves silently. `DifferentialResolver<AstWildcard, Datalog>` compares
//! the two; a fail-closed error is only agreement when production also rejects.

use super::*;

use swc_common::DUMMY_SP;

/// The fact-based resolver. See module docs.
pub struct DatalogResolver;

fn datalog_mode(selector: &AnonymousStatementSelector) -> selector_match::Mode {
    match selector.identifiers {
        SourceMatchIdentifierMode::Exact => selector_match::Mode::Exact,
        SourceMatchIdentifierMode::AlphaAll => selector_match::Mode::AlphaAll,
    }
}

/// Facts for a single top-level statement (wrapped in a one-item module so the
/// extractor's owner-ordinal join and the matcher's root anchoring see one root).
fn item_facts(item: &ModuleItem) -> Option<chunk_facts::ChunkFacts> {
    let module = Module {
        span: DUMMY_SP,
        body: vec![item.clone()],
        shebang: None,
    };
    chunk_facts::extract_facts(&module).ok()
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
        let body_indices = body_facts
            .iter()
            .map(selector_match::Index::build)
            .collect();
        let mut var_declarator_subjects = Vec::new();
        for (body_idx, item) in module.body.iter().enumerate() {
            let Some(var) = item_var_decl(item) else {
                continue;
            };
            for (declarator_idx, declarator) in var.decls.iter().enumerate() {
                if let Some(facts) = item_facts(&single_declarator_item(item, declarator)) {
                    var_declarator_subjects.push(VarDeclaratorSubject {
                        body_idx,
                        declarator_idx,
                        init_kind: selector_match::var_declarator_init_kind(&facts),
                        index: selector_match::Index::build(&facts),
                    });
                }
            }
        }
        Self {
            module,
            body_root_kinds,
            body_indices,
            var_declarator_subjects,
        }
    }
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
    // A non-extractable statement projects to empty facts (no root) and so
    // matches nothing — the same outcome as the old skip; any divergence would
    // surface in the differential.
    for (body_idx, subject_index) in chunk.body_indices.iter().enumerate() {
        if let Some(kind) = prefilter
            && chunk.body_root_kinds[body_idx] != Some(kind)
        {
            continue;
        }
        if selector_match::matches_indexed(&needle_index, subject_index, mode)
            .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?
        {
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

/// Resolve a single-declarator var-decl member selector by matching its
/// declarator against every declarator of every var-decl owner (production's
/// per-declarator path), so the match count — hence categoricity — is faithful.
fn resolve_var_declarator_member(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<ResolvedMemberBinding> {
    let needle_facts = item_facts(needle)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let mode = datalog_mode(selector);
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
    // Match the needle against each var-decl owner's declarator via the cached
    // synthetic single-declarator indices (built once for the chunk), so this
    // scan is index-build-free. A sound init-kind prefilter skips declarators
    // whose initializer kind cannot match the needle's, without a full match.
    let needle_index = selector_match::Index::build(&needle_facts);
    let init_prefilter = selector_match::needle_var_declarator_init_kind_prefilter(&needle_facts);
    let mut matches: Vec<ResolvedMemberBinding> = Vec::new();
    for subject in &chunk.var_declarator_subjects {
        if let Some(kind) = init_prefilter
            && subject.init_kind != Some(kind)
        {
            continue;
        }
        if !selector_match::matches_indexed(&needle_index, &subject.index, mode)
            .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?
        {
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
        matches.push(binding);
    }
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` did not match any declarator in the chunk"
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` is ambiguous — matched {} declarators",
            multiple.len(),
        ),
    }
}

/// Resolve a declarator-hole member selector (`const DECLARATORS, x = …,
/// DECLARATORS;` with a `target_binding`): for each var-decl owner in the chunk,
/// try each candidate binding name at the target's declarator position —
/// prebinding it so the alpha bijection pins the target's identity — and take
/// the greedy-leftmost declarator alignment. `alignment[target_decl_idx]` names
/// the subject declarator the target pinned, whose binding is the resolved
/// owner. Categoricity is at the owner level (one matched owner = unique).
/// Mirrors production's `find_matching_target_var_decl_with_declarator_holes`.
fn resolve_declarator_hole_member(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<ResolvedMemberBinding> {
    let target_binding = selector.target_binding.as_deref().ok_or_else(|| {
        anyhow::anyhow!("datalog resolver: declarator-hole member selector needs target_binding")
    })?;
    let needle_var = item_var_decl(needle).ok_or_else(|| {
        anyhow::anyhow!("datalog resolver: declarator-hole needle is not a variable declaration")
    })?;
    let (target_decl_idx, target_binding_idx) =
        selector_var_declarator_binding_location(needle_var, request_id, selector, target_binding)?;
    let needle_facts = item_facts(needle)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let mode = datalog_mode(selector);
    // Probe the needle once: an unsupported construct errors uniformly (and makes
    // the per-subject `.expect` below sound — the only `Unsupported` source is the
    // needle construct, invariant across subjects and prebindings).
    selector_match::var_declarator_alignment_indexed(&needle_index, &needle_index, mode, None)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<ResolvedMemberBinding> = Vec::new();
    for (body_idx, item) in chunk.module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        // Reuse the cached body index across the candidate-binding inner loop:
        // every alignment attempt for this owner shares one prebuilt subject index.
        let subject_index = &chunk.body_indices[body_idx];
        let alignment = target_binding_candidate_names(candidate_var, target_binding_idx)
            .into_iter()
            .find_map(|candidate_binding| {
                selector_match::var_declarator_alignment_indexed(
                    &needle_index,
                    subject_index,
                    mode,
                    Some((target_binding, &candidate_binding)),
                )
                .expect("needle construct already probed as supported")
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
        matches.push(binding);
    }
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` did not match any declarator-hole variable-declaration owner"
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` is ambiguous — matched {} owners",
            multiple.len(),
        ),
    }
}

/// Parse a selector's `match_source` to its top-level statements (any count).
fn parse_needles(
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<ModuleItem>> {
    Ok(js_ast::parse_js_module_ast(
        &format!("<datalog needle in {request_id}>"),
        &selector.match_source,
    )?
    .body)
}

/// Multi-statement member whose target item is a single-declarator var-decl:
/// resolve it per-declarator across contiguous windows (the target may live in a
/// declarator of a multi-declarator owner), reading the matched declarator's
/// binding at `target_binding_idx`. Owner-level categoricity: exactly one
/// match. Mirrors `find_matching_target_binding_ranges_with_single_declarator`.
fn resolve_single_declarator_target_window(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
    target_item_idx: usize,
    target_binding_idx: usize,
) -> Result<ResolvedMemberBinding> {
    let needle_facts = needles
        .iter()
        .map(item_facts)
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
        datalog_mode(selector),
    )
    .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<ResolvedMemberBinding> = Vec::new();
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
        matches.push(binding);
    }
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` did not match any single-declarator target window"
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` is ambiguous — {} single-declarator target windows",
            multiple.len(),
        ),
    }
}

/// Multi-statement member: align the needle statements against the chunk body as
/// a fixed contiguous window, then read the target binding from the body
/// statement at the window's target offset. Mirrors production's member path
/// (`find_matching_target_bindings` → `find_matching_body_ranges`), which is
/// fixed-window — **not** the gapped module-level `STMT_LIST` subsequence the
/// anonymous/group paths use.
fn resolve_member_multi(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<ResolvedMemberBinding> {
    let target_binding = selector.target_binding.as_deref().ok_or_else(|| {
        anyhow::anyhow!("datalog resolver: multi-statement member selector needs target_binding")
    })?;
    let (target_item_idx, target_binding_idx) =
        selector_binding_location(needles, request_id, selector, target_binding)?;
    // A single-declarator var-decl target is matched per-declarator within the
    // contiguous window (so a target inside a multi-declarator owner is found),
    // mirroring production's `find_matching_target_binding_ranges_with_single_declarator`.
    if selector_single_var_declarator(&needles[target_item_idx]).is_some() {
        return resolve_single_declarator_target_window(
            chunk,
            needles,
            request_id,
            export_name,
            selector,
            target_item_idx,
            target_binding_idx,
        );
    }
    // A declarator-**hole** target inside a multi-statement window takes the same
    // general path: production does not special-case it (`find_matching_target_bindings`
    // only special-cases a single-declarator target), so the whole-statement match
    // plus `declared_bindings[target_binding_idx]` is exactly production's behavior —
    // the matcher already absorbs the DECLARATORS hole, and the declared-binding
    // index lines up because the hole declares nothing.
    let needle_facts = needles
        .iter()
        .map(item_facts)
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_indices: Vec<_> = needle_facts
        .iter()
        .map(selector_match::Index::build)
        .collect();
    let starts = selector_match::match_fixed_window_sequence_indexed(
        &needle_indices,
        &chunk.body_indices,
        datalog_mode(selector),
    )
    .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<ResolvedMemberBinding> = Vec::new();
    for start in starts {
        let body_idx = start + target_item_idx;
        let Some(binding) = declared_bindings(&chunk.module.body[body_idx])
            .into_iter()
            .nth(target_binding_idx)
        else {
            bail!("datalog resolver: target binding index out of range");
        };
        matches.push(binding);
    }
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` did not match any top-level statement range"
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match for export \
             `{export_name}` is ambiguous — {} ranges",
            multiple.len(),
        ),
    }
}

/// Multi-statement anonymous selector: align the needle statements against the
/// chunk body, then project each alignment onto the selector's target-statement
/// indices (mirrors `find_anonymous_statement_body_index_groups`).
fn resolve_anonymous_multi(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<Vec<usize>>> {
    let target_indices =
        anonymous_selector_target_statement_indices(request_id, selector, needles)?;
    let needle_facts = needles
        .iter()
        .map(item_facts)
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_indices: Vec<_> = needle_facts
        .iter()
        .map(selector_match::Index::build)
        .collect();
    let alignments = selector_match::match_top_level_sequence_indexed(
        &needle_indices,
        &chunk.body_indices,
        datalog_mode(selector),
    )
    .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut groups = Vec::new();
    for alignment in alignments {
        let mut group = Vec::with_capacity(target_indices.len());
        for &target_idx in &target_indices {
            let Some(Some(body_idx)) = alignment.get(target_idx) else {
                bail!(
                    "datalog resolver: anonymous target statement {target_idx} matched by a \
                     STMT_LIST hole"
                );
            };
            group.push(*body_idx);
        }
        groups.push(group);
    }
    Ok(groups)
}

/// Binding-group branch 2 — a single-statement declarator-hole needle (the
/// `*-module_*` group, e.g. tooltip's `const DECLARATORS_BEFORE = null, a = …,
/// DECLARATORS_GAP = null, b = …, DECLARATORS_AFTER = null;`): one plain
/// (un-prebound) declarator alignment per candidate owner yields every target's
/// declarator at once. Mirrors `resolve_member_binding_group_with_declarator_holes`.
fn resolve_group_declarator_holes(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<ResolvedMemberBindingGroup> {
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
    let needle_facts = item_facts(needle)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let mode = datalog_mode(selector);
    selector_match::var_declarator_alignment_indexed(&needle_index, &needle_index, mode, None)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<(usize, BTreeMap<String, ResolvedMemberBinding>)> = Vec::new();
    for (body_idx, item) in chunk.module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        let Some(alignment) = selector_match::var_declarator_alignment_indexed(
            &needle_index,
            &chunk.body_indices[body_idx],
            mode,
            None,
        )
        .expect("needle construct already probed as supported") else {
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
            resolved.insert(target.clone(), binding);
        }
        matches.push((body_idx, resolved));
    }
    one_group_match(matches, request_id)
}

/// Binding-group branch 1 — a single-statement single-declarator needle: match
/// the declarator across every owner's declarators (a target inside a
/// multi-declarator owner is found), reading each target's binding from the one
/// matched declarator. Mirrors the single-declarator branch of
/// `resolve_member_binding_group_impl`.
fn resolve_group_single_declarator(
    chunk: &ChunkResolver,
    needle: &ModuleItem,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<ResolvedMemberBindingGroup> {
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
    let needle_facts = item_facts(needle)
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_index = selector_match::Index::build(&needle_facts);
    let init_prefilter = selector_match::needle_var_declarator_init_kind_prefilter(&needle_facts);
    let mode = datalog_mode(selector);
    selector_match::matches_indexed(&needle_index, &needle_index, mode)
        .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let mut matches: Vec<(usize, BTreeMap<String, ResolvedMemberBinding>)> = Vec::new();
    for subject in &chunk.var_declarator_subjects {
        if let Some(kind) = init_prefilter
            && subject.init_kind != Some(kind)
        {
            continue;
        }
        if !selector_match::matches_indexed(&needle_index, &subject.index, mode)
            .expect("needle construct already probed as supported")
        {
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
            resolved.insert(target.clone(), binding.clone());
        }
        matches.push((subject.body_idx, resolved));
    }
    one_group_match(matches, request_id)
}

/// Binding-group branch 3 — a general (multi-statement or non-var) needle: a
/// single top-level sequence alignment supplies every target's owner statement,
/// read by declared-binding index. Mirrors the `find_matching_body_group_alignments`
/// branch of `resolve_member_binding_group_impl`.
fn resolve_group_general(
    chunk: &ChunkResolver,
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<ResolvedMemberBindingGroup> {
    let target_locations: BTreeMap<String, (usize, usize)> = exports_by_target
        .keys()
        .map(|target| {
            selector_binding_location(needles, request_id, selector, target)
                .map(|location| (target.clone(), location))
        })
        .collect::<Result<_>>()?;
    let needle_facts = needles
        .iter()
        .map(item_facts)
        .collect::<Option<Vec<_>>>()
        .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
    let needle_indices: Vec<_> = needle_facts
        .iter()
        .map(selector_match::Index::build)
        .collect();
    let alignments = selector_match::match_top_level_sequence_indexed(
        &needle_indices,
        &chunk.body_indices,
        datalog_mode(selector),
    )
    .map_err(|unsupported| anyhow::anyhow!("datalog resolver: {}", unsupported.reason))?;
    let alignment = match alignments.as_slice() {
        [single] => single,
        [] => bail!(
            "logical_module {request_id}: binding_groups[].source_match did not match any \
             top-level declaration range"
        ),
        multiple => bail!(
            "logical_module {request_id}: binding_groups[].source_match is ambiguous — {} ranges",
            multiple.len(),
        ),
    };
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
        resolved.insert(target.clone(), binding);
    }
    Ok(ResolvedMemberBindingGroup {
        body_idx: alignment.iter().flatten().copied().next().unwrap_or(0),
        bindings: resolved,
    })
}

/// Owner-level categoricity for the per-owner group branches: exactly one owner
/// must match (production rejects zero or several).
fn one_group_match(
    mut matches: Vec<(usize, BTreeMap<String, ResolvedMemberBinding>)>,
    request_id: &str,
) -> Result<ResolvedMemberBindingGroup> {
    match matches.len() {
        1 => {
            let (body_idx, bindings) = matches.remove(0);
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

impl ChunkResolver<'_> {
    pub fn resolve_member(
        &self,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<ResolvedMemberBinding> {
        let needles = parse_needles(request_id, selector)?;
        let [needle] = needles.as_slice() else {
            return resolve_member_multi(self, &needles, request_id, export_name, selector);
        };
        // Declarator-hole var-decls need alignment-aware extraction: a DECLARATORS
        // run hole absorbs declarators, so the owner's binding index no longer
        // lines up with the needle's. Resolve via the greedy-leftmost declarator
        // alignment (production's `find_matching_target_var_decl_with_declarator_holes`).
        if selector_var_decl_has_declarator_holes(needle) {
            return resolve_declarator_hole_member(self, needle, request_id, export_name, selector);
        }
        // A single-declarator var-decl resolves at declarator granularity (so a
        // match inside a multi-declarator owner is counted) — production's path.
        if selector_single_var_declarator(needle).is_some() {
            return resolve_var_declarator_member(self, needle, request_id, export_name, selector);
        }
        let target_binding_idx = match &selector.target_binding {
            Some(target_binding) => {
                let (target_item_idx, binding_idx) = selector_binding_location(
                    std::slice::from_ref(needle),
                    request_id,
                    selector,
                    target_binding,
                )?;
                debug_assert_eq!(target_item_idx, 0, "single-statement needle");
                binding_idx
            }
            None => 0,
        };
        let needle_facts = item_facts(needle)
            .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
        let indices = matching_body_indices(self, &needle_facts, datalog_mode(selector))?;
        let [body_idx] = indices.as_slice() else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match for export \
                 `{export_name}` resolved to {} top-level statements; expected exactly one",
                indices.len(),
            );
        };
        let declared = declared_bindings(&self.module.body[*body_idx]);
        if selector.target_binding.is_none() && declared.len() != 1 {
            bail!(
                "datalog resolver: export `{export_name}` matched a statement declaring {} \
                 bindings; needs a single-declarator selector or target_binding",
                declared.len(),
            );
        }
        declared
            .into_iter()
            .nth(target_binding_idx)
            .ok_or_else(|| anyhow::anyhow!("datalog resolver: target binding index out of range"))
    }

    pub fn resolve_member_group(
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
        // Branch order mirrors `resolve_member_binding_group_impl`: single-declarator
        // before declarator-holes (a lone hole declarator is single-declarator too),
        // then the general sequence path.
        if needles.len() == 1 && selector_single_var_declarator(first).is_some() {
            return resolve_group_single_declarator(
                self,
                first,
                request_id,
                selector,
                exports_by_target,
            );
        }
        if needles.len() == 1 && selector_var_decl_has_declarator_holes(first) {
            return resolve_group_declarator_holes(
                self,
                first,
                request_id,
                selector,
                exports_by_target,
            );
        }
        resolve_group_general(self, &needles, request_id, selector, exports_by_target)
    }

    pub fn resolve_anonymous_groups(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<Vec<usize>>> {
        let needles = parse_needles(request_id, selector)?;
        let [needle] = needles.as_slice() else {
            return resolve_anonymous_multi(self, &needles, request_id, selector);
        };
        let needle_facts = item_facts(needle)
            .ok_or_else(|| anyhow::anyhow!("datalog resolver: needle did not project to facts"))?;
        Ok(
            matching_body_indices(self, &needle_facts, datalog_mode(selector))?
                .into_iter()
                .map(|body_idx| vec![body_idx])
                .collect(),
        )
    }
}

impl SelectorResolver for DatalogResolver {
    fn resolve_member(
        &self,
        module: &Module,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<ResolvedMemberBinding> {
        ChunkResolver::new(module).resolve_member(request_id, export_name, selector)
    }

    fn resolve_member_group(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<ResolvedMemberBindingGroup> {
        ChunkResolver::new(module).resolve_member_group(request_id, selector, exports_by_target)
    }

    fn resolve_anonymous_groups(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<Vec<usize>>> {
        ChunkResolver::new(module).resolve_anonymous_groups(request_id, selector)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[derive(Default)]
    struct CollectingSink(RefCell<Vec<ResolverDisagreement>>);

    impl DisagreementSink for CollectingSink {
        fn record(&self, disagreement: ResolverDisagreement) {
            self.0.borrow_mut().push(disagreement);
        }
    }

    fn member(match_source: &str, target_binding: Option<&str>) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: target_binding.map(str::to_string),
            target_statement: None,
            target_statements: None,
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
            target_statement: None,
            target_statements: None,
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
            let datalog = DatalogResolver
                .resolve_member_group(&chunk, "test", &selector, &exports)
                .expect("datalog resolves the group");
            assert_eq!(datalog.bindings["a"].binding_name, "aClass");
            assert_eq!(datalog.bindings["b"].binding_name, "bClass");
            let production = AstWildcardResolver
                .resolve_member_group(&chunk, "test", &selector, &exports)
                .expect("production resolves the group");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_resolves_general_group_like_production() {
        js_ast::with_swc_globals(|| {
            // A multi-statement (general-path) group: a leading anonymous statement
            // then two single-declarator targets, matched as a contiguous window.
            let chunk = module("init();\nconst alpha = makeA();\nconst beta = makeB();\n");
            let selector = group("init();\nconst a = makeA();\nconst b = makeB();");
            let exports = exports(&[("a", "ExportA"), ("b", "ExportB")]);
            let datalog = DatalogResolver
                .resolve_member_group(&chunk, "test", &selector, &exports)
                .expect("datalog resolves the general group");
            assert_eq!(datalog.bindings["a"].binding_name, "alpha");
            assert_eq!(datalog.bindings["b"].binding_name, "beta");
            let production = AstWildcardResolver
                .resolve_member_group(&chunk, "test", &selector, &exports)
                .expect("production resolves");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_resolves_member_like_production() {
        js_ast::with_swc_globals(|| {
            let chunk = module("function alpha(n) { return n + 1; }\nconst beta = alpha(2);\n");
            // A function with a body the alpha selector matches structurally.
            let selector = member("function f(x) { return x + 1; }", Some("f"));
            let datalog = DatalogResolver
                .resolve_member(&chunk, "test", "Alpha", &selector)
                .expect("datalog resolves the function");
            let production = AstWildcardResolver
                .resolve_member(&chunk, "test", "Alpha", &selector)
                .expect("production resolves the function");
            assert_eq!(datalog.binding_name, "alpha");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn differential_is_silent_when_datalog_agrees_with_production() {
        js_ast::with_swc_globals(|| {
            // A function member (not a var declarator — that path routes through
            // per-declarator matching the datalog resolver fail-closes on).
            let chunk = module("function alpha() { return 7; }\nfunction beta() { return 8; }\n");
            let selector = member("function f() { return 7; }", None);
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: DatalogResolver,
                sink: &sink,
            };
            let resolved = differential
                .resolve_member(&chunk, "test", "Alpha", &selector)
                .expect("primary resolves");
            assert_eq!(resolved.binding_name, "alpha");
            assert!(
                sink.0.borrow().is_empty(),
                "datalog and production must agree, got {:?}",
                sink.0.borrow(),
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_declarator_inside_multi_declarator_owner() {
        js_ast::with_swc_globals(|| {
            // The target init lives in the second declarator of a multi-declarator
            // statement — only declarator-level matching finds it.
            let chunk = module("const a = 1, target = compute();\nconst other = 2;\n");
            let selector = member("const x = compute();", Some("x"));
            let datalog = DatalogResolver
                .resolve_member(&chunk, "test", "X", &selector)
                .expect("datalog resolves the inner declarator");
            assert_eq!(datalog.binding_name, "target");
            let production = AstWildcardResolver
                .resolve_member(&chunk, "test", "X", &selector)
                .expect("production resolves");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_var_declarator_categoricity_matches_production() {
        js_ast::with_swc_globals(|| {
            // The same init appears in two declarators (one inside a
            // multi-declarator owner) — ambiguous. The resolver must count both
            // (declarator-level), so it rejects exactly as production does; a
            // whole-statement match would miss the multi-declarator one and
            // spuriously resolve unique.
            let chunk = module("const a = 1, b = compute();\nconst c = compute();\n");
            let selector = member("const x = compute();", Some("x"));
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: DatalogResolver,
                sink: &sink,
            };
            assert!(
                differential
                    .resolve_member(&chunk, "test", "X", &selector)
                    .is_err(),
                "two declarators with the same init are ambiguous",
            );
            assert!(
                sink.0.borrow().is_empty(),
                "datalog must detect the same ambiguity as production, got {:?}",
                sink.0.borrow(),
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_declarator_hole_member_like_production() {
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
            let datalog = DatalogResolver
                .resolve_member(&chunk, "test", "C", &selector)
                .expect("datalog resolves the declarator-hole target");
            assert_eq!(datalog.binding_name, "theClass");
            let production = AstWildcardResolver
                .resolve_member(&chunk, "test", "C", &selector)
                .expect("production resolves");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_declarator_hole_categoricity_matches_production() {
        js_ast::with_swc_globals(|| {
            // Two separate var-decl owners each match the hole needle → ambiguous
            // at the owner level. The resolver must reject exactly as production
            // does (no spurious unique resolution).
            let chunk = module("const a = \"abc\";\nconst b = \"abc\";\n");
            let selector = member(
                "const DECLARATORS_BEFORE = null, c = STR_LITERAL_MATCHING_RE(\"^abc$\"), \
                 DECLARATORS_AFTER = null;",
                Some("c"),
            );
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: DatalogResolver,
                sink: &sink,
            };
            assert!(
                differential
                    .resolve_member(&chunk, "test", "C", &selector)
                    .is_err(),
                "two matching owners are ambiguous",
            );
            assert!(
                sink.0.borrow().is_empty(),
                "datalog must detect the same ambiguity as production, got {:?}",
                sink.0.borrow(),
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_single_declarator_target_window_like_production() {
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
            let datalog = DatalogResolver
                .resolve_member(&chunk, "test", "T", &selector)
                .expect("datalog resolves the windowed single-declarator target");
            assert_eq!(datalog.binding_name, "theTarget");
            let production = AstWildcardResolver
                .resolve_member(&chunk, "test", "T", &selector)
                .expect("production resolves");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_single_declarator_target_window_categoricity_matches_production() {
        js_ast::with_swc_globals(|| {
            // The window shape appears twice → ambiguous; both resolvers reject.
            let chunk = module(
                "function h1(n) { return n + 1; }\nconst a = makeThing();\n\
                 function h2(m) { return m + 1; }\nconst b = makeThing();\n",
            );
            let selector = member(
                "function f(x) { return x + 1; }\nconst t = makeThing();",
                Some("t"),
            );
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: DatalogResolver,
                sink: &sink,
            };
            assert!(
                differential
                    .resolve_member(&chunk, "test", "T", &selector)
                    .is_err(),
                "two matching windows are ambiguous",
            );
            assert!(
                sink.0.borrow().is_empty(),
                "datalog must detect the same ambiguity as production, got {:?}",
                sink.0.borrow(),
            );
        });
    }

    #[test]
    fn datalog_resolver_resolves_multi_statement_declarator_hole_target_like_production() {
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
            let datalog = DatalogResolver
                .resolve_member(&chunk, "test", "M", &selector)
                .expect("datalog resolves the windowed declarator-hole target");
            assert_eq!(datalog.binding_name, "theTarget");
            let production = AstWildcardResolver
                .resolve_member(&chunk, "test", "M", &selector)
                .expect("production resolves");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_resolves_multi_statement_anonymous_like_production() {
        js_ast::with_swc_globals(|| {
            // A two-statement target_statements selector matching a contiguous
            // window inside the chunk body.
            let chunk = module("x();\nfoo();\nbar();\ny();\n");
            let selector = AnonymousStatementSelector {
                match_source: "foo();\nbar();".to_string(),
                identifiers: SourceMatchIdentifierMode::Exact,
                target_binding: None,
                target_statement: None,
                target_statements: Some(spec::TargetStatements::Indices(vec![0, 1])),
                wildcard_string_literals: BTreeSet::new(),
            };
            let datalog = DatalogResolver
                .resolve_anonymous_groups(&chunk, "test", &selector)
                .expect("datalog resolves the multi-statement window");
            assert_eq!(datalog, vec![vec![1, 2]]);
            let production = AstWildcardResolver
                .resolve_anonymous_groups(&chunk, "test", &selector)
                .expect("production resolves");
            assert_eq!(datalog, production);
        });
    }

    #[test]
    fn datalog_resolver_resolves_anonymous_statement() {
        js_ast::with_swc_globals(|| {
            let chunk = module("init();\nregister(widget);\nteardown();\n");
            let selector = member("register(ANYTHING);", None);
            let groups = DatalogResolver
                .resolve_anonymous_groups(&chunk, "test", &selector)
                .expect("datalog resolves the anonymous statement");
            // matches exactly the `register(widget);` statement at body index 1.
            assert_eq!(groups, vec![vec![1]]);
            let production = AstWildcardResolver
                .resolve_anonymous_groups(&chunk, "test", &selector)
                .expect("production resolves");
            assert_eq!(groups, production);
        });
    }
}
