//! Source-covered local operations collected before value lowering erases writes.

use super::effect_coverage::{EffectCoverageFragment, RootEffectIdentity};
use super::value_model::{
    CallSite, LiteralPropertyKind, PropertyDefinition, ValueExpr, object_data_property_name,
    static_property_name,
};
use crate::analysis_inputs::ForeignInvocation;
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
    /// Created on entry to its handler, rather than in the activation prologue.
    CatchParameter,
    /// These activation values must already be established by the caller.
    /// Source identity alone does not instantiate a function value.
    NamedSelf(u32),
    HoistedFunction(u32),
}

#[derive(Debug, Clone)]
pub(super) enum LocalCallee {
    Binding(Id),
    /// Evaluated through the same creation transfer as a standalone closure,
    /// before any argument. Source identity supplies no invocation coverage.
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

#[derive(Debug, Clone)]
pub(super) enum LocalInvocation {
    Call,
    OptionalCall,
    Construct,
}

impl LocalInvocation {
    pub(super) fn foreign_invocation(&self) -> ForeignInvocation {
        match self {
            Self::Call | Self::OptionalCall => ForeignInvocation::Call,
            Self::Construct => ForeignInvocation::Construct,
        }
    }
}

#[derive(Debug, Clone)]
pub(super) struct LocalCall {
    pub(super) invocation: LocalInvocation,
    pub(super) callee: LocalCallee,
    pub(super) arguments: Arc<[LocalExpression]>,
    pub(super) span: Span,
}

impl LocalCall {
    /// Only optional invocation of a direct reference is represented here.
    /// Optional receiver lookup and continuing chains have another evaluation
    /// frontier and retain their conservative source protocol.
    pub(super) fn optional_callee(chain: &OptChainExpr) -> Option<LocalCallee> {
        let OptChainBase::Call(call) = chain.base.as_ref() else {
            return None;
        };
        if !chain.optional || call.args.iter().any(|argument| argument.spread.is_some()) {
            return None;
        }
        let Expr::Ident(binding) = call.callee.unwrap_parens() else {
            return None;
        };
        (binding.sym != "eval").then(|| LocalCallee::Binding(binding.to_id()))
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
    /// Creating a source-checked callable defers its body and parameter
    /// defaults. Capture availability, retention and later invocation are
    /// separate consumer obligations.
    pub(super) fn effect_coverage(self) -> EffectCoverageFragment {
        match self {
            Self::Function(_)
            | Self::Arrow(_)
            | Self::Method { .. }
            | Self::Getter { .. }
            | Self::Setter { .. } => EffectCoverageFragment::no_effects(),
        }
    }

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
    Binary {
        operator: BinaryOp,
        left: Box<Self>,
        right: Box<Self>,
        span: Span,
    },
    /// Each substitution is evaluated and converted before the next one.
    Template {
        substitutions: Arc<[Self]>,
        span: Span,
    },

    Value(ValueExpr),
    /// A canonical primitive literal has no coercion or user-code effect.
    PrimitiveLiteral(PrimKinds),
    /// The fresh receiver supplied by a checked constructor query.
    This,
    Sequence(Arc<[Self]>),
    Record {
        allocation: u32,
        properties: Arc<[PropertyDefinition<Self>]>,
    },
    Array {
        allocation: u32,
        elements: Arc<[LocalArrayElement]>,
    },
    /// One direct Set construction from the exact source array argument. The
    /// ordered consumer evaluates the array once, then applies the shared
    /// native iterator/constructor transfer to that value.
    SetConstruction {
        array: Box<Self>,
        source: ValueExpr,
        site: CallSite,
    },
    Assign {
        binding: Id,
        value: Arc<Self>,
        span: Span,
    },
    /// Prefix/postfix increment/decrement share read, ToNumeric and PutValue
    /// effects. This scalar domain tracks numeric kinds, not the old/new value.
    UpdateBinding {
        binding: Id,
        span: Span,
    },
    Closure(DeferredCallableCreation),
    ClassDefinition(ClassDefinition),
    Call(LocalCall),
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
    /// Own public data definition or private-slot installation, distinct from
    /// assignment to an already installed private slot.
    DefineThisField {
        property: ClassFieldKey,
        value: Arc<Self>,
    },
    AssignPrivateThisField {
        property: String,
        value: Arc<Self>,
    },
    AssignPublicThisField {
        property: String,
        value: Arc<Self>,
    },
}

#[derive(Debug, Clone)]
pub(super) enum ClassFieldKey {
    Public(String),
    Private(String),
}

/// Canonical class creation, independent of deferred constructor/field bodies.
/// Eager heritage, decorators, computed names and static initialization are
/// excluded before this operation reaches an interpreter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ClassDefinition {
    pub(super) class: u32,
    pub(super) span: Span,
}

impl ClassDefinition {
    pub(super) fn lower(class: &Class) -> Option<Self> {
        if class.super_class.is_some()
            || !class.decorators.is_empty()
            || class.is_abstract
            || class.type_params.is_some()
            || class.super_type_params.is_some()
            || !class.implements.is_empty()
        {
            return None;
        }
        let mut constructor = false;
        for member in &class.body {
            let covered = match member {
                ClassMember::Constructor(body) if !constructor => {
                    constructor = true;
                    !matches!(body.key, PropName::Computed(_))
                        && body.params.iter().all(|parameter| {
                            matches!(parameter,
                        ParamOrTsParamProp::Param(parameter) if parameter.decorators.is_empty())
                        })
                }
                ClassMember::Method(method) => {
                    method.function.decorators.is_empty()
                        && method
                            .function
                            .params
                            .iter()
                            .all(|parameter| parameter.decorators.is_empty())
                        && !matches!(method.key, PropName::Computed(_))
                }
                ClassMember::PrivateMethod(method) => {
                    method.function.decorators.is_empty()
                        && method
                            .function
                            .params
                            .iter()
                            .all(|parameter| parameter.decorators.is_empty())
                }
                ClassMember::ClassProp(field) => {
                    !field.is_static
                        && field.decorators.is_empty()
                        && !matches!(field.key, PropName::Computed(_))
                }
                ClassMember::PrivateProp(field) => !field.is_static && field.decorators.is_empty(),
                ClassMember::Empty(_) => true,
                _ => false,
            };
            if !covered {
                return None;
            }
        }
        Some(Self {
            class: class.span.lo.0,
            span: class.span,
        })
    }
}

#[derive(Debug, Clone)]
pub(super) struct ClassConstructorBody {
    pub(super) class: u32,
    pub(super) span: Span,
    /// Canonical activation sites for instance initializers and the body.
    pub(super) callables: Arc<[u32]>,
    pub(super) initializers: LocalBody,
    pub(super) constructor: LocalBody,
}

#[derive(Debug, Clone, Copy)]
pub(super) enum ConstructorPhase {
    Initializers,
    Body,
}

#[derive(Debug, Clone)]
pub(super) enum LocalArrayElement {
    Hole,
    Value(LocalExpression),
    Spread { value: LocalExpression, span: Span },
}

#[derive(Debug, Clone)]
pub(super) enum LocalOperation {
    Initialize(Id, Option<LocalExpression>),
    Evaluate(LocalExpression),
    Return(Option<LocalExpression>, Span),
    Throw(LocalExpression, Span),
    TryCatch {
        body: LocalRegion,
        binding: Option<Id>,
        handler: LocalRegion,
    },
}

#[derive(Debug, Clone)]
pub(super) struct LocalRegion {
    pub(super) span: Span,
    pub(super) operations: Arc<[LocalOperation]>,
}

#[derive(Debug, Clone)]
pub(super) struct LocalParameter {
    pub(super) binding: Id,
    pub(super) default: Option<LocalExpression>,
}

#[derive(Debug, Clone)]
pub(super) struct OrderedRoot {
    pub(super) identity: RootEffectIdentity,
    pub(super) body: LocalBody,
}

#[derive(Debug, Clone)]
pub(super) struct LocalBody {
    pub(super) bindings: BTreeMap<Id, LocalBinding>,
    pub(super) parameters: Arc<[LocalParameter]>,
    pub(super) operations: Arc<[LocalOperation]>,
    pub(super) source_span: Option<Span>,
    /// Present for a binding initializer or expression-statement root lowered
    /// from its exact source expression. Callable bodies have no root-owner span.
    root_expression_span: Option<Span>,
    /// Sorted source reads outside this body's parameters and declarations.
    /// Shared across body clones and reused by every invocation/provider query.
    free_reads: Option<Arc<[Id]>>,
    captured_writes: Option<Arc<[Id]>>,
    allocation_candidate_syntax: bool,
    closure_candidate_syntax: bool,
    closure_sites: Arc<[u32]>,
    /// Source contains a bare empty keyed-collection construction. Provider
    /// read projection treats that native callee separately from captures;
    /// every consumer still proves its actual constructor identity.
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
        ValueExpr::StrictEquality(left, right) | ValueExpr::Binary(_, left, right) => {
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

fn literal_definition_name(name: &PropName, kind: LiteralPropertyKind) -> Option<String> {
    if !matches!(
        name,
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
    ) {
        return None;
    }
    match kind {
        // Only a key/value data definition has the special prototype form.
        LiteralPropertyKind::Data => object_data_property_name(name),
        LiteralPropertyKind::Method | LiteralPropertyKind::Getter | LiteralPropertyKind::Setter => {
            static_property_name(name)
        }
        LiteralPropertyKind::Opaque => None,
    }
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
        ValueExpr::StrictEquality(left, right) | ValueExpr::Binary(_, left, right) => {
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
        match expression.unwrap_parens() {
            Expr::Bin(binary) if super::value_model::modeled_binary_operator(binary.op) => {
                Some(Self::Binary {
                    operator: binary.op,
                    left: Box::new(Self::lower(&binary.left, class_template)?),
                    right: Box::new(Self::lower(&binary.right, class_template)?),
                    span: binary.span,
                })
            }

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
                                name: Some(literal_definition_name(
                                    &pair.key,
                                    LiteralPropertyKind::Data,
                                )?),
                                value: Self::lower(&pair.value, class_template)?,
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
                                    name: Some(literal_definition_name(
                                        &method.key,
                                        LiteralPropertyKind::Method,
                                    )?),
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
                                    name: Some(literal_definition_name(
                                        &getter.key,
                                        LiteralPropertyKind::Getter,
                                    )?),
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
                                    name: Some(literal_definition_name(
                                        &setter.key,
                                        LiteralPropertyKind::Setter,
                                    )?),
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
                        None => Some(LocalArrayElement::Hole),
                        Some(element) if element.spread.is_none() => Some(
                            LocalArrayElement::Value(Self::lower(&element.expr, class_template)?),
                        ),
                        Some(element) => {
                            let spread = element.spread.as_ref()?;
                            Some(LocalArrayElement::Spread {
                                value: Self::lower(&element.expr, class_template)?,
                                span: *spread,
                            })
                        }
                    })
                    .collect::<Option<Arc<[_]>>>()?,
            }),
            Expr::New(construction)
                if construction.type_args.is_none()
                    && matches!(construction.callee.unwrap_parens(), Expr::Ident(name) if name.sym == "Set")
                    && let [argument] = construction.args.as_deref().unwrap_or_default()
                    && argument.spread.is_none()
                    && matches!(argument.expr.unwrap_parens(), Expr::Array(array)
                        if array.elems.iter().flatten().any(|element| element.spread.is_some())) =>
            {
                let Expr::Array(array) = argument.expr.unwrap_parens() else {
                    unreachable!("guarded direct Set constructor has an array argument")
                };
                let (source, _) = ValueExpr::from_expr_with_coverage(&argument.expr);
                if !matches!(source, ValueExpr::ObjectLiteral(allocation, _) if allocation == array.span.lo.0)
                {
                    return None;
                }
                Some(Self::SetConstruction {
                    array: Box::new(Self::lower(&argument.expr, class_template)?),
                    source,
                    site: construction.span.into(),
                })
            }
            Expr::New(construction)
                if construction.type_args.is_none()
                    && !(construction.args.as_deref().unwrap_or_default().is_empty()
                        && matches!(construction.callee.unwrap_parens(), Expr::Ident(binding)
                            if matches!(binding.sym.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet")))
                    && !construction
                        .args
                        .as_deref()
                        .unwrap_or_default()
                        .iter()
                        .any(|argument| argument.spread.is_some()) =>
            {
                Some(Self::Call(LocalCall {
                    invocation: LocalInvocation::Construct,
                    callee: LocalCallee::lower(&construction.callee, class_template)?,
                    arguments: construction
                        .args
                        .as_deref()
                        .unwrap_or_default()
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, class_template))
                        .collect::<Option<Arc<[_]>>>()?,
                    span: construction.span,
                }))
            }
            Expr::Tpl(template) => Some(Self::Template {
                substitutions: template
                    .exprs
                    .iter()
                    .map(|expression| Self::lower(expression, class_template))
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
                    .map(|expression| Self::lower(expression, class_template))
                    .collect::<Option<Arc<[_]>>>()?,
            )),
            Expr::Call(call) if !call.args.iter().any(|argument| argument.spread.is_some()) => {
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                let callee = LocalCallee::lower(callee, class_template)?;
                Some(Self::Call(LocalCall {
                    invocation: LocalInvocation::Call,
                    callee,
                    arguments: call
                        .args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, class_template))
                        .collect::<Option<Arc<[_]>>>()?,
                    span: call.span,
                }))
            }
            Expr::OptChain(chain) => {
                let callee = LocalCall::optional_callee(chain)?;
                let OptChainBase::Call(call) = chain.base.as_ref() else {
                    unreachable!("optional callee admission requires an invocation")
                };
                Some(Self::Call(LocalCall {
                    invocation: LocalInvocation::OptionalCall,
                    callee,
                    arguments: call
                        .args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, class_template))
                        .collect::<Option<Arc<[_]>>>()?,
                    span: call.span,
                }))
            }
            Expr::Member(member) => {
                let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                    return None;
                };
                let property = super::value_model::static_prop(&member.prop)?;
                if !super::value_model::MemberKey::of(&member.prop)
                    .supports_own_data_read(&property)
                {
                    return None;
                }
                Some(Self::ReadField {
                    receiver: receiver.to_id(),
                    property,
                    span: member.span,
                })
            }
            Expr::Assign(assignment) if assignment.op == AssignOp::Assign => {
                let value = Arc::new(Self::lower(&assignment.right, class_template)?);
                match &assignment.left {
                    AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                        Some(Self::Assign {
                            binding: binding.id.to_id(),
                            value,
                            span: assignment.span,
                        })
                    }
                    AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                        if class_template && matches!(member.obj.unwrap_parens(), Expr::This(_)) {
                            return match &member.prop {
                                MemberProp::PrivateName(private) => {
                                    Some(Self::AssignPrivateThisField {
                                        property: private.name.to_string(),
                                        value,
                                    })
                                }
                                MemberProp::Ident(property) => Some(Self::AssignPublicThisField {
                                    property: property.sym.to_string(),
                                    value,
                                }),
                                MemberProp::Computed(_) => None,
                            };
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
            Expr::Update(update) => {
                let Expr::Ident(binding) = update.arg.unwrap_parens() else {
                    return None;
                };
                Some(Self::UpdateBinding {
                    binding: binding.to_id(),
                    span: update.span,
                })
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
            Self::Binary { left, right, .. } => {
                left.reads(reads);
                right.reads(reads);
            }

            Self::Value(value) => value.bindings(reads),
            Self::PrimitiveLiteral(_) => {}
            Self::Record { properties, .. } => {
                for property in properties.iter() {
                    let value = property.value();
                    value.reads(reads);
                }
            }
            Self::Array { elements, .. } => {
                for element in elements.iter() {
                    match element {
                        LocalArrayElement::Hole => {}
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => value.reads(reads),
                    }
                }
            }
            Self::SetConstruction { array, source, .. } => {
                array.reads(reads);
                source.bindings(reads);
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
            Self::Assign { binding, value, .. } => {
                reads.insert(binding.clone());
                value.reads(reads);
            }
            Self::UpdateBinding { binding, .. } => {
                reads.insert(binding.clone());
            }
            Self::Closure(_) | Self::ClassDefinition(_) | Self::This => {}
            Self::Call(LocalCall {
                callee, arguments, ..
            }) => {
                match callee {
                    LocalCallee::Binding(binding)
                    | LocalCallee::Member {
                        receiver: binding, ..
                    } => {
                        reads.insert(binding.clone());
                    }
                    // Capturing a cell does not GetValue it. Invocation owns
                    // the deferred body's actual reads and effects.
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
            Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => value.reads(reads),
        }
    }

    fn allocation_syntax(
        &self,
        bindings: &BTreeMap<Id, LocalBinding>,
        closure_sites: &mut BTreeSet<u32>,
    ) -> (bool, bool) {
        match self {
            Self::Binary { left, right, .. } => {
                let (left_allocation, left_closure) =
                    left.allocation_syntax(bindings, closure_sites);
                let (right_allocation, right_closure) =
                    right.allocation_syntax(bindings, closure_sites);
                (
                    left_allocation && right_allocation,
                    left_closure && right_closure,
                )
            }

            Self::Call(LocalCall { arguments, .. }) => {
                // Grammar admission is only a candidate. Each consumer must
                // select the actual callee and transfer its invocation.
                let (mut allocation, mut closure) = (true, true);
                for argument in arguments.iter() {
                    let (argument_allocation, argument_closure) =
                        argument.allocation_syntax(bindings, closure_sites);
                    allocation &= argument_allocation;
                    closure &= argument_closure;
                }
                (allocation, closure)
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
                for element in elements.iter() {
                    let value = match element {
                        LocalArrayElement::Hole => continue,
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => value,
                    };
                    let (allocation, closure) = value.allocation_syntax(bindings, closure_sites);
                    allocation_only &= allocation;
                    closure_allowed &= closure;
                }
                (allocation_only, closure_allowed)
            }
            Self::SetConstruction { array, .. } => {
                let (allocation, _) = array.allocation_syntax(bindings, closure_sites);
                (allocation, false)
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
            Self::Assign { binding, value, .. } => {
                let (allocation_only, closure_allowed) =
                    value.allocation_syntax(bindings, closure_sites);
                let local = bindings.contains_key(binding);
                (local && allocation_only, local && closure_allowed)
            }
            Self::UpdateBinding { binding, .. } => {
                let local = bindings.contains_key(binding);
                (local, local)
            }
            Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => {
                let (allocation_only, _) = value.allocation_syntax(bindings, closure_sites);
                (allocation_only, false)
            }
            Self::Value(value) => (true, unconditional_value(value)),
            Self::PrimitiveLiteral(_) | Self::ClassDefinition(_) => (true, true),
            Self::This | Self::ReadField { .. } => (true, false),
        }
    }

    fn requires_keyed_collection_new(&self) -> bool {
        match self {
            Self::Binary { left, right, .. } => {
                left.requires_keyed_collection_new() || right.requires_keyed_collection_new()
            }

            Self::Value(value) => value_contains_keyed_collection_new(value),
            Self::Record { properties, .. } => properties
                .iter()
                .any(|property| property.value().requires_keyed_collection_new()),
            Self::Array { elements, .. } => elements.iter().any(|element| match element {
                LocalArrayElement::Hole => false,
                LocalArrayElement::Value(value) | LocalArrayElement::Spread { value, .. } => {
                    value.requires_keyed_collection_new()
                }
            }),
            Self::SetConstruction { .. } => true,
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => expressions.iter().any(Self::requires_keyed_collection_new),
            Self::Assign { value, .. }
            | Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => value.requires_keyed_collection_new(),
            Self::Call(LocalCall { arguments, .. }) => {
                arguments.iter().any(Self::requires_keyed_collection_new)
            }
            Self::PrimitiveLiteral(_)
            | Self::This
            | Self::Closure(_)
            | Self::ClassDefinition(_)
            | Self::ReadField { .. }
            | Self::UpdateBinding { .. } => false,
        }
    }

    fn writes(&self, writes: &mut BTreeSet<Id>) {
        match self {
            Self::Binary { left, right, .. } => {
                left.writes(writes);
                right.writes(writes);
            }

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
            Self::UpdateBinding { binding, .. } => {
                writes.insert(binding.clone());
            }
            Self::Array { elements, .. } => {
                for element in elements.iter() {
                    match element {
                        LocalArrayElement::Hole => {}
                        LocalArrayElement::Value(value)
                        | LocalArrayElement::Spread { value, .. } => value.writes(writes),
                    }
                }
            }
            Self::SetConstruction { array, .. } => array.writes(writes),
            Self::Sequence(expressions)
            | Self::Template {
                substitutions: expressions,
                ..
            } => {
                for expression in expressions.iter() {
                    expression.writes(writes);
                }
            }
            Self::Call(LocalCall { arguments, .. }) => {
                for argument in arguments.iter() {
                    argument.writes(writes);
                }
            }
            Self::AssignField { value, .. }
            | Self::DefineThisField { value, .. }
            | Self::AssignPrivateThisField { value, .. }
            | Self::AssignPublicThisField { value, .. } => value.writes(writes),
            Self::Value(_)
            | Self::PrimitiveLiteral(_)
            | Self::This
            | Self::ReadField { .. }
            | Self::Closure(_)
            | Self::ClassDefinition(_) => {}
        }
    }
}

impl LocalBody {
    pub(super) fn from_initializer(expression: &Expr) -> Option<Self> {
        let mut body = Self::lower_expression(std::iter::empty(), expression)?;
        body.root_expression_span = Some(expression.span());
        Some(body)
    }

    pub(super) fn from_class_declaration(definition: &ClassDefinition) -> Self {
        let mut body = Self::from_operations(
            BTreeMap::new(),
            Vec::new(),
            vec![LocalOperation::Return(
                Some(LocalExpression::ClassDefinition(definition.clone())),
                definition.span,
            )],
        );
        body.root_expression_span = Some(definition.span);
        body
    }

    pub(super) fn from_expression_statement(expression: &Expr) -> Option<Self> {
        let mut body = Self::from_initializer(expression)?;
        let [LocalOperation::Return(Some(value), _)] = body.operations.as_ref() else {
            unreachable!("expression lowering produces one value operation");
        };
        body.operations = Arc::from([LocalOperation::Evaluate(value.clone())]);
        Some(body)
    }

    pub(super) fn untagged_template_root(&self) -> bool {
        self.root_expression_span.is_some()
            && matches!(
                self.operations.as_ref(),
                [LocalOperation::Return(
                    Some(LocalExpression::Template { .. }),
                    _
                )]
            )
    }

    /// Exact terminal source call. Its operands retain independent coverage.
    /// This selects ordered execution, never positive effect coverage.
    pub(super) fn terminal_call_root(&self) -> Option<Span> {
        self.root_expression_span?;
        let value = match self.operations.as_ref() {
            [LocalOperation::Return(Some(value), _) | LocalOperation::Evaluate(value)] => value,
            _ => return None,
        };
        match value {
            LocalExpression::Call(LocalCall { span, .. }) => Some(*span),
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
        let operations = Self::lower_region(&body.stmts, &mut bindings, has_defaults, false)?;
        let mut lowered = Self::from_operations(bindings, parameters, operations);
        lowered.source_span = Some(body.span);
        Some(lowered)
    }

    fn lower_region(
        statements: &[Stmt],
        bindings: &mut BTreeMap<Id, LocalBinding>,
        has_defaults: bool,
        nested: bool,
    ) -> Option<Vec<LocalOperation>> {
        let mut operations = Vec::new();
        let mut terminal = false;
        for statement in statements {
            if nested
                && matches!(statement, Stmt::Decl(declaration)
                if !matches!(declaration, Decl::Var(variable) if variable.kind == VarDeclKind::Var))
            {
                return None;
            }
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
                    // Block lexical environments have a distinct entry/exit
                    // protocol. Until represented, retain conservative execution.
                    if nested && declaration.kind != VarDeclKind::Var {
                        return None;
                    }
                    lower_simple_var_declaration(declaration, false, bindings, &mut operations)?;
                }
                Stmt::Expr(expression) => operations.push(LocalOperation::Evaluate(
                    LocalExpression::lower(&expression.expr, false)?,
                )),
                Stmt::Return(statement) => {
                    operations.push(LocalOperation::Return(
                        LocalExpression::optional(statement.arg.as_deref(), false)?,
                        statement.span,
                    ));
                    terminal = true;
                }
                Stmt::Throw(statement) => {
                    operations.push(LocalOperation::Throw(
                        LocalExpression::lower(&statement.arg, false)?,
                        statement.span,
                    ));
                    terminal = true;
                }
                Stmt::Try(statement) if statement.finalizer.is_none() => {
                    let handler = statement.handler.as_ref()?;
                    let binding = match &handler.param {
                        Some(parameter) => Some(simple_binding(parameter)?),
                        None => None,
                    };
                    if let Some(binding) = &binding
                        && bindings
                            .insert(binding.clone(), LocalBinding::CatchParameter)
                            .is_some()
                    {
                        return None;
                    }
                    operations.push(LocalOperation::TryCatch {
                        body: LocalRegion {
                            span: statement.block.span,
                            operations: Self::lower_region(
                                &statement.block.stmts,
                                bindings,
                                has_defaults,
                                true,
                            )?
                            .into(),
                        },
                        binding,
                        handler: LocalRegion {
                            span: handler.body.span,
                            operations: Self::lower_region(
                                &handler.body.stmts,
                                bindings,
                                has_defaults,
                                true,
                            )?
                            .into(),
                        },
                    });
                }
                _ => return None,
            }
        }
        Some(operations)
    }

    /// A concise arrow has the same ordered return semantics as a block body.
    /// Retain that operation directly without manufacturing or revisiting AST.
    pub(super) fn lower_expression<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        expression: &Expr,
    ) -> Option<Self> {
        let (bindings, parameters) = Self::lower_parameters(parameters)?;
        let mut lowered = Self::from_operations(
            bindings,
            parameters,
            vec![LocalOperation::Return(
                Some(LocalExpression::lower(expression, false)?),
                expression.span(),
            )],
        );
        lowered.source_span = Some(expression.span());
        Some(lowered)
    }

    /// Canonically lower the allocation effects of a base class constructor.
    /// The caller still proves binding stability and native identity; this
    /// collector retains every constructor-time field operation in order.
    pub(super) fn lower_class_constructor(class: &Class) -> Option<ClassConstructorBody> {
        if class.super_class.is_some() || !class.decorators.is_empty() {
            return None;
        }

        let mut constructor = None;
        let mut private_fields = BTreeSet::new();
        let mut field_operations = Vec::new();
        let mut callables = Vec::new();
        for member in &class.body {
            match member {
                ClassMember::Constructor(definition) if constructor.is_none() => {
                    callables.push(definition.span.lo.0);
                    constructor = Some(definition);
                }
                ClassMember::Constructor(_) => return None,
                ClassMember::PrivateProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        if field.value.is_some() {
                            callables.push(field.span.lo.0);
                        }
                        let property = field.key.name.to_string();
                        private_fields.insert(property.clone());
                        field_operations.push(LocalOperation::Evaluate(
                            LocalExpression::DefineThisField {
                                property: ClassFieldKey::Private(property),
                                value: Arc::new(match field.value.as_deref() {
                                    Some(value) => LocalExpression::lower(value, true)?,
                                    None => LocalExpression::PrimitiveLiteral(PrimKinds::UNDEFINED),
                                }),
                            },
                        ));
                    }
                }
                ClassMember::ClassProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        if field.value.is_some() {
                            callables.push(field.span.lo.0);
                        }
                        if !matches!(
                            field.key,
                            PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                        ) {
                            return None;
                        }
                        field_operations.push(LocalOperation::Evaluate(
                            LocalExpression::DefineThisField {
                                property: ClassFieldKey::Public(static_property_name(&field.key)?),
                                value: Arc::new(match field.value.as_deref() {
                                    Some(value) => LocalExpression::lower(value, true)?,
                                    None => LocalExpression::PrimitiveLiteral(PrimKinds::UNDEFINED),
                                }),
                            },
                        ));
                    }
                }
                ClassMember::AutoAccessor(accessor) if !accessor.is_static => return None,
                // Installation and captures of instance private methods are
                // absent from this allocation/retention plan.
                ClassMember::PrivateMethod(method) if !method.is_static => return None,
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
                        fn checked_stores(
                            expression: &LocalExpression,
                            private_fields: &BTreeSet<String>,
                        ) -> bool {
                            match expression {
                                LocalExpression::AssignPublicThisField { .. } => true,
                                LocalExpression::AssignPrivateThisField { property, .. } => {
                                    private_fields.contains(property)
                                }
                                LocalExpression::Sequence(expressions) => expressions
                                    .iter()
                                    .all(|expression| checked_stores(expression, private_fields)),
                                _ => false,
                            }
                        }
                        if !checked_stores(&operation, &private_fields) {
                            return None;
                        }
                        constructor_operations.push(LocalOperation::Evaluate(operation));
                    }
                    Stmt::Return(statement) => {
                        constructor_operations.push(LocalOperation::Return(
                            LocalExpression::optional(statement.arg.as_deref(), true)?,
                            statement.span,
                        ));
                        break;
                    }
                    _ => return None,
                }
            }
        }

        Some(ClassConstructorBody {
            class: class.span.lo.0,
            span: class.span,
            callables: callables.into(),
            initializers: Self::from_operations(BTreeMap::new(), Vec::new(), field_operations),
            constructor: Self::from_operations(bindings, local_parameters, constructor_operations),
        })
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
        let (mut allocation_candidate_syntax, mut closure_candidate_syntax) =
            (no_defaults, no_defaults);
        let mut closure_sites = BTreeSet::new();
        for expression in expressions() {
            let (allocation, closure) = expression.allocation_syntax(&bindings, &mut closure_sites);
            allocation_candidate_syntax &= allocation;
            closure_candidate_syntax &= closure;
        }
        closure_candidate_syntax &= !operations.iter().any(|operation| {
            matches!(
                operation,
                LocalOperation::Throw(..) | LocalOperation::TryCatch { .. }
            )
        });
        closure_candidate_syntax &= !closure_sites.is_empty();
        let requires_keyed_collection_new =
            expressions().any(LocalExpression::requires_keyed_collection_new);
        let closure_sites: Arc<[_]> = closure_sites.into_iter().collect();
        Self {
            bindings,
            parameters: parameters.into(),
            operations: operations.into(),
            source_span: None,
            root_expression_span: None,
            free_reads: (!reads.is_empty()).then(|| reads.into_iter().collect()),
            captured_writes: (!writes.is_empty()).then(|| writes.into_iter().collect()),
            allocation_candidate_syntax,
            closure_candidate_syntax,
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

    pub(super) fn root_expression_span(&self) -> Option<Span> {
        self.root_expression_span
    }

    pub(super) fn direct_set_construction_root(&self) -> bool {
        self.root_expression_span.is_some()
            && matches!(
                self.operations.as_ref(),
                [LocalOperation::Return(
                    Some(LocalExpression::SetConstruction { .. }),
                    _
                )]
            )
    }

    pub(super) fn captured_writes(&self) -> &[Id] {
        self.captured_writes.as_deref().unwrap_or_default()
    }

    /// Grammar candidate for the shared allocation consumer. Calls still need
    /// actual callee selection and body transfer; captured writes remain
    /// unsupported. A refusal never retries another evaluator.
    pub(super) fn allocation_candidate_syntax(&self) -> bool {
        self.allocation_candidate_syntax
    }

    pub(super) fn requires_keyed_collection_new(&self) -> bool {
        self.requires_keyed_collection_new
    }

    /// Also includes callable creation. Capture availability and returned
    /// reference retention remain independent consumer obligations.
    pub(super) fn closure_candidate_syntax(&self) -> bool {
        self.closure_candidate_syntax
    }

    pub(super) fn closure_sites(&self) -> &[u32] {
        &self.closure_sites
    }

    fn expressions(operations: &[LocalOperation]) -> std::vec::IntoIter<&LocalExpression> {
        let mut expressions = Vec::new();
        for operation in operations {
            match operation {
                LocalOperation::Initialize(_, value) | LocalOperation::Return(value, _) => {
                    expressions.extend(value)
                }
                LocalOperation::Evaluate(value) | LocalOperation::Throw(value, _) => {
                    expressions.push(value)
                }
                LocalOperation::TryCatch { body, handler, .. } => {
                    expressions.extend(Self::expressions(&body.operations));
                    expressions.extend(Self::expressions(&handler.operations));
                }
            }
        }
        expressions.into_iter()
    }
}
