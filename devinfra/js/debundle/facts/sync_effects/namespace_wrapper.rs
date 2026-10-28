//! Source shape for an identity-caching namespace facade.
//!
//! This is a candidate, not an execution certificate. The caller must also
//! prove native identities, a private stable cache and an ordinary input.

use swc_ecma_ast::*;

#[derive(Debug, Clone)]
pub(super) struct NamespaceWrapperCandidate {
    pub(super) cache: Id,
    pub(super) object: Id,
    pub(super) function_end: u32,
}

fn ident(expr: &Expr, expected: &Id) -> bool {
    matches!(expr.unwrap_parens(), Expr::Ident(id) if id.to_id() == *expected)
}

fn member<'a>(expr: &'a Expr, name: &str) -> Option<&'a Expr> {
    let Expr::Member(member) = expr.unwrap_parens() else {
        return None;
    };
    matches!(&member.prop, MemberProp::Ident(property) if property.sym.as_ref() == name)
        .then_some(member.obj.as_ref())
}

fn call(expr: &Expr, count: usize) -> Option<(&Expr, Vec<&Expr>)> {
    let Expr::Call(call) = expr.unwrap_parens() else {
        return None;
    };
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    (call.args.len() == count && call.args.iter().all(|arg| arg.spread.is_none())).then(|| {
        (
            callee.as_ref(),
            call.args.iter().map(|arg| arg.expr.as_ref()).collect(),
        )
    })
}

fn native_call<'a>(expr: &'a Expr, root: &Id, method: &str, count: usize) -> Option<Vec<&'a Expr>> {
    let (callee, args) = call(expr, count)?;
    ident(member(callee, method)?, root).then_some(args)
}

fn local_call<'a>(
    expr: &'a Expr,
    object: &Id,
    method: &str,
    count: usize,
) -> Option<Vec<&'a Expr>> {
    let (callee, args) = call(expr, count)?;
    ident(member(callee, method)?, object).then_some(args)
}

fn single_const(stmt: &Stmt) -> Option<(Id, &Expr)> {
    let Stmt::Decl(Decl::Var(decl)) = stmt else {
        return None;
    };
    let [declarator] = decl.decls.as_slice() else {
        return None;
    };
    let Pat::Ident(binding) = &declarator.name else {
        return None;
    };
    (decl.kind == VarDeclKind::Const && !decl.declare)
        .then(|| (binding.id.to_id(), declarator.init.as_deref()))
        .and_then(|(id, init)| Some((id, init?)))
}

fn single_stmt(stmt: &Stmt) -> &Stmt {
    match stmt {
        Stmt::Block(block) if block.stmts.len() == 1 => &block.stmts[0],
        other => other,
    }
}

fn one_if(stmt: &Stmt) -> Option<(&Expr, &Stmt)> {
    let Stmt::If(branch) = stmt else {
        return None;
    };
    branch
        .alt
        .is_none()
        .then_some((&branch.test, single_stmt(&branch.cons)))
}

fn expression(stmt: &Stmt) -> Option<&Expr> {
    let Stmt::Expr(expr) = stmt else {
        return None;
    };
    Some(&expr.expr)
}

fn returned(stmt: &Stmt, binding: &Id) -> bool {
    matches!(stmt, Stmt::Return(ret) if ret.arg.as_deref().is_some_and(|arg| ident(arg, binding)))
}

fn exact_object_test(expr: &Expr, value: &Id) -> bool {
    let Expr::Bin(and) = expr.unwrap_parens() else {
        return false;
    };
    if and.op != BinaryOp::LogicalAnd {
        return false;
    }
    let Expr::Bin(non_null) = and.left.unwrap_parens() else {
        return false;
    };
    let Expr::Bin(object_type) = and.right.unwrap_parens() else {
        return false;
    };
    matches!(non_null.op, BinaryOp::NotEqEq | BinaryOp::NotEq)
        && ident(&non_null.left, value)
        && matches!(non_null.right.unwrap_parens(), Expr::Lit(Lit::Null(_)))
        && matches!(object_type.op, BinaryOp::EqEqEq | BinaryOp::EqEq)
        && matches!(object_type.left.unwrap_parens(), Expr::Unary(unary)
            if unary.op == UnaryOp::TypeOf && ident(&unary.arg, value))
        && matches!(object_type.right.unwrap_parens(), Expr::Lit(Lit::Str(string))
            if string.value.as_str() == Some("object"))
}

fn cache_lookup(stmt: &Stmt, is_object: &Id, cache: &Id, value: &Id) -> bool {
    let Stmt::If(branch) = stmt else {
        return false;
    };
    if !ident(&branch.test, is_object) || branch.alt.is_some() {
        return false;
    }
    let Stmt::Block(body) = branch.cons.as_ref() else {
        return false;
    };
    let [declaration, conditional] = body.stmts.as_slice() else {
        return false;
    };
    let Some((cached, init)) = single_const(declaration) else {
        return false;
    };
    let Some(arguments) = local_call(init, cache, "get", 1) else {
        return false;
    };
    let [arg] = arguments.as_slice() else {
        return false;
    };
    let Some((test, on_hit)) = one_if(conditional) else {
        return false;
    };
    ident(arg, value) && ident(test, &cached) && returned(on_hit, &cached)
}

fn default_property(stmt: &Stmt, flag: &Id, namespace: &Id, value: &Id, object: &Id) -> bool {
    let Some((test, body)) = one_if(stmt) else {
        return false;
    };
    let Some(args) =
        expression(body).and_then(|expr| native_call(expr, object, "defineProperty", 3))
    else {
        return false;
    };
    let [target, key, descriptor] = args.as_slice() else {
        return false;
    };
    ident(test, flag)
        && ident(target, namespace)
        && matches!(key.unwrap_parens(), Expr::Lit(Lit::Str(string))
            if string.value.as_str() == Some("default"))
        && matches!(descriptor.unwrap_parens(), Expr::Object(object)
            if object.props.len() == 2
                && matches!(&object.props[0], PropOrSpread::Prop(property)
                    if matches!(property.as_ref(), Prop::KeyValue(pair)
                        if matches!(&pair.key, PropName::Ident(key) if key.sym.as_ref() == "enumerable")
                            && matches!(pair.value.unwrap_parens(), Expr::Lit(Lit::Bool(boolean)) if boolean.value)))
                && matches!(&object.props[1], PropOrSpread::Prop(property)
                    if matches!(property.as_ref(), Prop::Shorthand(id) if id.to_id() == *value)))
}

fn deferred_getter(expr: &Expr, value: &Id, name: &Id) -> bool {
    let Expr::Object(object) = expr.unwrap_parens() else {
        return false;
    };
    let [enumerable, getter] = object.props.as_slice() else {
        return false;
    };
    matches!(enumerable, PropOrSpread::Prop(property)
        if matches!(property.as_ref(), Prop::KeyValue(pair)
            if matches!(&pair.key, PropName::Ident(key) if key.sym.as_ref() == "enumerable")
                && matches!(pair.value.unwrap_parens(), Expr::Lit(Lit::Bool(boolean)) if boolean.value)))
        && matches!(getter, PropOrSpread::Prop(property)
            if matches!(property.as_ref(), Prop::Method(method)
                if matches!(&method.key, PropName::Ident(key) if key.sym.as_ref() == "get")
                    && method.function.params.is_empty()
                    && method.function.body.as_ref().is_some_and(|body| matches!(body.stmts.as_slice(),
                        [Stmt::Return(ret)] if ret.arg.as_deref().is_some_and(|arg| matches!(arg.unwrap_parens(), Expr::Member(member)
                            if ident(&member.obj, value)
                                && matches!(&member.prop, MemberProp::Computed(key) if ident(&key.expr, name))))))))
}

fn own_names_loop(stmt: &Stmt, namespace: &Id, value: &Id, object: &Id) -> bool {
    let Stmt::ForOf(loop_stmt) = stmt else {
        return false;
    };
    let ForHead::VarDecl(declaration) = &loop_stmt.left else {
        return false;
    };
    let [binding] = declaration.decls.as_slice() else {
        return false;
    };
    let Pat::Ident(name) = &binding.name else {
        return false;
    };
    let name = name.id.to_id();
    let Some(arguments) = native_call(&loop_stmt.right, object, "getOwnPropertyNames", 1) else {
        return false;
    };
    let [source] = arguments.as_slice() else {
        return false;
    };
    let Some((test, body)) = one_if(single_stmt(&loop_stmt.body)) else {
        return false;
    };
    let Expr::Unary(negated) = test.unwrap_parens() else {
        return false;
    };
    let Some((callee, args)) = call(&negated.arg, 2) else {
        return false;
    };
    let Some(prototype) =
        member(callee, "call").and_then(|callee| member(callee, "hasOwnProperty"))
    else {
        return false;
    };
    let Some(args_define) =
        expression(body).and_then(|expr| native_call(expr, object, "defineProperty", 3))
    else {
        return false;
    };
    matches!(declaration.kind, VarDeclKind::Const | VarDeclKind::Let)
        && !declaration.declare
        && binding.init.is_none()
        && !loop_stmt.is_await
        && ident(source, value)
        && negated.op == UnaryOp::Bang
        && member(prototype, "prototype").is_some_and(|root| ident(root, object))
        && matches!(args.as_slice(), [target, key] if ident(target, namespace) && ident(key, &name))
        && matches!(args_define.as_slice(), [target, key, descriptor]
            if ident(target, namespace) && ident(key, &name) && deferred_getter(descriptor, value, &name))
}

fn cache_store(stmt: &Stmt, is_object: &Id, cache: &Id, value: &Id, namespace: &Id) -> bool {
    let Some((test, body)) = one_if(stmt) else {
        return false;
    };
    ident(test, is_object)
        && expression(body)
            .and_then(|expr| local_call(expr, cache, "set", 2))
            .is_some_and(|args| {
                matches!(args.as_slice(), [key, stored]
                if ident(key, value) && ident(stored, namespace))
            })
}

/// Admit only the complete seven-step shape. Every eager call and returned
/// closure is accounted for; later semantic rules certify its prerequisites.
pub(super) fn candidate(function: &Function) -> Option<NamespaceWrapperCandidate> {
    if function.is_async || function.is_generator || !function.decorators.is_empty() {
        return None;
    }
    let [value, flag] = function.params.as_slice() else {
        return None;
    };
    if !value.decorators.is_empty() || !flag.decorators.is_empty() {
        return None;
    }
    let (Pat::Ident(value), Pat::Ident(flag)) = (&value.pat, &flag.pat) else {
        return None;
    };
    let value = value.id.to_id();
    let flag = flag.id.to_id();
    let [
        object_test,
        lookup,
        allocation,
        default,
        names,
        store,
        result,
    ] = function.body.as_ref()?.stmts.as_slice()
    else {
        return None;
    };
    let (is_object, test) = single_const(object_test)?;
    if !exact_object_test(test, &value) {
        return None;
    }
    let Stmt::If(lookup_branch) = lookup else {
        return None;
    };
    let Stmt::Block(lookup_body) = lookup_branch.cons.as_ref() else {
        return None;
    };
    // The cache is recovered from the receiver of the exact `get` call.
    let (cached, cached_init) = single_const(lookup_body.stmts.first()?)?;
    let _ = cached;
    let (cache_receiver, _) = call(cached_init, 1)?;
    let cache = ident_from(member(cache_receiver, "get")?)?;
    if !cache_lookup(lookup, &is_object, &cache, &value) {
        return None;
    }
    let (namespace, create) = single_const(allocation)?;
    let (create_callee, _) = call(create, 1)?;
    let object = ident_from(member(create_callee, "create")?)?;
    let arguments = native_call(create, &object, "create", 1)?;
    let [prototype] = arguments.as_slice() else {
        return None;
    };
    let prototype_arguments = native_call(prototype, &object, "getPrototypeOf", 1)?;
    let [source] = prototype_arguments.as_slice() else {
        return None;
    };
    (ident(source, &value)
        && default_property(default, &flag, &namespace, &value, &object)
        && own_names_loop(names, &namespace, &value, &object)
        && cache_store(store, &is_object, &cache, &value, &namespace)
        && returned(result, &namespace))
    .then_some(NamespaceWrapperCandidate {
        cache,
        object,
        function_end: function.span.hi.0,
    })
}

fn ident_from(expr: &Expr) -> Option<Id> {
    let Expr::Ident(id) = expr.unwrap_parens() else {
        return None;
    };
    Some(id.to_id())
}
