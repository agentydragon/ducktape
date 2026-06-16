use super::*;

/// `target_binding` names a binding the selector source never declares.
/// Shared by the statement-level, declarator-level, and single-needle
/// target-binding location resolvers, which all reject this case identically.
fn target_binding_not_declared(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> anyhow::Error {
    anyhow::anyhow!(
        "logical_module {request_id}: members[].selector.source_match target_binding \
         `{target_binding}` is not declared by the selector source:\n{match_source}",
        match_source = selector.match_source,
    )
}

/// `target_binding` is declared more than once in the selector source.
/// `index_kind` names what the reported `indices` count (e.g.
/// `"statement/binding"`, `"declared-binding"`, `"declarator/binding"`), the
/// only detail that varies between the resolvers.
fn target_binding_ambiguous(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
    index_kind: &str,
    indices: impl std::fmt::Debug,
) -> anyhow::Error {
    anyhow::anyhow!(
        "logical_module {request_id}: members[].selector.source_match target_binding \
         `{target_binding}` is ambiguous within the selector source at {index_kind} \
         indices {indices:?}. Refine the selector source:\n{match_source}",
        match_source = selector.match_source,
    )
}

pub(crate) fn selector_binding_location(
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<(usize, usize)> {
    let selector_binding_locations: Vec<(usize, usize)> = needles
        .iter()
        .enumerate()
        .flat_map(|(item_idx, item)| {
            declared_bindings(item).into_iter().enumerate().filter_map(
                move |(binding_idx, binding)| {
                    (binding.binding_name == target_binding).then_some((item_idx, binding_idx))
                },
            )
        })
        .collect();
    match selector_binding_locations.as_slice() {
        [single] => Ok(*single),
        [] => Err(target_binding_not_declared(
            request_id,
            selector,
            target_binding,
        )),
        multiple => Err(target_binding_ambiguous(
            request_id,
            selector,
            target_binding,
            "statement/binding",
            multiple,
        )),
    }
}

pub(crate) fn find_member_binding_matches(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    filter: BodyIndexFilter<'_>,
) -> Result<Vec<MemberBindingMatch>> {
    let parsed = parse_selector_module_with_capability_check(
        request_id,
        "members[].selector.source_match",
        format!("<member source_match in {request_id}>"),
        &selector.match_source,
        "members[].selector.source_match",
    )?;
    if let Some(target_binding) = &selector.target_binding {
        if parsed.body.is_empty() {
            bail!(
                "logical_module {request_id}: members[].selector.source_match parsed to zero \
                 statements; selector source must contain at least one top-level statement \
                 when target_binding is used:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        if parsed.body.len() == 1 && selector_single_var_declarator(&parsed.body[0]).is_some() {
            return find_matching_target_var_declarators(
                runtime_module,
                request_id,
                &parsed.body[0],
                selector,
                target_binding,
                filter,
            );
        }
        return find_matching_target_bindings(
            runtime_module,
            request_id,
            &parsed.body,
            selector,
            target_binding,
            filter,
        );
    }
    let parsed_items: Vec<&ModuleItem> = parsed.body.iter().collect();
    let needle = match parsed_items.as_slice() {
        [single] => *single,
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match parsed to zero \
             statements; selector source must contain exactly one top-level declaration:\n{match_source}",
            match_source = selector.match_source,
        ),
        _ => bail!(
            "logical_module {request_id}: members[].selector.source_match parsed to {} \
             statements; selector source must contain exactly one top-level declaration unless \
             target_binding is used:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
    };
    if selector_single_var_declarator(needle).is_some() {
        return find_matching_var_declarators(runtime_module, needle, selector, filter);
    }
    let mut matches = Vec::new();
    for body_idx in find_matching_body_indices(runtime_module, needle, selector, filter) {
        let item = require_body_item(runtime_module, body_idx)?;
        let declared = declared_bindings(item);
        match declared.as_slice() {
            [single] => matches.push(MemberBindingMatch {
                body_idx,
                binding: single.clone(),
            }),
            [] => {}
            _ => bail!(
                "logical_module {request_id}: members[].selector.source_match matched \
                 top-level statement at body index {body_idx}, but that statement declares \
                 {} bindings ({}). Use a single-declarator selector or refine the match. \
                 Source:\n{match_source}",
                declared.len(),
                declared
                    .iter()
                    .map(|binding| binding.binding_name.as_str())
                    .collect::<Vec<_>>()
                    .join(", "),
                match_source = selector.match_source,
            ),
        }
    }
    Ok(matches)
}

pub(crate) fn find_matching_target_bindings(
    runtime_module: &Module,
    request_id: &str,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
    target_binding: &str,
    filter: BodyIndexFilter<'_>,
) -> Result<Vec<MemberBindingMatch>> {
    if needles.len() == 1 && selector_var_decl_has_declarator_holes(&needles[0]) {
        return find_matching_target_var_decl_with_declarator_holes(
            runtime_module,
            request_id,
            &needles[0],
            selector,
            target_binding,
            filter,
        );
    }
    let (target_item_idx, target_binding_idx) =
        selector_binding_location(needles, request_id, selector, target_binding)?;
    if selector_single_var_declarator(&needles[target_item_idx]).is_some() {
        return find_matching_target_binding_ranges_with_single_declarator(
            runtime_module,
            needles,
            selector,
            target_item_idx,
            target_binding_idx,
            filter,
        );
    }
    let mut matches = Vec::new();
    for body_idx in
        find_matching_body_ranges(runtime_module, needles, selector, target_item_idx, filter)
    {
        let matched_body_idx = body_idx + target_item_idx;
        let item = require_body_item(runtime_module, matched_body_idx)?;
        let declared = declared_bindings(item);
        let Some(binding) = declared.get(target_binding_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` matched top-level statement at body index {matched_body_idx}, but \
                 the matched statement declares only {} bindings. Source:\n{match_source}",
                declared.len(),
                match_source = selector.match_source,
            );
        };
        matches.push(MemberBindingMatch {
            body_idx: matched_body_idx,
            binding: binding.clone(),
        });
    }
    Ok(matches)
}

pub(crate) fn find_matching_target_binding_ranges_with_single_declarator(
    runtime_module: &Module,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
    target_item_idx: usize,
    target_binding_idx: usize,
    filter: BodyIndexFilter<'_>,
) -> Result<Vec<MemberBindingMatch>> {
    if needles.is_empty() || needles.len() > runtime_module.body.len() {
        return Ok(Vec::new());
    }
    let wildcard_idents = wildcard_ident_names_for_module_items(needles);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    let mut matches = Vec::new();
    SyntaxContext::within_ignored_ctxt(|| {
        for (body_idx, candidates) in runtime_module.body.windows(needles.len()).enumerate() {
            if !filter.allows(body_idx + target_item_idx) {
                continue;
            }
            let mut matcher = AstWildcardMatcher::new(selector, &wildcard_idents, alpha);
            let window = SingleDeclaratorTargetWindow {
                needles,
                candidates,
                target_item_idx,
                target_binding_idx,
            };
            let target_matches = window.collect_matches(&mut matcher);
            for binding in target_matches {
                matches.push(MemberBindingMatch {
                    body_idx: body_idx + target_item_idx,
                    binding,
                });
            }
        }
    });
    Ok(matches)
}

pub(crate) struct SingleDeclaratorTargetWindow<'a> {
    needles: &'a [ModuleItem],
    candidates: &'a [ModuleItem],
    target_item_idx: usize,
    target_binding_idx: usize,
}

impl SingleDeclaratorTargetWindow<'_> {
    fn collect_matches(&self, matcher: &mut AstWildcardMatcher<'_>) -> Vec<ResolvedMemberBinding> {
        let mut matches = Vec::new();
        self.match_items(matcher, 0, None, &mut matches);
        matches
    }

    fn match_items(
        &self,
        matcher: &mut AstWildcardMatcher<'_>,
        item_idx: usize,
        target_binding: Option<ResolvedMemberBinding>,
        matches: &mut Vec<ResolvedMemberBinding>,
    ) {
        if item_idx == self.needles.len() {
            if let Some(target_binding) = target_binding {
                matches.push(target_binding);
            }
            return;
        }
        let snapshot = matcher.snapshot();
        if item_idx == self.target_item_idx {
            let Some(candidate_var) = item_var_decl(&self.candidates[item_idx]) else {
                return;
            };
            for declarator in &candidate_var.decls {
                matcher.restore(snapshot.clone());
                if !matcher.match_single_var_declarator_item(
                    &self.needles[item_idx],
                    &self.candidates[item_idx],
                    declarator,
                ) {
                    continue;
                }
                let declared = declared_bindings_for_var_declarator(declarator);
                let Some(binding) = declared.get(self.target_binding_idx).cloned() else {
                    continue;
                };
                self.match_items(matcher, item_idx + 1, Some(binding), matches);
            }
        } else if matcher.match_module_item(&self.needles[item_idx], &self.candidates[item_idx]) {
            self.match_items(matcher, item_idx + 1, target_binding, matches);
        }
        matcher.restore(snapshot);
    }
}

pub(crate) fn find_matching_target_var_declarators(
    runtime_module: &Module,
    request_id: &str,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
    filter: BodyIndexFilter<'_>,
) -> Result<Vec<MemberBindingMatch>> {
    let selector_bindings = declared_bindings(needle);
    let selector_binding_indices: Vec<usize> = selector_bindings
        .iter()
        .enumerate()
        .filter_map(|(idx, binding)| (binding.binding_name == target_binding).then_some(idx))
        .collect();
    let target_binding_idx = match selector_binding_indices.as_slice() {
        [single] => *single,
        [] => {
            return Err(target_binding_not_declared(
                request_id,
                selector,
                target_binding,
            ));
        }
        multiple => {
            return Err(target_binding_ambiguous(
                request_id,
                selector,
                target_binding,
                "declared-binding",
                multiple,
            ));
        }
    };
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclaratorPrefilter::new(needle, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        if !filter.allows(body_idx) {
            continue;
        }
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        for declarator in &candidate_var.decls {
            if !prefilter.declarator_can_match(declarator) {
                continue;
            }
            if !prepared.matches_single_var_declarator(item, declarator) {
                continue;
            }
            let declared = declared_bindings_for_var_declarator(declarator);
            let Some(binding) = declared.get(target_binding_idx) else {
                bail!(
                    "logical_module {request_id}: members[].selector.source_match target_binding \
                     `{target_binding}` matched a variable declarator at body index {body_idx}, \
                     but that declarator declares only {} bindings. Source:\n{match_source}",
                    declared.len(),
                    match_source = selector.match_source,
                );
            };
            matches.push(MemberBindingMatch {
                body_idx,
                binding: binding.clone(),
            });
        }
    }
    Ok(matches)
}

pub(crate) fn find_matching_target_var_decl_with_declarator_holes(
    runtime_module: &Module,
    request_id: &str,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
    filter: BodyIndexFilter<'_>,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_var =
        item_var_decl(needle).expect("caller checked selector_var_decl_has_declarator_holes");
    let (target_decl_idx, target_binding_idx) =
        selector_var_declarator_binding_location(needle_var, request_id, selector, target_binding)?;
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclWithDeclaratorHolesPrefilter::new(needle_var, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        if !filter.allows(body_idx) {
            continue;
        }
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        let Some(alignment) = target_binding_candidate_names(candidate_var, target_binding_idx)
            .into_iter()
            .find_map(|candidate_binding| {
                if !prepared.matches_with_prebound_binding(item, target_binding, &candidate_binding)
                {
                    return None;
                }
                prepared.var_declarator_alignment_with_prebound_binding(
                    needle_var,
                    candidate_var,
                    target_binding,
                    &candidate_binding,
                )
            })
        else {
            continue;
        };
        let Some(Some(candidate_decl_idx)) = alignment.get(target_decl_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` was matched by a DECLARATORS hole instead of a pinned \
                 selector declarator. Refine the selector:\n{match_source}",
                match_source = selector.match_source,
            );
        };
        let Some(candidate_declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` aligned to missing candidate declarator index \
                 {candidate_decl_idx}. Source:\n{match_source}",
                match_source = selector.match_source,
            );
        };
        let declared = declared_bindings_for_var_declarator(candidate_declarator);
        let Some(binding) = declared.get(target_binding_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` matched a variable declarator at body index {body_idx}, \
                 but that declarator declares only {} bindings. Source:\n{match_source}",
                declared.len(),
                match_source = selector.match_source,
            );
        };
        matches.push(MemberBindingMatch {
            body_idx,
            binding: binding.clone(),
        });
    }
    Ok(matches)
}

pub(crate) fn resolve_member_binding_group_with_declarator_holes(
    runtime_module: &Module,
    request_id: &str,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<ResolvedMemberBindingGroup> {
    let needle_var =
        item_var_decl(needle).expect("caller checked selector_var_decl_has_declarator_holes");
    let target_locations = exports_by_target
        .keys()
        .map(|target_binding| {
            Ok((
                target_binding.clone(),
                selector_var_declarator_binding_location(
                    needle_var,
                    request_id,
                    selector,
                    target_binding,
                )?,
            ))
        })
        .collect::<Result<BTreeMap<_, _>>>()?;
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclWithDeclaratorHolesPrefilter::new(needle_var, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        if !prepared.matches(item) {
            continue;
        }
        let Some(alignment) = prepared.var_declarator_alignment(needle_var, candidate_var) else {
            continue;
        };
        let mut resolved = BTreeMap::new();
        for (target_binding, (target_decl_idx, target_binding_idx)) in &target_locations {
            let Some(Some(candidate_decl_idx)) = alignment.get(*target_decl_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match \
                     target_binding `{target_binding}` was matched by a DECLARATORS hole \
                     instead of a pinned selector declarator. Refine the selector:\n{match_source}",
                    match_source = selector.match_source,
                );
            };
            let Some(candidate_declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match \
                     target_binding `{target_binding}` aligned to missing candidate \
                     declarator index {candidate_decl_idx}. Source:\n{match_source}",
                    match_source = selector.match_source,
                );
            };
            let declared = declared_bindings_for_var_declarator(candidate_declarator);
            let Some(binding) = declared.get(*target_binding_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match \
                     target_binding `{target_binding}` matched a variable declarator at \
                     body index {body_idx}, but that declarator declares only {} bindings. \
                     Source:\n{match_source}",
                    declared.len(),
                    match_source = selector.match_source,
                );
            };
            resolved.insert(target_binding.clone(), binding.clone());
        }
        matches.push((body_idx, resolved));
    }
    match matches.as_slice() {
        [(body_idx, resolved)] => Ok(ResolvedMemberBindingGroup {
            body_idx: *body_idx,
            bindings: resolved.clone(),
        }),
        [] => {
            let target_hint = exports_by_target
                .iter()
                .map(|(target_binding, export_name)| {
                    format!("target_binding `{target_binding}` for export `{export_name}`")
                })
                .collect::<Vec<_>>()
                .join(", ");
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
            "logical_module {request_id}: binding_groups[].source_match is ambiguous — \
             matched {} top-level declarations at body indices {:?}. Refine the selector. \
             Source:\n{match_source}",
            multiple.len(),
            multiple
                .iter()
                .map(|(body_idx, _)| *body_idx)
                .collect::<Vec<_>>(),
            match_source = selector.match_source,
        ),
    }
}

pub(crate) fn selector_var_declarator_binding_location(
    needle_var: &VarDecl,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<(usize, usize)> {
    let selector_binding_locations = needle_var
        .decls
        .iter()
        .enumerate()
        .flat_map(|(declarator_idx, declarator)| {
            declared_bindings_for_var_declarator(declarator)
                .into_iter()
                .enumerate()
                .filter_map(move |(binding_idx, binding)| {
                    (binding.binding_name == target_binding)
                        .then_some((declarator_idx, binding_idx))
                })
        })
        .collect::<Vec<_>>();
    match selector_binding_locations.as_slice() {
        [single] => Ok(*single),
        [] => Err(target_binding_not_declared(
            request_id,
            selector,
            target_binding,
        )),
        multiple => Err(target_binding_ambiguous(
            request_id,
            selector,
            target_binding,
            "declarator/binding",
            multiple,
        )),
    }
}

pub(crate) fn selector_single_var_declarator(needle: &ModuleItem) -> Option<&VarDecl> {
    match needle {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.decls.len() == 1 => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) if matches!(&export.decl, Decl::Var(var) if var.decls.len() == 1) => {
            match &export.decl {
                Decl::Var(var) => Some(var),
                _ => None,
            }
        }
        _ => None,
    }
}

pub(crate) fn selector_var_decl_has_declarator_holes(needle: &ModuleItem) -> bool {
    item_var_decl(needle).is_some_and(|var| {
        var.decls
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
    })
}

pub(crate) fn find_matching_var_declarators(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    filter: BodyIndexFilter<'_>,
) -> Result<Vec<MemberBindingMatch>> {
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclaratorPrefilter::new(needle, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        if !filter.allows(body_idx) {
            continue;
        }
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        for declarator in &candidate_var.decls {
            if !prefilter.declarator_can_match(declarator) {
                continue;
            }
            if !prepared.matches_single_var_declarator(item, declarator) {
                continue;
            }
            let declared = declared_bindings_for_var_declarator(declarator);
            match declared.as_slice() {
                [single] => matches.push(MemberBindingMatch {
                    body_idx,
                    binding: single.clone(),
                }),
                [] => {}
                _ => bail!(
                    "members[].selector.source_match matched a variable declarator at body \
                     index {body_idx}, but that declarator binds multiple names ({}). \
                     Refine the selector to a single-binding declarator.",
                    declared
                        .iter()
                        .map(|binding| binding.binding_name.as_str())
                        .collect::<Vec<_>>()
                        .join(", "),
                ),
            }
        }
    }
    Ok(matches)
}

/// Cheap candidate prefilters for the per-declarator matching loops.
/// Both `find_matching_var_declarators` paths clone a fresh
/// single-declarator `ModuleItem` per candidate declarator before
/// running the full structural match; these keys reject most
/// candidates before that clone.
pub(crate) struct VarDeclaratorPrefilter {
    /// `var`/`let`/`const` of the needle's declaration. Both the
    /// wildcard matcher (`match_var_decl`) and plain structural
    /// equality require kind equality, so a kind mismatch can never
    /// match — sound in every mode.
    needle_kind: VarDeclKind,
    /// The needle declarator's plain-`Ident` binding name, when the
    /// match is exact (no wildcards, no alpha renaming). In that mode
    /// equality requires the candidate declarator to bind the same
    /// plain ident, so a name mismatch can never match. `None`
    /// disables the name key (wildcards, alpha mode, or a
    /// destructuring pattern).
    needle_ident: Option<Atom>,
    /// Exact string-literal initializer of the needle declarator. This
    /// remains sound in alpha mode because alpha only renames
    /// identifiers; string literals must still match exactly unless
    /// selector-level string wildcards are enabled.
    needle_string_init: Option<String>,
}

impl VarDeclaratorPrefilter {
    pub(crate) fn new(needle: &ModuleItem, prepared: &PreparedNeedle) -> Self {
        let needle_var = selector_single_var_declarator(needle)
            .expect("caller checked the needle is a single-declarator var decl");
        let needle_declarator = &needle_var.decls[0];
        let needle_ident = (prepared.no_wildcards && !prepared.alpha)
            .then(|| match &needle_declarator.name {
                Pat::Ident(ident) => Some(ident.id.sym.clone()),
                _ => None,
            })
            .flatten();
        let needle_string_init = prepared
            .selector
            .wildcard_string_literals
            .is_empty()
            .then(|| {
                needle_declarator
                    .init
                    .as_deref()
                    .and_then(string_literal_expr_value)
            })
            .flatten();
        Self {
            needle_kind: needle_var.kind,
            needle_ident,
            needle_string_init,
        }
    }

    pub(crate) fn var_decl_can_match(&self, candidate: &VarDecl) -> bool {
        candidate.kind == self.needle_kind
    }

    pub(crate) fn declarator_can_match(&self, declarator: &VarDeclarator) -> bool {
        if let Some(needle_sym) = &self.needle_ident
            && !matches!(&declarator.name, Pat::Ident(ident) if ident.id.sym == *needle_sym)
        {
            return false;
        }
        let Some(needle_string_init) = &self.needle_string_init else {
            return true;
        };
        declarator
            .init
            .as_deref()
            .and_then(string_literal_expr_value)
            .as_ref()
            == Some(needle_string_init)
    }
}

/// Cheap candidate prefilter for variable declarations that use
/// `DECLARATORS_*` list holes. The full matcher must still validate
/// declaration structure and alpha bindings, but pinned direct string
/// predicates are enough to reject most declarations before recursive
/// list-hole alignment.
pub(crate) struct VarDeclWithDeclaratorHolesPrefilter {
    needle_kind: VarDeclKind,
    pinned_literal_predicates: Vec<StringLiteralPredicate>,
}

impl VarDeclWithDeclaratorHolesPrefilter {
    pub(crate) fn new(needle: &VarDecl, prepared: &PreparedNeedle<'_>) -> Self {
        let pinned_literal_predicates = needle
            .decls
            .iter()
            .filter(|declarator| declarator_list_hole_name(declarator).is_none())
            .filter_map(|declarator| {
                declarator.init.as_deref().and_then(|init| {
                    string_literal_predicate_for_expr(
                        init,
                        prepared.selector,
                        &prepared.string_literal_regexes,
                    )
                })
            })
            .collect();
        Self {
            needle_kind: needle.kind,
            pinned_literal_predicates,
        }
    }

    pub(crate) fn var_decl_can_match(&self, candidate: &VarDecl) -> bool {
        if candidate.kind != self.needle_kind {
            return false;
        }
        if self.pinned_literal_predicates.is_empty() {
            return true;
        }
        let mut candidate_start = 0;
        for predicate in &self.pinned_literal_predicates {
            let Some(candidate_idx) = candidate
                .decls
                .iter()
                .enumerate()
                .skip(candidate_start)
                .find_map(|(candidate_idx, declarator)| {
                    declarator
                        .init
                        .as_deref()
                        .and_then(string_literal_expr_value_ref)
                        .is_some_and(|value| predicate.matches(value))
                        .then_some(candidate_idx)
                })
            else {
                return false;
            };
            candidate_start = candidate_idx + 1;
        }
        true
    }
}
