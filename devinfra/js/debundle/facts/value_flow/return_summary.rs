//! Bounded relationships between one call's argument and its normal result.
//!
//! These are value-origin facts, not effect, native-integrity or completion
//! certificates. The ordinary invocation and property semantics still apply.

use swc_ecma_ast::*;

use super::{BindingSlot, FlowSymbols, PropertySlot};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ReturnSummary {
    Argument,
    ArgumentOrDefault,
    ArgumentProperty(PropertySlot),
    ForwardChoices([BindingSlot; 2]),
}

pub(super) fn function(function: &Function, symbols: &mut FlowSymbols) -> Option<ReturnSummary> {
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
    expression(
        return_expression(function.body.as_ref()?)?,
        &parameter.id,
        symbols,
    )
}

pub(super) fn arrow(arrow: &ArrowExpr, symbols: &mut FlowSymbols) -> Option<ReturnSummary> {
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
    expression(returned, &parameter.id, symbols)
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

fn expression(
    expression: &Expr,
    parameter: &Ident,
    symbols: &mut FlowSymbols,
) -> Option<ReturnSummary> {
    if is_parameter(expression, parameter) {
        return Some(ReturnSummary::Argument);
    }
    let returned = match expression.unwrap_parens() {
        Expr::Seq(sequence) => {
            let (returned, prefix) = sequence.exprs.split_last()?;
            if !prefix
                .iter()
                .all(|expression| is_effect_prefix(expression, parameter))
            {
                return None;
            }
            returned.as_ref().unwrap_parens()
        }
        returned => returned,
    };
    if let Expr::Member(member) = returned
        && is_parameter(&member.obj, parameter)
        && let MemberProp::Ident(property) = &member.prop
    {
        return Some(ReturnSummary::ArgumentProperty(
            symbols.property(property.sym.to_string()),
        ));
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

fn is_effect_prefix(expression: &Expr, parameter: &Ident) -> bool {
    let Expr::Call(call) = expression.unwrap_parens() else {
        return false;
    };
    if !call.args.is_empty() {
        return false;
    }
    let Callee::Expr(callee) = &call.callee else {
        return false;
    };
    matches!(callee.unwrap_parens(), Expr::Ident(identifier)
        if identifier.sym != "eval" && identifier.to_id() != parameter.to_id())
}

/// Prepared from the final return only; the existing lowering walk separately
/// certifies all writes, dynamic scope and owned returns before admission.
pub(super) struct Candidate {
    pub parameter: BindingSlot,
    pub return_start: u32,
    pub summary: ReturnSummary,
}

pub(super) fn candidate(function: &Function, symbols: &mut FlowSymbols) -> Option<Candidate> {
    if function.is_async || function.is_generator || !function.decorators.is_empty() {
        return None;
    }
    let first = function.params.first()?;
    let Pat::Ident(parameter) = &first.pat else {
        return None;
    };
    let mut parameters = std::collections::BTreeSet::new();
    for parameter in &function.params {
        let Pat::Ident(binding) = &parameter.pat else {
            return None;
        };
        if !parameter.decorators.is_empty() || !parameters.insert(binding.id.to_id()) {
            return None;
        }
    }
    let Stmt::Return(returned) = function.body.as_ref()?.stmts.last()? else {
        return None;
    };
    let mut tail = returned.arg.as_deref()?.unwrap_parens();
    if let Expr::Seq(sequence) = tail {
        tail = sequence.exprs.last()?.unwrap_parens();
    }
    let summary = if is_parameter(tail, &parameter.id) {
        ReturnSummary::Argument
    } else if let Expr::Cond(conditional) = tail {
        let helper = |expression: &Expr, symbols: &mut FlowSymbols| {
            let Expr::Call(call) = expression.unwrap_parens() else {
                return None;
            };
            let [argument] = call.args.as_slice() else {
                return None;
            };
            if argument.spread.is_some() || !is_parameter(&argument.expr, &parameter.id) {
                return None;
            }
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            let Expr::Ident(callee) = callee.unwrap_parens() else {
                return None;
            };
            Some(symbols.binding(callee.to_id()))
        };
        ReturnSummary::ForwardChoices([
            helper(&conditional.cons, symbols)?,
            helper(&conditional.alt, symbols)?,
        ])
    } else {
        return None;
    };
    Some(Candidate {
        parameter: symbols.binding(parameter.id.to_id()),
        return_start: returned.span.lo.0,
        summary,
    })
}

#[cfg(test)]
mod finite_tests {
    use super::*;
    use std::collections::BTreeSet;

    fn summary(source: &str) -> Option<ReturnSummary> {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module_ast("<return-summary>", source)
                .expect("valid module fixture");
            let ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) = &module.body[0] else {
                panic!("first function fixture");
            };
            let function = function.function.span.lo.0;
            let views = crate::facts::top_level_item_views(&module.body);
            let program = super::super::FlowProgram::collect(&views, &BTreeSet::new());
            program.return_summaries.get(&function).copied()
        })
    }

    #[test]
    fn identity_leaf_preserves_property_effects_and_ignores_nested_returns() {
        assert_eq!(
            summary(
                "function keep(p) { p.label = 'x'; function nested() { return 1; } return (effect(), p); }"
            ),
            Some(ReturnSummary::Argument)
        );
        // All source is parsed as strict modules: arguments is not a parameter alias.
        assert_eq!(
            summary("function keep(p) { arguments[0] = other; return p; }"),
            Some(ReturnSummary::Argument)
        );
    }

    #[test]
    fn identity_leaf_declines_every_parameter_rebinding_family() {
        for body in [
            "var p = other; return p;",
            "function p() {} return p;",
            "function nested() { p = other; } nested(); return p;",
            "({value: p} = source); return p;",
            "for (var p of source) {} return p;",
            "p++; return p;",
            "if (flag) return; return p;",
            "eval('p = other'); return p;",
        ] {
            assert_eq!(
                summary(&format!("function keep(p) {{ {body} }}")),
                None,
                "{body}"
            );
        }
    }

    #[test]
    fn forwarding_is_exactly_two_direct_calls_of_the_original_parameter() {
        assert!(matches!(
            summary(
                "function choose(p, unused) { effect(); return (effect(), flag ? left(p) : right(p)); }"
            ),
            Some(ReturnSummary::ForwardChoices(_))
        ));
        for expression in [
            "flag ? left(other) : right(p)",
            "flag ? left(...p) : right(p)",
            "flag ? object.left(p) : right(p)",
        ] {
            assert_eq!(
                summary(&format!("function choose(p) {{ return {expression}; }}")),
                None
            );
        }
    }
}
