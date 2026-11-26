use super::*;

/// The primitive-only bindings a function reads, if it does nothing but
/// return an expression of operators and templates over its plain
/// parameters, literals and such bindings: called with primitives it runs no
/// other code, cannot throw and returns a primitive.
pub(super) fn primitive_function(
    function: &Expr,
    bindings: &FxHashMap<Id, Binding>,
    primitives: &FxHashSet<Binding>,
) -> Option<Vec<Binding>> {
    let (params, body): (Vec<&Pat>, &Expr) = match function {
        Expr::Arrow(arrow) if !arrow.is_async && !arrow.is_generator => (
            arrow.params.iter().collect(),
            match &*arrow.body {
                ArrowFunctionBody::Expr(body) => body,
                ArrowFunctionBody::FunctionBody(block) => returned(&block.stmts)?,
            },
        ),
        Expr::Fn(FnExpr { function, .. }) if !function.is_async && !function.is_generator => (
            function.params.iter().map(|param| &param.pat).collect(),
            returned(&function.body.as_ref()?.stmts)?,
        ),
        _ => return None,
    };
    let params: FxHashSet<Id> = params
        .into_iter()
        .map(|param| match param {
            Pat::Ident(name) => Some(name.id.to_id()),
            _ => None,
        })
        .collect::<Option<_>>()?;
    let mut reads = Vec::new();
    primitive(
        body,
        bindings,
        primitives,
        PrimitiveScope::FunctionParameters(&params),
        &mut reads,
    )
    .then_some(reads)
}

/// The expression of a body that is one `return`.
pub(super) fn returned(statements: &[Stmt]) -> Option<&Expr> {
    match statements {
        [
            Stmt::Return(ReturnStmt {
                arg: Some(value), ..
            }),
        ] => Some(value),
        _ => None,
    }
}

/// The named data properties of an object literal: the key, whether the
/// value is written as a primitive, and the value.
pub(super) fn literal_keys(object: &ObjectLit) -> Vec<(String, bool, Option<&Expr>)> {
    object
        .props
        .iter()
        .filter_map(|prop| match prop {
            PropOrSpread::Prop(prop) => match &**prop {
                Prop::Shorthand(key) => Some((key.sym.to_string(), false, None)),
                Prop::KeyValue(pair) => {
                    let primitive = literal_only(&pair.value);
                    let value = Some(&*pair.value);
                    match &pair.key {
                        PropName::Ident(key) => Some((key.sym.to_string(), primitive, value)),
                        PropName::Str(key) => Some((js_ast::str_value(key), primitive, value)),
                        _ => None,
                    }
                }
                _ => None,
            },
            PropOrSpread::Spread(_) => None,
        })
        .collect()
}

pub(super) fn unparen(mut expr: &Expr) -> &Expr {
    while let Expr::Paren(paren) = expr {
        expr = &paren.expr;
    }
    expr
}

/// An enum-shaped call: an inline function or arrow of one parameter whose
/// body only stores literals on the parameter under literal names, called
/// with `E || (E = {})` or `E || {}`.
pub(super) struct EnumCall<'a> {
    pub(super) target: &'a Ident,
    /// The argument assigns the binding: `E || (E = {})`.
    pub(super) assigns: bool,
    /// The call returns the parameter.
    pub(super) returns: bool,
}

pub(super) fn enum_call(expr: &Expr) -> Option<EnumCall<'_>> {
    let Expr::Call(CallExpr {
        callee: Callee::Expr(callee),
        args,
        ..
    }) = unparen(expr)
    else {
        return None;
    };
    let [
        ExprOrSpread {
            spread: None,
            expr: argument,
        },
    ] = args.as_slice()
    else {
        return None;
    };
    let Expr::Bin(BinExpr {
        op: BinaryOp::LogicalOr,
        left,
        right,
        ..
    }) = unparen(argument)
    else {
        return None;
    };
    let Expr::Ident(target) = unparen(left) else {
        return None;
    };
    let empty =
        |expr: &Expr| matches!(unparen(expr), Expr::Object(object) if object.props.is_empty());
    let assigns = match unparen(right) {
        Expr::Assign(assign) => {
            let fresh = assign.op == AssignOp::Assign
                && matches!(&assign.left, AssignTarget::Simple(SimpleAssignTarget::Ident(assigned))
                    if assigned.id.to_id() == target.to_id())
                && empty(&assign.right);
            if !fresh {
                return None;
            }
            true
        }
        other if empty(other) => false,
        _ => return None,
    };
    // The parameter, and the body as statements or as one expression.
    let (param, statements, expression): (&Pat, &[Stmt], Option<&Expr>) = match unparen(callee) {
        Expr::Fn(FnExpr {
            ident: None,
            function,
        }) if !function.is_async && !function.is_generator => match function.params.as_slice() {
            [param] if param.decorators.is_empty() => {
                (&param.pat, &function.body.as_ref()?.stmts, None)
            }
            _ => return None,
        },
        Expr::Arrow(arrow) if !arrow.is_async && !arrow.is_generator => {
            let [param] = arrow.params.as_slice() else {
                return None;
            };
            match &*arrow.body {
                ArrowFunctionBody::FunctionBody(block) => (param, &block.stmts, None),
                ArrowFunctionBody::Expr(body) => (param, &[], Some(body)),
            }
        }
        _ => return None,
    };
    let Pat::Ident(param) = param else {
        return None;
    };
    let param = param.id.to_id();
    let on_param =
        |expr: &Expr| matches!(unparen(expr), Expr::Ident(ident) if ident.to_id() == param);
    // `e.name = literal`, `e["name"] = literal`.
    let named = |assign: &AssignExpr| {
        let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left else {
            return false;
        };
        let key = match &member.prop {
            MemberProp::Ident(_) => true,
            MemberProp::Computed(computed) => matches!(&*computed.expr, Expr::Lit(Lit::Str(_))),
            MemberProp::PrivateName(_) => false,
        };
        assign.op == AssignOp::Assign && key && on_param(&member.obj) && literal_only(&assign.right)
    };
    let store = |expr: &Expr| {
        let Expr::Assign(assign) = unparen(expr) else {
            return false;
        };
        if named(assign) {
            return true;
        }
        // `e[e.name = literal] = "name"`.
        let AssignTarget::Simple(SimpleAssignTarget::Member(MemberExpr {
            obj,
            prop: MemberProp::Computed(computed),
            ..
        })) = &assign.left
        else {
            return false;
        };
        assign.op == AssignOp::Assign
            && on_param(obj)
            && matches!(&*assign.right, Expr::Lit(Lit::Str(_)))
            && matches!(unparen(&computed.expr), Expr::Assign(inner) if named(inner))
    };
    // Stores, then optionally the parameter itself.
    let mut parts: Vec<&Expr> = Vec::new();
    let mut returns = false;
    fn spread<'e>(expr: &'e Expr, parts: &mut Vec<&'e Expr>) {
        match unparen(expr) {
            Expr::Seq(seq) => parts.extend(seq.exprs.iter().map(|part| unparen(part))),
            other => parts.push(other),
        }
    }
    for (index, statement) in statements.iter().enumerate() {
        match statement {
            Stmt::Expr(statement) => spread(&statement.expr, &mut parts),
            Stmt::Return(ReturnStmt {
                arg: Some(value), ..
            }) if index + 1 == statements.len() => {
                spread(value, &mut parts);
                returns = true;
            }
            _ => return None,
        }
    }
    if let Some(expression) = expression {
        spread(expression, &mut parts);
        returns = true;
    }
    if returns {
        // What is returned must be the parameter.
        if !on_param(parts.pop()?) {
            return None;
        }
    }
    parts.iter().all(|part| store(part)).then_some(EnumCall {
        target,
        assigns,
        returns,
    })
}

/// The binding an enum-shaped statement defines: `enum_call` with the
/// argument `E || (E = {})`.
pub(super) fn enum_definition(expr: &Expr) -> Option<&Ident> {
    enum_call(expr)
        .filter(|call| call.assigns)
        .map(|call| call.target)
}
