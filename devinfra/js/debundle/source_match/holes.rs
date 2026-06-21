use super::*;

pub(crate) fn is_anything_expr_hole(expr: &Expr) -> bool {
    matches!(expr, Expr::Ident(ident) if ident_is_anything(ident))
}

pub(crate) fn string_literal_regex_pattern(expr: &Expr) -> Option<String> {
    let Expr::Call(call) = expr else {
        return None;
    };
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Ident(callee) = callee.as_ref() else {
        return None;
    };
    if callee.sym.as_ref() != STRING_LITERAL_REGEX_PREDICATE {
        return None;
    }
    let Expr::Lit(Lit::Str(pattern)) = call.args[0].expr.as_ref() else {
        return None;
    };
    Some(pattern.value.to_string_lossy().to_string())
}

pub(crate) fn statement_hole_name(stmt: &Stmt) -> Option<&str> {
    let Stmt::Expr(ExprStmt { expr, .. }) = stmt else {
        return None;
    };
    let Expr::Ident(ident) = expr.as_ref() else {
        return None;
    };
    Some(ident.sym.as_ref())
}

pub(crate) fn is_anything_stmt_hole(stmt: &Stmt) -> bool {
    matches!(
        stmt,
        Stmt::Expr(ExprStmt { expr, .. }) if is_anything_expr_hole(expr)
    )
}

/// The name of a statement-list hole (`STMT_LIST` / `STMT_LIST_*;`) if
/// `stmt` is one.
pub(crate) fn statement_list_hole_name(stmt: &Stmt) -> Option<&str> {
    hole_name_for(statement_hole_name(stmt)?, STMT_LIST_HOLE_KEYWORD)
}

/// The name of a top-level statement-list hole (`STMT_LIST` /
/// `STMT_LIST_*;`) if `item` is one. Module declarations cannot be
/// holes because the selector syntax is an expression statement.
pub(crate) fn module_item_list_hole_name(item: &ModuleItem) -> Option<&str> {
    let ModuleItem::Stmt(stmt) = item else {
        return None;
    };
    statement_list_hole_name(stmt)
}

/// The name of a declarator-list hole (`DECLARATORS` /
/// `DECLARATORS_*`) if `declarator` is one. The initializer is ignored:
/// `const` syntax requires one, so selectors usually write
/// `DECLARATORS_BEFORE = null`.
pub(crate) fn declarator_list_hole_name(declarator: &VarDeclarator) -> Option<&str> {
    let Pat::Ident(ident) = &declarator.name else {
        return None;
    };
    hole_name_for(ident.id.sym.as_ref(), DECLARATORS_HOLE_KEYWORD)
        .or_else(|| anything_hole_name(ident.id.sym.as_ref()))
}

pub(crate) fn is_anything_declarator_list_hole(declarator: &VarDeclarator) -> bool {
    matches!(
        &declarator.name,
        Pat::Ident(ident) if binding_ident_is_anything(ident)
    )
}

pub(crate) fn is_anything_pat_hole(pat: &Pat) -> bool {
    matches!(pat, Pat::Ident(ident) if binding_ident_is_anything(ident))
}

/// The name of an object-**pattern** property-list hole (`OBJECT_PROPS` /
/// `OBJECT_PROPS_*`) if `prop` is one. Mirrors [`object_property_list_hole_name`]
/// for destructuring patterns: the hole is written as a shorthand binding
/// (`const { OBJECT_PROPS, x } = …`), parsed as an `ObjectPatProp::Assign` with
/// no default, and absorbs a run of destructured properties. Anonymous
/// `ANYTHING` is the sugar form.
pub(crate) fn object_pat_prop_list_hole_name(prop: &ObjectPatProp) -> Option<&str> {
    let ObjectPatProp::Assign(assign) = prop else {
        return None;
    };
    if assign.value.is_some() {
        return None;
    }
    let name = assign.key.id.sym.as_ref();
    hole_name_for(name, OBJECT_PROPS_HOLE_KEYWORD).or_else(|| anything_hole_name(name))
}

pub(crate) fn anything_hole_name(name: &str) -> Option<&str> {
    (name == ANYTHING_HOLE_KEYWORD).then_some(name)
}

pub(crate) fn unsupported_selector_hole_name(name: &str) -> Option<&str> {
    let rest = name.strip_prefix(ANYTHING_HOLE_KEYWORD)?;
    rest.starts_with('_').then_some(name)
}

pub(crate) fn is_anything_class_rest_hole(member: &ClassMember) -> bool {
    let ClassMember::ClassProp(prop) = member else {
        return false;
    };
    prop.value.is_none() && prop_name_is_anything(&prop.key)
}

pub(crate) fn prop_name_is_anything(prop_name: &PropName) -> bool {
    matches!(prop_name, PropName::Ident(ident) if ident.sym.as_ref() == ANYTHING_HOLE_KEYWORD)
}

pub(crate) fn binding_ident_is_anything(ident: &BindingIdent) -> bool {
    ident.id.sym.as_ref() == ANYTHING_HOLE_KEYWORD
}

pub(crate) fn ident_is_anything(ident: &Ident) -> bool {
    ident.sym.as_ref() == ANYTHING_HOLE_KEYWORD
}
