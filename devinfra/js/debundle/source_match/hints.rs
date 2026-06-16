use super::*;

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct MismatchReason {
    pub(crate) score: usize,
    pub(crate) reason: String,
}

pub(crate) struct ClassCandidateHint {
    body_idx: usize,
    declared: Vec<String>,
    member_labels: Vec<String>,
    matched_pinned_labels: usize,
    pinned_label_count: usize,
    reason: MismatchReason,
}

pub(crate) struct VarDeclCandidateHint {
    body_idx: usize,
    declared: Vec<String>,
    declarator_labels: Vec<String>,
    matched_pinned_declarators: usize,
    pinned_declarator_count: usize,
    candidate_declarator_count: usize,
    reason: MismatchReason,
}

pub(crate) fn source_match_no_match_hint(
    runtime_module: &Module,
    selector: &AnonymousStatementSelector,
) -> Option<String> {
    let parsed =
        parse_source_match_module_ast("<source_match diagnostic>", &selector.match_source).ok()?;
    let [needle] = parsed.body.as_slice() else {
        return None;
    };
    if module_item_list_hole_name(needle).is_some() {
        return None;
    }
    let wildcard_idents = wildcard_ident_names(needle);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    SyntaxContext::within_ignored_ctxt(|| {
        if let Some(hint) = class_source_match_no_match_hint(
            runtime_module,
            needle,
            selector,
            &wildcard_idents,
            alpha,
        ) {
            return Some(hint);
        }
        if let Some(hint) = var_declarator_source_match_no_match_hint(
            runtime_module,
            needle,
            selector,
            &wildcard_idents,
            alpha,
        ) {
            return Some(hint);
        }
        runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(body_idx, candidate)| {
                first_mismatch_reason(needle, candidate, selector, &wildcard_idents, alpha).map(
                    |reason| {
                        let declared = declared_bindings(candidate)
                            .into_iter()
                            .map(|binding| binding.binding_name)
                            .collect::<Vec<_>>();
                        (body_idx, declared, reason)
                    },
                )
            })
            .max_by_key(|(_, _, reason)| reason.score)
            .map(|(body_idx, declared, reason)| {
                let declared_hint = if declared.is_empty() {
                    "declares no bindings".to_string()
                } else {
                    format!(
                        "declares {}",
                        declared
                            .iter()
                            .map(|name| format!("`{name}`"))
                            .collect::<Vec<_>>()
                            .join(", ")
                    )
                };
                format!(
                    "\nNearest candidate: top-level body index {body_idx} ({declared_hint}).\
                     \nFirst mismatch: {reason}",
                    reason = reason.reason,
                )
            })
    })
}

pub(crate) fn class_source_match_no_match_hint(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<String> {
    let needle_class = module_item_class_decl(needle)?;
    let pinned_label_count = needle_class
        .class
        .body
        .iter()
        .filter(|member| !is_class_rest_hole(member) && class_member_label(member).is_some())
        .count();
    if pinned_label_count == 0 {
        return None;
    }

    let mut candidates = runtime_module
        .body
        .iter()
        .enumerate()
        .filter_map(|(body_idx, candidate)| {
            let candidate_class = module_item_class_decl(candidate)?;
            let reason = first_class_decl_mismatch_reason(
                needle_class,
                candidate_class,
                selector,
                wildcard_idents,
                alpha,
            )?;
            let declared = declared_bindings(candidate)
                .into_iter()
                .map(|binding| binding.binding_name)
                .collect::<Vec<_>>();
            let member_labels = candidate_class
                .class
                .body
                .iter()
                .filter_map(class_member_label)
                .collect::<Vec<_>>();
            let matched_pinned_labels = count_pinned_class_member_labels_in_order(
                &needle_class.class,
                &candidate_class.class,
            );
            Some(ClassCandidateHint {
                body_idx,
                declared,
                member_labels,
                matched_pinned_labels,
                pinned_label_count,
                reason,
            })
        })
        .collect::<Vec<_>>();

    if candidates.is_empty() {
        return None;
    }

    candidates.sort_by(|left, right| {
        right
            .matched_pinned_labels
            .cmp(&left.matched_pinned_labels)
            .then_with(|| right.reason.score.cmp(&left.reason.score))
            .then_with(|| left.body_idx.cmp(&right.body_idx))
    });

    let rendered = candidates
        .iter()
        .take(3)
        .map(render_class_candidate_hint)
        .collect::<Vec<_>>()
        .join("\n");

    Some(format!("\nNearest class candidates:\n{rendered}"))
}

pub(crate) fn var_declarator_source_match_no_match_hint(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<String> {
    let needle_var = item_var_decl(needle)?;
    if !needle_var
        .decls
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
    {
        return None;
    }
    let pinned_declarator_count = needle_var
        .decls
        .iter()
        .filter(|declarator| declarator_list_hole_name(declarator).is_none())
        .count();
    if pinned_declarator_count == 0 {
        return None;
    }

    let mut candidates = runtime_module
        .body
        .iter()
        .enumerate()
        .filter_map(|(body_idx, candidate)| {
            let candidate_var = item_var_decl(candidate)?;
            if needle_var.kind != candidate_var.kind {
                return None;
            }
            let reason = first_var_decl_mismatch_reason(
                needle_var,
                candidate_var,
                selector,
                wildcard_idents,
                alpha,
            );
            let declared = declared_bindings(candidate)
                .into_iter()
                .map(|binding| binding.binding_name)
                .collect::<Vec<_>>();
            let declarator_labels = candidate_var
                .decls
                .iter()
                .map(render_var_declarator_label)
                .collect::<Vec<_>>();
            let matched_pinned_declarators = count_pinned_var_declarators_in_order(
                needle_var,
                candidate_var,
                selector,
                wildcard_idents,
                alpha,
            );
            Some(VarDeclCandidateHint {
                body_idx,
                declared,
                declarator_labels,
                matched_pinned_declarators,
                pinned_declarator_count,
                candidate_declarator_count: candidate_var.decls.len(),
                reason,
            })
        })
        .collect::<Vec<_>>();

    if candidates.is_empty() {
        return None;
    }

    candidates.sort_by(|left, right| {
        right
            .matched_pinned_declarators
            .cmp(&left.matched_pinned_declarators)
            .then_with(|| {
                right
                    .candidate_declarator_count
                    .cmp(&left.candidate_declarator_count)
            })
            .then_with(|| right.reason.score.cmp(&left.reason.score))
            .then_with(|| left.body_idx.cmp(&right.body_idx))
    });

    let rendered = candidates
        .iter()
        .take(3)
        .map(render_var_decl_candidate_hint)
        .collect::<Vec<_>>()
        .join("\n");
    let guidance = render_var_declarator_selector_guidance(selector);

    Some(format!(
        "\nNearest variable declaration candidates:\n{rendered}\n{guidance}"
    ))
}

pub(crate) fn first_class_decl_mismatch_reason(
    needle: &ClassDecl,
    candidate: &ClassDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<MismatchReason> {
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    if matcher.match_decl(
        &Decl::Class(needle.clone()),
        &Decl::Class(candidate.clone()),
    ) {
        return None;
    }
    Some(first_decl_mismatch_reason(
        &Decl::Class(needle.clone()),
        &Decl::Class(candidate.clone()),
        selector,
        wildcard_idents,
        alpha,
    ))
}

pub(crate) fn render_class_candidate_hint(candidate: &ClassCandidateHint) -> String {
    let declared_hint = if candidate.declared.is_empty() {
        "declares no bindings".to_string()
    } else {
        format!(
            "declares {}",
            candidate
                .declared
                .iter()
                .map(|name| format!("`{name}`"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    let member_hint = render_class_member_labels(&candidate.member_labels);
    format!(
        "- top-level body index {body_idx} ({declared_hint}); members: {member_hint}; \
         matched {matched}/{total} pinned member names in order. First mismatch: {reason}",
        body_idx = candidate.body_idx,
        matched = candidate.matched_pinned_labels,
        total = candidate.pinned_label_count,
        reason = candidate.reason.reason,
    )
}

pub(crate) fn render_class_member_labels(labels: &[String]) -> String {
    const MAX_LABELS: usize = 12;
    if labels.is_empty() {
        return "<none>".to_string();
    }
    let mut rendered = labels
        .iter()
        .take(MAX_LABELS)
        .map(|label| format!("`{label}`"))
        .collect::<Vec<_>>();
    if labels.len() > MAX_LABELS {
        rendered.push(format!("... +{} more", labels.len() - MAX_LABELS));
    }
    rendered.join(", ")
}

pub(crate) fn render_var_decl_candidate_hint(candidate: &VarDeclCandidateHint) -> String {
    let declared_hint = if candidate.declared.is_empty() {
        "declares no bindings".to_string()
    } else {
        format!(
            "declares {}",
            candidate
                .declared
                .iter()
                .map(|name| format!("`{name}`"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    let declarator_hint = render_var_declarator_labels(&candidate.declarator_labels);
    format!(
        "- top-level body index {body_idx} ({declared_hint}); declarators: {declarator_hint}; \
         matched {matched}/{total} pinned declarators in order. First mismatch: {reason}",
        body_idx = candidate.body_idx,
        matched = candidate.matched_pinned_declarators,
        total = candidate.pinned_declarator_count,
        reason = candidate.reason.reason,
    )
}

pub(crate) fn render_var_declarator_selector_guidance(
    selector: &AnonymousStatementSelector,
) -> String {
    let mut guidance = "Selector guidance: add `DECLARATORS_* = null` pseudo-declarators \
before, after, or between pinned declarators for unrelated siblings in the same \
var/let/const declaration."
        .to_string();
    if selector.target_binding.is_some() {
        guidance.push_str(
            " `target_binding` resolves one selector-local binding for the current export; \
use one `binding_groups` entry when several exports should come from this matched declaration.",
        );
    } else {
        guidance.push_str(
            " Use `binding_groups` when several exports should come from one matched declaration.",
        );
    }
    guidance
}

pub(crate) fn render_var_declarator_labels(labels: &[String]) -> String {
    const MAX_LABELS: usize = 10;
    if labels.is_empty() {
        return "<none>".to_string();
    }
    let mut rendered = labels.iter().take(MAX_LABELS).cloned().collect::<Vec<_>>();
    if labels.len() > MAX_LABELS {
        rendered.push(format!("... +{} more", labels.len() - MAX_LABELS));
    }
    rendered.join(", ")
}

pub(crate) fn render_var_declarator_label(declarator: &VarDeclarator) -> String {
    if let Some(hole_name) = declarator_list_hole_name(declarator) {
        return format!("`{hole_name}`");
    }
    let bindings = binding_targets::binding_name_strings(&declarator.name);
    let binding_label = if bindings.is_empty() {
        "<pattern>".to_string()
    } else {
        bindings.join("/")
    };
    match declarator.init.as_deref() {
        Some(init) => format!("`{binding_label} = {}`", expr_shape_label(init)),
        None => format!("`{binding_label}`"),
    }
}

pub(crate) fn expr_shape_label(expr: &Expr) -> String {
    if string_literal_regex_pattern(expr).is_some() {
        return format!("{STRING_LITERAL_REGEX_PREDICATE}(...)");
    }
    match expr {
        Expr::Ident(ident) => ident.sym.to_string(),
        Expr::Lit(lit) => lit_shape_label(lit),
        Expr::Call(call) => format!("{}(...)", callee_shape_label(&call.callee)),
        Expr::New(new) => format!("new {}(...)", expr_shape_label(&new.callee)),
        Expr::Member(member) => member_expr_shape_label(member),
        Expr::Object(_) => "{...}".to_string(),
        Expr::Array(_) => "[...]".to_string(),
        Expr::Arrow(_) => "(...) => ...".to_string(),
        Expr::Fn(_) => "function(...)".to_string(),
        Expr::Class(_) => "class {...}".to_string(),
        Expr::Tpl(_) => "`...`".to_string(),
        Expr::TaggedTpl(tagged) => format!("{} `...`", expr_shape_label(&tagged.tag)),
        _ => "expression".to_string(),
    }
}

pub(crate) fn callee_shape_label(callee: &Callee) -> String {
    match callee {
        Callee::Super(_) => "super".to_string(),
        Callee::Import(_) => "import".to_string(),
        Callee::Expr(expr) => expr_shape_label(expr),
    }
}

pub(crate) fn member_expr_shape_label(member: &MemberExpr) -> String {
    let obj = expr_shape_label(&member.obj);
    match &member.prop {
        MemberProp::Ident(prop) => format!("{obj}.{}", prop.sym),
        MemberProp::PrivateName(prop) => format!("{obj}.#{}", prop.name),
        MemberProp::Computed(_) => format!("{obj}[...]"),
    }
}

pub(crate) fn lit_shape_label(lit: &Lit) -> String {
    match lit {
        Lit::Str(str_) => format!("\"{}\"", str_.value.to_string_lossy()),
        Lit::Bool(bool_) => bool_.value.to_string(),
        Lit::Null(_) => "null".to_string(),
        Lit::Num(num) => num.value.to_string(),
        Lit::BigInt(_) => "0n".to_string(),
        Lit::Regex(_) => "/.../".to_string(),
        Lit::JSXText(_) => "jsx-text".to_string(),
    }
}

pub(crate) fn key_value_prop_ident_value(prop: &KeyValueProp) -> Option<&Ident> {
    match prop.value.as_ref() {
        Expr::Ident(ident) => Some(ident),
        _ => None,
    }
}

pub(crate) fn key_value_pat_binding_ident_value(prop: &KeyValuePatProp) -> Option<&BindingIdent> {
    match prop.value.as_ref() {
        Pat::Ident(ident) => Some(ident),
        _ => None,
    }
}

pub(crate) fn prop_name_matches_ident_key(prop_name: &PropName, ident: &Ident) -> bool {
    match prop_name {
        PropName::Ident(key) => key.sym == ident.sym,
        PropName::Str(key) => key.value.to_string_lossy() == ident.sym.as_ref(),
        _ => false,
    }
}

pub(crate) fn prop_name_matches_binding_key(prop_name: &PropName, ident: &BindingIdent) -> bool {
    prop_name_matches_ident_key(prop_name, &ident.id)
}

pub(crate) fn count_pinned_class_member_labels_in_order(
    needle: &Class,
    candidate: &Class,
) -> usize {
    let mut candidate_start = 0;
    let mut matched = 0;
    for needle_member in &needle.body {
        if is_class_rest_hole(needle_member) {
            continue;
        }
        let Some(needle_label) = class_member_label(needle_member) else {
            continue;
        };
        let Some(candidate_idx) = candidate
            .body
            .iter()
            .enumerate()
            .skip(candidate_start)
            .find_map(|(candidate_idx, candidate_member)| {
                (class_member_label(candidate_member).as_deref() == Some(needle_label.as_str()))
                    .then_some(candidate_idx)
            })
        else {
            break;
        };
        matched += 1;
        candidate_start = candidate_idx + 1;
    }
    matched
}

pub(crate) fn count_pinned_var_declarators_in_order(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> usize {
    pinned_var_declarator_matches_in_order(needle, candidate, selector, wildcard_idents, alpha)
        .len()
}

pub(crate) fn pinned_var_declarator_matches_in_order(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Vec<(usize, usize)> {
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    let mut candidate_start = 0;
    let mut matches = Vec::new();
    for (needle_idx, needle_declarator) in needle.decls.iter().enumerate() {
        if declarator_list_hole_name(needle_declarator).is_some() {
            continue;
        }
        let Some(candidate_idx) = candidate
            .decls
            .iter()
            .enumerate()
            .skip(candidate_start)
            .find_map(|(candidate_idx, candidate_declarator)| {
                let snapshot = matcher.snapshot();
                if matcher.match_var_declarator(needle_declarator, candidate_declarator) {
                    Some(candidate_idx)
                } else {
                    matcher.restore(snapshot);
                    None
                }
            })
        else {
            break;
        };
        matches.push((needle_idx, candidate_idx));
        candidate_start = candidate_idx + 1;
    }
    matches
}

pub(crate) fn first_pinned_var_declarator_mismatch_reason(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> String {
    let matches =
        pinned_var_declarator_matches_in_order(needle, candidate, selector, wildcard_idents, alpha);
    let pinned_indices = needle
        .decls
        .iter()
        .enumerate()
        .filter_map(|(idx, declarator)| {
            declarator_list_hole_name(declarator)
                .is_none()
                .then_some(idx)
        })
        .collect::<Vec<_>>();
    if let Some(&needle_idx) = pinned_indices.get(matches.len()) {
        let candidate_start = matches
            .last()
            .map(|(_, candidate_idx)| candidate_idx + 1)
            .unwrap_or(0);
        let remaining = candidate
            .decls
            .iter()
            .skip(candidate_start)
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        let remaining_hint = if remaining.is_empty() {
            "no candidate declarators remain".to_string()
        } else {
            format!(
                "remaining candidate declarators: {}",
                render_var_declarator_labels(&remaining)
            )
        };
        return format!(
            "selector pinned declarator #{needle_idx} {} was not found in order ({remaining_hint})",
            render_var_declarator_label(&needle.decls[needle_idx]),
        );
    }
    first_var_declarator_hole_placement_mismatch_reason(needle, candidate, &matches)
}

pub(crate) fn first_var_declarator_hole_placement_mismatch_reason(
    needle: &VarDecl,
    candidate: &VarDecl,
    matches: &[(usize, usize)],
) -> String {
    let Some(&(first_needle_idx, first_candidate_idx)) = matches.first() else {
        return "pinned declarators matched in order, but DECLARATORS_* hole placement differed"
            .to_string();
    };
    if first_candidate_idx > 0 && !has_declarator_hole_before(needle, first_needle_idx) {
        let skipped = candidate
            .decls
            .iter()
            .take(first_candidate_idx)
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        return format!(
            "candidate has unmatched leading declarator(s) before selector declarator \
             #{first_needle_idx} {}: {}. Add a `DECLARATORS_* = null` pseudo-declarator \
             before the first pinned declarator.",
            render_var_declarator_label(&needle.decls[first_needle_idx]),
            render_var_declarator_labels(&skipped),
        );
    }

    for pair in matches.windows(2) {
        let [left, right] = pair else {
            continue;
        };
        let (left_needle_idx, left_candidate_idx) = *left;
        let (right_needle_idx, right_candidate_idx) = *right;
        let gap_start = left_candidate_idx + 1;
        if gap_start >= right_candidate_idx {
            continue;
        }
        if has_declarator_hole_between(needle, left_needle_idx, right_needle_idx) {
            continue;
        }
        let skipped = candidate.decls[gap_start..right_candidate_idx]
            .iter()
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        return format!(
            "candidate has unmatched declarator(s) between selector declarator \
             #{left_needle_idx} {} and #{right_needle_idx} {}: {}. Add a \
             `DECLARATORS_* = null` pseudo-declarator between those pinned declarators.",
            render_var_declarator_label(&needle.decls[left_needle_idx]),
            render_var_declarator_label(&needle.decls[right_needle_idx]),
            render_var_declarator_labels(&skipped),
        );
    }

    let Some(&(last_needle_idx, last_candidate_idx)) = matches.last() else {
        return "pinned declarators matched in order, but DECLARATORS_* hole placement differed"
            .to_string();
    };
    if last_candidate_idx + 1 < candidate.decls.len()
        && !has_declarator_hole_after(needle, last_needle_idx)
    {
        let skipped = candidate
            .decls
            .iter()
            .skip(last_candidate_idx + 1)
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        return format!(
            "candidate has unmatched trailing declarator(s) after selector declarator \
             #{last_needle_idx} {}: {}. Add a `DECLARATORS_* = null` pseudo-declarator \
             after the last pinned declarator.",
            render_var_declarator_label(&needle.decls[last_needle_idx]),
            render_var_declarator_labels(&skipped),
        );
    }

    "pinned declarators matched in order, but DECLARATORS_* hole placement differed. \
Check that each unrelated sibling declarator is covered by a hole before, after, or \
between pinned declarators."
        .to_string()
}

pub(crate) fn has_declarator_hole_before(needle: &VarDecl, needle_idx: usize) -> bool {
    needle.decls[..needle_idx]
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
}

pub(crate) fn has_declarator_hole_between(
    needle: &VarDecl,
    left_needle_idx: usize,
    right_needle_idx: usize,
) -> bool {
    needle.decls[left_needle_idx + 1..right_needle_idx]
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
}

pub(crate) fn has_declarator_hole_after(needle: &VarDecl, needle_idx: usize) -> bool {
    needle.decls[needle_idx + 1..]
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
}

pub(crate) fn module_item_class_decl(item: &ModuleItem) -> Option<&ClassDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => Some(class),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Class(class),
            ..
        })) => Some(class),
        _ => None,
    }
}

pub(crate) fn first_mismatch_reason(
    needle: &ModuleItem,
    candidate: &ModuleItem,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<MismatchReason> {
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    if matcher.match_module_item(needle, candidate) {
        return None;
    }
    Some(match (needle, candidate) {
        (ModuleItem::Stmt(needle), ModuleItem::Stmt(candidate)) => {
            first_stmt_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        (ModuleItem::ModuleDecl(needle), ModuleItem::ModuleDecl(candidate)) => {
            first_module_decl_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        _ => MismatchReason {
            score: 1,
            reason: format!(
                "top-level item kind differs: selector is {}, candidate is {}",
                module_item_kind(needle),
                module_item_kind(candidate),
            ),
        },
    })
}

pub(crate) fn first_module_decl_mismatch_reason(
    needle: &ModuleDecl,
    candidate: &ModuleDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    match (needle, candidate) {
        (ModuleDecl::ExportDecl(needle), ModuleDecl::ExportDecl(candidate)) => {
            first_decl_mismatch_reason(
                &needle.decl,
                &candidate.decl,
                selector,
                wildcard_idents,
                alpha,
            )
        }
        _ if std::mem::discriminant(needle) != std::mem::discriminant(candidate) => {
            MismatchReason {
                score: 10,
                reason: format!(
                    "module declaration kind differs: selector is {}, candidate is {}",
                    module_decl_kind(needle),
                    module_decl_kind(candidate),
                ),
            }
        }
        _ => MismatchReason {
            score: 20,
            reason: "module declaration shape differs".to_string(),
        },
    }
}

pub(crate) fn first_stmt_mismatch_reason(
    needle: &Stmt,
    candidate: &Stmt,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    match (needle, candidate) {
        (Stmt::Decl(needle), Stmt::Decl(candidate)) => {
            first_decl_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        _ if std::mem::discriminant(needle) != std::mem::discriminant(candidate) => {
            MismatchReason {
                score: 10,
                reason: format!(
                    "statement kind differs: selector is {}, candidate is {}",
                    stmt_kind(needle),
                    stmt_kind(candidate),
                ),
            }
        }
        _ => MismatchReason {
            score: 20,
            reason: "statement shape differs".to_string(),
        },
    }
}

pub(crate) fn first_decl_mismatch_reason(
    needle: &Decl,
    candidate: &Decl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    match (needle, candidate) {
        (Decl::Class(needle), Decl::Class(candidate)) => {
            if !alpha && needle.ident.sym != candidate.ident.sym {
                return MismatchReason {
                    score: 40,
                    reason: format!(
                        "class name differs: selector `{}`, candidate `{}`",
                        needle.ident.sym, candidate.ident.sym,
                    ),
                };
            }
            first_class_mismatch_reason(
                &needle.class,
                &candidate.class,
                selector,
                wildcard_idents,
                alpha,
            )
        }
        (Decl::Fn(needle), Decl::Fn(candidate)) => {
            if !alpha && needle.ident.sym != candidate.ident.sym {
                return MismatchReason {
                    score: 40,
                    reason: format!(
                        "function name differs: selector `{}`, candidate `{}`",
                        needle.ident.sym, candidate.ident.sym,
                    ),
                };
            }
            MismatchReason {
                score: 35,
                reason: "function signature or body differs".to_string(),
            }
        }
        (Decl::Var(needle), Decl::Var(candidate)) => {
            first_var_decl_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        _ if std::mem::discriminant(needle) != std::mem::discriminant(candidate) => {
            MismatchReason {
                score: 30,
                reason: format!(
                    "declaration kind differs: selector is {}, candidate is {}",
                    decl_kind(needle),
                    decl_kind(candidate),
                ),
            }
        }
        _ => MismatchReason {
            score: 35,
            reason: "declaration shape differs".to_string(),
        },
    }
}

pub(crate) fn first_var_decl_mismatch_reason(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    if needle.kind != candidate.kind {
        return MismatchReason {
            score: 45,
            reason: format!(
                "variable declaration kind differs: selector is {}, candidate is {}",
                var_decl_kind(needle.kind),
                var_decl_kind(candidate.kind),
            ),
        };
    }
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    if matcher
        .match_var_declarator_slice_with_alignment(&needle.decls, &candidate.decls)
        .is_none()
    {
        let reason = if needle
            .decls
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
        {
            first_pinned_var_declarator_mismatch_reason(
                needle,
                candidate,
                selector,
                wildcard_idents,
                alpha,
            )
        } else {
            format!(
                "variable declarators differ: selector has {} declarator(s), candidate has {}",
                needle.decls.len(),
                candidate.decls.len(),
            )
        };
        return MismatchReason { score: 55, reason };
    }
    MismatchReason {
        score: 35,
        reason: "variable declaration shape differs".to_string(),
    }
}

pub(crate) fn first_class_mismatch_reason(
    needle: &Class,
    candidate: &Class,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    let mut candidate_start = 0;
    for needle_member in &needle.body {
        if is_class_rest_hole(needle_member) {
            continue;
        }
        let Some(needle_label) = class_member_label(needle_member) else {
            continue;
        };
        let mut found_label = false;
        for (candidate_idx, candidate_member) in
            candidate.body.iter().enumerate().skip(candidate_start)
        {
            if class_member_label(candidate_member).as_deref() != Some(needle_label.as_str()) {
                continue;
            }
            found_label = true;
            candidate_start = candidate_idx + 1;
            let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
            if !matcher.match_class_member(needle_member, candidate_member) {
                return MismatchReason {
                    score: 65,
                    reason: format!(
                        "class member `{needle_label}` matched by name, but its signature or body differs"
                    ),
                };
            }
            break;
        }
        if !found_label {
            return MismatchReason {
                score: 70,
                reason: format!(
                    "selector class pinned member `{needle_label}` was not found in the candidate class body in order"
                ),
            };
        }
    }
    MismatchReason {
        score: 45,
        reason: "class heritage, decorators, or member order differs".to_string(),
    }
}

pub(crate) fn class_member_label(member: &ClassMember) -> Option<String> {
    match member {
        ClassMember::Constructor(_) => Some("constructor".to_string()),
        ClassMember::Method(method) => prop_name_label(&method.key),
        ClassMember::PrivateMethod(method) => Some(format!("#{}", method.key.name)),
        ClassMember::ClassProp(prop) => prop_name_label(&prop.key),
        ClassMember::PrivateProp(prop) => Some(format!("#{}", prop.key.name)),
        ClassMember::AutoAccessor(accessor) => match &accessor.key {
            Key::Public(key) => prop_name_label(key),
            Key::Private(key) => Some(format!("#{}", key.name)),
        },
        ClassMember::StaticBlock(_) => Some("static block".to_string()),
        _ => None,
    }
}

pub(crate) fn prop_name_label(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(str_) => Some(str_.value.to_string_lossy().to_string()),
        PropName::Num(num) => Some(num.value.to_string()),
        PropName::BigInt(bigint) => Some(bigint.value.to_string()),
        PropName::Computed(_) => Some("<computed>".to_string()),
    }
}

pub(crate) fn module_item_kind(item: &ModuleItem) -> &'static str {
    match item {
        ModuleItem::ModuleDecl(_) => "module declaration",
        ModuleItem::Stmt(_) => "statement",
    }
}

pub(crate) fn module_decl_kind(decl: &ModuleDecl) -> &'static str {
    match decl {
        ModuleDecl::Import(_) => "import",
        ModuleDecl::ExportDecl(_) => "export declaration",
        ModuleDecl::ExportNamed(_) => "named export",
        ModuleDecl::ExportDefaultDecl(_) => "default declaration export",
        ModuleDecl::ExportDefaultExpr(_) => "default expression export",
        ModuleDecl::ExportAll(_) => "export all",
        ModuleDecl::TsImportEquals(_) => "typescript import equals",
        ModuleDecl::TsExportAssignment(_) => "typescript export assignment",
        ModuleDecl::TsNamespaceExport(_) => "typescript namespace export",
    }
}

pub(crate) fn stmt_kind(stmt: &Stmt) -> &'static str {
    match stmt {
        Stmt::Block(_) => "block",
        Stmt::Empty(_) => "empty",
        Stmt::Debugger(_) => "debugger",
        Stmt::With(_) => "with",
        Stmt::Return(_) => "return",
        Stmt::Labeled(_) => "labeled",
        Stmt::Break(_) => "break",
        Stmt::Continue(_) => "continue",
        Stmt::If(_) => "if",
        Stmt::Switch(_) => "switch",
        Stmt::Throw(_) => "throw",
        Stmt::Try(_) => "try",
        Stmt::While(_) => "while",
        Stmt::DoWhile(_) => "do while",
        Stmt::For(_) => "for",
        Stmt::ForIn(_) => "for in",
        Stmt::ForOf(_) => "for of",
        Stmt::Decl(_) => "declaration",
        Stmt::Expr(_) => "expression",
    }
}

pub(crate) fn decl_kind(decl: &Decl) -> &'static str {
    match decl {
        Decl::Class(_) => "class",
        Decl::Fn(_) => "function",
        Decl::Var(_) => "variable",
        Decl::Using(_) => "using",
        Decl::TsInterface(_) => "typescript interface",
        Decl::TsTypeAlias(_) => "typescript type alias",
        Decl::TsEnum(_) => "typescript enum",
        Decl::TsModule(_) => "typescript module",
    }
}

pub(crate) fn var_decl_kind(kind: VarDeclKind) -> &'static str {
    match kind {
        VarDeclKind::Var => "var",
        VarDeclKind::Let => "let",
        VarDeclKind::Const => "const",
    }
}
