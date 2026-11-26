use super::*;

/// A selector projects ordinary and exported declarations and import locals.
/// Declarator-list holes stand for a sequence, so they never declare a binding.
pub fn declared_bindings(item: &ModuleItem) -> Vec<String> {
    match js_ast::item_decl(item) {
        Some(Decl::Var(var)) => var
            .decls
            .iter()
            .flat_map(declared_bindings_for_var_declarator)
            .collect(),
        Some(decl) => binding_targets::declaration_name_strings(decl),
        None => match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
                .specifiers
                .iter()
                .map(|specifier| specifier.local().sym.to_string())
                .collect(),
            _ => Vec::new(),
        },
    }
}

pub(crate) fn declared_bindings_for_var_declarator(declarator: &VarDeclarator) -> Vec<String> {
    if declarator_list_hole_name(declarator).is_some() {
        return Vec::new();
    }
    binding_targets::binding_name_strings(&declarator.name)
}

pub(crate) fn target_binding_candidate_names(
    candidate_var: &VarDecl,
    target_binding_idx: usize,
) -> Vec<String> {
    candidate_var
        .decls
        .iter()
        .filter_map(|declarator| {
            declared_bindings_for_var_declarator(declarator)
                .into_iter()
                .nth(target_binding_idx)
        })
        .collect()
}

/// Locate the selector-local projected binding among the statements' declared
/// bindings as `(statement index, declared-binding index)`, requiring it to be
/// declared exactly once across the selector source.
pub(crate) fn selector_binding_location(
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> MatchResult<(usize, usize)> {
    binding_location(
        needles.iter().map(declared_bindings),
        ProjectionIndexKind::StatementBinding,
        request_id,
        selector,
        target_binding,
    )
}

/// Locate the selector-local projected binding within a single var-decl needle's
/// declarators as `(declarator index, declared-binding index)`, requiring it to be
/// declared exactly once.
pub(crate) fn selector_var_declarator_binding_location(
    needle_var: &VarDecl,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> MatchResult<(usize, usize)> {
    binding_location(
        needle_var
            .decls
            .iter()
            .map(declared_bindings_for_var_declarator),
        ProjectionIndexKind::DeclaratorBinding,
        request_id,
        selector,
        target_binding,
    )
}

/// Return the unique (group, binding) position, preserving every position for
/// the ambiguity diagnostic in the caller's coordinate system.
fn binding_location(
    groups: impl IntoIterator<Item = impl AsRef<[String]>>,
    index_kind: ProjectionIndexKind,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target: &str,
) -> MatchResult<(usize, usize)> {
    let mut positions = Vec::new();
    for (group_idx, group) in groups.into_iter().enumerate() {
        for (binding_idx, name) in group.as_ref().iter().enumerate() {
            if name == target {
                positions.push((group_idx, binding_idx));
            }
        }
    }
    let problem = match positions.as_slice() {
        [position] => return Ok(*position),
        [] => BindingProblem::NotDeclared,
        _ => BindingProblem::Ambiguous {
            index_kind,
            indices: positions,
        },
    };
    Err(SourceMatchError::binding(
        request_id, selector, target, problem,
    ))
}

pub(super) fn declared_target_index(
    declared: &[String],
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target: &str,
) -> MatchResult<usize> {
    binding_location(
        [declared],
        ProjectionIndexKind::StatementBinding,
        request_id,
        selector,
        target,
    )
    .map(|(_, binding_idx)| binding_idx)
}

pub(crate) fn selector_has_single_var_declarator(needle: &ModuleItem) -> bool {
    item_var_decl(needle).is_some_and(|var| var.decls.len() == 1)
}

/// Whether a var-decl needle uses any `DECLARATORS_*` list hole.
pub(crate) fn selector_var_decl_has_declarator_holes(needle: &ModuleItem) -> bool {
    item_var_decl(needle).is_some_and(|var| {
        var.decls
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
    })
}
