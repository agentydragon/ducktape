//! Primitive types of operator results and of the expressions built from them.
//!
//! Every result is the set of types the value can have when evaluation
//! completes; `None` means it may be an object. Throwing is not modeled here:
//! an operation that always throws on its operands yields the empty set, and
//! callers keep classifying the evaluation's own effects and throws.

use super::builtins::{self, ArgSel, Invocation, Return, Row};
use super::value::PrimKinds;
use swc_ecma_ast::*;

/// Where `evaluate` reads identifiers.
pub trait PrimScope {
    /// The types of the binding `ident` resolves to, when they are tracked.
    fn binding(&self, ident: &Ident) -> Option<PrimKinds>;

    /// Whether `global` (with `member`, `global.member`) names the intact
    /// intrinsic `row` summarizes.
    fn intact_intrinsic(&self, row: &Row, global: &Ident, member: Option<&str>) -> bool;
}

/// A scope that tracks no binding and no intrinsic.
pub struct NoBindings;

impl PrimScope for NoBindings {
    fn binding(&self, _: &Ident) -> Option<PrimKinds> {
        None
    }

    fn intact_intrinsic(&self, _: &Row, _: &Ident, _: Option<&str>) -> bool {
        false
    }
}

const NOT_NUMERIC: PrimKinds = PrimKinds::ANY
    .without(PrimKinds::BIGINT)
    .without(PrimKinds::SYMBOL);

/// `ToNumeric` (ECMA-262 § 7.1.3) after `ToPrimitive`: an object operand can
/// produce any primitive.
fn to_numeric(operand: Option<PrimKinds>) -> PrimKinds {
    let operand = operand.unwrap_or(PrimKinds::ANY);
    let mut result = PrimKinds::NONE;
    if operand.intersects(NOT_NUMERIC) {
        result = result.union(PrimKinds::NUMBER);
    }
    if operand.intersects(PrimKinds::BIGINT) {
        result = result.union(PrimKinds::BIGINT);
    }
    result
}

/// ECMA-262 § 13.5 unary operators.
fn unary(op: UnaryOp, operand: Option<PrimKinds>) -> PrimKinds {
    match op {
        UnaryOp::TypeOf => PrimKinds::STRING,
        UnaryOp::Void => PrimKinds::UNDEFINED,
        UnaryOp::Bang | UnaryOp::Delete => PrimKinds::BOOLEAN,
        // `ToNumber` throws on BigInt and Symbol.
        UnaryOp::Plus => {
            if operand.unwrap_or(PrimKinds::ANY).intersects(NOT_NUMERIC) {
                PrimKinds::NUMBER
            } else {
                PrimKinds::NONE
            }
        }
        UnaryOp::Minus | UnaryOp::Tilde => to_numeric(operand),
    }
}

/// ECMA-262 § 13.4 update expressions.
fn update(operand: Option<PrimKinds>) -> PrimKinds {
    to_numeric(operand)
}

/// ECMA-262 § 13.15.3 `ApplyStringOrNumericBinaryOperator` and the relational
/// and equality operators. `None` for the short-circuit operators, whose
/// result is one of their operands.
fn binary(op: BinaryOp, left: Option<PrimKinds>, right: Option<PrimKinds>) -> Option<PrimKinds> {
    let (left, right) = (
        left.unwrap_or(PrimKinds::ANY),
        right.unwrap_or(PrimKinds::ANY),
    );
    let numeric = |unsigned_shift: bool| {
        let mut result = PrimKinds::NONE;
        if left.intersects(NOT_NUMERIC) && right.intersects(NOT_NUMERIC) {
            result = result.union(PrimKinds::NUMBER);
        }
        // BigInt has no unsigned right shift; mixing BigInt with Number throws.
        if !unsigned_shift
            && left.intersects(PrimKinds::BIGINT)
            && right.intersects(PrimKinds::BIGINT)
        {
            result = result.union(PrimKinds::BIGINT);
        }
        result
    };
    Some(match op {
        BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing => return None,
        BinaryOp::EqEq
        | BinaryOp::NotEq
        | BinaryOp::EqEqEq
        | BinaryOp::NotEqEq
        | BinaryOp::Lt
        | BinaryOp::LtEq
        | BinaryOp::Gt
        | BinaryOp::GtEq
        | BinaryOp::In
        | BinaryOp::InstanceOf => PrimKinds::BOOLEAN,
        BinaryOp::Add => {
            // A String operand converts the other with `ToString`, which
            // throws only on Symbol.
            let not_symbol = PrimKinds::ANY.without(PrimKinds::SYMBOL);
            let string = (left.intersects(PrimKinds::STRING) && right.intersects(not_symbol))
                || (right.intersects(PrimKinds::STRING) && left.intersects(not_symbol));
            let non_string = PrimKinds::ANY.without(PrimKinds::STRING);
            let mut result = if left.intersects(non_string) && right.intersects(non_string) {
                numeric(false)
            } else {
                PrimKinds::NONE
            };
            if string {
                result = result.union(PrimKinds::STRING);
            }
            result
        }
        BinaryOp::ZeroFillRShift => numeric(true),
        BinaryOp::Sub
        | BinaryOp::Mul
        | BinaryOp::Div
        | BinaryOp::Mod
        | BinaryOp::Exp
        | BinaryOp::BitOr
        | BinaryOp::BitXor
        | BinaryOp::BitAnd
        | BinaryOp::LShift
        | BinaryOp::RShift => numeric(false),
    })
}

/// The primitive types `expr` can complete with, or `None` when it may be an
/// object or its shape is not modeled.
pub fn evaluate(expr: &Expr, scope: &impl PrimScope) -> Option<PrimKinds> {
    match expr.unwrap_parens() {
        Expr::Lit(literal) => match literal {
            Lit::Str(_) => Some(PrimKinds::STRING),
            Lit::Num(_) => Some(PrimKinds::NUMBER),
            Lit::Bool(_) => Some(PrimKinds::BOOLEAN),
            Lit::Null(_) => Some(PrimKinds::NULL),
            Lit::BigInt(_) => Some(PrimKinds::BIGINT),
            Lit::Regex(_) | Lit::JSXText(_) => None,
        },
        Expr::Tpl(_) => Some(PrimKinds::STRING),
        Expr::Ident(ident) => scope.binding(ident),
        Expr::Unary(unary_expr) => Some(unary(unary_expr.op, evaluate(&unary_expr.arg, scope))),
        Expr::Update(update_expr) => Some(update(evaluate(&update_expr.arg, scope))),
        Expr::Bin(binary_expr) => {
            let left = evaluate(&binary_expr.left, scope);
            let right = evaluate(&binary_expr.right, scope);
            binary(binary_expr.op, left, right).or_else(|| Some(left?.union(right?)))
        }
        Expr::Cond(cond) => Some(evaluate(&cond.cons, scope)?.union(evaluate(&cond.alt, scope)?)),
        Expr::Seq(sequence) => evaluate(sequence.exprs.last()?, scope),
        Expr::Call(call) => intrinsic_call(call, scope),
        _ => None,
    }
}

/// A builtin row's primitive return, when the callee is its intact intrinsic
/// and every operand the row coerces is primitive.
fn intrinsic_call(call: &CallExpr, scope: &impl PrimScope) -> Option<PrimKinds> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let (global, member) = match callee.unwrap_parens() {
        Expr::Ident(global) => (global, None),
        Expr::Member(member) => match (member.obj.unwrap_parens(), &member.prop) {
            (Expr::Ident(global), MemberProp::Ident(name)) => (global, Some(name.sym.as_ref())),
            _ => return None,
        },
        _ => return None,
    };
    let row = builtins::lookup(global.sym.as_ref(), member, Invocation::Call)?;
    if !scope.intact_intrinsic(row, global, member)
        || !call.args.iter().all(|argument| argument.spread.is_none())
    {
        return None;
    }
    let Return::Prim(kinds) = row.returns else {
        return None;
    };
    let admitted = row.arity.contains(&call.args.len())
        && row.coerces.iter().all(|(operand, _)| {
            let operands = match *operand {
                ArgSel::Arg(index) => call.args.get(index..=index).unwrap_or_default(),
                ArgSel::Rest(start) => call.args.get(start..).unwrap_or_default(),
            };
            operands.iter().all(|argument| {
                evaluate(&argument.expr, scope).is_some_and(|kinds| !kinds.is_empty())
            })
        });
    admitted.then_some(kinds)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Tracks `n` as Number, `b` as BigInt and `s` as String; `Symbol` and
    /// `Math` are intact.
    struct Bindings;

    impl PrimScope for Bindings {
        fn binding(&self, ident: &Ident) -> Option<PrimKinds> {
            match ident.sym.as_ref() {
                "n" => Some(PrimKinds::NUMBER),
                "b" => Some(PrimKinds::BIGINT),
                "s" => Some(PrimKinds::STRING),
                _ => None,
            }
        }

        fn intact_intrinsic(&self, _: &Row, global: &Ident, _: Option<&str>) -> bool {
            matches!(global.sym.as_ref(), "Symbol" | "Math")
        }
    }

    #[test]
    fn expression_types_follow_ecmascript_operator_rules() {
        let number_or_string = PrimKinds::NUMBER.union(PrimKinds::STRING);
        let numeric = PrimKinds::NUMBER.union(PrimKinds::BIGINT);
        for (source, expected) in [
            ("n + 1", Some(PrimKinds::NUMBER)),
            ("b * 2n", Some(PrimKinds::BIGINT)),
            ("n + b", Some(PrimKinds::NONE)),
            ("s + b", Some(PrimKinds::STRING)),
            ("object + 1", Some(number_or_string)),
            ("-object", Some(numeric)),
            ("+b", Some(PrimKinds::NONE)),
            ("b >>> 1n", Some(PrimKinds::NONE)),
            ("object < n", Some(PrimKinds::BOOLEAN)),
            ("n || s", Some(number_or_string)),
            ("n || object", None),
            ("Symbol('k')", Some(PrimKinds::SYMBOL)),
            ("Math.round(n)", Some(PrimKinds::NUMBER)),
            ("Math.round(object)", None),
            ("Math.max(n)", None),
        ] {
            let module = raw_js_test_support::parse(&format!("({source});"));
            let [ModuleItem::Stmt(Stmt::Expr(statement))] = module.body.as_slice() else {
                panic!("{source} parses as one expression statement");
            };
            assert_eq!(evaluate(&statement.expr, &Bindings), expected, "{source}");
        }
    }
}
