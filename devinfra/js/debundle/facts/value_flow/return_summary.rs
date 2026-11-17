//! Bounded relationships between one call's argument and its normal result.
//!
//! These are value-origin facts, not effect, native-integrity or completion
//! certificates. The ordinary invocation and property semantics still apply.

use swc_ecma_ast::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ReturnSummary {
    Argument,
    ArgumentOrDefault,
}

pub(super) fn function(function: &Function) -> Option<ReturnSummary> {
    if function.is_async || function.is_generator || !function.decorators.is_empty() {
        return None;
    }
    let [parameter] = function.params.as_slice() else {
        return None;
    };
    if !parameter.decorators.is_empty() {
        return None;
    }
    let Pat::Ident(parameter) = &parameter.pat else {
        return None;
    };
    expression(return_expression(function.body.as_ref()?)?, &parameter.id)
}

pub(super) fn arrow(arrow: &ArrowExpr) -> Option<ReturnSummary> {
    if arrow.is_async || arrow.is_generator {
        return None;
    }
    let [Pat::Ident(parameter)] = arrow.params.as_slice() else {
        return None;
    };
    let returned = match arrow.body.as_ref() {
        ArrowFunctionBody::Expr(expression) => expression.as_ref(),
        ArrowFunctionBody::FunctionBody(body) => return_expression(body)?,
    };
    expression(returned, &parameter.id)
}

fn return_expression(body: &FunctionBody) -> Option<&Expr> {
    let [Stmt::Return(returned)] = body.stmts.as_slice() else {
        return None;
    };
    returned.arg.as_deref()
}

fn is_parameter(expression: &Expr, parameter: &Ident) -> bool {
    matches!(expression.unwrap_parens(), Expr::Ident(identifier) if identifier.to_id() == parameter.to_id())
}

fn member<'a>(expression: &'a Expr, property: &str) -> Option<&'a Expr> {
    let Expr::Member(member) = expression.unwrap_parens() else {
        return None;
    };
    matches!(&member.prop, MemberProp::Ident(name) if name.sym == property)
        .then_some(member.obj.as_ref())
}

fn and(expression: &Expr) -> Option<(&Expr, &Expr)> {
    let Expr::Bin(binary) = expression.unwrap_parens() else {
        return None;
    };
    (binary.op == BinaryOp::LogicalAnd).then_some((&binary.left, &binary.right))
}

fn expression(expression: &Expr, parameter: &Ident) -> Option<ReturnSummary> {
    if is_parameter(expression, parameter) {
        return Some(ReturnSummary::Argument);
    }
    let Expr::Cond(conditional) = expression.unwrap_parens() else {
        return None;
    };
    if !is_parameter(&conditional.alt, parameter)
        || !is_parameter(member(&conditional.cons, "default")?, parameter)
    {
        return None;
    }
    // Recognize the bounded interop shape. Nothing in the body can rebind the
    // parameter or expose its lexical cell. Guard identity is immaterial to
    // this return relation: either branch is included. Reads and the guard
    // call (including a shadowed/mutated guard) keep ordinary effect analysis.
    let (prefix, guard) = and(&conditional.test)?;
    let (value, flag) = and(prefix)?;
    if !is_parameter(value, parameter) || !is_parameter(member(flag, "__esModule")?, parameter) {
        return None;
    }
    let Expr::Call(guard) = guard.unwrap_parens() else {
        return None;
    };
    let Callee::Expr(callee) = &guard.callee else {
        return None;
    };
    let object = member(
        member(member(callee, "call")?, "hasOwnProperty")?,
        "prototype",
    )?;
    if !matches!(object.unwrap_parens(), Expr::Ident(object) if object.sym == "Object") {
        return None;
    }
    let [argument, key] = guard.args.as_slice() else {
        return None;
    };
    (argument.spread.is_none()
        && key.spread.is_none()
        && is_parameter(&argument.expr, parameter)
        && matches!(key.expr.unwrap_parens(), Expr::Lit(Lit::Str(key)) if key.value == "default"))
    .then_some(ReturnSummary::ArgumentOrDefault)
}
