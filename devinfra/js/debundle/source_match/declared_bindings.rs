use super::*;

pub(crate) fn item_var_decl(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
    }
}

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
            .map(|binding_name| ResolvedMemberBinding {
                binding_name,
                kind: Some(BindingSourceKind::ImportSpecifier),
            })
            .collect(),
        _ => Vec::new(),
    }
}

pub(crate) fn declared_bindings_for_decl(decl: &Decl) -> Vec<ResolvedMemberBinding> {
    match decl {
        Decl::Fn(f) => vec![ResolvedMemberBinding {
            binding_name: f.ident.sym.to_string(),
            kind: Some(BindingSourceKind::FunctionDeclaration),
        }],
        Decl::Class(c) => vec![ResolvedMemberBinding {
            binding_name: c.ident.sym.to_string(),
            kind: Some(BindingSourceKind::ClassDeclaration),
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
        .map(|binding_name| ResolvedMemberBinding {
            binding_name,
            kind: Some(BindingSourceKind::VariableDeclarator),
        })
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

/// A `target_binding` rejection: the shared `logical_module … target_binding
/// `{target_binding}` ` prefix and the `:\n{match_source}` selector-source suffix
/// written once, with `problem` carrying the varying middle (why this binding is
/// unusable). Both the statement-level and declarator-level resolvers report
/// through here.
fn target_binding_error(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
    problem: &str,
) -> anyhow::Error {
    anyhow::anyhow!(
        "logical_module {request_id}: members[].selector.source_match target_binding \
         `{target_binding}` {problem}:\n{match_source}",
        match_source = selector.match_source,
    )
}

/// `target_binding` names a binding the selector source never declares.
fn target_binding_not_declared(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> anyhow::Error {
    target_binding_error(
        request_id,
        selector,
        target_binding,
        "is not declared by the selector source",
    )
}

/// `target_binding` is declared more than once in the selector source.
/// `index_kind` names what the reported `indices` count (e.g.
/// `"statement/binding"`, `"declarator/binding"`), the only detail that varies
/// between the resolvers.
fn target_binding_ambiguous(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
    index_kind: &str,
    indices: impl std::fmt::Debug,
) -> anyhow::Error {
    target_binding_error(
        request_id,
        selector,
        target_binding,
        &format!(
            "is ambiguous within the selector source at {index_kind} indices \
             {indices:?}. Refine the selector source"
        ),
    )
}

/// Locate the selector-local `target_binding` among the statements' declared
/// bindings as `(statement index, declared-binding index)`, requiring it to be
/// declared exactly once across the selector source.
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

/// Locate the selector-local `target_binding` within a single var-decl needle's
/// declarators as `(declarator index, declared-binding index)`, requiring it to be
/// declared exactly once.
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
