use super::*;

pub(crate) fn declared_bindings(item: &ModuleItem) -> Vec<ResolvedMemberBinding> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declared_bindings_for_decl(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            declared_bindings_for_decl(&export.decl)
        }
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
            .specifiers
            .iter()
            .map(|specifier| match specifier {
                ImportSpecifier::Named(named) => named.local.sym.to_string(),
                ImportSpecifier::Default(default) => default.local.sym.to_string(),
                ImportSpecifier::Namespace(namespace) => namespace.local.sym.to_string(),
            })
            .map(|binding_name| ResolvedMemberBinding { binding_name })
            .collect(),
        _ => Vec::new(),
    }
}

pub(crate) fn declared_bindings_for_decl(decl: &Decl) -> Vec<ResolvedMemberBinding> {
    match decl {
        Decl::Fn(f) => vec![ResolvedMemberBinding {
            binding_name: f.ident.sym.to_string(),
        }],
        Decl::Class(c) => vec![ResolvedMemberBinding {
            binding_name: c.ident.sym.to_string(),
        }],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(declared_bindings_for_var_declarator)
            .collect(),
        _ => Vec::new(),
    }
}

pub(crate) fn declared_bindings_for_var_declarator(
    declarator: &VarDeclarator,
) -> Vec<ResolvedMemberBinding> {
    if declarator_list_hole_name(declarator).is_some() {
        return Vec::new();
    }
    binding_targets::binding_name_strings(&declarator.name)
        .into_iter()
        .map(|binding_name| ResolvedMemberBinding { binding_name })
        .collect()
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
                .get(target_binding_idx)
                .map(|binding| binding.binding_name.clone())
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
        [] => Err(SourceMatchError::binding(
            request_id,
            selector,
            target_binding,
            BindingProblem::NotDeclared,
        )),
        multiple => Err(SourceMatchError::binding(
            request_id,
            selector,
            target_binding,
            BindingProblem::Ambiguous {
                index_kind: ProjectionIndexKind::StatementBinding,
                indices: multiple.to_vec(),
            },
        )),
    }
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
        [] => Err(SourceMatchError::binding(
            request_id,
            selector,
            target_binding,
            BindingProblem::NotDeclared,
        )),
        multiple => Err(SourceMatchError::binding(
            request_id,
            selector,
            target_binding,
            BindingProblem::Ambiguous {
                index_kind: ProjectionIndexKind::DeclaratorBinding,
                indices: multiple.to_vec(),
            },
        )),
    }
}

/// The lone `VarDecl` of a single-declarator var-decl statement or `export`
/// var-decl needle (`None` otherwise).
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

/// Whether a var-decl needle uses any `DECLARATORS_*` list hole.
pub(crate) fn selector_var_decl_has_declarator_holes(needle: &ModuleItem) -> bool {
    item_var_decl(needle).is_some_and(|var| {
        var.decls
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
    })
}
