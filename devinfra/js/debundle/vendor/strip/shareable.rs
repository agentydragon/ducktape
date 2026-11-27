use swc_ecma_ast::*;

use super::item_analysis::static_member_name;

/// Declaration shape every shareable-`var` predicate below requires: a
/// non-empty declaration whose every declarator binds one plain identifier to
/// an initializer the caller accepts. Vendor stripping hoists these
/// declarations into shared modules by name, so a destructured binding or a
/// declaration that is only partly shareable has to stay in its own module.
fn shareable_uniform_declaration(
    var: &VarDecl,
    initializer_is_shareable: impl Fn(&Expr) -> bool,
) -> bool {
    !var.decls.is_empty()
        && var.decls.iter().all(|decl| {
            matches!(&decl.name, Pat::Ident(_))
                && decl.init.as_deref().is_some_and(&initializer_is_shareable)
        })
}

pub(super) fn is_shareable_intrinsic_alias_var(var: &VarDecl) -> bool {
    shareable_uniform_declaration(var, is_shareable_intrinsic_alias_expr)
}

pub(super) fn is_shareable_literal_var(var: &VarDecl) -> bool {
    shareable_uniform_declaration(var, is_shareable_literal_expr)
}

pub(super) fn is_shareable_function_var(var: &VarDecl) -> bool {
    shareable_uniform_declaration(var, |expr| matches!(expr, Expr::Fn(_) | Expr::Arrow(_)))
}

pub(super) fn is_empty_object_var(var: &VarDecl) -> bool {
    shareable_uniform_declaration(
        var,
        |expr| matches!(expr, Expr::Object(object) if object.props.is_empty()),
    )
}

/// A literal a wrapper may share verbatim: the primitive literals `js_ast`
/// recognises, plus `RegExp`, under any amount of parenthesization.
pub(super) fn is_shareable_literal_expr(expr: &Expr) -> bool {
    let literal = expr.unwrap_parens();
    matches!(literal, Expr::Lit(Lit::Regex(_))) || js_ast::is_primitive_literal(literal)
}

fn is_shareable_intrinsic_alias_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => is_shareable_intrinsic_alias_expr(&paren.expr),
        Expr::Ident(ident) => matches_global_intrinsic(ident.sym.as_ref()),
        Expr::Member(member) => {
            !member.prop.is_computed()
                && is_shareable_intrinsic_alias_expr(&member.obj)
                && static_member_name(&member.prop).is_some()
        }
        _ => false,
    }
}

pub(super) fn is_shareable_vite_map_deps_var(var: &VarDecl) -> bool {
    let [decl] = var.decls.as_slice() else {
        return false;
    };
    let Pat::Ident(declared) = &decl.name else {
        return false;
    };
    let Some(Expr::Arrow(arrow)) = decl.init.as_deref() else {
        return false;
    };
    let Some(input_param) = arrow
        .params
        .first()
        .and_then(pat_ident_name)
        .map(str::to_owned)
    else {
        return false;
    };
    if !arrow_has_default_to_ident(arrow, declared.id.sym.as_ref()) {
        return false;
    }
    let Some((cache_param, callback_param)) = vite_map_deps_body_access(arrow, &input_param) else {
        return false;
    };
    arrow.params.iter().any(|param| {
        pat_ident_name(param)
            .is_some_and(|param_name| param_name == cache_param && param_name != callback_param)
    })
}

pub(super) fn is_shareable_global_object_fallback_var(var: &VarDecl) -> bool {
    shareable_uniform_declaration(var, is_global_object_fallback_expr)
}

fn is_global_object_fallback_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => is_global_object_fallback_expr(&paren.expr),
        Expr::Object(object) => object.props.is_empty(),
        Expr::Cond(cond) => {
            let Some(global_name) = defined_global_typeof_test(&cond.test) else {
                return false;
            };
            matches!(
                cond.cons.as_ref(),
                Expr::Ident(ident) if ident.sym.as_ref() == global_name
            ) && is_global_object_fallback_expr(&cond.alt)
        }
        _ => false,
    }
}

fn defined_global_typeof_test(expr: &Expr) -> Option<&str> {
    let Expr::Bin(bin) = expr else {
        return None;
    };
    let Expr::Unary(unary) = bin.left.as_ref() else {
        return None;
    };
    if unary.op != UnaryOp::TypeOf {
        return None;
    }
    let Expr::Ident(ident) = unary.arg.as_ref() else {
        return None;
    };
    let name = ident.sym.as_ref();
    if !matches!(name, "globalThis" | "window" | "global" | "self") {
        return None;
    }
    let Expr::Lit(Lit::Str(value)) = bin.right.as_ref() else {
        return None;
    };
    let literal = value.value.as_str();
    let is_defined_check = matches!(bin.op, BinaryOp::Lt | BinaryOp::NotEq | BinaryOp::NotEqEq)
        && matches!(literal, Some("u" | "undefined"));
    is_defined_check.then_some(name)
}

fn arrow_has_default_to_ident(arrow: &ArrowExpr, name: &str) -> bool {
    arrow.params.iter().any(|param| {
        let Pat::Assign(assign) = param else {
            return false;
        };
        matches!(
            assign.right.as_ref(),
            Expr::Ident(ident) if ident.sym.as_ref() == name
        )
    })
}

fn vite_map_deps_body_access(arrow: &ArrowExpr, input_param: &str) -> Option<(String, String)> {
    let ArrowFunctionBody::Expr(body) = arrow.body.as_ref() else {
        return None;
    };
    let Expr::Call(call) = body.as_ref() else {
        return None;
    };
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    if !is_static_member_call(callee, input_param, "map") || call.args.len() != 1 {
        return None;
    }
    let Expr::Arrow(callback) = call.args.first()?.expr.as_ref() else {
        return None;
    };
    let [callback_param] = callback.params.as_slice() else {
        return None;
    };
    let callback_param = pat_ident_name(callback_param)?.to_owned();
    let ArrowFunctionBody::Expr(callback_body) = callback.body.as_ref() else {
        return None;
    };
    let Expr::Member(member) = callback_body.as_ref() else {
        return None;
    };
    let Expr::Ident(cache_param) = member.obj.as_ref() else {
        return None;
    };
    let MemberProp::Computed(computed) = &member.prop else {
        return None;
    };
    if !matches!(
        computed.expr.as_ref(),
        Expr::Ident(index) if index.sym.as_ref() == callback_param
    ) {
        return None;
    }
    Some((cache_param.sym.to_string(), callback_param))
}

pub(super) fn is_static_member_call(expr: &Expr, obj_name: &str, prop_name: &str) -> bool {
    let Expr::Member(member) = expr else {
        return false;
    };
    matches!(
        member.obj.as_ref(),
        Expr::Ident(obj) if obj.sym.as_ref() == obj_name
    ) && static_member_name(&member.prop).as_deref() == Some(prop_name)
}

fn pat_ident_name(pat: &Pat) -> Option<&str> {
    match pat {
        Pat::Ident(ident) => Some(ident.id.sym.as_ref()),
        Pat::Assign(assign) => pat_ident_name(&assign.left),
        _ => None,
    }
}

pub(super) fn matches_global_intrinsic(name: &str) -> bool {
    matches!(
        name,
        "AggregateError"
            | "Array"
            | "ArrayBuffer"
            | "Atomics"
            | "BigInt"
            | "BigInt64Array"
            | "BigUint64Array"
            | "Boolean"
            | "DataView"
            | "Date"
            | "Error"
            | "EvalError"
            | "FinalizationRegistry"
            | "Float32Array"
            | "Float64Array"
            | "Function"
            | "Int8Array"
            | "Int16Array"
            | "Int32Array"
            | "Intl"
            | "JSON"
            | "Map"
            | "Math"
            | "Number"
            | "Object"
            | "Promise"
            | "Proxy"
            | "RangeError"
            | "ReferenceError"
            | "Reflect"
            | "RegExp"
            | "Set"
            | "SharedArrayBuffer"
            | "String"
            | "Symbol"
            | "SyntaxError"
            | "TypeError"
            | "URIError"
            | "Uint8Array"
            | "Uint8ClampedArray"
            | "Uint16Array"
            | "Uint32Array"
            | "WeakMap"
            | "WeakRef"
            | "WeakSet"
    )
}
