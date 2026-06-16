use super::*;

pub(crate) fn expression_hole_name(expr: &Expr) -> Option<&str> {
    let Expr::Ident(ident) = expr else {
        return None;
    };
    hole_name_for(ident.sym.as_ref(), EXPR_HOLE_KEYWORD)
        .or_else(|| anything_hole_name(ident.sym.as_ref()))
}

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

pub(crate) fn string_literal_matches_regex(pattern: &str, candidate_value: &Wtf8Atom) -> bool {
    Regex::new(pattern)
        .is_ok_and(|regex| regex.is_match(candidate_value.to_string_lossy().as_ref()))
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

/// The name of an argument-list hole (`ARGS` / `ARGS_*`) if
/// `expr_or_spread` is one. The hole token itself is a plain identifier
/// argument; the run it absorbs may contain spread or non-spread
/// arguments.
pub(crate) fn argument_list_hole_name(expr_or_spread: &ExprOrSpread) -> Option<&str> {
    if expr_or_spread.spread.is_some() {
        return None;
    }
    let Expr::Ident(ident) = expr_or_spread.expr.as_ref() else {
        return None;
    };
    hole_name_for(ident.sym.as_ref(), ARGS_HOLE_KEYWORD)
}

/// The name of an object-literal property-list hole (`OBJECT_PROPS` /
/// `OBJECT_PROPS_*`) if `prop_or_spread` is one. Anonymous `ANYTHING`
/// is the sugar form. The hole token itself is a shorthand property;
/// the run it absorbs may contain ordinary properties or spreads.
pub(crate) fn object_property_list_hole_name(prop_or_spread: &PropOrSpread) -> Option<&str> {
    let PropOrSpread::Prop(prop) = prop_or_spread else {
        return None;
    };
    let Prop::Shorthand(ident) = prop.as_ref() else {
        return None;
    };
    hole_name_for(ident.sym.as_ref(), OBJECT_PROPS_HOLE_KEYWORD)
        .or_else(|| anything_hole_name(ident.sym.as_ref()))
}

pub(crate) fn anything_hole_name(name: &str) -> Option<&str> {
    (name == ANYTHING_HOLE_KEYWORD).then_some(name)
}

pub(crate) fn unsupported_selector_hole_name(name: &str) -> Option<&str> {
    let rest = name.strip_prefix(ANYTHING_HOLE_KEYWORD)?;
    rest.starts_with('_').then_some(name)
}

/// Whether a hole name is the anonymous form: the bare keyword, or the
/// keyword with a trailing underscore but no suffix. Anonymous holes
/// match independently at every occurrence instead of binding for
/// cross-occurrence equality.
pub(crate) fn hole_is_anonymous(name: &str, keyword: &str) -> bool {
    matches!(name.strip_prefix(keyword), Some("") | Some("_"))
}

/// Whether `member` is a `CLASS_REST;` class-member hole: a class field
/// whose key is exactly the keyword and which has no initializer. Matched
/// as an exact token (not a `CLASS_REST_*` prefix) so it never collides
/// with a real field whose name merely starts with `CLASS_REST`; since it
/// never binds, a suffix would carry no meaning anyway.
pub(crate) fn is_class_rest_hole(member: &ClassMember) -> bool {
    let ClassMember::ClassProp(prop) = member else {
        return false;
    };
    prop.value.is_none()
        && matches!(
            &prop.key,
            PropName::Ident(ident)
                if ident.sym.as_ref() == CLASS_REST_HOLE_KEYWORD
                    || ident.sym.as_ref() == ANYTHING_HOLE_KEYWORD
        )
}

/// Whether `case` is a `case CASE_REST:` switch-case hole: a `case`
/// clause whose test is exactly the bare keyword identifier and whose
/// body is empty. Matched as an exact token (not a `CASE_REST_*` prefix)
/// so it never collides with a real `case CASE_REST:` discriminant; since
/// it never binds, a suffix would carry no meaning anyway. A `default:`
/// clause is never a hole.
pub(crate) fn is_case_rest_hole(case: &SwitchCase) -> bool {
    case.cons.is_empty()
        && matches!(
            case.test.as_deref(),
            Some(Expr::Ident(ident)) if ident.sym.as_ref() == CASE_REST_HOLE_KEYWORD
        )
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
