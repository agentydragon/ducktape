//! Purity projection of the builtin summary table
//! (`facts/abstract_domain/builtins.rs`). A native call is pure when its row
//! invokes nothing, writes only fresh allocations or its plain-data operands,
//! and every listed coercion and type check runs no user code and completes
//! normally on the call's syntactic operands. Callee identity is the
//! unshadowed global spelling under A11; the census slots in `requires` are
//! the synchronous interpreter's premise, not purity's.

use super::*;
use crate::facts::abstract_domain::builtins::{self, ArgSel, Invocation, Row, Throws};
use crate::facts::abstract_domain::effect::CoerceOp;
use crate::facts::abstract_domain::heap::Mutation;

/// What an operand must syntactically be for the row's steps on it to run no
/// user code and complete normally.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Operand {
    /// The row does not inspect it.
    Any,
    /// A primitive: `ToString` completes for every primitive the
    /// result-primitive shapes can produce (never a Symbol).
    Primitive,
    /// A non-BigInt primitive literal, for `ToNumber`.
    Number,
    /// An ordinary object whose own properties are data properties.
    PlainData,
    /// A fresh object literal.
    Object,
    /// A fresh array literal, iterated by the intrinsic Array iterator.
    Array(Elements),
}

/// What iteration does with each element of a fresh array operand.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Elements {
    /// Stored or compared by identity.
    Any,
    /// Read as a `[key, value]` pair; `keyed` converts the key with
    /// `ToPropertyKey`.
    Entries { keyed: bool },
}

/// Whether the row can certify a call at all: it invokes nothing, writes
/// only fresh allocations or operands its coercions already require to be
/// plain data, and throws nothing a syntactic operand shape cannot exclude.
fn certifies(row: &Row) -> bool {
    row.invokes.is_empty()
        && matches!(row.throws, Throws::Never | Throws::UnlessObjects(_))
        && row.mutates.iter().all(|mutation| {
            matches!(
                mutation,
                Mutation::Integrity(_)
                    | Mutation::FreshOwnDescriptorsOf(_)
                    | Mutation::FreshOwnKeysOf(_)
                    | Mutation::FreshRetains(_)
            )
        })
}

/// The requirement on the operand at `index`; `None` when no syntactic shape
/// meets the row's steps on it.
fn operand(row: &Row, index: usize) -> Option<Operand> {
    let selects = |selector: ArgSel| match selector {
        ArgSel::Arg(arg) => arg == index,
        ArgSel::Rest(from) => index >= from,
    };
    let steps: Vec<CoerceOp> = row
        .coerces
        .iter()
        .filter(|(selector, _)| selects(*selector))
        .map(|&(_, step)| step)
        .collect();
    let frozen = row
        .mutates
        .iter()
        .any(|mutation| matches!(*mutation, Mutation::Integrity(selector) if selects(selector)));
    let object = matches!(row.throws, Throws::UnlessObjects(selectors)
        if selectors.iter().any(|&selector| selects(selector)));
    if steps.contains(&CoerceOp::Iterate) {
        return (!frozen && !object).then_some(Operand::Array(
            if steps.contains(&CoerceOp::EntryGet) {
                Elements::Entries {
                    keyed: steps.contains(&CoerceOp::EntryKeyToPropertyKey),
                }
            } else {
                Elements::Any
            },
        ));
    }
    let internal = frozen
        || steps.iter().any(|step| {
            matches!(
                step,
                CoerceOp::ToObject
                    | CoerceOp::Get
                    | CoerceOp::OwnPropertyKeys
                    | CoerceOp::GetOwnProperty
                    | CoerceOp::DefineOwnProperty
                    | CoerceOp::PreventExtensions
            )
        });
    let to_string = steps.contains(&CoerceOp::ToString);
    let to_number = steps.contains(&CoerceOp::ToNumber);
    match (internal, to_string, to_number, object) {
        (false, false, false, false) => Some(Operand::Any),
        // A plain-data operand is an object.
        (true, false, false, _) => Some(Operand::PlainData),
        (false, true, false, false) => Some(Operand::Primitive),
        (false, false, true, false) => Some(Operand::Number),
        (false, false, false, true) => Some(Operand::Object),
        _ => None,
    }
}

/// Whether every operand count is admitted and no operand is inspected, so
/// the call is pure whenever its arguments evaluate purely.
pub(crate) fn admits_any_operands(row: &Row) -> bool {
    certifies(row)
        && *row.arity.start() == 0
        && *row.arity.end() == usize::MAX
        && row.coerces.is_empty()
        && !row
            .mutates
            .iter()
            .any(|mutation| matches!(mutation, Mutation::Integrity(_)))
        && row.throws == Throws::Never
}

/// Whether a one-argument call reads its operand's own data or iterates it,
/// at most freezing it, and keeps no other reference to it.
pub(crate) fn only_inspects_operand(row: &Row) -> bool {
    certifies(row)
        && row.arity.contains(&1)
        && matches!(
            operand(row, 0),
            Some(Operand::PlainData | Operand::Array(_))
        )
        && row
            .mutates
            .iter()
            .all(|mutation| *mutation == Mutation::Integrity(ArgSel::Arg(0)))
}

/// The global spelling of a direct callee: `global` or `global.member`.
pub(crate) fn native_callee(callee: &Expr) -> Option<(&str, Option<&str>)> {
    match callee {
        Expr::Ident(global) => Some((global.sym.as_ref(), None)),
        Expr::Member(member) => {
            static_member(member).map(|(global, property)| (global, Some(property)))
        }
        _ => None,
    }
}

/// `global.property` with an identifier receiver and a static name.
pub(crate) fn static_member(member: &MemberExpr) -> Option<(&str, &str)> {
    let (Expr::Ident(global), MemberProp::Ident(property)) = (member.obj.as_ref(), &member.prop)
    else {
        return None;
    };
    Some((global.sym.as_ref(), property.sym.as_ref()))
}

/// One evaluated operand of a native call.
pub(crate) struct NativeOperand<'a> {
    pub(crate) expr: &'a Expr,
    pub(crate) spread: Option<Span>,
}

impl<'a> From<&'a ExprOrSpread> for NativeOperand<'a> {
    fn from(arg: &'a ExprOrSpread) -> Self {
        Self {
            expr: &arg.expr,
            spread: arg.spread,
        }
    }
}

/// The purity of a native call through its row: `None` when the callee is
/// not an unshadowed intrinsic with a certifying row or an operand's shape
/// cannot meet the row; otherwise the purity of evaluating the operands, with
/// a coercion reason for each operand that may run user code when converted.
/// `operands` are the evaluated arguments; a tagged template's engine-supplied
/// template object precedes them.
#[allow(clippy::too_many_arguments)]
pub(crate) fn native_call_purity(
    (global, member): (&str, Option<&str>),
    invocation: Invocation,
    operands: &[NativeOperand<'_>],
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    if shadowed.contains(global) || local_shadowed.contains(global) {
        return None;
    }
    let row = builtins::lookup(global, member, invocation)?;
    let first = usize::from(invocation == Invocation::Tag);
    if !certifies(row) || !row.arity.contains(&(first + operands.len())) {
        return None;
    }
    let spelling = member.map_or_else(|| global.to_string(), |member| format!("{global}.{member}"));
    let requirements = (first..first + operands.len())
        .map(|index| operand(row, index))
        .collect::<Option<Vec<_>>>()?;
    // A spread supplies an unknown number of operands.
    if operands.iter().any(|operand| operand.spread.is_some()) && !admits_any_operands(row) {
        return None;
    }
    let mut verdict = Purity::pure();
    for (operand, requirement) in operands.iter().zip(requirements) {
        let evaluation = || {
            let spread = operand.spread.map_or_else(Purity::pure, |span| {
                Purity::from_reason(PurityRule::ArraySpread, span)
            });
            spread.worst(classify_expr_purity(
                operand.expr,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            ))
        };
        let purity = match requirement {
            Operand::Any => evaluation(),
            Operand::Primitive => {
                let purity = evaluation();
                if is_result_primitive(operand.expr, &graph.primitive_bindings, local_shadowed) {
                    purity
                } else {
                    purity.worst(Purity::from_reason_with_detail(
                        PurityRule::CoercingOperator,
                        operand.expr.span(),
                        format!("{spelling} runs ToString on a possibly-object operand"),
                    ))
                }
            }
            Operand::Number => {
                let purity = evaluation();
                if matches!(
                    operand.expr.unwrap_parens(),
                    Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_))
                ) {
                    purity
                } else {
                    purity.worst(Purity::from_reason_with_detail(
                        PurityRule::CoercingOperator,
                        operand.expr.span(),
                        format!("{spelling} runs ToNumber on a possibly-object or BigInt operand"),
                    ))
                }
            }
            Operand::PlainData => {
                plain_data_operand(operand.expr, shadowed, local_shadowed, declared_pure, graph)?
            }
            Operand::Object => {
                if !matches!(operand.expr.unwrap_parens(), Expr::Object(_)) {
                    return None;
                }
                evaluation()
            }
            Operand::Array(elements) => {
                let Expr::Array(array) = operand.expr.unwrap_parens() else {
                    return None;
                };
                fresh_array_elements(
                    array,
                    elements,
                    &spelling,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                )?
            }
        };
        verdict = verdict.worst(purity);
    }
    Some(verdict)
}

/// The evaluation purity of an operand proved plain data: a fresh object
/// literal with only data properties, an array literal, an admitted
/// `PlainData` binding, or the namespace facade's fresh result.
fn plain_data_operand(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    match expr.unwrap_parens() {
        // A local binding of the same name is a different value.
        Expr::Ident(ident) => (graph.is_plain_data(ident.sym.as_ref())
            && !local_shadowed.contains(ident.sym.as_ref()))
        .then(|| {
            let purity = Purity::pure();
            match graph.plain_data_dependencies(ident.sym.as_ref()) {
                Some(dependencies) => purity.with_dependencies(dependencies),
                None => purity,
            }
        }),
        expr @ Expr::Object(object) => object
            .props
            .iter()
            .all(is_plain_data_prop)
            .then(|| classify_expr_purity(expr, shadowed, local_shadowed, declared_pure, graph)),
        expr @ Expr::Array(_) => Some(classify_expr_purity(
            expr,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            is_pure_object_define_property_on_fresh_namespace(
                callee,
                &call.args,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
            .then(|| all_args_pure(&call.args, shadowed, local_shadowed, declared_pure, graph))
        }
        _ => None,
    }
}

/// The evaluation purity of a fresh array literal's elements under the
/// iteration's per-element steps; `None` for a hole or a malformed entry.
fn fresh_array_elements(
    array: &ArrayLit,
    elements: Elements,
    spelling: &str,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    array
        .elems
        .iter()
        .map(|element| {
            // A hole is `undefined`, which an entry read rejects.
            let element = element.as_ref()?;
            iterable_element(
                element,
                elements,
                spelling,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
        })
        .try_fold(Purity::pure(), |verdict, purity| {
            Some(verdict.worst(purity?))
        })
}

/// One element of a fresh array iterated by a native row. A spread element
/// expands a fresh array source under the same element steps.
pub(crate) fn iterable_element(
    element: &ExprOrSpread,
    elements: Elements,
    spelling: &str,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    if let Some(spread) = element.spread {
        return Some(
            classify_fresh_array_spread_source(
                &element.expr,
                Some((elements, spelling)),
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
            .unwrap_or_else(|| Purity::from_reason(PurityRule::ArraySpread, spread)),
        );
    }
    let Elements::Entries { keyed } = elements else {
        return Some(classify_expr_purity(
            &element.expr,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ));
    };
    let Expr::Array(entry) = element.expr.unwrap_parens() else {
        return None;
    };
    let [Some(key), Some(value)] = entry.elems.as_slice() else {
        return None;
    };
    if key.spread.is_some() || value.spread.is_some() {
        return None;
    }
    let mut purity =
        classify_expr_purity(&key.expr, shadowed, local_shadowed, declared_pure, graph).worst(
            classify_expr_purity(&value.expr, shadowed, local_shadowed, declared_pure, graph),
        );
    if keyed
        && !is_safe_property_key(
            &key.expr,
            shadowed,
            local_shadowed,
            &graph.primitive_bindings,
            &graph.symbol_const_bindings,
        )
    {
        purity = purity.worst(Purity::from_reason_with_detail(
            PurityRule::ToPropertyKeyCoercion,
            key.expr.span(),
            format!("{spelling} runs ToPropertyKey on a possibly-object entry key"),
        ));
    }
    Some(purity)
}
