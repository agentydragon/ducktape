//! Source-aware `source_matches[]` suggestions for repeated multi-declarator claims.

use super::*;

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(super) struct BindingGroupSuggestionKey {
    module: String,
    body_idx: usize,
    selector_key: String,
}

#[derive(Debug, Clone)]
pub(super) struct BindingGroupSuggestionCandidate {
    key: BindingGroupSuggestionKey,
    module: String,
    export_name: Option<String>,
    target_binding: String,
    declarator_idx: usize,
    declarator: VarDeclarator,
    declaration_kind: VarDeclKind,
}

pub(super) fn collect_binding_group_suggestion_candidate(
    runtime_module: Option<&SwcModule>,
    module_path: &str,
    export_name: Option<String>,
    selector: &AnonymousStatementSelector,
    exact_body_indices: Option<&[usize]>,
) -> Result<Option<BindingGroupSuggestionCandidate>> {
    let (Some(runtime_module), Some([body_idx])) = (runtime_module, exact_body_indices) else {
        return Ok(None);
    };
    let Some(runtime_var) = runtime_module
        .body
        .get(*body_idx)
        .and_then(js_ast::item_var_decl)
    else {
        return Ok(None);
    };
    if runtime_var.decls.len() < 2 {
        return Ok(None);
    }
    let Some(target_binding) = selector.target_binding.as_deref() else {
        return Ok(None);
    };
    let parsed = js_ast::parse_js_module_ast(
        "<member source_match consolidation>",
        &selector.match_source,
    )
    .with_context(
        || "parsing member source_match while building selector-debt binding group suggestion",
    )?;
    let [needle] = parsed.body.as_slice() else {
        return Ok(None);
    };
    let Some(needle_var) = js_ast::item_var_decl(needle) else {
        return Ok(None);
    };
    if needle_var.decls.len() < 2 {
        return Ok(None);
    }
    let Some((declarator_idx, declarator)) = selector_target_declarator(needle_var, target_binding)
    else {
        return Ok(None);
    };
    Ok(Some(BindingGroupSuggestionCandidate {
        key: BindingGroupSuggestionKey {
            module: module_path.to_owned(),
            body_idx: *body_idx,
            selector_key: selector_group_key(selector),
        },
        module: module_path.to_owned(),
        export_name,
        target_binding: target_binding.to_owned(),
        declarator_idx,
        declarator: declarator.clone(),
        declaration_kind: needle_var.kind,
    }))
}

pub(super) fn collect_binding_group_suggestions(
    candidates: Vec<BindingGroupSuggestionCandidate>,
) -> Vec<SourceAwareBindingGroupSuggestion> {
    let mut by_key =
        BTreeMap::<BindingGroupSuggestionKey, Vec<BindingGroupSuggestionCandidate>>::new();
    for candidate in candidates {
        by_key
            .entry(candidate.key.clone())
            .or_default()
            .push(candidate);
    }
    let mut groups = by_key
        .into_values()
        .filter(|candidates| candidates.len() >= 2)
        .map(build_binding_group_suggestion)
        .collect::<Vec<_>>();
    groups.sort_by(|a, b| {
        b.selectors
            .len()
            .cmp(&a.selectors.len())
            .then_with(|| a.body_idx.cmp(&b.body_idx))
            .then_with(|| a.declaration_kind.cmp(&b.declaration_kind))
            .then_with(|| a.module.cmp(&b.module))
    });
    groups
}

pub(super) fn build_binding_group_suggestion(
    mut candidates: Vec<BindingGroupSuggestionCandidate>,
) -> SourceAwareBindingGroupSuggestion {
    candidates.sort_by(|a, b| {
        a.declarator_idx
            .cmp(&b.declarator_idx)
            .then_with(|| a.module.cmp(&b.module))
            .then_with(|| a.export_name.cmp(&b.export_name))
    });
    let first = &candidates[0];
    let match_source = render_var_source_match_template(first.declaration_kind, &candidates);
    let selectors = candidates
        .iter()
        .map(|candidate| SourceMatchBindingGroupSelector {
            module: candidate.module.clone(),
            export_name: candidate.export_name.clone(),
            target_binding: candidate.target_binding.clone(),
            declarator_idx: candidate.declarator_idx,
        })
        .collect::<Vec<_>>();
    let candidate_yaml = render_source_matches_candidate_yaml(&selectors, &match_source);
    SourceAwareBindingGroupSuggestion {
        module: first.key.module.clone(),
        body_idx: first.key.body_idx,
        declaration_kind: js_ast::var_decl_kind_str(first.declaration_kind).to_string(),
        selectors,
        candidate_yaml,
    }
}

pub(super) fn selector_group_key(selector: &AnonymousStatementSelector) -> String {
    format!(
        "{:?}\0{}",
        selector.identifiers,
        normalize_match(&selector.match_source),
    )
}

pub(super) fn selector_target_declarator<'a>(
    var: &'a VarDecl,
    target_binding: &str,
) -> Option<(usize, &'a VarDeclarator)> {
    var.decls.iter().enumerate().find(|(_, declarator)| {
        simple_declarator_binding_name(declarator).as_deref() == Some(target_binding)
    })
}

pub(super) fn render_var_source_match_template(
    kind: VarDeclKind,
    targets: &[BindingGroupSuggestionCandidate],
) -> String {
    let mut lines = vec![format!(
        "{} DECLARATORS_BEFORE = null,",
        js_ast::var_decl_kind_str(kind)
    )];
    let mut previous_declarator_idx = None;
    for (target_idx, target) in targets.iter().enumerate() {
        if let Some(previous) = previous_declarator_idx
            && target.declarator_idx > previous + 1
        {
            lines.push(format!("  DECLARATORS_BETWEEN_{target_idx} = null,"));
        }
        let initializer = selector_initializer_template(
            &target.target_binding,
            target.declarator.init.as_deref(),
        );
        lines.push(format!("  {} = {initializer},", target.target_binding));
        previous_declarator_idx = Some(target.declarator_idx);
    }
    lines.push("  DECLARATORS_AFTER = null;".to_string());
    lines.join("\n")
}

pub(super) fn render_source_matches_candidate_yaml(
    selectors: &[SourceMatchBindingGroupSelector],
    match_source: &str,
) -> String {
    let mut lines = vec!["source_matches:".to_string(), "  - match: |".to_string()];
    lines.extend(indent_lines(match_source, 6));
    lines.push("    bindings:".to_string());
    for selector in selectors {
        let export_name = selector
            .export_name
            .as_deref()
            .unwrap_or(selector.target_binding.as_str());
        if export_name == selector.target_binding {
            lines.push(format!("      - {}", selector.target_binding));
        } else {
            lines.push(format!("      - local: {}", selector.target_binding));
            lines.push(format!("        name: {export_name}"));
        }
    }
    lines.join("\n")
}

pub(super) fn indent_lines(source: &str, spaces: usize) -> Vec<String> {
    let prefix = " ".repeat(spaces);
    source
        .lines()
        .map(|line| format!("{prefix}{line}"))
        .collect()
}

pub(super) fn selector_initializer_template(
    selector_local_name: &str,
    init: Option<&Expr>,
) -> String {
    match init {
        Some(Expr::Lit(Lit::Str(value))) => format!(
            "STR_LITERAL_MATCHING_RE(\"^{}$\")",
            js_string_escape(&regex_escape(&value.value.to_string_lossy()))
        ),
        Some(Expr::Lit(Lit::Bool(value))) => value.value.to_string(),
        Some(Expr::Lit(Lit::Null(_))) => "null".to_string(),
        Some(Expr::Lit(Lit::Num(value))) => value.value.to_string(),
        _ => format!("EXPR_{selector_local_name}"),
    }
}

pub(super) fn regex_escape(value: &str) -> String {
    let mut escaped = String::new();
    for ch in value.chars() {
        if matches!(
            ch,
            '\\' | '.' | '+' | '*' | '?' | '(' | ')' | '|' | '[' | ']' | '{' | '}' | '^' | '$'
        ) {
            escaped.push('\\');
        }
        escaped.push(ch);
    }
    escaped
}

pub(super) fn js_string_escape(value: &str) -> String {
    let mut escaped = String::new();
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str("\\\\"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            c if c.is_control() => escaped.push_str(&format!("\\u{{{:x}}}", c as u32)),
            c => escaped.push(c),
        }
    }
    escaped
}

pub(super) fn simple_declarator_binding_name(declarator: &VarDeclarator) -> Option<String> {
    match &declarator.name {
        Pat::Ident(ident) => Some(ident.id.sym.to_string()),
        _ => None,
    }
}
