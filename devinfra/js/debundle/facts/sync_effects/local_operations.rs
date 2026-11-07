//! Source-covered local operations collected before value lowering erases writes.

use super::value_model::{
    PropertyDefinition, ValueExpr, object_data_property_name, static_property_name,
};
use crate::facts::abstract_domain::value::Kind;
use analysis_model::PrimKinds;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum LocalBinding {
    Parameter,
    Var,
    Let,
    Const,
    /// These activation values must already be established by the caller.
    /// Source identity alone does not instantiate a function value.
    NamedSelf(u32),
    HoistedFunction(u32),
}

#[derive(Debug, Clone)]
pub(super) enum LocalCallee {
    Binding(Id),
    /// A direct closure expression is created before its arguments are
    /// evaluated. Creating it does not execute the body or its defaults.
    Creation(DeferredCallableCreation),
    Member {
        receiver: Id,
        property: String,
        span: Span,
    },
}

impl LocalCallee {
    fn lower(expression: &Expr, class_template: bool) -> Option<Self> {
        match expression.unwrap_parens() {
            // A direct eval reference can enter dynamic scope.
            Expr::Ident(binding) if binding.sym != "eval" => Some(Self::Binding(binding.to_id())),
            Expr::Fn(_) | Expr::Arrow(_) => {
                let LocalExpression::Closure(creation) =
                    LocalExpression::lower(expression, class_template)?
                else {
                    return None;
                };
                Some(Self::Creation(creation))
            }
            Expr::Member(member) => {
                let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                    return None;
                };
                let MemberProp::Ident(property) = &member.prop else {
                    return None;
                };
                Some(Self::Member {
                    receiver: receiver.to_id(),
                    property: property.sym.to_string(),
                    span: member.span,
                })
            }
            _ => None,
        }
    }
}

/// Home-object sites express source containment, never receiver ownership.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum DeferredCallableCreation {
    Function(u32),
    Arrow(u32),
    Method { callable: u32, home_object: u32 },
    Getter { callable: u32, home_object: u32 },
    Setter { callable: u32, home_object: u32 },
}

impl DeferredCallableCreation {
    pub(super) fn callable(self) -> u32 {
        match self {
            Self::Function(key) | Self::Arrow(key) => key,
            Self::Method { callable, .. }
            | Self::Getter { callable, .. }
            | Self::Setter { callable, .. } => callable,
        }
    }
    pub(super) fn home_object(self) -> Option<u32> {
        match self {
            Self::Function(_) | Self::Arrow(_) => None,
            Self::Method { home_object, .. }
            | Self::Getter { home_object, .. }
            | Self::Setter { home_object, .. } => Some(home_object),
        }
    }
}

#[derive(Debug, Clone)]
pub(super) enum LocalExpression {
    Value(ValueExpr),
    /// A canonical primitive literal has no coercion or user-code effect.
    PrimitiveLiteral(PrimKinds),
    /// The fresh receiver supplied by a checked constructor query.
    This,
    Sequence(Arc<[Self]>),
    /// Each substitution is evaluated and converted before the next one.
    Template {
        substitutions: Arc<[Self]>,
        span: Span,
    },
    Binary {
        op: BinaryOp,
        left: Arc<Self>,
        right: Arc<Self>,
        span: Span,
    },
    Record {
        allocation: u32,
        properties: Arc<[PropertyDefinition<Self>]>,
    },
    Array {
        allocation: u32,
        elements: Arc<[Option<Self>]>,
    },
    Assign {
        binding: Id,
        value: Arc<Self>,
        span: Span,
    },
    Closure(DeferredCallableCreation),
    Call {
        callee: LocalCallee,
        arguments: Arc<[Self]>,
        span: Span,
    },
    ReadField {
        receiver: Id,
        property: String,
        span: Span,
    },
    AssignField {
        receiver: Id,
        property: String,
        value: Arc<Self>,
        span: Span,
    },
    /// Class field definition and declared private-field assignment both
    /// contribute weak containment to the fresh-instance retention projection.
    /// This does not model public/private descriptor identity or authorize a
    /// later field read.
    DefineThisField {
        property: String,
        value: Arc<Self>,
    },
}

#[derive(Debug, Clone)]
pub(super) enum LocalOperation {
    Initialize(Id, Option<LocalExpression>),
    Evaluate(LocalExpression),
    Return(Option<LocalExpression>),
    Throw(LocalExpression),
}

#[derive(Debug, Clone)]
pub(super) struct LocalParameter {
    pub(super) binding: Id,
    pub(super) default: Option<LocalExpression>,
}

#[derive(Debug, Clone)]
pub(super) struct LocalBody {
    pub(super) bindings: BTreeMap<Id, LocalBinding>,
    pub(super) parameters: Arc<[LocalParameter]>,
    pub(super) operations: Arc<[LocalOperation]>,
    /// Present only for a root variable initializer lowered from its exact
    /// canonical source expression. Callable bodies have no root-owner span.
    initializer_span: Option<Span>,
    /// Sorted source reads outside this body's parameters and declarations.
    /// Shared across body clones and reused by every invocation/provider query.
    free_reads: Option<Arc<[Id]>>,
    captured_writes: Option<Arc<[Id]>>,
    allocation_only_syntax: bool,
    closure_allocation_syntax: bool,
    closure_sites: Arc<[u32]>,
    /// These operations have a shared provider transfer, but the application
    /// interpreter has no keyed-collection heap lane. Keep that interpreter on
    /// its existing flat path when the body contains one.
    requires_keyed_collection_new: bool,
}

fn is_keyed_collection_new(value: &ValueExpr) -> bool {
    matches!(
        value,
        ValueExpr::New(callee, arguments, _)
            if arguments.is_empty()
                && matches!(callee.as_ref(), ValueExpr::Binding(binding)
                    if matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet"))
    )
}

fn value_contains_keyed_collection_new(value: &ValueExpr) -> bool {
    match value {
        ValueExpr::New(callee, arguments, _) => {
            is_keyed_collection_new(value)
                || value_contains_keyed_collection_new(callee)
                || arguments.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::Member(object, ..)
        | ValueExpr::ArrayIteratorPrototype(object)
        | ValueExpr::SetIteratorPrototype(object)
        | ValueExpr::LogicalNot(object)
        | ValueExpr::Void(object) => value_contains_keyed_collection_new(object),
        ValueExpr::Call(callee, arguments, _) => {
            value_contains_keyed_collection_new(callee)
                || arguments.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::Primitive(operands)
        | ValueExpr::Sequence(operands)
        | ValueExpr::Alternatives(operands) => {
            operands.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .any(|(_, value)| value_contains_keyed_collection_new(value)),
        ValueExpr::StrictEquality(left, right) => {
            value_contains_keyed_collection_new(left) || value_contains_keyed_collection_new(right)
        }
        ValueExpr::Conditional { test, alternatives } => {
            value_contains_keyed_collection_new(test)
                || alternatives.iter().any(value_contains_keyed_collection_new)
        }
        ValueExpr::Binding(_)
        | ValueExpr::Alloc(..)
        | ValueExpr::Undefined
        | ValueExpr::NonCallable(_)
        | ValueExpr::Unknown => false,
    }
}

fn simple_binding(pattern: &Pat) -> Option<Id> {
    match pattern {
        Pat::Ident(binding) if binding.type_ann.is_none() && !binding.id.optional => {
            Some(binding.id.to_id())
        }
        _ => None,
    }
}

fn lower_simple_var_declaration(
    declaration: &VarDecl,
    class_template: bool,
    bindings: &mut BTreeMap<Id, LocalBinding>,
    operations: &mut Vec<LocalOperation>,
) -> Option<()> {
    let kind = match declaration.kind {
        VarDeclKind::Var => LocalBinding::Var,
        VarDeclKind::Let => LocalBinding::Let,
        VarDeclKind::Const => LocalBinding::Const,
    };
    for declarator in &declaration.decls {
        if declarator.definite {
            return None;
        }
        let binding = simple_binding(&declarator.name)?;
        match bindings.get(&binding) {
            Some(LocalBinding::Var | LocalBinding::Parameter) if kind == LocalBinding::Var => {}
            Some(_) => return None,
            None => {
                bindings.insert(binding.clone(), kind);
            }
        }
        let value = LocalExpression::optional(declarator.init.as_deref(), class_template)?;
        if kind == LocalBinding::Const && value.is_none() {
            return None;
        }
        operations.push(LocalOperation::Initialize(binding, value));
    }
    Some(())
}

fn literal_definition_name(name: &PropName) -> Option<String> {
    matches!(
        name,
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
    )
    .then(|| object_data_property_name(name))
    .flatten()
}

/// The central allocation transfer has no conditional-path provenance. Keep
/// branches outside this ordered subset until their effects can be tagged.
fn unconditional_value(value: &ValueExpr) -> bool {
    match value {
        ValueExpr::Binding(_) | ValueExpr::NonCallable(_) | ValueExpr::Undefined => true,
        ValueExpr::Alloc(Kind::RegExp, _) => true,
        ValueExpr::New(..) if is_keyed_collection_new(value) => true,
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .all(|(_, value)| unconditional_value(value)),
        ValueExpr::StrictEquality(left, right) => {
            unconditional_value(left) && unconditional_value(right)
        }
        ValueExpr::Void(value) | ValueExpr::LogicalNot(value) => unconditional_value(value),
        ValueExpr::Sequence(values) => values.iter().all(unconditional_value),
        _ => false,
    }
}

impl LocalExpression {
    fn optional(expression: Option<&Expr>, class_template: bool) -> Option<Option<Self>> {
        match expression {
            Some(expression) => Some(Some(Self::lower(expression, class_template)?)),
            None => Some(None),
        }
    }

    fn lower(expression: &Expr, class_template: bool) -> Option<Self> {
        Self::lower_with_binary(expression, class_template, true)
    }

    fn lower_with_binary(
        expression: &Expr,
        class_template: bool,
        allow_binary: bool,
    ) -> Option<Self> {
        match expression.unwrap_parens() {
            Expr::Lit(Lit::Str(_)) => Some(Self::PrimitiveLiteral(PrimKinds::STRING)),
            Expr::Lit(Lit::Num(_)) => Some(Self::PrimitiveLiteral(PrimKinds::NUMBER)),
            Expr::Lit(Lit::Bool(_)) => Some(Self::PrimitiveLiteral(PrimKinds::BOOLEAN)),
            Expr::Lit(Lit::Null(_)) => Some(Self::PrimitiveLiteral(PrimKinds::NULL)),
            Expr::Lit(Lit::BigInt(_)) => Some(Self::PrimitiveLiteral(PrimKinds::BIGINT)),
            Expr::Object(object) => {
                let properties = object
                    .props
                    .iter()
                    .map(|property| {
                        let PropOrSpread::Prop(property) = property else {
                            return None;
                        };
                        match property.as_ref() {
                            Prop::Shorthand(binding) => Some(PropertyDefinition::Data {
                                name: Some(binding.sym.to_string()),
                                value: Self::Value(ValueExpr::Binding(binding.to_id())),
                            }),
                            Prop::KeyValue(pair) => Some(PropertyDefinition::Data {
                                name: Some(literal_definition_name(&pair.key)?),
                                value: Self::lower_with_binary(
                                    &pair.value,
                                    class_template,
                                    allow_binary,
                                )?,
                            }),
                            Prop::Method(method)
                                if method.function.decorators.is_empty()
                                    && method
                                        .function
                                        .params
                                        .iter()
                                        .all(|parameter| parameter.decorators.is_empty()) =>
                            {
                                Some(PropertyDefinition::Method {
                                    name: Some(literal_definition_name(&method.key)?),
                                    value: Self::Closure(DeferredCallableCreation::Method {
                                        callable: method.function.span.lo.0,
                                        home_object: object.span.lo.0,
                                    }),
                                })
                            }
                            Prop::Getter(getter)
                                if getter.function.decorators.is_empty()
                                    && getter
                                        .function
                                        .params
                                        .iter()
                                        .all(|parameter| parameter.decorators.is_empty()) =>
                            {
                                Some(PropertyDefinition::Getter {
                                    name: Some(literal_definition_name(&getter.key)?),
                                    value: Self::Closure(DeferredCallableCreation::Getter {
                                        callable: getter.function.span.lo.0,
                                        home_object: object.span.lo.0,
                                    }),
                                })
                            }
                            Prop::Setter(setter)
                                if setter.function.decorators.is_empty()
                                    && setter
                                        .function
                                        .params
                                        .iter()
                                        .all(|parameter| parameter.decorators.is_empty()) =>
                            {
                                Some(PropertyDefinition::Setter {
                                    name: Some(literal_definition_name(&setter.key)?),
                                    value: Self::Closure(DeferredCallableCreation::Setter {
                                        callable: setter.function.span.lo.0,
                                        home_object: object.span.lo.0,
                                    }),
                                })
                            }
                            _ => None,
                        }
                    })
                    .collect::<Option<Arc<[_]>>>()?;
                Some(Self::Record {
                    allocation: object.span.lo.0,
                    properties,
                })
            }
            Expr::Array(array) => Some(Self::Array {
                allocation: array.span.lo.0,
                elements: array
                    .elems
                    .iter()
                    .map(|element| match element {
                        None => Some(None),
                        Some(element) if element.spread.is_none() => Some(Some(
                            Self::lower_with_binary(&element.expr, class_template, allow_binary)?,
                        )),
                        _ => None,
                    })
                    .collect::<Option<Arc<[_]>>>()?,
            }),
            Expr::Tpl(template) if template.exprs.is_empty() => {
                Some(Self::Value(ValueExpr::NonCallable(PrimKinds::STRING)))
            }
            Expr::Tpl(template) => Some(Self::Template {
                substitutions: template
                    .exprs
                    .iter()
                    .map(|expression| {
                        Self::lower_with_binary(expression, class_template, allow_binary)
                    })
                    .collect::<Option<Arc<[_]>>>()?,
                span: template.span,
            }),
            Expr::Fn(function)
                if (function.ident.is_none() || !class_template)
                    && function.function.decorators.is_empty()
                    && function
                        .function
                        .params
                        .iter()
                        .all(|parameter| parameter.decorators.is_empty()) =>
            {
                Some(Self::Closure(DeferredCallableCreation::Function(
                    function.function.span.lo.0,
                )))
            }
            Expr::Arrow(arrow) => Some(Self::Closure(DeferredCallableCreation::Arrow(
                arrow.span.lo.0,
            ))),
            Expr::Seq(sequence) => Some(Self::Sequence(
                sequence
                    .exprs
                    .iter()
                    .map(|expression| {
                        Self::lower_with_binary(expression, class_template, allow_binary)
                    })
                    .collect::<Option<Arc<[_]>>>()?,
            )),
            Expr::Bin(binary) if allow_binary && ordered_primitive_binary(binary.op) => {
                Some(Self::Binary {
                    op: binary.op,
                    left: Arc::new(Self::lower_with_binary(
                        &binary.left,
                        class_template,
                        allow_binary,
                    )?),
                    right: Arc::new(Self::lower_with_binary(
                        &binary.right,
                        class_template,
                        allow_binary,
                    )?),
                    span: binary.span,
                })
            }
            Expr::Call(call) if !call.args.iter().any(|argument| argument.spread.is_some()) => {
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                let callee = LocalCallee::lower(callee, class_template)?;
                Some(Self::Call {
                    callee,
                    arguments: call
                        .args
                        .iter()
                        .map(|argument| {
                            Self::lower_with_binary(&argument.expr, class_template, allow_binary)
                        })
                        .collect::<Option<Arc<[_]>>>()?,
                    span: call.span,
                })
            }
            Expr::Member(member) => {
                let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                    return None;
                };
                let MemberProp::Ident(property) = &member.prop else {
                    return None;
                };
                Some(Self::ReadField {
                    receiver: receiver.to_id(),
                    property: property.sym.to_string(),
                    span: member.span,
                })
            }
            Expr::Assign(assignment) if assignment.op == AssignOp::Assign => {
                let value = Arc::new(Self::lower_with_binary(
                    &assignment.right,
                    class_template,
                    allow_binary,
                )?);
                match &assignment.left {
                    AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                        Some(Self::Assign {
                            binding: binding.id.to_id(),
                            value,
                            span: assignment.span,
                        })
                    }
                    AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                        if class_template
                            && matches!(member.obj.unwrap_parens(), Expr::This(_))
                            && let MemberProp::PrivateName(private) = &member.prop
                        {
                            return Some(Self::DefineThisField {
                                property: private.name.to_string(),
                                value,
                            });
                        }
                        let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                            return None;
                        };
                        let MemberProp::Ident(property) = &member.prop else {
                            return None;
                        };
                        Some(Self::AssignField {
                            receiver: receiver.to_id(),
                            property: property.sym.to_string(),
                            value,
                            span: assignment.span,
                        })
                    }
                    _ => None,
                }
            }
            Expr::This(_) if class_template => Some(Self::This),
            _ => {
                let (value, covered) = ValueExpr::from_expr_with_coverage(expression);
                let keyed_collection_new = is_keyed_collection_new(&value);
                (covered || keyed_collection_new)
                    .then(|| unconditional_value(&value).then_some(Self::Value(value)))
                    .flatten()
            }
        }
    }

    fn reads(&self, reads: &mut BTreeSet<Id>) {
        match self {
            Self::Value(value) => value.bindings(reads),
            Self::PrimitiveLiteral(_) => {}
            Self::Record { properties, .. } => {
                for property in properties.iter() {
                    let value = property.value();
                    value.reads(reads);
                }
            }
            Self::Array { elements, .. } => {
                for element in elements.iter().flatten() {
                    element.reads(reads);
                }
            }
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => {
                for expression in expressions.iter() {
                    expression.reads(reads);
                }
            }
            Self::Binary { left, right, .. } => {
                left.reads(reads);
                right.reads(reads);
            }
            Self::Assign { binding, value, .. } => {
                reads.insert(binding.clone());
                value.reads(reads);
            }
            Self::Closure(_) | Self::This => {}
            Self::Call {
                callee, arguments, ..
            } => {
                match callee {
                    LocalCallee::Binding(binding)
                    | LocalCallee::Member {
                        receiver: binding, ..
                    } => {
                        reads.insert(binding.clone());
                    }
                    // Capturing a cell does not GetValue it. The invoked body
                    // owns its actual reads and effects.
                    LocalCallee::Creation(_) => {}
                }
                for argument in arguments.iter() {
                    argument.reads(reads);
                }
            }
            Self::ReadField { receiver, .. } => {
                reads.insert(receiver.clone());
            }
            Self::AssignField {
                receiver, value, ..
            } => {
                reads.insert(receiver.clone());
                value.reads(reads);
            }
            Self::DefineThisField { value, .. } => value.reads(reads),
        }
    }

    fn allocation_syntax(
        &self,
        bindings: &BTreeMap<Id, LocalBinding>,
        closure_sites: &mut BTreeSet<u32>,
    ) -> (bool, bool) {
        match self {
            Self::Call { arguments, .. } => {
                for argument in arguments.iter() {
                    argument.allocation_syntax(bindings, closure_sites);
                }
                (false, false)
            }
            Self::Closure(callable) => {
                closure_sites.insert(callable.callable());
                (false, true)
            }
            Self::Record { properties, .. } => {
                let (mut allocation_only, mut closure_allowed) = (true, true);
                for property in properties.iter() {
                    let value = property.value();
                    let (allocation, closure) = value.allocation_syntax(bindings, closure_sites);
                    allocation_only &= allocation;
                    closure_allowed &= closure;
                }
                (allocation_only, closure_allowed)
            }
            Self::Array { elements, .. } => {
                let (mut allocation_only, mut closure_allowed) = (true, true);
                for element in elements.iter().flatten() {
                    let (allocation, closure) = element.allocation_syntax(bindings, closure_sites);
                    allocation_only &= allocation;
                    closure_allowed &= closure;
                }
                (allocation_only, closure_allowed)
            }
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => {
                let (mut allocation_only, mut closure_allowed) = (true, true);
                for expression in expressions.iter() {
                    let (allocation, closure) =
                        expression.allocation_syntax(bindings, closure_sites);
                    allocation_only &= allocation;
                    closure_allowed &= closure;
                }
                (allocation_only, closure_allowed)
            }
            Self::Binary { left, right, .. } => {
                left.allocation_syntax(bindings, closure_sites);
                right.allocation_syntax(bindings, closure_sites);
                (false, false)
            }
            Self::Assign { binding, value, .. } => {
                let (allocation_only, closure_allowed) =
                    value.allocation_syntax(bindings, closure_sites);
                let local = bindings.contains_key(binding);
                (local && allocation_only, local && closure_allowed)
            }
            Self::AssignField { value, .. } | Self::DefineThisField { value, .. } => {
                let (allocation_only, _) = value.allocation_syntax(bindings, closure_sites);
                (allocation_only, false)
            }
            Self::Value(value) => (true, unconditional_value(value)),
            Self::PrimitiveLiteral(_) => (true, true),
            Self::This | Self::ReadField { .. } => (true, false),
        }
    }

    fn requires_keyed_collection_new(&self) -> bool {
        match self {
            Self::Value(value) => value_contains_keyed_collection_new(value),
            Self::Record { properties, .. } => properties
                .iter()
                .any(|property| property.value().requires_keyed_collection_new()),
            Self::Array { elements, .. } => elements
                .iter()
                .flatten()
                .any(Self::requires_keyed_collection_new),
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => expressions.iter().any(Self::requires_keyed_collection_new),
            Self::Binary { left, right, .. } => {
                left.requires_keyed_collection_new() || right.requires_keyed_collection_new()
            }
            Self::Assign { value, .. }
            | Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. } => value.requires_keyed_collection_new(),
            Self::Call { arguments, .. } => {
                arguments.iter().any(Self::requires_keyed_collection_new)
            }
            Self::PrimitiveLiteral(_) | Self::This | Self::Closure(_) | Self::ReadField { .. } => {
                false
            }
        }
    }

    fn writes(&self, writes: &mut BTreeSet<Id>) {
        match self {
            Self::Record { properties, .. } => {
                for property in properties.iter() {
                    let value = property.value();
                    value.writes(writes);
                }
            }
            Self::Assign { binding, value, .. } => {
                writes.insert(binding.clone());
                value.writes(writes);
            }
            Self::Array { elements, .. } => {
                for element in elements.iter().flatten() {
                    element.writes(writes);
                }
            }
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => {
                for expression in expressions.iter() {
                    expression.writes(writes);
                }
            }
            Self::Binary { left, right, .. } => {
                left.writes(writes);
                right.writes(writes);
            }
            Self::Call { arguments, .. } => {
                for argument in arguments.iter() {
                    argument.writes(writes);
                }
            }
            Self::AssignField { value, .. } | Self::DefineThisField { value, .. } => {
                value.writes(writes)
            }
            Self::Value(_)
            | Self::PrimitiveLiteral(_)
            | Self::This
            | Self::ReadField { .. }
            | Self::Closure(_) => {}
        }
    }
}

/// The ordered binary transfer handles only eager operators whose successful
/// result is primitive. Equality without strict semantics and coercive
/// operators still fall back when either evaluated operand may be an object.
fn ordered_primitive_binary(op: BinaryOp) -> bool {
    !matches!(
        op,
        BinaryOp::LogicalAnd
            | BinaryOp::LogicalOr
            | BinaryOp::NullishCoalescing
            | BinaryOp::In
            | BinaryOp::InstanceOf
            | BinaryOp::EqEqEq
            | BinaryOp::NotEqEq
    )
}

impl LocalBody {
    pub(super) fn from_initializer(expression: &Expr) -> Option<Self> {
        let mut body = Self::lower_expression_with_binary(std::iter::empty(), expression, false)?;
        body.initializer_span = Some(expression.span());
        Some(body)
    }

    /// Exact terminal source call, with no other root operation or operand.
    /// This selects ordered execution, never positive effect coverage.
    pub(super) fn single_identifier_call_initializer(&self) -> Option<Span> {
        self.initializer_span?;
        match self.operations.as_ref() {
            [
                LocalOperation::Return(Some(LocalExpression::Call {
                    callee: LocalCallee::Binding(_),
                    arguments,
                    span,
                })),
            ] if arguments.is_empty() => Some(*span),
            _ => None,
        }
    }

    /// Called by the canonical function collector. Unsupported syntax discards
    /// the entire candidate; the ordinary conservative lanes remain available.
    pub(super) fn lower<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        body: &FunctionBody,
    ) -> Option<Self> {
        let (mut bindings, parameters) = Self::lower_parameters(parameters)?;
        let has_defaults = parameters
            .iter()
            .any(|parameter| parameter.default.is_some());
        let mut operations = Vec::new();
        let mut terminal = false;
        for statement in &body.stmts {
            // Non-simple parameters have a separate body var environment.
            // The shared plan does not yet represent that second scope.
            if has_defaults
                && matches!(statement, Stmt::Decl(Decl::Var(declaration))
                    if declaration.kind == VarDeclKind::Var)
            {
                return None;
            }
            if terminal {
                // The canonical collector supplies all declaration identities
                // and kinds after visiting the complete body, including dead
                // pattern and loop declarations. Their initializers do not run.
                continue;
            }
            match statement {
                Stmt::Empty(_) => {}
                Stmt::Decl(Decl::Var(declaration)) if !declaration.declare => {
                    lower_simple_var_declaration(
                        declaration,
                        false,
                        &mut bindings,
                        &mut operations,
                    )?;
                }
                Stmt::Expr(expression) => operations.push(LocalOperation::Evaluate(
                    LocalExpression::lower(&expression.expr, false)?,
                )),
                Stmt::Return(statement) => {
                    operations.push(LocalOperation::Return(LocalExpression::optional(
                        statement.arg.as_deref(),
                        false,
                    )?));
                    terminal = true;
                }
                Stmt::Throw(statement) => {
                    operations.push(LocalOperation::Throw(LocalExpression::lower(
                        &statement.arg,
                        false,
                    )?));
                    terminal = true;
                }
                _ => return None,
            }
        }
        Some(Self::from_operations(bindings, parameters, operations))
    }

    /// A concise arrow has the same ordered return semantics as a block body.
    /// Retain that operation directly without manufacturing or revisiting AST.
    pub(super) fn lower_expression<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        expression: &Expr,
    ) -> Option<Self> {
        Self::lower_expression_with_binary(parameters, expression, true)
    }

    fn lower_expression_with_binary<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        expression: &Expr,
        allow_binary: bool,
    ) -> Option<Self> {
        let (bindings, parameters) = Self::lower_parameters(parameters)?;
        Some(Self::from_operations(
            bindings,
            parameters,
            vec![LocalOperation::Return(Some(
                LocalExpression::lower_with_binary(expression, false, allow_binary)?,
            ))],
        ))
    }

    /// Canonically lower the allocation effects of a base class constructor.
    /// The caller still proves binding stability and native identity; this
    /// collector retains every constructor-time field operation in order.
    pub(super) fn lower_class_constructor(class: &Class) -> Option<(Self, Vec<Id>)> {
        if class.super_class.is_some() || !class.decorators.is_empty() {
            return None;
        }

        let mut constructor = None;
        let mut private_fields = BTreeSet::new();
        let mut field_operations = Vec::new();
        for member in &class.body {
            match member {
                ClassMember::Constructor(definition) if constructor.is_none() => {
                    constructor = Some(definition);
                }
                ClassMember::Constructor(_) => return None,
                ClassMember::PrivateProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        let property = field.key.name.to_string();
                        private_fields.insert(property.clone());
                        if let Some(value) = field.value.as_deref() {
                            field_operations.push(LocalOperation::Evaluate(
                                LocalExpression::DefineThisField {
                                    property,
                                    value: Arc::new(LocalExpression::lower(value, true)?),
                                },
                            ));
                        }
                    }
                }
                ClassMember::ClassProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        if !matches!(
                            field.key,
                            PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                        ) {
                            return None;
                        }
                        if let Some(value) = field.value.as_deref() {
                            field_operations.push(LocalOperation::Evaluate(
                                LocalExpression::DefineThisField {
                                    property: static_property_name(&field.key)?,
                                    value: Arc::new(LocalExpression::lower(value, true)?),
                                },
                            ));
                        }
                    }
                }
                ClassMember::AutoAccessor(accessor) if !accessor.is_static => return None,
                ClassMember::Method(method) if !method.function.decorators.is_empty() => {
                    return None;
                }
                ClassMember::PrivateMethod(method) if !method.function.decorators.is_empty() => {
                    return None;
                }
                _ => {}
            }
        }

        let mut bindings = BTreeMap::new();
        let mut parameters = Vec::new();
        let mut local_parameters = Vec::new();
        let mut constructor_operations = Vec::new();
        if let Some(constructor) = constructor {
            for parameter in &constructor.params {
                let ParamOrTsParamProp::Param(parameter) = parameter else {
                    return None;
                };
                if !parameter.decorators.is_empty() {
                    return None;
                }
                let Pat::Ident(binding) = &parameter.pat else {
                    return None;
                };
                if bindings
                    .insert(binding.id.to_id(), LocalBinding::Parameter)
                    .is_some()
                {
                    return None;
                }
                parameters.push(binding.id.to_id());
                local_parameters.push(LocalParameter {
                    binding: binding.id.to_id(),
                    default: None,
                });
            }

            for statement in &constructor.body.as_ref()?.stmts {
                match statement {
                    Stmt::Empty(_) => {}
                    Stmt::Decl(Decl::Var(declaration)) if !declaration.declare => {
                        lower_simple_var_declaration(
                            declaration,
                            true,
                            &mut bindings,
                            &mut constructor_operations,
                        )?;
                    }
                    Stmt::Expr(expression) => {
                        let operation = LocalExpression::lower(&expression.expr, true)?;
                        let LocalExpression::DefineThisField { property, .. } = &operation else {
                            return None;
                        };
                        if !private_fields.contains(property) {
                            return None;
                        }
                        constructor_operations.push(LocalOperation::Evaluate(operation));
                    }
                    Stmt::Return(statement) => {
                        constructor_operations.push(LocalOperation::Return(
                            LocalExpression::optional(statement.arg.as_deref(), true)?,
                        ));
                        break;
                    }
                    _ => return None,
                }
            }
        }

        field_operations.extend(constructor_operations);
        Some((
            Self::from_operations(bindings, local_parameters, field_operations),
            parameters,
        ))
    }

    fn lower_parameters<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
    ) -> Option<(BTreeMap<Id, LocalBinding>, Vec<LocalParameter>)> {
        let mut bindings = BTreeMap::new();
        let mut lowered = Vec::new();
        for parameter in parameters {
            let (binding, default) = match parameter {
                Pat::Ident(_) => (simple_binding(parameter)?, None),
                Pat::Assign(assignment) => (
                    simple_binding(&assignment.left)?,
                    Some(LocalExpression::lower(&assignment.right, false)?),
                ),
                _ => return None,
            };
            let parameter = LocalParameter { binding, default };
            if bindings
                .insert(parameter.binding.clone(), LocalBinding::Parameter)
                .is_some()
            {
                return None;
            }
            lowered.push(parameter);
        }
        Some((bindings, lowered))
    }

    fn from_operations(
        bindings: BTreeMap<Id, LocalBinding>,
        parameters: Vec<LocalParameter>,
        operations: Vec<LocalOperation>,
    ) -> Self {
        let expressions = || {
            parameters
                .iter()
                .filter_map(|parameter| parameter.default.as_ref())
                .chain(Self::expressions(&operations))
        };
        let mut reads = BTreeSet::new();
        for expression in expressions() {
            expression.reads(&mut reads);
        }
        reads.retain(|binding| !bindings.contains_key(binding));
        let mut writes = BTreeSet::new();
        for expression in expressions() {
            expression.writes(&mut writes);
        }
        writes.retain(|binding| !bindings.contains_key(binding));
        // Provider execution does not consume defaults yet. Retaining their
        // source plan must not admit a previously refused provider summary.
        let no_defaults = parameters
            .iter()
            .all(|parameter| parameter.default.is_none());
        let (mut allocation_only_syntax, mut closure_allocation_syntax) =
            (no_defaults, no_defaults);
        let mut closure_sites = BTreeSet::new();
        for expression in expressions() {
            let (allocation, closure) = expression.allocation_syntax(&bindings, &mut closure_sites);
            allocation_only_syntax &= allocation;
            closure_allocation_syntax &= closure;
        }
        closure_allocation_syntax &= !operations
            .iter()
            .any(|operation| matches!(operation, LocalOperation::Throw(_)));
        closure_allocation_syntax &= !closure_sites.is_empty();
        let requires_keyed_collection_new =
            expressions().any(LocalExpression::requires_keyed_collection_new);
        let closure_sites: Arc<[_]> = closure_sites.into_iter().collect();
        Self {
            bindings,
            parameters: parameters.into(),
            operations: operations.into(),
            initializer_span: None,
            free_reads: (!reads.is_empty()).then(|| reads.into_iter().collect()),
            captured_writes: (!writes.is_empty()).then(|| writes.into_iter().collect()),
            allocation_only_syntax,
            closure_allocation_syntax,
            closure_sites,
            requires_keyed_collection_new,
        }
    }

    pub(super) fn with_declarations(
        mut self,
        variables: impl IntoIterator<Item = Id>,
        lexical: impl IntoIterator<Item = (Id, LocalBinding)>,
    ) -> Self {
        for binding in variables {
            self.bindings.entry(binding).or_insert(LocalBinding::Var);
        }
        for (binding, kind) in lexical {
            self.bindings.insert(binding, kind);
        }
        self.free_reads = self.free_reads.as_ref().and_then(|reads| {
            let reads: Arc<[_]> = reads
                .iter()
                .filter(|binding| !self.bindings.contains_key(*binding))
                .cloned()
                .collect();
            (!reads.is_empty()).then_some(reads)
        });
        self.captured_writes = self.captured_writes.as_ref().and_then(|writes| {
            let writes: Arc<[_]> = writes
                .iter()
                .filter(|binding| !self.bindings.contains_key(*binding))
                .cloned()
                .collect();
            (!writes.is_empty()).then_some(writes)
        });
        self
    }

    pub(super) fn free_reads(&self) -> &[Id] {
        self.free_reads.as_deref().unwrap_or_default()
    }

    pub(super) fn initializer_span(&self) -> Option<Span> {
        self.initializer_span
    }

    pub(super) fn captured_writes(&self) -> &[Id] {
        self.captured_writes.as_deref().unwrap_or_default()
    }

    /// Calls and captured writes remain outside the allocation-only provider
    /// proof. Closure literals use the separate shared eligibility result;
    /// either migrated query refuses without retrying another evaluator.
    pub(super) fn allocation_only_syntax(&self) -> bool {
        self.allocation_only_syntax
    }

    pub(super) fn requires_keyed_collection_new(&self) -> bool {
        self.requires_keyed_collection_new
    }

    /// The shared body classification admits closure literals in addition to
    /// allocation-only syntax. Both answers and canonical sites are collected
    /// in one source-covered traversal at construction.
    pub(super) fn closure_allocation_syntax(&self) -> bool {
        self.closure_allocation_syntax
    }

    pub(super) fn closure_sites(&self) -> &[u32] {
        &self.closure_sites
    }

    fn expressions(operations: &[LocalOperation]) -> impl Iterator<Item = &LocalExpression> {
        operations.iter().filter_map(|operation| match operation {
            LocalOperation::Initialize(_, value) | LocalOperation::Return(value) => value.as_ref(),
            LocalOperation::Evaluate(value) | LocalOperation::Throw(value) => Some(value),
        })
    }
}
