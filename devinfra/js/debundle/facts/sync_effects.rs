//! Call-site execution effects. Function values and function bodies are
//! deliberately separate: returning a closure never executes that closure.

use super::statement_facts::SynchronousCalls;
use super::value_flow::{Values as FlowValues, static_prop as static_string_prop};
use super::*;
use std::cmp::Ordering;
use std::sync::Arc;
use swc_common::{Spanned, SyntaxContext};

// Numeric index literals are exact own-property names on retained arrays.
fn static_prop(property: &MemberProp) -> Option<String> {
    static_string_prop(property).or_else(|| match property {
        MemberProp::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Num(number))
                if number.value >= 0.0
                    && number.value <= u32::MAX as f64
                    && number.value.fract() == 0.0 =>
            {
                Some((number.value as u32).to_string())
            }
            _ => None,
        },
        _ => None,
    })
}

type Values = BTreeSet<AbstractValue>;
type Context = BTreeMap<Id, Values>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct CallSite {
    start: u32,
    end: u32,
}

impl From<Span> for CallSite {
    fn from(span: Span) -> Self {
        Self {
            start: span.lo.0,
            end: span.hi.0,
        }
    }
}

/// Contexts are interned bottom-up by the executor. Structural equality of
/// captures becomes identity equality, so comparing value sets and cache keys
/// never recursively compares a shared capture DAG along every path.
#[derive(Debug, Clone)]
struct Captured<T> {
    identity: usize,
    values: Arc<T>,
}

type CapturedContext = Captured<Context>;

impl<T> PartialEq for Captured<T> {
    fn eq(&self, other: &Self) -> bool {
        self.identity == other.identity
    }
}

impl<T> Eq for Captured<T> {}

impl<T> PartialOrd for Captured<T> {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl<T> Ord for Captured<T> {
    fn cmp(&self, other: &Self) -> Ordering {
        self.identity.cmp(&other.identity)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum AbstractValue {
    Origin(ValueOrigin),
    Closure(u32, CapturedContext),
    /// A class's static or prototype surface can expose its deferred methods.
    /// Merely creating the class does not execute those methods.
    ClassMembers(u32),
    /// A constructed local class instance retains its class methods and the
    /// callback values stored by its proven constructor.
    Instance(u32, Captured<Values>),
    CallbackFactory,
    InlineCallbackFactory,
    /// A fresh opaque return value retaining callbacks without publishing them.
    DeferredCallbacks(Captured<Values>),
    ObjectAssign,
    /// Array constructor/prototype capability obtained from a proven array.
    ArrayIntrinsic,
    GlobalObject,
    Undefined,
    NonCallable,
    /// Evaluating an expression throws before producing a value (for example,
    /// a lexical binding read in its temporal dead zone).
    Unavailable,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ArrayIterationMethod {
    ForEach,
    Filter,
    Map,
}

#[derive(Debug, Clone)]
struct ArrayIteration {
    method: ArrayIterationMethod,
    source: ValueExpr,
    callback: ValueExpr,
    result: Option<u32>,
}

#[derive(Debug, Clone)]
struct ArrayElement {
    spread: bool,
    value: ValueExpr,
}

/// Slot values are an ordered overapproximation. The ordinary Object origin
/// remains the identity used by every mutation, publication and taint lane.
#[derive(Debug, Clone)]
struct DenseArray {
    slots: Vec<Values>,
}

#[derive(Debug, Clone)]
struct FromEntriesMappedProof {
    source: ValueExpr,
    key: String,
    map_call_site: CallSite,
    object_intrinsic: Id,
}

#[derive(Debug, Clone)]
enum ValueExpr {
    Binding(Id),
    Origin(ValueOrigin),
    ObjectLiteral(u32, Vec<(Option<String>, Self)>),
    Undefined,
    NonCallable,
    Member(Box<Self>, Option<String>, bool),
    Call(Box<Self>, Vec<Self>, CallSite),
    New(Box<Self>, Vec<Self>),
    /// An operator result is primitive on successful evaluation. This is a
    /// value-kind fact, not a purity claim: keep operands so calls and
    /// unavailable reads inside them still flow through the evaluator.
    Primitive(Vec<Self>),
    Alternatives(Vec<Self>),
    Unknown,
}

fn map_entry_array_span(new_expr: &NewExpr) -> Option<u32> {
    if !matches!(new_expr.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym == "Map") {
        return None;
    }
    let [argument] = new_expr.args.as_deref().unwrap_or_default() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Array(entries) = argument.expr.unwrap_parens() else {
        return None;
    };
    for entry in &entries.elems {
        let Some(entry) = entry else {
            return None;
        };
        if entry.spread.is_some() {
            return None;
        }
        let Expr::Array(pair) = entry.expr.unwrap_parens() else {
            return None;
        };
        if pair.elems.len() != 2
            || pair.elems.iter().any(|element| {
                element
                    .as_ref()
                    .is_none_or(|element| element.spread.is_some())
            })
        {
            return None;
        }
    }
    Some(entries.span.lo.0)
}

fn from_entries_mapped_proof(
    call: &CallExpr,
    plain_array_bindings: &BTreeSet<String>,
) -> Option<FromEntriesMappedProof> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(from_entries) = callee.unwrap_parens() else {
        return None;
    };
    if !matches!(from_entries.obj.unwrap_parens(), Expr::Ident(object) if object.sym == "Object")
        || !matches!(&from_entries.prop, MemberProp::Ident(property) if property.sym == "fromEntries")
    {
        return None;
    }
    let [argument] = call.args.as_slice() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Call(map_call) = argument.expr.unwrap_parens() else {
        return None;
    };
    let Callee::Expr(map_callee) = &map_call.callee else {
        return None;
    };
    let Expr::Member(map_member) = map_callee.unwrap_parens() else {
        return None;
    };
    if !matches!(&map_member.prop, MemberProp::Ident(method) if method.sym == "map")
        || map_call.args.len() != 1
        || map_call.args[0].spread.is_some()
    {
        return None;
    }
    let Expr::Ident(source) = map_member.obj.unwrap_parens() else {
        return None;
    };
    if !plain_array_bindings.contains(source.sym.as_ref()) {
        return None;
    }
    let Expr::Arrow(mapper) = map_call.args[0].expr.unwrap_parens() else {
        return None;
    };
    if mapper.is_async
        || mapper.params.len() != 1
        || mapper.type_params.is_some()
        || mapper.return_type.is_some()
    {
        return None;
    }
    let Pat::Ident(parameter) = &mapper.params[0] else {
        return None;
    };
    let ArrowFunctionBody::Expr(body) = mapper.body.as_ref() else {
        return None;
    };
    let Expr::Array(pair) = body.unwrap_parens() else {
        return None;
    };
    if pair.elems.len() != 2
        || pair.elems.iter().any(|element| {
            element
                .as_ref()
                .is_none_or(|element| element.spread.is_some())
        })
    {
        return None;
    }
    let [Some(key), Some(value)] = pair.elems.as_slice() else {
        return None;
    };
    let Expr::Member(key_member) = key.expr.unwrap_parens() else {
        return None;
    };
    let MemberProp::Ident(key_property) = &key_member.prop else {
        return None;
    };
    if !matches!(key_member.obj.unwrap_parens(), Expr::Ident(id) if id.to_id() == parameter.id.to_id())
        || !matches!(value.expr.unwrap_parens(), Expr::Ident(id) if id.to_id() == parameter.id.to_id())
    {
        return None;
    }
    Some(FromEntriesMappedProof {
        source: ValueExpr::Binding(source.to_id()),
        key: key_property.sym.to_string(),
        map_call_site: map_call.span.into(),
        object_intrinsic: match from_entries.obj.unwrap_parens() {
            Expr::Ident(object) => object.to_id(),
            _ => unreachable!("checked Object.fromEntries receiver"),
        },
    })
}

#[derive(Debug, Clone, Default)]
struct ClassValueFacts {
    instance_fields: BTreeMap<String, ValueExpr>,
    ordinary_instance_methods: BTreeSet<String>,
    default_has_instance: bool,
    static_fields: BTreeMap<String, ValueExpr>,
    super_class: Option<ValueExpr>,
    opaque_instance: bool,
    opaque_static: bool,
    allocation_without_user_code: bool,
}

enum FreshValueStep {
    Allocate {
        binding: Id,
        constructor: Id,
    },
    Factory {
        binding: Id,
        callee: ValueExpr,
    },
    Literal {
        binding: Id,
        span: u32,
        own_fields: BTreeSet<String>,
    },
    Write {
        receiver: Id,
        property: String,
    },
}

enum FreshValue {
    Instance(u32),
    Factory,
    Literal(BTreeSet<String>),
}

fn fresh_literal_own_fields(object: &ObjectLit) -> Option<BTreeSet<String>> {
    let mut fields = BTreeSet::new();
    for property in &object.props {
        let (name, value) = match property {
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair) if !matches!(pair.key, PropName::Computed(_)) => {
                    (object_data_property_name(&pair.key)?, pair.value.as_ref())
                }
                Prop::Shorthand(ident) => {
                    if ident.sym == "__proto__" || !fields.insert(ident.sym.to_string()) {
                        return None;
                    }
                    continue;
                }
                _ => return None,
            },
            PropOrSpread::Spread(_) => return None,
        };
        if name == "__proto__"
            || !matches!(value.unwrap_parens(), Expr::Ident(_)) && !value_without_user_code(value)
            || !fields.insert(name)
        {
            return None;
        }
    }
    Some(fields)
}

fn primitive_constant(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_)) => true,
        Expr::Unary(unary) if unary.op != UnaryOp::Delete => primitive_constant(&unary.arg),
        Expr::Bin(binary) if !matches!(binary.op, BinaryOp::In | BinaryOp::InstanceOf) => {
            primitive_constant(&binary.left) && primitive_constant(&binary.right)
        }
        _ => false,
    }
}

fn primitive_literal(expression: &Expr) -> bool {
    matches!(
        expression.unwrap_parens(),
        Expr::Lit(Lit::Str(_) | Lit::Bool(_) | Lit::Null(_) | Lit::Num(_) | Lit::BigInt(_))
    )
}

fn value_without_user_code(expression: &Expr) -> bool {
    if primitive_constant(expression) {
        return true;
    }
    match expression.unwrap_parens() {
        Expr::Arrow(_) => true,
        Expr::Fn(function) => {
            function.function.decorators.is_empty()
                && function
                    .function
                    .params
                    .iter()
                    .all(|parameter| parameter.decorators.is_empty())
        }
        Expr::Array(array) => array.elems.is_empty(),
        // A11 fixes Array.prototype.values as an ordinary data property.
        // The empty receiver cannot publish the instance being initialized.
        Expr::Member(member) => {
            matches!(&member.prop, MemberProp::Ident(property) if property.sym == "values")
                && matches!(member.obj.unwrap_parens(), Expr::Array(array) if array.elems.is_empty())
        }
        _ => false,
    }
}

fn fresh_value_step(item: &ModuleItem) -> Option<FreshValueStep> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration)))
            if declaration.kind == VarDeclKind::Const && declaration.decls.len() == 1 =>
        {
            let declaration = &declaration.decls[0];
            let Pat::Ident(binding) = &declaration.name else {
                return None;
            };
            let initializer = declaration.init.as_deref()?.unwrap_parens();
            if let Expr::Object(object) = initializer {
                return fresh_literal_own_fields(object).map(|own_fields| {
                    FreshValueStep::Literal {
                        binding: binding.id.to_id(),
                        span: object.span.lo.0,
                        own_fields,
                    }
                });
            }
            if let Expr::Call(call) = initializer {
                let [argument] = call.args.as_slice() else {
                    return None;
                };
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                match callee.unwrap_parens() {
                    Expr::Ident(_) => {}
                    Expr::Member(member)
                        if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                            && matches!(member.prop, MemberProp::Ident(_)) => {}
                    _ => return None,
                }
                return (argument.spread.is_none()
                    && matches!(argument.expr.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                    && value_without_user_code(&argument.expr))
                .then(|| FreshValueStep::Factory {
                    binding: binding.id.to_id(),
                    callee: ValueExpr::from_expr(callee),
                });
            }
            let Expr::New(allocation) = initializer else {
                return None;
            };
            let Expr::Ident(constructor) = allocation.callee.unwrap_parens() else {
                return None;
            };
            allocation
                .args
                .as_ref()
                .is_none_or(Vec::is_empty)
                .then(|| FreshValueStep::Allocate {
                    binding: binding.id.to_id(),
                    constructor: constructor.to_id(),
                })
        }
        ModuleItem::Stmt(Stmt::Expr(statement)) => {
            let Expr::Assign(assignment) = statement.expr.unwrap_parens() else {
                return None;
            };
            if assignment.op != AssignOp::Assign || !value_without_user_code(&assignment.right) {
                return None;
            }
            let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left else {
                return None;
            };
            let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                return None;
            };
            let MemberProp::Ident(property) = &member.prop else {
                return None;
            };
            (property.sym != "__proto__").then(|| FreshValueStep::Write {
                receiver: receiver.to_id(),
                property: property.sym.to_string(),
            })
        }
        _ => None,
    }
}

fn fresh_value_writes(
    steps: &[Option<FreshValueStep>],
    facts: &NamespaceValueFacts,
    factory_imports: &BTreeSet<Id>,
    factory_members: &BTreeMap<Id, BTreeSet<String>>,
) -> BTreeSet<StatementOrdinal> {
    let mut active: Option<(Id, FreshValue)> = None;
    let mut writes = BTreeSet::new();
    for (ordinal, step) in steps.iter().enumerate() {
        match step {
            Some(FreshValueStep::Allocate {
                binding,
                constructor,
            }) => {
                active = facts
                    .resolve_class(
                        &ValueExpr::Binding(constructor.clone()),
                        &mut BTreeSet::new(),
                    )
                    .filter(|(key, surface)| {
                        *surface == ClassSurface::Constructor
                            && facts
                                .classes
                                .get(key)
                                .is_some_and(|class| class.allocation_without_user_code)
                    })
                    .map(|(key, _)| (binding.clone(), FreshValue::Instance(key)));
            }
            Some(FreshValueStep::Factory { binding, callee }) => {
                let is_factory = match callee {
                    ValueExpr::Binding(callee) => factory_imports.contains(callee),
                    ValueExpr::Member(object, Some(property), true) => {
                        matches!(object.as_ref(), ValueExpr::Binding(namespace)
                            if factory_members.get(namespace)
                                .is_some_and(|members| members.contains(property)))
                    }
                    _ => false,
                };
                active = is_factory.then(|| (binding.clone(), FreshValue::Factory));
            }
            Some(FreshValueStep::Literal {
                binding,
                span: _,
                own_fields,
            }) => {
                active = Some((binding.clone(), FreshValue::Literal(own_fields.clone())));
            }
            Some(FreshValueStep::Write { receiver, property })
                if active.as_ref().is_some_and(|(binding, value)| {
                    binding == receiver
                        && match value {
                            FreshValue::Instance(key) => {
                                facts.classes[key].instance_fields.contains_key(property)
                            }
                            FreshValue::Factory => property != "prototype",
                            FreshValue::Literal(own_fields) => own_fields.contains(property),
                        }
                }) =>
            {
                writes.insert(StatementOrdinal(ordinal));
            }
            _ => active = None,
        }
    }
    writes
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum ClassSurface {
    Constructor,
    Instance,
}

#[derive(Debug, Clone, Default)]
struct NamespaceValueFacts {
    classes: BTreeMap<u32, ClassValueFacts>,
    verified_data_objects: BTreeSet<u32>,
    /// A binding is resolved only when the retained syntax has exactly one
    /// assignment for it. Reassignments and alternatives remain opaque.
    bindings: BTreeMap<Id, ValueExpr>,
    binding_assignments: BTreeMap<Id, Vec<ValueExpr>>,
    unstable_class_bindings: BTreeSet<Id>,
}

impl ValueExpr {
    fn from_expr(expr: &Expr) -> Self {
        match expr.unwrap_parens() {
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Fn(function) => Self::Origin(ValueOrigin::Function(function.function.span.lo.0)),
            Expr::Arrow(arrow) => Self::Origin(ValueOrigin::Function(arrow.span.lo.0)),
            Expr::Class(class) => Self::Origin(ValueOrigin::Class(class.class.span.lo.0)),
            Expr::Object(object) => Self::ObjectLiteral(
                object.span.lo.0,
                object
                    .props
                    .iter()
                    .map(|property| match property {
                        PropOrSpread::Spread(spread) => (None, Self::from_expr(&spread.expr)),
                        PropOrSpread::Prop(property) => match property.as_ref() {
                            Prop::KeyValue(pair) => (
                                object_data_property_name(&pair.key),
                                Self::from_expr(&pair.value),
                            ),
                            Prop::Shorthand(ident) => {
                                (Some(ident.sym.to_string()), Self::Binding(ident.to_id()))
                            }
                            Prop::Assign(assign) => (
                                Some(assign.key.sym.to_string()),
                                Self::from_expr(&assign.value),
                            ),
                            Prop::Method(method) => (
                                static_property_name(&method.key),
                                Self::Origin(ValueOrigin::Function(method.function.span.lo.0)),
                            ),
                            Prop::Getter(getter) => (
                                static_property_name(&getter.key),
                                Self::Origin(ValueOrigin::Function(getter.function.span.lo.0)),
                            ),
                            Prop::Setter(setter) => (
                                static_property_name(&setter.key),
                                Self::Origin(ValueOrigin::Function(setter.function.span.lo.0)),
                            ),
                        },
                    })
                    .collect(),
            ),
            Expr::Array(array) => Self::ObjectLiteral(
                array.span.lo.0,
                array
                    .elems
                    .iter()
                    .flatten()
                    .map(|element| (None, Self::from_expr(&element.expr)))
                    .collect(),
            ),
            Expr::Member(member) => Self::Member(
                Box::new(Self::from_expr(&member.obj)),
                static_prop(&member.prop),
                matches!(member.prop, MemberProp::Ident(_)),
            ),
            Expr::OptChain(chain) => match chain.base.as_ref() {
                OptChainBase::Member(member)
                    if matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                        && matches!(member.prop, MemberProp::Ident(_)) =>
                {
                    Self::Alternatives(vec![
                        Self::Undefined,
                        Self::Member(
                            Box::new(Self::from_expr(&member.obj)),
                            static_prop(&member.prop),
                            true,
                        ),
                    ])
                }
                // Calls, computed keys and receiver chains retain the unknown
                // publication fallback until their provenance is supported.
                _ => Self::Unknown,
            },
            Expr::Call(call) if call.args.iter().all(|arg| arg.spread.is_none()) => {
                match &call.callee {
                    Callee::Expr(callee) => Self::Call(
                        Box::new(Self::from_expr(callee)),
                        call.args
                            .iter()
                            .map(|arg| Self::from_expr(&arg.expr))
                            .collect(),
                        call.span.into(),
                    ),
                    Callee::Import(_) | Callee::Super(_) => Self::Unknown,
                }
            }
            Expr::New(new_expr)
                if new_expr
                    .args
                    .as_deref()
                    .unwrap_or_default()
                    .iter()
                    .all(|arg| arg.spread.is_none()) =>
            {
                Self::New(
                    Box::new(Self::from_expr(&new_expr.callee)),
                    new_expr
                        .args
                        .as_deref()
                        .unwrap_or_default()
                        .iter()
                        .map(|arg| Self::from_expr(&arg.expr))
                        .collect(),
                )
            }
            Expr::Cond(cond) => Self::Alternatives(vec![
                Self::from_expr(&cond.cons),
                Self::from_expr(&cond.alt),
            ]),
            Expr::Bin(bin)
                if matches!(
                    bin.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                Self::Alternatives(vec![
                    Self::from_expr(&bin.left),
                    Self::from_expr(&bin.right),
                ])
            }
            Expr::Bin(bin) => Self::Primitive(vec![
                Self::from_expr(&bin.left),
                Self::from_expr(&bin.right),
            ]),
            Expr::Unary(unary) => Self::Primitive(vec![Self::from_expr(&unary.arg)]),
            Expr::Update(update) => Self::Primitive(vec![Self::from_expr(&update.arg)]),
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or(Self::Unknown, |expr| Self::from_expr(expr)),
            Expr::Assign(assign) => Self::from_expr(&assign.right),
            Expr::Lit(Lit::Regex(regex)) => Self::ObjectLiteral(regex.span.lo.0, Vec::new()),
            Expr::Lit(_) | Expr::Tpl(_) => Self::NonCallable,
            _ => Self::Unknown,
        }
    }

    fn bindings(&self, result: &mut BTreeSet<Id>) {
        match self {
            Self::Binding(id) => {
                result.insert(id.clone());
            }
            Self::Member(object, _, _) => object.bindings(result),
            Self::Call(callee, args, _) | Self::New(callee, args) => {
                callee.bindings(result);
                for arg in args {
                    arg.bindings(result);
                }
            }
            Self::Alternatives(options) => {
                for option in options {
                    option.bindings(result);
                }
            }
            Self::Primitive(operands) => {
                for operand in operands {
                    operand.bindings(result);
                }
            }
            Self::ObjectLiteral(_, properties) => {
                for (_, value) in properties {
                    value.bindings(result);
                }
            }
            Self::Origin(_) | Self::Undefined | Self::NonCallable | Self::Unknown => {}
        }
    }

    /// Whether this value has an explicit namespace path through its syntax or
    /// a binding already proven to carry that path. Unrelated call/constructor
    /// results are covered by the namespace contract's author audit, while
    /// their use as mutation receivers is handled separately below.
    fn carries_namespace(&self, id: &Id, aliases: &BTreeSet<Id>) -> bool {
        match self {
            Self::Binding(binding) => binding == id || aliases.contains(binding),
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.carries_namespace(id, aliases)),
            Self::Primitive(_) => false,
            Self::Alternatives(options) => options
                .iter()
                .any(|value| value.carries_namespace(id, aliases)),
            Self::Member(object, _, _) => {
                matches!(object.as_ref(), Self::Call(callee, _, _)
                    if Self::is_direct_namespace_member_callee(callee, id))
                    || object.carries_namespace(id, aliases)
            }
            Self::Call(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || (!Self::is_direct_namespace_member_callee(callee, id)
                        && callee.carries_namespace(id, aliases))
            }
            Self::New(callee, args) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || callee.carries_namespace(id, aliases)
            }
            // Unknown syntax is not evidence that a value aliases the
            // namespace. Sinks still handle unknown receivers and
            // publications conservatively below.
            Self::Unknown => false,
            Self::Origin(_) | Self::Undefined | Self::NonCallable => false,
        }
    }

    fn is_direct_namespace_member_callee(callee: &Self, id: &Id) -> bool {
        matches!(callee, Self::Member(object, Some(_), true)
            if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id))
    }

    /// Whether a value being published may expose the namespace. Unsupported
    /// syntax and unsupported projections remain possible namespace values;
    /// aliases to those values are propagated by `infer_namespace_aliases` so
    /// the uncertainty reaches a later sink.
    fn may_publish_namespace(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        self.may_publish_namespace_inner(
            id,
            aliases,
            facts,
            &mut BTreeSet::new(),
            &mut BTreeSet::new(),
        )
    }

    /// Equality with the namespace differs from publishing an object that
    /// contains it. A fresh literal is not the namespace, while a read of one
    /// of its ordinary own data properties can recover the namespace.
    fn may_equal_namespace(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
        visiting: &mut BTreeSet<Id>,
    ) -> bool {
        match self {
            Self::ObjectLiteral(_, _) | Self::Origin(ValueOrigin::Function(_)) => false,
            // Class metadata may contain a namespace path, including a
            // self-referential static initializer. Keep the existing
            // conservative class-surface fallback.
            Self::Origin(ValueOrigin::Class(_)) => self.may_publish_namespace(id, aliases, facts),
            Self::Binding(binding) => {
                if binding == id
                    || aliases.contains(binding)
                    || facts.unstable_class_bindings.contains(binding)
                {
                    return true;
                }
                if !visiting.insert(binding.clone()) {
                    return false;
                }
                let possible = facts
                    .binding_assignments
                    .get(binding)
                    .is_some_and(|values| {
                        values
                            .iter()
                            .any(|value| value.may_equal_namespace(id, aliases, facts, visiting))
                    });
                visiting.remove(binding);
                possible
            }
            Self::Alternatives(values) => values
                .iter()
                .any(|value| value.may_equal_namespace(id, aliases, facts, visiting)),
            Self::Member(object, Some(property), true) => {
                if let Some(value) = facts.own_data_member(object, property, id, aliases) {
                    value.may_equal_namespace(id, aliases, facts, visiting)
                } else {
                    self.may_publish_namespace(id, aliases, facts)
                }
            }
            _ => self.may_publish_namespace(id, aliases, facts),
        }
    }

    fn may_publish_namespace_inner(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
        visiting_bindings: &mut BTreeSet<Id>,
        visiting_classes: &mut BTreeSet<(u32, ClassSurface)>,
    ) -> bool {
        match self {
            Self::Unknown => true,
            Self::Primitive(_) => false,
            Self::ObjectLiteral(_, properties) => properties.iter().any(|(_, value)| {
                value.may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }),
            Self::Alternatives(options) => options.iter().any(|value| {
                value.may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }),
            Self::Member(object, property, is_static) => {
                let projected = property
                    .as_deref()
                    .filter(|_| *is_static)
                    .and_then(|property| facts.own_data_member(object, property, id, aliases));
                projected.unwrap_or(object).may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }
            Self::Call(callee, args, _) => {
                args.iter().any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }) || (!Self::is_direct_namespace_member_callee(callee, id)
                    && callee.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    ))
            }
            Self::New(callee, args) => {
                let local_class = facts
                    .resolve_class(callee, &mut BTreeSet::new())
                    .is_some_and(|(_, surface)| surface == ClassSurface::Constructor);
                args.iter().any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }) || (!local_class
                    && callee.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    ))
                    || facts.class_may_publish(
                        self,
                        id,
                        aliases,
                        visiting_bindings,
                        visiting_classes,
                    )
            }
            Self::Binding(binding) => {
                if binding == id
                    || aliases.contains(binding)
                    || facts.unstable_class_bindings.contains(binding)
                {
                    true
                } else if let Some(values) = facts.binding_assignments.get(binding) {
                    if !visiting_bindings.insert(binding.clone()) {
                        // A binding cycle contributes no namespace provenance
                        // without a namespace or unknown-value seed. The
                        // enclosing traversal still checks every alternative.
                        false
                    } else {
                        let result = values.iter().any(|value| {
                            value.may_publish_namespace_inner(
                                id,
                                aliases,
                                facts,
                                visiting_bindings,
                                visiting_classes,
                            )
                        });
                        visiting_bindings.remove(binding);
                        result
                    }
                } else {
                    false
                }
            }
            Self::Origin(ValueOrigin::Class(_)) => {
                facts.class_may_publish(self, id, aliases, visiting_bindings, visiting_classes)
            }
            Self::Origin(_) | Self::Undefined | Self::NonCallable => false,
        }
    }

    /// Whether this value could be the namespace object used as a property
    /// write receiver. Unknown call and constructor results remain possible
    /// receivers here, even though merely storing or returning those unrelated
    /// results does not establish namespace provenance.
    fn may_be_namespace_mutation_receiver(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        receiver_aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        match self {
            Self::Binding(binding) => {
                binding == id || aliases.contains(binding) || receiver_aliases.contains(binding)
            }
            Self::Primitive(_) => false,
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.carries_namespace(id, aliases)),
            Self::Alternatives(options) => options.iter().any(|value| {
                value.may_be_namespace_mutation_receiver(id, aliases, receiver_aliases, facts)
            }),
            Self::Member(object, property, is_static) => {
                if let Some(value) = property
                    .as_deref()
                    .filter(|_| *is_static)
                    .and_then(|property| facts.own_data_member(object, property, id, aliases))
                {
                    return value.may_be_namespace_mutation_receiver(
                        id,
                        aliases,
                        receiver_aliases,
                        facts,
                    );
                }
                if facts.unique_literal(object).is_some() {
                    // A getter, computed key, spread, or prototype setter can
                    // return the namespace even when the literal is fresh.
                    return true;
                }
                matches!(object.as_ref(), Self::Call(callee, _, _)
                    if Self::is_direct_namespace_member_callee(callee, id))
                    || matches!(object.as_ref(), Self::Unknown | Self::New(_, _))
                    || object.may_be_namespace_mutation_receiver(
                        id,
                        aliases,
                        receiver_aliases,
                        facts,
                    )
            }
            Self::Call(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || !Self::is_direct_namespace_member_callee(callee, id)
            }
            Self::New(_, _) | Self::Unknown => true,
            Self::Origin(_) | Self::Undefined | Self::NonCallable => false,
        }
    }

    /// A callback result may contain one-hop namespace member reads as
    /// ordinary data-property values. That exception applies only to the
    /// direct returned object graph; aliases and other unknown shapes remain
    /// subject to the ordinary publication check.
    fn may_publish_namespace_in_factory_result(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        match self {
            Self::ObjectLiteral(_, properties) => properties.iter().any(|(name, value)| {
                let direct_member_value = name.is_some()
                    && matches!(value, Self::Member(object, Some(_), true)
                        if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id));
                !direct_member_value
                    && value.may_publish_namespace_in_factory_result(id, aliases, facts)
            }),
            _ => self.may_publish_namespace(id, aliases, facts),
        }
    }
}

fn infer_namespace_aliases(
    namespace: &Id,
    assignments: &[&(Id, ValueExpr)],
    facts: &NamespaceValueFacts,
) -> BTreeSet<Id> {
    let mut aliases = BTreeSet::new();
    loop {
        let mut changed = false;
        for assignment in assignments {
            let (target, value) = *assignment;
            if target != namespace
                && value.may_equal_namespace(namespace, &aliases, facts, &mut BTreeSet::new())
                && aliases.insert(target.clone())
            {
                changed = true;
            }
        }
        if !changed {
            return aliases;
        }
    }
}

impl NamespaceValueFacts {
    fn from_assignments(
        classes: BTreeMap<u32, ClassValueFacts>,
        verified_data_objects: BTreeSet<u32>,
        assignments: &[&(Id, ValueExpr)],
    ) -> Self {
        let mut values = BTreeMap::<Id, Vec<ValueExpr>>::new();
        for (binding, value) in assignments {
            values
                .entry(binding.clone())
                .or_default()
                .push(value.clone());
        }
        let mut facts = Self {
            classes,
            verified_data_objects,
            bindings: values
                .iter()
                .filter(|(_, values)| values.len() == 1)
                .map(|(binding, values)| (binding.clone(), values[0].clone()))
                .collect(),
            binding_assignments: values,
            unstable_class_bindings: BTreeSet::new(),
        };
        facts.unstable_class_bindings = facts
            .binding_assignments
            .iter()
            .filter_map(|(binding, values)| {
                let may_be_class = values
                    .iter()
                    .any(|value| facts.may_be_local_class_value(value, &mut BTreeSet::new()));
                let uniquely_resolved = values.len() == 1
                    && facts
                        .resolve_class(&values[0], &mut BTreeSet::new())
                        .is_some();
                (may_be_class && !uniquely_resolved).then(|| binding.clone())
            })
            .collect();
        facts
    }

    fn own_data_member<'a>(
        &'a self,
        object: &'a ValueExpr,
        property: &str,
        id: &Id,
        aliases: &BTreeSet<Id>,
    ) -> Option<&'a ValueExpr> {
        let ValueExpr::ObjectLiteral(span, properties) = self.unique_literal(object)? else {
            return None;
        };
        if !self.verified_data_objects.contains(span) {
            return None;
        }
        // A container with no explicit namespace path keeps the prior
        // conservative receiver rule. Its field might gain an accessor or
        // other value through a call without publishing this namespace.
        if !properties
            .iter()
            .any(|(_, value)| value.carries_namespace(id, aliases))
        {
            return None;
        }
        let mut values = properties
            .iter()
            .filter(|(name, _)| name.as_deref() == Some(property))
            .map(|(_, value)| value);
        let value = values.next()?;
        values.next().is_none().then_some(value)
    }

    fn unique_literal<'a>(&'a self, object: &'a ValueExpr) -> Option<&'a ValueExpr> {
        let object = match object {
            ValueExpr::Binding(binding) => self.bindings.get(binding)?,
            value => value,
        };
        matches!(object, ValueExpr::ObjectLiteral(_, _)).then_some(object)
    }

    fn may_be_local_class_value(&self, value: &ValueExpr, visiting: &mut BTreeSet<Id>) -> bool {
        match value {
            ValueExpr::Origin(ValueOrigin::Class(_)) => true,
            ValueExpr::Binding(binding) => {
                if !visiting.insert(binding.clone()) {
                    return false;
                }
                let result = self.binding_assignments.get(binding).is_some_and(|values| {
                    values
                        .iter()
                        .any(|value| self.may_be_local_class_value(value, visiting))
                });
                visiting.remove(binding);
                result
            }
            ValueExpr::Alternatives(values) => values
                .iter()
                .any(|value| self.may_be_local_class_value(value, visiting)),
            ValueExpr::Member(object, _, _) => {
                self.resolve_class(object, &mut BTreeSet::new()).is_some()
                    || self.may_be_local_class_value(object, visiting)
            }
            _ => false,
        }
    }

    fn resolve_class(
        &self,
        value: &ValueExpr,
        visiting: &mut BTreeSet<Id>,
    ) -> Option<(u32, ClassSurface)> {
        match value {
            ValueExpr::Origin(ValueOrigin::Class(span)) => Some((*span, ClassSurface::Constructor)),
            ValueExpr::Binding(binding) => {
                if !visiting.insert(binding.clone()) {
                    return None;
                }
                let result = self
                    .bindings
                    .get(binding)
                    .and_then(|value| self.resolve_class(value, visiting));
                visiting.remove(binding);
                result
            }
            ValueExpr::New(callee, _) => self
                .resolve_class(callee, visiting)
                .filter(|(_, surface)| *surface == ClassSurface::Constructor)
                .map(|(span, _)| (span, ClassSurface::Instance)),
            _ => None,
        }
    }

    fn class_may_publish(
        &self,
        value: &ValueExpr,
        id: &Id,
        aliases: &BTreeSet<Id>,
        visiting_bindings: &mut BTreeSet<Id>,
        visiting_classes: &mut BTreeSet<(u32, ClassSurface)>,
    ) -> bool {
        let Some((span, surface)) = self.resolve_class(value, &mut BTreeSet::new()) else {
            return false;
        };
        if !visiting_classes.insert((span, surface)) {
            return true;
        }
        let result = self.classes.get(&span).is_none_or(|class| {
            let (opaque, fields) = match surface {
                ClassSurface::Constructor => (class.opaque_static, &class.static_fields),
                ClassSurface::Instance => (class.opaque_instance, &class.instance_fields),
            };
            let inherited_static_may_publish = if surface == ClassSurface::Constructor && !opaque {
                class.super_class.as_ref().is_some_and(|super_class| {
                    let Some((base_span, ClassSurface::Constructor)) =
                        self.resolve_class(super_class, &mut BTreeSet::new())
                    else {
                        return true;
                    };
                    self.class_may_publish(
                        &ValueExpr::Origin(ValueOrigin::Class(base_span)),
                        id,
                        aliases,
                        visiting_bindings,
                        visiting_classes,
                    )
                })
            } else {
                false
            };
            opaque
                || inherited_static_may_publish
                || fields.values().any(|field| {
                    field.may_publish_namespace_inner(
                        id,
                        aliases,
                        self,
                        visiting_bindings,
                        visiting_classes,
                    )
                })
        });
        visiting_classes.remove(&(span, surface));
        result
    }
}

fn add_class_field(facts: &mut ClassValueFacts, is_static: bool, name: String, value: ValueExpr) {
    let fields = if is_static {
        &mut facts.static_fields
    } else {
        &mut facts.instance_fields
    };
    if fields.insert(name.clone(), value).is_some() {
        fields.insert(name, ValueExpr::Unknown);
    }
}

fn collect_class_value_facts(class: &Class) -> ClassValueFacts {
    let mut facts = ClassValueFacts {
        super_class: class.super_class.as_deref().map(ValueExpr::from_expr),
        allocation_without_user_code: class.super_class.is_none() && class.decorators.is_empty(),
        default_has_instance: class.super_class.is_none() && class.decorators.is_empty(),
        ..ClassValueFacts::default()
    };
    if class.super_class.is_some() {
        facts.opaque_instance = true;
    }
    if !class.decorators.is_empty() {
        facts.opaque_instance = true;
        facts.opaque_static = true;
    }
    let mut accessor_names = BTreeSet::new();
    let mut dynamic_instance_keys = !class.decorators.is_empty();
    for member in &class.body {
        facts.default_has_instance &= match member {
            ClassMember::Method(method) => {
                !method.is_static && method.function.decorators.is_empty()
            }
            ClassMember::PrivateMethod(method) => {
                !method.is_static && method.function.decorators.is_empty()
            }
            ClassMember::ClassProp(property) => {
                !property.is_static && property.decorators.is_empty()
            }
            ClassMember::PrivateProp(property) => {
                !property.is_static && property.decorators.is_empty()
            }
            ClassMember::Constructor(_) | ClassMember::Empty(_) => true,
            _ => false,
        };
        match member {
            ClassMember::Method(method) if !method.is_static => {
                if let Some(name) = static_property_name(&method.key) {
                    if method.kind == MethodKind::Method && method.function.decorators.is_empty() {
                        facts.ordinary_instance_methods.insert(name);
                    } else {
                        accessor_names.insert(name);
                    }
                } else {
                    dynamic_instance_keys = true;
                }
            }
            ClassMember::ClassProp(property) if !property.is_static => {
                dynamic_instance_keys |= static_property_name(&property.key).is_none()
                    || !property.decorators.is_empty();
            }
            ClassMember::AutoAccessor(_) => dynamic_instance_keys = true,
            _ => {}
        }
        facts.allocation_without_user_code &= match member {
            ClassMember::ClassProp(property) => {
                !property.is_static
                    && property.decorators.is_empty()
                    && static_property_name(&property.key).is_some_and(|name| name != "__proto__")
                    && property
                        .value
                        .as_deref()
                        .is_none_or(value_without_user_code)
            }
            ClassMember::Method(method) => {
                !method.is_static
                    && matches!(method.kind, MethodKind::Method)
                    && method.function.decorators.is_empty()
                    && method
                        .function
                        .params
                        .iter()
                        .all(|parameter| parameter.decorators.is_empty())
            }
            ClassMember::Empty(_) => true,
            _ => false,
        };
        match member {
            ClassMember::ClassProp(prop) => {
                if !prop.decorators.is_empty() {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                }
                let Some(name) = static_property_name(&prop.key) else {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                };
                add_class_field(
                    &mut facts,
                    prop.is_static,
                    name,
                    prop.value
                        .as_deref()
                        .map_or(ValueExpr::Undefined, ValueExpr::from_expr),
                );
            }
            ClassMember::PrivateProp(prop) => {
                if !prop.decorators.is_empty() {
                    if prop.is_static {
                        facts.opaque_static = true;
                    } else {
                        facts.opaque_instance = true;
                    }
                    continue;
                }
                add_class_field(
                    &mut facts,
                    prop.is_static,
                    format!("#{}", prop.key.name),
                    prop.value
                        .as_deref()
                        .map_or(ValueExpr::Undefined, ValueExpr::from_expr),
                );
            }
            ClassMember::Method(method)
                if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
            {
                if method.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::PrivateMethod(method)
                if matches!(method.kind, MethodKind::Getter | MethodKind::Setter) =>
            {
                if method.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::Constructor(_) => facts.opaque_instance = true,
            ClassMember::AutoAccessor(accessor) => {
                if accessor.is_static {
                    facts.opaque_static = true;
                } else {
                    facts.opaque_instance = true;
                }
            }
            ClassMember::StaticBlock(_) => facts.opaque_static = true,
            ClassMember::TsIndexSignature(_) => {
                facts.opaque_instance = true;
                facts.opaque_static = true;
            }
            _ => {}
        }
    }
    if dynamic_instance_keys {
        facts.ordinary_instance_methods.clear();
    } else {
        facts
            .ordinary_instance_methods
            .retain(|name| !accessor_names.contains(name));
    }
    facts
}

fn collect_pattern_assignments(
    pattern: &Pat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match pattern {
        Pat::Ident(binding) => assignments.push((binding.id.to_id(), value.clone())),
        Pat::Assign(assign) => {
            let value =
                ValueExpr::Alternatives(vec![value.clone(), ValueExpr::from_expr(&assign.right)]);
            collect_pattern_assignments(&assign.left, &value, assignments);
        }
        Pat::Rest(rest) => {
            collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
        }
        Pat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        let projected = static_property_name(&property.key)
                            .map_or(ValueExpr::Unknown, |name| {
                                ValueExpr::Member(Box::new(value.clone()), Some(name), false)
                            });
                        collect_pattern_assignments(&property.value, &projected, assignments);
                    }
                    ObjectPatProp::Assign(property) => {
                        let projected = ValueExpr::Member(
                            Box::new(value.clone()),
                            Some(property.key.id.sym.to_string()),
                            false,
                        );
                        let projected =
                            property
                                .value
                                .as_deref()
                                .map_or(projected.clone(), |default| {
                                    ValueExpr::Alternatives(vec![
                                        projected,
                                        ValueExpr::from_expr(default),
                                    ])
                                });
                        assignments.push((property.key.id.to_id(), projected));
                    }
                    ObjectPatProp::Rest(rest) => {
                        collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
                    }
                }
            }
        }
        // Array destructuring invokes the iterator protocol. Retain its bound
        // names but do not infer element identity from a syntax-only array
        // shape; unknown iteration results must reach later publication sinks.
        Pat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_pattern_assignments(element, &ValueExpr::Unknown, assignments);
            }
        }
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

fn collect_assign_target_assignments(
    target: &AssignTargetPat,
    value: &ValueExpr,
    assignments: &mut Vec<(Id, ValueExpr)>,
) {
    match target {
        AssignTargetPat::Object(object) => {
            for property in &object.props {
                match property {
                    ObjectPatProp::KeyValue(property) => {
                        let projected = static_property_name(&property.key)
                            .map_or(ValueExpr::Unknown, |name| {
                                ValueExpr::Member(Box::new(value.clone()), Some(name), false)
                            });
                        collect_pattern_assignments(&property.value, &projected, assignments);
                    }
                    ObjectPatProp::Assign(property) => {
                        let projected = ValueExpr::Member(
                            Box::new(value.clone()),
                            Some(property.key.id.sym.to_string()),
                            false,
                        );
                        let projected =
                            property
                                .value
                                .as_deref()
                                .map_or(projected.clone(), |default| {
                                    ValueExpr::Alternatives(vec![
                                        projected,
                                        ValueExpr::from_expr(default),
                                    ])
                                });
                        assignments.push((property.key.id.to_id(), projected));
                    }
                    ObjectPatProp::Rest(rest) => {
                        collect_pattern_assignments(&rest.arg, &ValueExpr::Unknown, assignments);
                    }
                }
            }
        }
        AssignTargetPat::Array(array) => {
            for element in array.elems.iter().flatten() {
                collect_pattern_assignments(element, &ValueExpr::Unknown, assignments);
            }
        }
        AssignTargetPat::Invalid(_) => {}
    }
}

fn infer_mutation_receiver_aliases(
    namespace: &Id,
    assignments: &[&(Id, ValueExpr)],
    aliases: &BTreeSet<Id>,
    facts: &NamespaceValueFacts,
) -> BTreeSet<Id> {
    let mut receiver_aliases = BTreeSet::new();
    loop {
        let mut changed = false;
        for assignment in assignments {
            let (target, value) = *assignment;
            if target != namespace
                && value.may_be_namespace_mutation_receiver(
                    namespace,
                    aliases,
                    &receiver_aliases,
                    facts,
                )
                && receiver_aliases.insert(target.clone())
            {
                changed = true;
            }
        }
        if !changed {
            return receiver_aliases;
        }
    }
}

fn has_definite_primitive_result(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(Lit::Regex(_)) => false,
        Expr::Lit(_) | Expr::Tpl(_) | Expr::Unary(_) | Expr::Update(_) => true,
        Expr::Bin(binary) => !matches!(
            binary.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ),
        _ => false,
    }
}

/// Admit only arguments whose evaluation cannot itself trigger caller code.
/// The provider must separately be audited for every admitted argument shape.
/// The call result stays opaque and the ordinary visitor records the effects.
fn no_reentry_primitive_argument(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Lit(literal) => !matches!(literal, Lit::Regex(_)),
        Expr::Unary(unary) if matches!(unary.op, UnaryOp::Void | UnaryOp::Bang) => {
            matches!(unary.arg.unwrap_parens(), Expr::Lit(literal) if !matches!(literal, Lit::Regex(_)))
        }
        Expr::Tpl(template) => template.exprs.is_empty(),
        _ => false,
    }
}

fn no_reentry_inert_argument(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Object(object) => object.props.iter().all(|property| {
            let PropOrSpread::Prop(property) = property else {
                return false;
            };
            let Prop::KeyValue(pair) = property.as_ref() else {
                return false;
            };
            let static_key = match &pair.key {
                PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
                PropName::Str(string) => string.value.to_string_lossy() != "__proto__",
                PropName::Num(_) | PropName::BigInt(_) => true,
                PropName::Computed(_) => false,
            };
            static_key && no_reentry_inert_argument(&pair.value)
        }),
        _ => no_reentry_primitive_argument(expression),
    }
}

#[derive(Debug, Clone)]
struct Parameter {
    binding: Option<Id>,
    default: Option<ValueExpr>,
}

impl Parameter {
    fn from_pattern(pattern: &Pat) -> Self {
        match pattern {
            Pat::Ident(binding) => Self {
                binding: Some(binding.id.to_id()),
                default: None,
            },
            Pat::Assign(assign) => Self {
                binding: match assign.left.as_ref() {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                },
                default: Some(ValueExpr::from_expr(&assign.right)),
            },
            _ => Self {
                binding: None,
                default: None,
            },
        }
    }
}

#[derive(Debug, Clone)]
struct Call {
    kind: CallKind,
    callee: ValueExpr,
    args: Vec<ValueExpr>,
    span: Span,
    caller: Option<u32>,
    owner: StatementOrdinal,
    plain_object_sources: bool,
    no_sync_arguments: bool,
    no_reentry_candidate: bool,
    no_reentry_record_candidate: bool,
    has_spread: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CallKind {
    Call,
    New,
    ImplicitCoercion,
    ArraySpread,
    ObjectSpread,
    LocalMemberRead,
    InProtocol,
    InstanceOfProtocol,
    DeepDestructuringProtocol,
}

fn shallow_data_pattern(pattern: &Pat) -> bool {
    fn leaf(pattern: &Pat) -> bool {
        matches!(pattern, Pat::Ident(_))
            || matches!(pattern, Pat::Assign(assign) if matches!(assign.left.as_ref(), Pat::Ident(_)))
    }
    match pattern {
        Pat::Object(object) => object.props.iter().all(|property| match property {
            ObjectPatProp::KeyValue(property) => leaf(&property.value),
            ObjectPatProp::Assign(_) => true,
            ObjectPatProp::Rest(rest) => matches!(rest.arg.as_ref(), Pat::Ident(_)),
        }),
        Pat::Array(array) => array.elems.iter().flatten().all(|element| match element {
            Pat::Rest(rest) => matches!(rest.arg.as_ref(), Pat::Ident(_)),
            other => leaf(other),
        }),
        _ => false,
    }
}

fn data_read_operand(expression: &Expr) -> bool {
    match expression.unwrap_parens() {
        Expr::Ident(_)
        | Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::BigInt(_) | Lit::Bool(_) | Lit::Null(_)) => {
            true
        }
        Expr::Member(member) if matches!(member.prop, MemberProp::Ident(_)) => {
            data_read_operand(&member.obj)
        }
        _ => false,
    }
}

fn plain_assign_source_property(property: &PropOrSpread) -> bool {
    let PropOrSpread::Prop(property) = property else {
        return false;
    };
    let name = match property.as_ref() {
        Prop::KeyValue(pair) => &pair.key,
        Prop::Shorthand(ident) => return ident.sym.as_ref() != "__proto__",
        _ => return false,
    };
    match name {
        PropName::Ident(ident) => ident.sym.as_ref() != "__proto__",
        PropName::Str(string) => string.value.to_string_lossy() != "__proto__",
        PropName::Num(_) | PropName::BigInt(_) => true,
        PropName::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => string.value.to_string_lossy() != "__proto__",
            Expr::Lit(Lit::Num(_)) | Expr::Lit(Lit::BigInt(_)) => true,
            _ => false,
        },
    }
}

fn static_property_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(string) => Some(string.value.to_string_lossy().into_owned()),
        PropName::Num(number) => Some(number.value.to_string()),
        PropName::BigInt(number) => Some(number.value.to_string()),
        PropName::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => Some(string.value.to_string_lossy().into_owned()),
            Expr::Lit(Lit::Num(number)) => Some(number.value.to_string()),
            _ => None,
        },
    }
}

fn object_data_property_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) if ident.sym == "__proto__" => None,
        PropName::Str(string) if string.value.to_string_lossy() == "__proto__" => None,
        _ => static_property_name(name),
    }
}

#[derive(Debug, Clone)]
struct CallbackResultWrite {
    receiver: ValueExpr,
    property_key: ValueExpr,
    simple_assignment: bool,
    data_read_operands: bool,
    property: Option<String>,
    value: ValueExpr,
}

#[derive(Debug, Clone)]
struct Callable {
    owner: StatementOrdinal,
    created_after_parent_await: bool,
    params: Vec<Parameter>,
    reads: BTreeSet<Id>,
    rebinds: BTreeSet<Id>,
    calls: Vec<Call>,
    local_assignments: Vec<(Id, ValueExpr)>,
    returns: Vec<ValueExpr>,
    local_functions: BTreeSet<Id>,
    literal_captures: BTreeSet<Id>,
    literal_exposures: BTreeSet<Id>,
    escaped_bindings: BTreeSet<Id>,
    namespace_escape_values: Vec<ValueExpr>,
    member_writes: Vec<(Id, String)>,
    member_value_writes: Vec<(Id, Option<String>, ValueExpr)>,
    callback_result_writes: Vec<CallbackResultWrite>,
    dynamic_property_effect: bool,
    dynamic_property_receivers: Vec<ValueExpr>,
    opaque: bool,
    generator: bool,
    may_fall_through: bool,
}

#[derive(Debug, Clone)]
struct LocalConstructor {
    key: Option<u32>,
    /// No constructor body or instance fields run during direct allocation.
    inert_allocation: bool,
    /// Own data fields written by the checked constructor body.
    own_fields: BTreeSet<String>,
    /// Parameter property reads must come from fresh plain data objects.
    property_reads: BTreeSet<usize>,
    /// Unqualified reads must resolve to local bindings, not host getters.
    binding_reads: BTreeSet<Id>,
}

fn safe_constructor_value(
    expression: &Expr,
    params: &[Option<Id>],
    property_reads: &mut BTreeSet<usize>,
    binding_reads: &mut BTreeSet<Id>,
) -> bool {
    match expression.unwrap_parens() {
        Expr::Ident(id) => {
            if !params
                .iter()
                .any(|param| param.as_ref() == Some(&id.to_id()))
            {
                binding_reads.insert(id.to_id());
            }
            true
        }
        Expr::Lit(_) => true,
        Expr::Unary(unary) if unary.op == UnaryOp::Bang => {
            safe_constructor_value(&unary.arg, params, property_reads, binding_reads)
        }
        Expr::Member(member) if matches!(&member.prop, MemberProp::Ident(_)) => {
            let Expr::Ident(id) = member.obj.unwrap_parens() else {
                return false;
            };
            let Some(index) = params
                .iter()
                .position(|param| param.as_ref() == Some(&id.to_id()))
            else {
                return false;
            };
            property_reads.insert(index);
            true
        }
        Expr::Bin(bin) if matches!(bin.op, BinaryOp::NullishCoalescing | BinaryOp::EqEqEq) => {
            safe_constructor_value(&bin.left, params, property_reads, binding_reads)
                && safe_constructor_value(&bin.right, params, property_reads, binding_reads)
        }
        _ => false,
    }
}

fn safe_constructor_assignment(
    expression: &Expr,
    params: &[Option<Id>],
    property_reads: &mut BTreeSet<usize>,
    binding_reads: &mut BTreeSet<Id>,
    written_properties: &mut BTreeSet<String>,
) -> bool {
    match expression.unwrap_parens() {
        Expr::Seq(seq) => seq.exprs.iter().all(|expr| {
            safe_constructor_assignment(
                expr,
                params,
                property_reads,
                binding_reads,
                written_properties,
            )
        }),
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            let AssignTarget::Simple(SimpleAssignTarget::Member(target)) = &assign.left else {
                return false;
            };
            let MemberProp::Ident(property) = &target.prop else {
                return false;
            };
            if !matches!(target.obj.unwrap_parens(), Expr::This(_))
                || property.sym.as_ref() == "__proto__"
                || !safe_constructor_value(&assign.right, params, property_reads, binding_reads)
            {
                return false;
            }
            written_properties.insert(property.sym.to_string());
            true
        }
        _ => false,
    }
}

fn local_constructor(class: &Class) -> Option<LocalConstructor> {
    if class.super_class.is_some() || !class.decorators.is_empty() {
        return None;
    }
    let mut constructor = None;
    let mut has_instance_fields = false;
    for member in &class.body {
        match member {
            ClassMember::Constructor(ctor) if constructor.is_none() => constructor = Some(ctor),
            ClassMember::Method(method)
                if method.kind != MethodKind::Setter
                    && !matches!(method.key, PropName::Computed(_))
                    && method.function.decorators.is_empty() => {}
            ClassMember::PrivateMethod(method) if method.function.decorators.is_empty() => {}
            ClassMember::ClassProp(prop)
                if !prop.is_static
                    && prop.decorators.is_empty()
                    && !matches!(prop.key, PropName::Computed(_))
                    && prop.value.as_deref().is_none_or(|value| {
                        matches!(value.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                    }) =>
            {
                has_instance_fields = true
            }
            ClassMember::PrivateProp(prop)
                if !prop.is_static
                    && prop.decorators.is_empty()
                    && prop.value.as_deref().is_none_or(|value| {
                        matches!(value.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_))
                    }) =>
            {
                has_instance_fields = true
            }
            ClassMember::Empty(_) => {}
            _ => return None,
        }
    }
    let Some(ctor) = constructor else {
        return Some(LocalConstructor {
            key: None,
            inert_allocation: !has_instance_fields,
            own_fields: BTreeSet::new(),
            property_reads: BTreeSet::new(),
            binding_reads: BTreeSet::new(),
        });
    };
    let params: Vec<_> = ctor
        .params
        .iter()
        .map(|param| match param {
            ParamOrTsParamProp::Param(param) if param.decorators.is_empty() => match &param.pat {
                Pat::Ident(binding) => Some(binding.id.to_id()),
                _ => None,
            },
            _ => None,
        })
        .collect();
    if params.iter().any(Option::is_none) {
        return None;
    }
    let mut property_reads = BTreeSet::new();
    let mut binding_reads = BTreeSet::new();
    let mut written_properties = BTreeSet::new();
    for statement in &ctor.body.as_ref()?.stmts {
        let Stmt::Expr(expr) = statement else {
            return None;
        };
        if !safe_constructor_assignment(
            &expr.expr,
            &params,
            &mut property_reads,
            &mut binding_reads,
            &mut written_properties,
        ) {
            return None;
        }
    }
    // A getter with the same name on this class's prototype makes assignment
    // to the fresh instance throw. All other accepted writes create ordinary
    // own data properties under the unmodified built-in prototype premise.
    if class.body.iter().any(|member| {
        matches!(member, ClassMember::Method(method)
            if !method.is_static && method.kind == MethodKind::Getter
                && static_property_name(&method.key)
                    .is_some_and(|name| written_properties.contains(&name)))
    }) {
        return None;
    }
    Some(LocalConstructor {
        key: Some(ctor.span.lo.0),
        inert_allocation: false,
        own_fields: written_properties,
        property_reads,
        binding_reads,
    })
}

impl Default for Callable {
    fn default() -> Self {
        Self {
            owner: StatementOrdinal(0),
            created_after_parent_await: false,
            params: Vec::new(),
            reads: BTreeSet::new(),
            rebinds: BTreeSet::new(),
            calls: Vec::new(),
            local_assignments: Vec::new(),
            returns: Vec::new(),
            local_functions: BTreeSet::new(),
            literal_captures: BTreeSet::new(),
            literal_exposures: BTreeSet::new(),
            escaped_bindings: BTreeSet::new(),
            namespace_escape_values: Vec::new(),
            member_writes: Vec::new(),
            member_value_writes: Vec::new(),
            callback_result_writes: Vec::new(),
            dynamic_property_effect: false,
            dynamic_property_receivers: Vec::new(),
            opaque: false,
            generator: false,
            may_fall_through: true,
        }
    }
}

fn sole_return_expression(body: &FunctionBody) -> Option<&Expr> {
    match body.stmts.as_slice() {
        [Stmt::Return(ret)] => ret.arg.as_deref(),
        _ => None,
    }
}

fn is_callback_forwarder(expr: &Expr, callback: &Id) -> bool {
    let body = match expr.unwrap_parens() {
        Expr::Arrow(arrow) if !arrow.is_async && arrow.params.is_empty() => {
            match arrow.body.as_ref() {
                ArrowFunctionBody::Expr(expr) => Some(expr.as_ref()),
                ArrowFunctionBody::FunctionBody(body) => sole_return_expression(body),
            }
        }
        Expr::Fn(function)
            if !function.function.is_async
                && !function.function.is_generator
                && function.function.params.is_empty()
                && function.function.decorators.is_empty() =>
        {
            function
                .function
                .body
                .as_ref()
                .and_then(sole_return_expression)
        }
        _ => None,
    };
    matches!(body.map(Expr::unwrap_parens), Some(Expr::Call(call)) if call.args.is_empty()
        && matches!(&call.callee, Callee::Expr(callee) if matches!(callee.unwrap_parens(), Expr::Ident(ident) if ident.to_id() == *callback)))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum IntrinsicRoot {
    GlobalObject,
    Error,
    Object,
    Array,
}

type IntrinsicRoots = BTreeSet<IntrinsicRoot>;

fn intrinsic_roots(
    value: &ValueExpr,
    roots: &BTreeMap<Id, IntrinsicRoots>,
    globals: &BTreeSet<&str>,
) -> IntrinsicRoots {
    match value {
        ValueExpr::Binding(id) if id.0.as_ref() == "Error" => {
            BTreeSet::from([IntrinsicRoot::Error])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Object" => {
            BTreeSet::from([IntrinsicRoot::Object])
        }
        ValueExpr::Binding(id) if id.0.as_ref() == "Array" => {
            BTreeSet::from([IntrinsicRoot::Array])
        }
        ValueExpr::Binding(id) if globals.contains(id.0.as_ref()) => {
            BTreeSet::from([IntrinsicRoot::GlobalObject])
        }
        ValueExpr::Binding(id) => roots.get(id).cloned().unwrap_or_default(),
        ValueExpr::Alternatives(options) => {
            options
                .iter()
                .fold(IntrinsicRoots::new(), |mut combined, option| {
                    combined.extend(intrinsic_roots(option, roots, globals));
                    combined
                })
        }
        ValueExpr::Member(base, property, _) => {
            let base_roots = intrinsic_roots(base, roots, globals);
            let mut out = IntrinsicRoots::new();
            for root in base_roots {
                match root {
                    IntrinsicRoot::GlobalObject => match property.as_deref() {
                        Some("Error") => {
                            out.insert(IntrinsicRoot::Error);
                        }
                        Some("Object") => {
                            out.insert(IntrinsicRoot::Object);
                        }
                        Some("Array") => {
                            out.insert(IntrinsicRoot::Array);
                        }
                        Some("globalThis" | "window" | "self" | "frames" | "top") | None => {
                            out.insert(IntrinsicRoot::GlobalObject);
                        }
                        _ => {}
                    },
                    // A member of a builtin constructor remains rooted in that
                    // constructor: Array.prototype.map, Array[Symbol.species],
                    // and Object.fromEntries are mutations of their respective
                    // intrinsic families.
                    IntrinsicRoot::Error | IntrinsicRoot::Object | IntrinsicRoot::Array => {
                        out.insert(root);
                    }
                }
            }
            out
        }
        _ => IntrinsicRoots::new(),
    }
}

fn intrinsic_write_target(member: &MemberExpr) -> ValueExpr {
    ValueExpr::Member(
        Box::new(ValueExpr::from_expr(&member.obj)),
        static_prop(&member.prop),
        matches!(member.prop, MemberProp::Ident(_)),
    )
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct FreshErrorWriteSite {
    owner: StatementOrdinal,
    callable: Option<u32>,
    receiver: Id,
    property: String,
}

struct FreshErrorWriteClaim {
    site: FreshErrorWriteSite,
    callee: Id,
    local_read: Option<Id>,
}

struct DestructuringMemberWrite {
    receiver: ValueExpr,
    /// The whole source (including defaults) over-approximates the target value.
    source: ValueExpr,
}

/// A sufficient source-privacy proof, independent of execution/value-flow facts.
/// Every configured root belongs to one potentially aliasing namespace family.
#[derive(Default)]
struct NamespacePrivacy {
    members: BTreeMap<Id, BTreeSet<String>>,
    import_sites: BTreeSet<u32>,
    allowed_read: Option<u32>,
    optional_members: BTreeSet<u32>,
    target_depth: usize,
    invalid: bool,
}

impl NamespacePrivacy {
    fn record_ident(&mut self, ident: &Ident) {
        if self.members.contains_key(&ident.to_id())
            && !self.import_sites.contains(&ident.span.lo.0)
            && self.allowed_read != Some(ident.span.lo.0)
        {
            self.invalid = true;
        }
    }

    fn read_receiver(&self, member: &MemberExpr) -> Option<u32> {
        if self.target_depth == 0
            && !self.optional_members.contains(&member.span.lo.0)
            && let Expr::Ident(receiver) = member.obj.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && self
                .members
                .get(&receiver.to_id())
                .is_some_and(|members| members.contains(property.sym.as_ref()))
        {
            Some(receiver.span.lo.0)
        } else {
            None
        }
    }
}

struct ExecutionIndex {
    owner: StatementOrdinal,
    current: Option<u32>,
    past_await: bool,
    may_skip_await_depth: u32,
    conditional_depth: u32,
    literal_depth: u32,
    factory_member_value_depth: u32,
    factory_callback_depth: u32,
    callables: BTreeMap<u32, Callable>,
    local_constructors: BTreeMap<u32, LocalConstructor>,
    class_values: BTreeMap<u32, ClassValueFacts>,
    class_owners: BTreeMap<u32, StatementOrdinal>,
    class_bindings: BTreeMap<u32, Id>,
    class_ids: BTreeSet<Id>,
    other_class_uses: BTreeSet<Id>,
    safe_class_target: Option<Id>,
    plain_data_objects: BTreeSet<u32>,
    exact_object_spread_copies: BTreeSet<u32>,
    map_entry_arrays: BTreeSet<u32>,
    dense_array_literals: BTreeMap<u32, usize>,
    array_elements: BTreeMap<u32, Vec<Option<ArrayElement>>>,
    array_iterations: BTreeMap<CallSite, ArrayIteration>,
    async_callables: BTreeSet<u32>,
    callable_member_reads: BTreeMap<u32, Vec<(ValueExpr, Option<String>)>>,
    direct_call_members: BTreeSet<CallSite>,
    member_write_sites: BTreeSet<CallSite>,
    primitive_data_properties: BTreeSet<(u32, String)>,
    from_entries_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    mapped_array_map_calls: BTreeMap<CallSite, FromEntriesMappedProof>,
    plain_array_bindings: BTreeSet<String>,
    mutated_intrinsic_roots: IntrinsicRoots,
    reassigned_map: bool,
    namespace_privacy: NamespacePrivacy,
    fresh_error_claims: Vec<FreshErrorWriteClaim>,
    intrinsic_mutation_receivers: Vec<ValueExpr>,
    destructuring_source: Option<ValueExpr>,
    destructuring_member_writes: Vec<DestructuringMemberWrite>,
    setter_owners: BTreeMap<Option<String>, BTreeSet<StatementOrdinal>>,
    hoisted_nested_functions: BTreeSet<u32>,
    owner_callable_prefix_reads: Vec<Option<BTreeSet<Id>>>,
    unindexed_accessor_owners: BTreeSet<StatementOrdinal>,
    top_calls: Vec<Vec<Call>>,
    top_property_writes: Vec<Vec<(Option<Id>, Option<String>)>>,
    top_dynamic_property_receivers: Vec<Vec<ValueExpr>>,
    top_assignments: Vec<Vec<(Id, ValueExpr)>>,
    top_definite_initializers: Vec<BTreeSet<Id>>,
    top_member_value_writes: Vec<Vec<(Id, Option<String>, ValueExpr)>>,
    top_callback_result_writes: Vec<Vec<CallbackResultWrite>>,
    top_exported_values: Vec<Vec<ValueExpr>>,
    top_metadata_decorations: Vec<Option<MetadataDecorateCall>>,
    imported_bindings: BTreeSet<Id>,
    hoisted_bindings: BTreeMap<Id, u32>,
    exported_hoisted_functions: BTreeSet<u32>,
    global_object_names: BTreeSet<&'static str>,
    hoisted_function_owners: BTreeSet<StatementOrdinal>,
    exported_declaration_owners: BTreeSet<StatementOrdinal>,
    exported_bindings: BTreeSet<Id>,
    linkage_owners: BTreeSet<StatementOrdinal>,
    dynamic_scope_owners: BTreeSet<StatementOrdinal>,
    opaque_top: BTreeSet<StatementOrdinal>,
    no_sync_callback_members: BTreeMap<String, BTreeSet<String>>,
    callback_factory_imports: BTreeSet<Id>,
    inline_callback_factory_imports: BTreeSet<Id>,
    callback_factory_members: BTreeMap<Id, BTreeSet<String>>,
    no_reentry_members: BTreeMap<Id, BTreeSet<String>>,
    no_reentry_record_members: BTreeMap<Id, BTreeSet<String>>,
    callback_factory_functions: BTreeSet<u32>,
}

impl Default for ExecutionIndex {
    fn default() -> Self {
        Self {
            owner: StatementOrdinal(0),
            current: None,
            past_await: false,
            may_skip_await_depth: 0,
            conditional_depth: 0,
            literal_depth: 0,
            factory_member_value_depth: 0,
            factory_callback_depth: 0,
            callables: BTreeMap::new(),
            local_constructors: BTreeMap::new(),
            class_values: BTreeMap::new(),
            class_owners: BTreeMap::new(),
            class_bindings: BTreeMap::new(),
            class_ids: BTreeSet::new(),
            other_class_uses: BTreeSet::new(),
            safe_class_target: None,
            plain_data_objects: BTreeSet::new(),
            exact_object_spread_copies: BTreeSet::new(),
            map_entry_arrays: BTreeSet::new(),
            dense_array_literals: BTreeMap::new(),
            array_elements: BTreeMap::new(),
            array_iterations: BTreeMap::new(),
            async_callables: BTreeSet::new(),
            callable_member_reads: BTreeMap::new(),
            direct_call_members: BTreeSet::new(),
            member_write_sites: BTreeSet::new(),
            primitive_data_properties: BTreeSet::new(),
            from_entries_map_calls: BTreeMap::new(),
            mapped_array_map_calls: BTreeMap::new(),
            plain_array_bindings: BTreeSet::new(),
            mutated_intrinsic_roots: IntrinsicRoots::new(),
            reassigned_map: false,
            namespace_privacy: NamespacePrivacy::default(),
            fresh_error_claims: Vec::new(),
            intrinsic_mutation_receivers: Vec::new(),
            destructuring_source: None,
            destructuring_member_writes: Vec::new(),
            setter_owners: BTreeMap::new(),
            hoisted_nested_functions: BTreeSet::new(),
            owner_callable_prefix_reads: Vec::new(),
            unindexed_accessor_owners: BTreeSet::new(),
            top_calls: Vec::new(),
            top_property_writes: Vec::new(),
            top_dynamic_property_receivers: Vec::new(),
            top_assignments: Vec::new(),
            top_definite_initializers: Vec::new(),
            top_member_value_writes: Vec::new(),
            top_callback_result_writes: Vec::new(),
            top_exported_values: Vec::new(),
            top_metadata_decorations: Vec::new(),
            imported_bindings: BTreeSet::new(),
            hoisted_bindings: BTreeMap::new(),
            exported_hoisted_functions: BTreeSet::new(),
            global_object_names: BTreeSet::new(),
            hoisted_function_owners: BTreeSet::new(),
            exported_declaration_owners: BTreeSet::new(),
            exported_bindings: BTreeSet::new(),
            linkage_owners: BTreeSet::new(),
            dynamic_scope_owners: BTreeSet::new(),
            opaque_top: BTreeSet::new(),
            no_sync_callback_members: BTreeMap::new(),
            callback_factory_imports: BTreeSet::new(),
            inline_callback_factory_imports: BTreeSet::new(),
            callback_factory_members: BTreeMap::new(),
            no_reentry_members: BTreeMap::new(),
            no_reentry_record_members: BTreeMap::new(),
            callback_factory_functions: BTreeSet::new(),
        }
    }
}

impl ExecutionIndex {
    fn collect(
        body: &[TopLevelItemView<'_>],
        hints: &AnalysisHints,
        flow: Option<&ChunkValueFlow>,
        plain_array_bindings: &BTreeSet<String>,
    ) -> Self {
        let mut index = Self {
            top_calls: vec![Vec::new(); body.len()],
            owner_callable_prefix_reads: vec![None; body.len()],
            top_property_writes: vec![Vec::new(); body.len()],
            top_dynamic_property_receivers: vec![Vec::new(); body.len()],
            top_assignments: vec![Vec::new(); body.len()],
            top_definite_initializers: vec![BTreeSet::new(); body.len()],
            top_member_value_writes: vec![Vec::new(); body.len()],
            top_callback_result_writes: vec![Vec::new(); body.len()],
            top_exported_values: vec![Vec::new(); body.len()],
            top_metadata_decorations: body
                .iter()
                .map(|item| metadata_decorate_call(item.as_module_item(), &hints.known_effects))
                .collect(),
            global_object_names: super::global_object::unshadowed_global_object_aliases(body),
            plain_array_bindings: plain_array_bindings.clone(),
            no_sync_callback_members: hints.no_sync_callback_members.clone(),
            ..Self::default()
        };
        let mut fresh_steps = Vec::with_capacity(body.len());
        let mut max_source_position = 0u32;
        for item in body {
            max_source_position = max_source_position.max(item.as_module_item().span().hi.0);
            // Seed all candidate roots before visiting any item, including imports
            // textually after a use. Other imported aliases remain an author audit.
            if (!hints.namespace_independent_members.is_empty()
                || !hints.no_reentry_members.is_empty()
                || !hints.no_reentry_record_members.is_empty())
                && let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item()
            {
                for specifier in &import.specifiers {
                    let local = specifier.local();
                    let name = local.sym.as_ref();
                    if hints.callback_factory_members.contains_key(name)
                        || hints.no_reentry_members.contains_key(name)
                        || hints.no_reentry_record_members.contains_key(name)
                        || hints.namespace_independent_members.contains_key(name)
                    {
                        index.namespace_privacy.members.insert(
                            local.to_id(),
                            hints
                                .namespace_independent_members
                                .get(name)
                                .cloned()
                                .unwrap_or_default(),
                        );
                        index.namespace_privacy.import_sites.insert(local.span.lo.0);
                    }
                }
            }
            let declaration = match item.as_module_item() {
                ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration))) => Some(declaration),
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    if let Decl::Class(declaration) = &export.decl {
                        Some(declaration)
                    } else {
                        None
                    }
                }
                _ => None,
            };
            if let Some(declaration) = declaration {
                let id = declaration.ident.to_id();
                index
                    .class_bindings
                    .insert(declaration.class.span.lo.0, id.clone());
                index.class_ids.insert(id);
            }
        }
        for (ordinal, item) in body.iter().enumerate() {
            fresh_steps.push(fresh_value_step(item.as_module_item()));
            index.owner = StatementOrdinal(ordinal);
            index.past_await = false;
            index.may_skip_await_depth = 0;
            index.conditional_depth = 0;
            index.literal_depth = 0;
            index.factory_member_value_depth = 0;
            index.factory_callback_depth = 0;
            index.collect_uninitialized_lexical_bindings(item.as_module_item());
            index.collect_export_roots(item.as_module_item());
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item() {
                for specifier in &import.specifiers {
                    let local = specifier.local();
                    index.imported_bindings.insert(local.to_id());
                    if hints.callback_factories.contains(local.sym.as_ref()) {
                        index.callback_factory_imports.insert(local.to_id());
                    }
                    if hints.inline_callback_factories.contains(local.sym.as_ref()) {
                        index.inline_callback_factory_imports.insert(local.to_id());
                    }
                    if let Some(members) = hints.callback_factory_members.get(local.sym.as_ref()) {
                        index
                            .callback_factory_members
                            .insert(local.to_id(), members.clone());
                    }
                    if let Some(members) = hints.no_reentry_members.get(local.sym.as_ref()) {
                        index
                            .no_reentry_members
                            .insert(local.to_id(), members.clone());
                    }
                    if let Some(members) = hints.no_reentry_record_members.get(local.sym.as_ref()) {
                        index
                            .no_reentry_record_members
                            .insert(local.to_id(), members.clone());
                    }
                }
            }
            item.as_module_item().visit_with(&mut index);
        }
        for (offset, proof) in index.array_iterations.values_mut().enumerate() {
            proof.result = u32::try_from(offset)
                .ok()
                .and_then(|offset| max_source_position.checked_add(offset)?.checked_add(1));
        }
        // An accessor fallback may run any callable owned by this item,
        // including a nested function installed as the setter. Union each
        // retained synchronous prefix instead of using whole lazy bodies.
        for callable in index.callables.values() {
            let reads = index.owner_callable_prefix_reads[callable.owner.0]
                .get_or_insert_with(BTreeSet::new);
            if !callable.created_after_parent_await {
                reads.extend(callable.reads.iter().cloned());
            }
        }
        let namespace_assignments = index
            .top_assignments
            .iter()
            .flatten()
            .chain(
                index
                    .callables
                    .values()
                    .flat_map(|callable| callable.local_assignments.iter()),
            )
            .collect::<Vec<_>>();
        let namespace_value_facts = NamespaceValueFacts::from_assignments(
            index.class_values.clone(),
            fresh_steps
                .iter()
                .filter_map(|step| match step {
                    Some(FreshValueStep::Literal { span, .. }) => Some(*span),
                    _ => None,
                })
                .collect(),
            &namespace_assignments,
        );
        // Track aliases to the built-in objects used by narrow construction
        // proofs. A value may retain multiple roots (for example, a
        // conditional alias of Object and Array), so merge sets rather than
        // imposing an arbitrary ordering on them.
        let mut intrinsic_aliases = BTreeMap::<Id, IntrinsicRoots>::new();
        loop {
            let mut changed = false;
            for (id, value) in &namespace_assignments {
                let found = intrinsic_roots(value, &intrinsic_aliases, &index.global_object_names);
                if !found.is_empty() {
                    let roots = intrinsic_aliases.entry((*id).clone()).or_default();
                    let before = roots.len();
                    roots.extend(found);
                    changed |= roots.len() != before;
                }
            }
            if !changed {
                break;
            }
        }
        let mut mutated_intrinsics = IntrinsicRoots::new();
        for (id, _) in &namespace_assignments {
            match id.0.as_ref() {
                "Error" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Error);
                }
                "Object" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Object);
                }
                "Array" => {
                    mutated_intrinsics.insert(IntrinsicRoot::Array);
                }
                _ => {}
            }
        }
        for receiver in &index.intrinsic_mutation_receivers {
            mutated_intrinsics.extend(intrinsic_roots(
                receiver,
                &intrinsic_aliases,
                &index.global_object_names,
            ));
        }
        index.mutated_intrinsic_roots = mutated_intrinsics.clone();
        let mut claim_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
        if !index.fresh_error_claims.is_empty()
            && !index.callback_factory_members.is_empty()
            && flow.is_some()
        {
            // A11 supplies the standard Error allocation/prototype behavior. Reject
            // visible replacement of that intrinsic, including writes through local
            // aliases of a known global object. This is not a general effect proof.
            let intrinsic_mutated = mutated_intrinsics.contains(&IntrinsicRoot::Error)
                || mutated_intrinsics.contains(&IntrinsicRoot::GlobalObject);
            if !intrinsic_mutated
                && index.dynamic_scope_owners.is_empty()
                && let Some(flow) = flow
            {
                for claim in &index.fresh_error_claims {
                    if !flow.is_declared(&claim.callee)
                        && claim
                            .local_read
                            .as_ref()
                            .is_none_or(|id| flow.is_declared(id))
                    {
                        *claim_counts.entry(claim.site.clone()).or_default() += 1;
                    }
                }
            }
            let mut write_counts = BTreeMap::<FreshErrorWriteSite, usize>::new();
            let mut count_writes = |owner, callable, writes: &[CallbackResultWrite]| {
                for write in writes {
                    if let (ValueExpr::Binding(receiver), Some(property)) =
                        (&write.receiver, &write.property)
                    {
                        *write_counts
                            .entry(FreshErrorWriteSite {
                                owner,
                                callable,
                                receiver: receiver.clone(),
                                property: property.clone(),
                            })
                            .or_default() += 1;
                    }
                }
            };
            for (owner, writes) in index.top_callback_result_writes.iter().enumerate() {
                count_writes(StatementOrdinal(owner), None, writes);
            }
            for (key, callable) in &index.callables {
                count_writes(callable.owner, Some(*key), &callable.callback_result_writes);
            }
            claim_counts.retain(|site, count| write_counts.get(site) == Some(count));
        }
        let certified = |owner, callable, receiver: &Id, property: &str| {
            claim_counts.contains_key(&FreshErrorWriteSite {
                owner,
                callable,
                receiver: receiver.clone(),
                property: property.to_owned(),
            })
        };
        let certified_callback_write = |owner, callable, write: &CallbackResultWrite| matches!((&write.receiver, &write.property), (ValueExpr::Binding(receiver), Some(property)) if certified(owner, callable, receiver, property));
        // A namespace member is an audited target only while its binding is
        // unchanged. Any local write could replace a member with an eager
        // callback invoker before the apparent factory call.
        // Total source privacy proves that this family cannot reach any unrelated
        // Unknown/This slot. It grants no execution or callback timing effects.
        let private_namespace_family = !index.namespace_privacy.members.is_empty()
            && !index.namespace_privacy.invalid
            && index.dynamic_scope_owners.is_empty();
        let keep_namespace = |id: &Id, members: &BTreeSet<String>, writes: &BTreeSet<_>| {
            if private_namespace_family {
                return true;
            }
            let aliases =
                infer_namespace_aliases(id, &namespace_assignments, &namespace_value_facts);
            let receiver_aliases = infer_mutation_receiver_aliases(
                id,
                &namespace_assignments,
                &aliases,
                &namespace_value_facts,
            );
            let is_namespace_or_alias = |binding: &Id| binding == id || aliases.contains(binding);
            let is_mutation_receiver = |receiver: &ValueExpr| {
                receiver.may_be_namespace_mutation_receiver(
                    id,
                    &aliases,
                    &receiver_aliases,
                    &namespace_value_facts,
                )
            };
            let top_member_write = index
                .top_property_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&StatementOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (StatementOrdinal(owner), write))
                })
                .any(|(owner, (receiver, property))| {
                    receiver.as_ref().is_some_and(|receiver| {
                        !property
                            .as_ref()
                            .is_some_and(|property| certified(owner, None, receiver, property))
                            && ((receiver == id
                                && property
                                    .as_ref()
                                    .is_none_or(|property| members.contains(property)))
                                || aliases.contains(receiver)
                                || receiver_aliases.contains(receiver))
                    })
                });
            let destructuring_write = index.destructuring_member_writes.iter().any(|write| {
                is_mutation_receiver(&write.receiver)
                    || write
                        .source
                        .may_publish_namespace(id, &aliases, &namespace_value_facts)
            });
            let top_dynamic_write = index
                .top_dynamic_property_receivers
                .iter()
                .flatten()
                .any(is_mutation_receiver);
            let top_callback_result_write = index
                .top_callback_result_writes
                .iter()
                .enumerate()
                .filter(|(owner, _)| !writes.contains(&StatementOrdinal(*owner)))
                .flat_map(|(owner, writes)| {
                    writes
                        .iter()
                        .map(move |write| (StatementOrdinal(owner), write))
                })
                .any(|(owner, write)| {
                    (is_mutation_receiver(&write.receiver)
                        && !certified_callback_write(owner, None, write))
                        || write
                            .value
                            .may_publish_namespace(id, &aliases, &namespace_value_facts)
                });
            let callable_write = index.callables.iter().any(|(key, callable)| {
                callable.member_writes.iter().any(|(receiver, property)| {
                    !certified(callable.owner, Some(*key), receiver, property)
                        && ((receiver == id && members.contains(property))
                            || aliases.contains(receiver)
                            || receiver_aliases.contains(receiver))
                }) || callable
                    .dynamic_property_receivers
                    .iter()
                    .any(is_mutation_receiver)
                    || callable.literal_exposures.iter().any(is_namespace_or_alias)
                    || callable.escaped_bindings.iter().any(is_namespace_or_alias)
                    || callable.namespace_escape_values.iter().any(|value| {
                        value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                    })
                    || callable.member_value_writes.iter().any(|(_, _, value)| {
                        value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                    })
                    || callable.callback_result_writes.iter().any(|write| {
                        (is_mutation_receiver(&write.receiver)
                            && !certified_callback_write(callable.owner, Some(*key), write))
                            || write.value.may_publish_namespace(
                                id,
                                &aliases,
                                &namespace_value_facts,
                            )
                    })
                    || callable.returns.iter().any(|value| {
                        value.may_publish_namespace_in_factory_result(
                            id,
                            &aliases,
                            &namespace_value_facts,
                        )
                    })
                    || callable.calls.iter().any(|call| {
                        call.args.iter().any(|value| {
                            value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                        })
                    })
            });
            let top_member_value =
                index
                    .top_member_value_writes
                    .iter()
                    .flatten()
                    .any(|(_, _, value)| {
                        value.may_publish_namespace(id, &aliases, &namespace_value_facts)
                    });
            let top_call_argument = index.top_calls.iter().flatten().any(|call| {
                call.args
                    .iter()
                    .any(|value| value.may_publish_namespace(id, &aliases, &namespace_value_facts))
            });
            let exported_value = index
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| value.may_publish_namespace(id, &aliases, &namespace_value_facts))
                || index.exported_bindings.iter().any(is_namespace_or_alias);
            !(top_member_write
                || destructuring_write
                || top_dynamic_write
                || top_callback_result_write
                || callable_write
                || top_member_value
                || top_call_argument
                || exported_value)
        };
        let namespace_family: Vec<_> = index.namespace_privacy.members.keys().cloned().collect();
        let family_audited_members: BTreeSet<_> = if namespace_family.is_empty() {
            BTreeSet::new()
        } else {
            index
                .callback_factory_members
                .values()
                .chain(index.no_reentry_members.values())
                .chain(index.no_reentry_record_members.values())
                .flat_map(|members| members.iter().cloned())
                .collect()
        };
        let factory_imports = index
            .callback_factory_imports
            .union(&index.inline_callback_factory_imports)
            .cloned()
            .collect();
        loop {
            let factory_members = index.callback_factory_members.clone();
            let no_reentry_members = index.no_reentry_members.clone();
            let no_reentry_record_members = index.no_reentry_record_members.clone();
            // The fresh result is private until these adjacent writes finish.
            // Only currently admitted factories can establish that provenance;
            // removing a producer must invalidate proofs depending on its result.
            let fresh_writes = if index.dynamic_scope_owners.is_empty() {
                fresh_value_writes(
                    &fresh_steps,
                    &namespace_value_facts,
                    &factory_imports,
                    &factory_members,
                )
            } else {
                BTreeSet::new()
            };
            let keep_audited_members = |id: &Id, members: &BTreeSet<String>| {
                if namespace_family.is_empty() {
                    keep_namespace(id, members, &fresh_writes)
                } else {
                    // Import names do not establish distinct object identities.
                    // Fallback must also certify every potentially aliasing root.
                    namespace_family
                        .iter()
                        .all(|root| keep_namespace(root, &family_audited_members, &fresh_writes))
                }
            };
            index
                .callback_factory_members
                .retain(|id, members| keep_audited_members(id, members));
            index.no_reentry_members.retain(|id, members| {
                index
                    .namespace_privacy
                    .members
                    .get(id)
                    .is_some_and(|independent| members.is_subset(independent))
                    && keep_audited_members(id, members)
            });
            index.no_reentry_record_members.retain(|id, members| {
                index
                    .namespace_privacy
                    .members
                    .get(id)
                    .is_some_and(|independent| members.is_subset(independent))
                    && keep_audited_members(id, members)
            });
            if index.callback_factory_members.len() == factory_members.len()
                && index.no_reentry_members.len() == no_reentry_members.len()
                && index.no_reentry_record_members.len() == no_reentry_record_members.len()
            {
                break;
            }
        }
        index
    }

    fn collect_uninitialized_lexical_bindings(&mut self, item: &ModuleItem) {
        let Some(declaration) = var_decl_of_item(item) else {
            return;
        };
        if declaration.kind == VarDeclKind::Var {
            for declarator in &declaration.decls {
                if declarator.init.is_some()
                    && let Pat::Ident(binding) = &declarator.name
                {
                    self.top_definite_initializers[self.owner.0].insert(binding.id.to_id());
                }
            }
            return;
        }
        for declarator in &declaration.decls {
            if let Pat::Ident(binding) = &declarator.name {
                let id = binding.id.to_id();
                self.top_definite_initializers[self.owner.0].insert(id.clone());
                if declarator.init.is_none() {
                    self.top_assignments[self.owner.0].push((id, ValueExpr::Undefined));
                }
            }
        }
    }

    fn collect_export_roots(&mut self, item: &ModuleItem) {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => {
                self.hoisted_function_owners.insert(self.owner);
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                self.exported_bindings.extend(collect_declared_names(item));
                if matches!(export.decl, Decl::Fn(_)) {
                    self.hoisted_function_owners.insert(self.owner);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                if let DefaultDecl::Fn(function) = &export.decl {
                    self.hoisted_function_owners.insert(self.owner);
                    self.exported_hoisted_functions
                        .insert(function.function.span.lo.0);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(export)) => {
                self.exported_declaration_owners.insert(self.owner);
                self.top_exported_values[self.owner.0].push(ValueExpr::from_expr(&export.expr));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) => {
                self.linkage_owners.insert(self.owner);
                if export.src.is_none() {
                    for specifier in &export.specifiers {
                        if let ExportSpecifier::Named(named) = specifier
                            && let ModuleExportName::Ident(local) = &named.orig
                        {
                            self.exported_bindings.insert(local.to_id());
                        }
                    }
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => {
                self.linkage_owners.insert(self.owner);
            }
            _ => {}
        }
    }

    fn with_callable(&mut self, key: u32, params: Vec<Parameter>, visit: impl FnOnce(&mut Self)) {
        let created_after_parent_await = self
            .current
            .and_then(|parent| self.callables.get(&parent))
            .is_some_and(|parent| parent.created_after_parent_await)
            || (self.past_await && !self.hoisted_nested_functions.contains(&key));
        let saved_current = self.current.replace(key);
        let saved_await = std::mem::replace(&mut self.past_await, false);
        let saved_may_skip = std::mem::replace(&mut self.may_skip_await_depth, 0);
        let saved_conditional = std::mem::replace(&mut self.conditional_depth, 0);
        let opaque_params = params.iter().any(|param| param.binding.is_none());
        self.callables.entry(key).or_insert_with(|| Callable {
            owner: self.owner,
            created_after_parent_await,
            params,
            opaque: opaque_params,
            ..Callable::default()
        });
        visit(self);
        self.current = saved_current;
        self.past_await = saved_await;
        self.may_skip_await_depth = saved_may_skip;
        self.conditional_depth = saved_conditional;
    }

    fn record_call(&mut self, call: Call) {
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.dynamic_scope_owners.insert(self.owner);
        }
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().calls.push(call);
        } else {
            self.top_calls[self.owner.0].push(call);
        }
    }

    fn record_source_protocol(&mut self, kind: CallKind, source: ValueExpr, span: Span) {
        self.record_call(Call {
            kind,
            callee: ValueExpr::Unknown,
            args: vec![source],
            span,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            has_spread: false,
        });
    }

    fn record_implicit_coercion(&mut self, expression: &Expr, span: Span) {
        if (self.current.is_some() && self.past_await) || has_definite_primitive_result(expression)
        {
            return;
        }
        self.record_implicit_coercion_value(ValueExpr::from_expr(expression), span);
    }

    fn record_implicit_coercion_value(&mut self, argument: ValueExpr, span: Span) {
        if self.current.is_some() && self.past_await {
            return;
        }
        self.record_call(Call {
            kind: CallKind::ImplicitCoercion,
            callee: ValueExpr::Unknown,
            args: vec![argument],
            span,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            has_spread: false,
        });
    }

    fn mark_opaque(&mut self) {
        if self.current.is_some() && self.past_await {
            return;
        }
        if let Some(key) = self.current {
            self.callables.get_mut(&key).unwrap().opaque = true;
        } else {
            self.opaque_top.insert(self.owner);
        }
    }

    fn mark_dynamic_property_effect(&mut self, receiver: Option<&Expr>) {
        if self.past_await {
            return;
        }
        if let Some(key) = self.current {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.dynamic_property_effect = true;
            callable
                .dynamic_property_receivers
                .push(receiver.map_or(ValueExpr::Unknown, ValueExpr::from_expr));
        } else {
            self.top_property_writes[self.owner.0].push((None, None));
            if let Some(receiver) = receiver {
                self.top_dynamic_property_receivers[self.owner.0]
                    .push(ValueExpr::from_expr(receiver));
            }
        }
    }

    fn mark_possible_namespace_escape(&mut self, expression: &Expr) {
        let Some(key) = self.current else {
            return;
        };
        let value = ValueExpr::from_expr(expression);
        let exposed = self
            .callback_factory_members
            .keys()
            .filter(|id| value.carries_namespace(id, &BTreeSet::new()))
            .cloned()
            .collect::<Vec<_>>();
        let callable = self.callables.get_mut(&key).unwrap();
        callable.escaped_bindings.extend(exposed);
        callable.namespace_escape_values.push(value);
    }

    fn is_factory_member_data_value(&self, key: &PropName, expression: &Expr) -> bool {
        if object_data_property_name(key).is_none() {
            return false;
        }
        let Expr::Member(member) = expression.unwrap_parens() else {
            return false;
        };
        matches!(member.obj.unwrap_parens(), Expr::Ident(namespace)
            if self.callback_factory_members.contains_key(&namespace.to_id()))
            && matches!(member.prop, MemberProp::Ident(_))
    }

    fn is_callback_factory_member_callee(&self, expression: &Expr) -> bool {
        let Expr::Member(member) = expression.unwrap_parens() else {
            return false;
        };
        let Expr::Ident(namespace) = member.obj.unwrap_parens() else {
            return false;
        };
        self.callback_factory_members
            .contains_key(&namespace.to_id())
            && matches!(member.prop, MemberProp::Ident(_))
    }

    fn inline_factory_callback_argument(&self, call: &CallExpr) -> Option<usize> {
        if call.args.len() != 1 || call.args[0].spread.is_some() {
            return None;
        }
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let is_factory = match callee.unwrap_parens() {
            Expr::Ident(callee) => self.callback_factory_imports.contains(&callee.to_id()),
            Expr::Member(member) => {
                let (Expr::Ident(namespace), MemberProp::Ident(property)) =
                    (member.obj.unwrap_parens(), &member.prop)
                else {
                    return None;
                };
                self.callback_factory_members
                    .get(&namespace.to_id())
                    .is_some_and(|members| members.contains(property.sym.as_ref()))
            }
            _ => false,
        };
        (is_factory
            && matches!(
                call.args[0].expr.unwrap_parens(),
                Expr::Fn(_) | Expr::Arrow(_)
            ))
        .then_some(0)
    }

    fn record_member_write(&mut self, member: &MemberExpr) {
        if self.past_await {
            return;
        }
        let receiver = match member.obj.unwrap_parens() {
            Expr::Ident(object) => Some(object.to_id()),
            _ => None,
        };
        let property = static_prop(&member.prop);
        match (self.current, receiver, property) {
            (Some(key), Some(receiver), Some(property)) => self
                .callables
                .get_mut(&key)
                .unwrap()
                .member_writes
                .push((receiver, property)),
            (None, receiver, property) => {
                self.top_property_writes[self.owner.0].push((receiver, property))
            }
            _ => self.mark_dynamic_property_effect(Some(&member.obj)),
        }
    }

    fn visit_may_skip<N: VisitWith<Self>>(&mut self, node: &N) {
        let before = self.past_await;
        self.may_skip_await_depth += 1;
        self.conditional_depth += 1;
        node.visit_children_with(self);
        self.conditional_depth -= 1;
        self.may_skip_await_depth -= 1;
        self.past_await = before;
    }
}

impl Visit for ExecutionIndex {
    fn visit_named_export(&mut self, export: &NamedExport) {
        for specifier in &export.specifiers {
            if let ExportSpecifier::Named(named) = specifier
                && let ModuleExportName::Ident(ident) = &named.orig
                && self.namespace_privacy.members.contains_key(&ident.to_id())
            {
                self.namespace_privacy.invalid = true;
            }
        }
        // Linkage is captured by collect_export_roots. Specifiers do not run
        // at module initialization or expose a class in a closed module.
    }

    fn visit_class(&mut self, class: &Class) {
        self.class_owners.insert(class.span.lo.0, self.owner);
        self.class_values
            .insert(class.span.lo.0, collect_class_value_facts(class));
        if let Some(constructor) = local_constructor(class) {
            self.local_constructors.insert(class.span.lo.0, constructor);
        }
        class.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, statement: &WithStmt) {
        self.dynamic_scope_owners.insert(self.owner);
        statement.visit_children_with(self);
        self.mark_opaque();
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        // Defining a class evaluates computed keys and static initializers,
        // but does not run its constructor or instance field initializers.
        // The ordinary SWC descent would put calls in those deferred bodies
        // into `top_calls`, falsely promoting their callees at class init.
        match member {
            ClassMember::ClassProp(prop) => prop.decorators.visit_with(self),
            ClassMember::PrivateProp(prop) => prop.decorators.visit_with(self),
            ClassMember::AutoAccessor(accessor) => accessor.decorators.visit_with(self),
            _ => {}
        }
        visit_eager_member_parts(self, member);
        match member {
            ClassMember::Method(method) => {
                if method.kind == MethodKind::Setter {
                    self.setter_owners
                        .entry(static_property_name(&method.key))
                        .or_default()
                        .insert(self.owner);
                }
                method.function.visit_with(self);
            }
            ClassMember::PrivateMethod(method) => {
                method.function.visit_with(self);
            }
            ClassMember::Constructor(ctor) => {
                // Keep inspecting the deferred body for nested accessors;
                // property-write promotion still needs their owner. Its
                // calls must stay out of the class owner's top-call list.
                let params = ctor
                    .params
                    .iter()
                    .map(|param| match param {
                        ParamOrTsParamProp::Param(param) => Parameter::from_pattern(&param.pat),
                        ParamOrTsParamProp::TsParamProp(_) => Parameter {
                            binding: None,
                            default: None,
                        },
                    })
                    .collect();
                self.with_callable(ctor.span.lo.0, params, |index| {
                    ctor.visit_children_with(index);
                });
            }
            ClassMember::ClassProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::PrivateProp(prop) if !prop.is_static => {
                if let Some(value) = &prop.value {
                    self.with_callable(prop.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            ClassMember::AutoAccessor(accessor) if !accessor.is_static => {
                if let Some(value) = &accessor.value {
                    self.with_callable(accessor.span.lo.0, Vec::new(), |index| {
                        value.visit_with(index);
                    });
                }
            }
            _ => {}
        }
    }

    fn visit_setter_prop(&mut self, setter: &SetterProp) {
        // Object-literal setters are not represented by a `Function` node in
        // SWC, so their bodies lack a per-Callable prefix summary.
        self.unindexed_accessor_owners.insert(self.owner);
        self.setter_owners
            .entry(static_property_name(&setter.key))
            .or_default()
            .insert(self.owner);
        setter.visit_children_with(self);
    }

    fn visit_getter_prop(&mut self, getter: &GetterProp) {
        self.unindexed_accessor_owners.insert(self.owner);
        getter.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, object: &ObjectLit) {
        if matches!(object.props.as_slice(), [PropOrSpread::Spread(_)]) {
            self.exact_object_spread_copies.insert(object.span.lo.0);
        }
        let plain_data = object.props.iter().all(|property| match property {
            PropOrSpread::Spread(_) => false,
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair) => {
                    static_property_name(&pair.key).is_some()
                        && static_property_name(&pair.key).as_deref() != Some("__proto__")
                }
                Prop::Shorthand(id) => id.sym.as_ref() != "__proto__",
                // Ordinary method syntax installs an own data property. Its
                // body is only stored here, so parameter property reads by a
                // local constructor cannot invoke it. Keep computed keys and
                // the special prototype name outside this narrow proof.
                Prop::Method(method) => {
                    !matches!(method.key, PropName::Computed(_))
                        && static_property_name(&method.key).is_some_and(|name| name != "__proto__")
                }
                _ => false,
            },
        });
        if plain_data {
            self.plain_data_objects.insert(object.span.lo.0);
        }
        let mut primitive_property_counts = BTreeMap::<String, usize>::new();
        let mut primitive_property_values = BTreeSet::<String>::new();
        for property in &object.props {
            let PropOrSpread::Prop(property) = property else {
                continue;
            };
            let (name, primitive_value) = match property.as_ref() {
                Prop::KeyValue(pair) => (
                    static_property_name(&pair.key),
                    primitive_literal(&pair.value),
                ),
                Prop::Shorthand(ident) => (Some(ident.sym.to_string()), false),
                Prop::Assign(assign) => (Some(assign.key.sym.to_string()), false),
                Prop::Method(method) => (static_property_name(&method.key), false),
                Prop::Getter(getter) => (static_property_name(&getter.key), false),
                Prop::Setter(setter) => (static_property_name(&setter.key), false),
            };
            if let Some(name) = name {
                *primitive_property_counts.entry(name.clone()).or_default() += 1;
                if primitive_value {
                    primitive_property_values.insert(name);
                }
            }
        }
        if plain_data {
            for (name, count) in primitive_property_counts {
                if count == 1 && primitive_property_values.contains(&name) {
                    self.primitive_data_properties
                        .insert((object.span.lo.0, name));
                }
            }
        }
        self.literal_depth += 1;
        for property in &object.props {
            match property {
                PropOrSpread::Spread(spread) => {
                    spread.expr.visit_with(self);
                    self.record_call(Call {
                        kind: CallKind::ObjectSpread,
                        callee: ValueExpr::Unknown,
                        args: vec![ValueExpr::from_expr(&spread.expr)],
                        span: spread.expr.span(),
                        caller: self.current,
                        owner: self.owner,
                        plain_object_sources: false,
                        no_sync_arguments: false,
                        no_reentry_candidate: false,
                        no_reentry_record_candidate: false,
                        has_spread: false,
                    });
                }
                PropOrSpread::Prop(property)
                    if let Prop::KeyValue(pair) = property.as_ref()
                        && self.is_factory_member_data_value(&pair.key, &pair.value) =>
                {
                    pair.key.visit_with(self);
                    // This one-hop member read is a deferred data value, not
                    // the namespace object itself. The factory contract's
                    // author must establish that it cannot alias the namespace
                    // or mutate a listed factory member; syntax alone cannot
                    // prove the imported member's runtime identity.
                    self.factory_member_value_depth += 1;
                    pair.value.visit_with(self);
                    self.factory_member_value_depth -= 1;
                }
                _ => property.visit_with(self),
            }
        }
        self.literal_depth -= 1;
    }

    fn visit_array_lit(&mut self, array: &ArrayLit) {
        if array.elems.iter().all(|element| {
            element
                .as_ref()
                .is_some_and(|element| element.spread.is_none())
        }) {
            self.dense_array_literals
                .insert(array.span.lo.0, array.elems.len());
        }
        self.array_elements.insert(
            array.span.lo.0,
            array
                .elems
                .iter()
                .map(|element| {
                    element.as_ref().map(|element| ArrayElement {
                        spread: element.spread.is_some(),
                        value: ValueExpr::from_expr(&element.expr),
                    })
                })
                .collect(),
        );
        self.literal_depth += 1;
        for element in array.elems.iter().flatten() {
            element.expr.visit_with(self);
            if let Some(span) = element.spread {
                self.record_call(Call {
                    kind: CallKind::ArraySpread,
                    callee: ValueExpr::Unknown,
                    args: vec![ValueExpr::from_expr(&element.expr)],
                    span,
                    caller: self.current,
                    owner: self.owner,
                    plain_object_sources: false,
                    no_sync_arguments: false,
                    no_reentry_candidate: false,
                    no_reentry_record_candidate: false,
                    has_spread: false,
                });
            }
        }
        self.literal_depth -= 1;
    }

    fn visit_fn_decl(&mut self, declaration: &FnDecl) {
        if let Some(key) = self.current {
            // Declarations are instantiated when the enclosing activation
            // begins, even if their source text follows an await.
            self.hoisted_nested_functions
                .insert(declaration.function.span.lo.0);
            let callable = self.callables.get_mut(&key).unwrap();
            callable.local_functions.insert(declaration.ident.to_id());
            callable.local_assignments.push((
                declaration.ident.to_id(),
                ValueExpr::Origin(ValueOrigin::Function(declaration.function.span.lo.0)),
            ));
        } else {
            self.hoisted_bindings
                .insert(declaration.ident.to_id(), declaration.function.span.lo.0);
        }
        declaration.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, declaration: &ClassDecl) {
        declaration.class.visit_with(self);
        if self.current.is_none() {
            self.top_definite_initializers[self.owner.0].insert(declaration.ident.to_id());
            self.top_assignments[self.owner.0].push((
                declaration.ident.to_id(),
                ValueExpr::Origin(ValueOrigin::Class(declaration.class.span.lo.0)),
            ));
        } else if let Some(key) = self.current {
            self.callables
                .get_mut(&key)
                .unwrap()
                .local_assignments
                .push((
                    declaration.ident.to_id(),
                    ValueExpr::Origin(ValueOrigin::Class(declaration.class.span.lo.0)),
                ));
        }
    }

    fn visit_if_stmt(&mut self, statement: &IfStmt) {
        statement.test.visit_with(self);
        let after_test = self.past_await;
        self.conditional_depth += 1;
        statement.cons.visit_with(self);
        self.conditional_depth -= 1;
        let after_then = self.past_await;
        self.past_await = after_test;
        if let Some(else_branch) = &statement.alt {
            self.conditional_depth += 1;
            else_branch.visit_with(self);
            self.conditional_depth -= 1;
            self.past_await &= after_then;
        } else {
            self.past_await = after_test;
        }
    }

    fn visit_cond_expr(&mut self, expression: &CondExpr) {
        expression.test.visit_with(self);
        let after_test = self.past_await;
        self.conditional_depth += 1;
        expression.cons.visit_with(self);
        self.conditional_depth -= 1;
        let after_then = self.past_await;
        self.past_await = after_test;
        self.conditional_depth += 1;
        expression.alt.visit_with(self);
        self.conditional_depth -= 1;
        self.past_await &= after_then;
    }

    fn visit_bin_expr(&mut self, expression: &BinExpr) {
        expression.left.visit_with(self);
        let after_left = self.past_await;
        if matches!(
            expression.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
            self.conditional_depth += 1;
        }
        let previous_target = self.safe_class_target.take();
        self.safe_class_target = if expression.op == BinaryOp::InstanceOf {
            match expression.right.unwrap_parens() {
                Expr::Ident(id) => Some(id.to_id()),
                _ => None,
            }
        } else {
            None
        };
        expression.right.visit_with(self);
        self.safe_class_target = previous_target;
        if matches!(
            expression.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
            self.conditional_depth -= 1;
            self.past_await = after_left;
        }
        match expression.op {
            // Strict comparisons, short-circuit operators and `instanceof`
            // do not apply ToPrimitive to their operands. `in` converts only
            // the left operand to a property key. The remaining eager binary
            // operators can convert either object operand.
            BinaryOp::EqEqEq
            | BinaryOp::NotEqEq
            | BinaryOp::InstanceOf
            | BinaryOp::LogicalAnd
            | BinaryOp::LogicalOr
            | BinaryOp::NullishCoalescing => {}
            BinaryOp::In => self.record_implicit_coercion(&expression.left, expression.span),
            _ => {
                self.record_implicit_coercion(&expression.left, expression.span);
                self.record_implicit_coercion(&expression.right, expression.span);
            }
        }
        if matches!(expression.op, BinaryOp::In | BinaryOp::InstanceOf) {
            self.record_call(Call {
                kind: if expression.op == BinaryOp::In {
                    CallKind::InProtocol
                } else {
                    CallKind::InstanceOfProtocol
                },
                callee: ValueExpr::Unknown,
                args: vec![
                    ValueExpr::from_expr(&expression.right),
                    ValueExpr::from_expr(&expression.left),
                ],
                span: expression.span,
                caller: self.current,
                owner: self.owner,
                plain_object_sources: false,
                no_sync_arguments: false,
                no_reentry_candidate: false,
                no_reentry_record_candidate: false,
                has_spread: false,
            });
        }
    }

    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        self.visit_may_skip(node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.visit_may_skip(node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        self.visit_may_skip(node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        self.visit_may_skip(node);
    }

    fn visit_function(&mut self, function: &Function) {
        if function.is_async {
            self.async_callables.insert(function.span.lo.0);
        }
        if let [param] = function.params.as_slice()
            && let Pat::Ident(binding) = &param.pat
            && param.decorators.is_empty()
            && !function.is_async
            && !function.is_generator
            && function.decorators.is_empty()
            && function
                .body
                .as_ref()
                .and_then(sole_return_expression)
                .is_some_and(|expr| is_callback_forwarder(expr, &binding.id.to_id()))
        {
            self.callback_factory_functions.insert(function.span.lo.0);
        }
        // Decorator expressions run when the method is defined. Defaults in
        // parameter patterns and the body run only when it is called.
        function.decorators.visit_with(self);
        for param in &function.params {
            param.decorators.visit_with(self);
        }
        let params = function
            .params
            .iter()
            .map(|param| Parameter::from_pattern(&param.pat))
            .collect();
        self.with_callable(function.span.lo.0, params, |index| {
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .may_fall_through = !function
                .body
                .as_ref()
                .is_some_and(|body| matches!(body.stmts.last(), Some(Stmt::Return(_))));
            index
                .callables
                .get_mut(&function.span.lo.0)
                .unwrap()
                .generator = function.is_generator;
            for param in &function.params {
                param.pat.visit_with(index);
            }
            if let Some(body) = &function.body {
                body.visit_with(index);
            }
        });
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        if arrow.is_async {
            self.async_callables.insert(arrow.span.lo.0);
        }
        let params = arrow.params.iter().map(Parameter::from_pattern).collect();
        self.with_callable(arrow.span.lo.0, params, |index| {
            index
                .callables
                .get_mut(&arrow.span.lo.0)
                .unwrap()
                .may_fall_through = match arrow.body.as_ref() {
                ArrowFunctionBody::Expr(_) => false,
                ArrowFunctionBody::FunctionBody(body) => {
                    !matches!(body.stmts.last(), Some(Stmt::Return(_)))
                }
            };
            arrow.visit_children_with(index);
            if let ArrowFunctionBody::Expr(expr) = arrow.body.as_ref() {
                index
                    .callables
                    .get_mut(&arrow.span.lo.0)
                    .unwrap()
                    .returns
                    .push(ValueExpr::from_expr(expr));
            }
        });
    }

    fn visit_ident(&mut self, ident: &Ident) {
        self.namespace_privacy.record_ident(ident);
        let id = ident.to_id();
        if self.class_ids.contains(&id) && self.safe_class_target.as_ref() != Some(&id) {
            self.other_class_uses.insert(id);
        }
        if !self.past_await
            && let Some(key) = self.current
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.reads.insert(ident.to_id());
            if self.literal_depth > 0 {
                callable.literal_captures.insert(ident.to_id());
                if self.factory_member_value_depth == 0 {
                    callable.literal_exposures.insert(ident.to_id());
                }
            }
        }
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        if !self.member_write_sites.contains(&member.span.into())
            && let Some(key) = self.current
        {
            self.callable_member_reads
                .entry(key)
                .or_default()
                .push((ValueExpr::from_expr(&member.obj), static_prop(&member.prop)));
        }
        let previous = self.namespace_privacy.allowed_read;
        self.namespace_privacy.allowed_read = self.namespace_privacy.read_receiver(member);
        member.obj.visit_with(self);
        self.namespace_privacy.allowed_read = previous;
        member.prop.visit_with(self);
        if !self.member_write_sites.contains(&member.span.into())
            && !self.direct_call_members.contains(&member.span.into())
        {
            self.record_call(Call {
                kind: CallKind::LocalMemberRead,
                callee: ValueExpr::Member(
                    Box::new(ValueExpr::from_expr(&member.obj)),
                    static_prop(&member.prop),
                    matches!(member.prop, MemberProp::Ident(_)),
                ),
                args: Vec::new(),
                span: member.span,
                caller: self.current,
                owner: self.owner,
                plain_object_sources: false,
                no_sync_arguments: false,
                no_reentry_candidate: false,
                no_reentry_record_candidate: false,
                has_spread: false,
            });
        }
    }

    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        self.namespace_privacy.record_ident(&binding.id);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        declarator.visit_children_with(self);
        if let Some(init) = &declarator.init {
            let kind = match &declarator.name {
                Pat::Object(_) | Pat::Array(_) if !shallow_data_pattern(&declarator.name) => {
                    Some(CallKind::DeepDestructuringProtocol)
                }
                Pat::Object(_) => Some(CallKind::ObjectSpread),
                Pat::Array(_) => Some(CallKind::ArraySpread),
                _ => None,
            };
            if let Some(kind) = kind {
                self.record_source_protocol(kind, ValueExpr::from_expr(init), declarator.span);
            }
            let mut assignments = Vec::new();
            collect_pattern_assignments(
                &declarator.name,
                &ValueExpr::from_expr(init),
                &mut assignments,
            );
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .local_assignments
                    .extend(assignments);
            } else {
                self.top_assignments[self.owner.0].extend(assignments);
            }
        }
    }

    fn visit_seq_expr(&mut self, sequence: &SeqExpr) {
        let mut fresh: Option<(Id, Id)> = None;
        for expression in &sequence.exprs {
            let mut continues = false;
            if let Expr::Assign(assignment) = expression.unwrap_parens()
                && assignment.op == AssignOp::Assign
            {
                if let AssignTarget::Simple(SimpleAssignTarget::Ident(receiver)) = &assignment.left
                    && let Expr::Call(call) = assignment.right.unwrap_parens()
                    && let Callee::Expr(callee) = &call.callee
                    && let Expr::Ident(callee) = callee.unwrap_parens()
                    && callee.sym == "Error"
                {
                    fresh = Some((receiver.id.to_id(), callee.to_id()));
                    continues = true;
                } else if let Some((receiver, callee)) = &fresh
                    && let AssignTarget::Simple(SimpleAssignTarget::Member(member)) =
                        &assignment.left
                    && matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.to_id() == *receiver)
                    && let Some(property) = static_prop(&member.prop)
                    && property != "__proto__"
                    && property != "stack"
                {
                    let local_read = match assignment.right.unwrap_parens() {
                        Expr::Ident(id) => Some(id.to_id()),
                        _ => None,
                    };
                    if local_read.is_some() || value_without_user_code(&assignment.right) {
                        if !self.past_await {
                            self.fresh_error_claims.push(FreshErrorWriteClaim {
                                site: FreshErrorWriteSite {
                                    owner: self.owner,
                                    callable: self.current,
                                    receiver: receiver.clone(),
                                    property,
                                },
                                callee: callee.clone(),
                                local_read,
                            });
                        }
                        continues = true;
                    }
                }
            }
            if !continues {
                fresh = None;
            }
            expression.visit_with(self);
        }
    }

    fn visit_pat(&mut self, pattern: &Pat) {
        if let Some(source) = &self.destructuring_source {
            let receiver = match pattern {
                Pat::Expr(expression) => Some(match expression.unwrap_parens() {
                    Expr::Member(member) => {
                        self.member_write_sites.insert(member.span.into());
                        self.intrinsic_mutation_receivers
                            .push(intrinsic_write_target(member));
                        ValueExpr::from_expr(&member.obj)
                    }
                    _ => ValueExpr::Unknown,
                }),
                Pat::Invalid(_) => Some(ValueExpr::Unknown),
                _ => None,
            };
            if let Some(receiver) = receiver {
                self.destructuring_member_writes
                    .push(DestructuringMemberWrite {
                        receiver,
                        source: source.clone(),
                    });
            }
        }
        pattern.visit_children_with(self);
    }

    fn visit_assign_pat(&mut self, assignment: &AssignPat) {
        let Some(source) = self.destructuring_source.take() else {
            assignment.visit_children_with(self);
            return;
        };
        self.destructuring_source = Some(ValueExpr::Alternatives(vec![
            source.clone(),
            ValueExpr::from_expr(&assignment.right),
        ]));
        self.namespace_privacy.target_depth += 1;
        assignment.left.visit_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = Some(source);
        assignment.right.visit_with(self);
    }

    fn visit_for_head(&mut self, head: &ForHead) {
        let previous_source = self.destructuring_source.take();
        if matches!(head, ForHead::Pat(_)) {
            self.destructuring_source = Some(ValueExpr::Unknown);
        }
        self.namespace_privacy.target_depth += 1;
        head.visit_children_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = previous_source;
    }

    fn visit_simple_assign_target(&mut self, target: &SimpleAssignTarget) {
        if let SimpleAssignTarget::Member(member) = target {
            self.member_write_sites.insert(member.span.into());
        }
        target.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
        if matches!(&assignment.left,
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding))
                if binding.id.sym == "Map")
        {
            self.reassigned_map = true;
        }
        if let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left {
            self.intrinsic_mutation_receivers
                .push(intrinsic_write_target(member));
        }
        let before_assignment = self.past_await;
        let previous_source = self.destructuring_source.take();
        if matches!(assignment.left, AssignTarget::Pat(_)) {
            self.destructuring_source = Some(ValueExpr::from_expr(&assignment.right));
        }
        self.namespace_privacy.target_depth += 1;
        assignment.left.visit_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.destructuring_source = previous_source;
        assignment.right.visit_with(self);
        if matches!(
            assignment.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            self.past_await = before_assignment;
        }
        if !matches!(
            assignment.op,
            AssignOp::Assign | AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            let left = match &assignment.left {
                AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                    ValueExpr::Binding(binding.id.to_id())
                }
                AssignTarget::Simple(SimpleAssignTarget::Member(member)) => ValueExpr::Member(
                    Box::new(ValueExpr::from_expr(&member.obj)),
                    static_prop(&member.prop),
                    matches!(member.prop, MemberProp::Ident(_)),
                ),
                _ => ValueExpr::Unknown,
            };
            self.record_implicit_coercion_value(left, assignment.span);
            self.record_implicit_coercion(&assignment.right, assignment.span);
        }
        if let (Some(key), AssignTarget::Simple(SimpleAssignTarget::Ident(binding))) =
            (self.current, &assignment.left)
        {
            let callable = self.callables.get_mut(&key).unwrap();
            callable.rebinds.insert(binding.id.to_id());
            callable
                .local_assignments
                .push((binding.id.to_id(), ValueExpr::from_expr(&assignment.right)));
        } else if let AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) = &assignment.left {
            self.top_assignments[self.owner.0]
                .push((binding.id.to_id(), ValueExpr::from_expr(&assignment.right)));
            if self.conditional_depth == 0 {
                self.top_definite_initializers[self.owner.0].insert(binding.id.to_id());
            }
        } else if let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assignment.left {
            let write = CallbackResultWrite {
                receiver: ValueExpr::from_expr(&member.obj),
                property_key: match &member.prop {
                    MemberProp::Computed(key) => ValueExpr::from_expr(&key.expr),
                    _ => ValueExpr::NonCallable,
                },
                simple_assignment: assignment.op == AssignOp::Assign,
                data_read_operands: data_read_operand(&assignment.right)
                    && match &member.prop {
                        MemberProp::Computed(key) => data_read_operand(&key.expr),
                        _ => true,
                    },
                property: static_prop(&member.prop),
                value: ValueExpr::from_expr(&assignment.right),
            };
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .callback_result_writes
                    .push(write);
            } else {
                self.top_callback_result_writes[self.owner.0].push(write);
            }
            if let Expr::Ident(receiver) = member.obj.unwrap_parens()
                && let Some(key) = static_prop(&member.prop)
            {
                let write = (
                    receiver.to_id(),
                    Some(key),
                    ValueExpr::from_expr(&assignment.right),
                );
                if let Some(callable) = self.current {
                    self.callables
                        .get_mut(&callable)
                        .unwrap()
                        .member_value_writes
                        .push(write);
                } else {
                    self.top_member_value_writes[self.owner.0].push(write);
                }
            } else if let Expr::Ident(receiver) = member.obj.unwrap_parens() {
                let write = (
                    receiver.to_id(),
                    None,
                    ValueExpr::from_expr(&assignment.right),
                );
                if let Some(callable) = self.current {
                    self.callables
                        .get_mut(&callable)
                        .unwrap()
                        .member_value_writes
                        .push(write);
                } else {
                    self.top_member_value_writes[self.owner.0].push(write);
                }
            }
            self.mark_possible_namespace_escape(&assignment.right);
            if assignment.op == AssignOp::Assign {
                self.record_member_write(member);
            } else {
                self.mark_dynamic_property_effect(Some(&member.obj));
            }
        } else if let AssignTarget::Pat(pattern) = &assignment.left {
            let kind = match pattern {
                AssignTargetPat::Array(_) | AssignTargetPat::Object(_) => {
                    Some(CallKind::DeepDestructuringProtocol)
                }
                _ => None,
            };
            if let Some(kind) = kind {
                self.record_source_protocol(
                    kind,
                    ValueExpr::from_expr(&assignment.right),
                    assignment.span,
                );
            }
            let mut assignments = Vec::new();
            collect_assign_target_assignments(
                pattern,
                &ValueExpr::from_expr(&assignment.right),
                &mut assignments,
            );
            if let Some(key) = self.current {
                let callable = self.callables.get_mut(&key).unwrap();
                for (binding, value) in assignments {
                    callable.rebinds.insert(binding.clone());
                    callable.local_assignments.push((binding, value));
                }
            } else {
                if self.conditional_depth == 0 {
                    self.top_definite_initializers[self.owner.0]
                        .extend(assignments.iter().map(|(binding, _)| binding.clone()));
                }
                self.top_assignments[self.owner.0].extend(assignments);
            }
            self.mark_dynamic_property_effect(None);
        } else {
            self.mark_dynamic_property_effect(None);
        }
    }

    fn visit_update_expr(&mut self, expression: &UpdateExpr) {
        if let Expr::Member(member) = expression.arg.unwrap_parens() {
            self.member_write_sites.insert(member.span.into());
        }

        if matches!(expression.arg.unwrap_parens(), Expr::Ident(id) if matches!(id.sym.as_ref(), "Error" | "Object" | "Array"))
        {
            self.intrinsic_mutation_receivers
                .push(ValueExpr::from_expr(&expression.arg));
        }

        if matches!(expression.arg.unwrap_parens(), Expr::Ident(ident) if ident.sym == "Map") {
            self.reassigned_map = true;
        }
        self.namespace_privacy.target_depth += 1;
        expression.visit_children_with(self);
        self.namespace_privacy.target_depth -= 1;
        self.record_implicit_coercion(&expression.arg, expression.span);
        if let Expr::Member(member) = expression.arg.unwrap_parens() {
            self.intrinsic_mutation_receivers
                .push(intrinsic_write_target(member));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
        self.mark_opaque();
    }

    fn visit_unary_expr(&mut self, expression: &UnaryExpr) {
        if expression.op == UnaryOp::Delete
            && let Expr::Member(member) = expression.arg.unwrap_parens()
        {
            self.member_write_sites.insert(member.span.into());
        }

        let target = usize::from(expression.op == UnaryOp::Delete);
        self.namespace_privacy.target_depth += target;
        expression.visit_children_with(self);
        self.namespace_privacy.target_depth -= target;
        if matches!(
            expression.op,
            UnaryOp::Plus | UnaryOp::Minus | UnaryOp::Tilde
        ) {
            self.record_implicit_coercion(&expression.arg, expression.span);
        }
        if expression.op == UnaryOp::Delete
            && let Expr::Member(member) = expression.arg.unwrap_parens()
        {
            self.intrinsic_mutation_receivers
                .push(intrinsic_write_target(member));
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
    }

    fn visit_return_stmt(&mut self, ret: &ReturnStmt) {
        ret.visit_children_with(self);
        if ret.arg.is_none()
            && let Some(key) = self.current
        {
            self.callables
                .get_mut(&key)
                .unwrap()
                .returns
                .push(ValueExpr::Undefined);
        }
        if let (Some(key), Some(arg)) = (self.current, &ret.arg) {
            if let ValueExpr::Binding(id) = ValueExpr::from_expr(arg) {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .escaped_bindings
                    .insert(id);
            }
            self.callables
                .get_mut(&key)
                .unwrap()
                .returns
                .push(ValueExpr::from_expr(arg));
        }
    }

    fn visit_throw_stmt(&mut self, thrown: &ThrowStmt) {
        thrown.visit_children_with(self);
        self.mark_possible_namespace_escape(&thrown.arg);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
        {
            self.direct_call_members.insert(member.span.into());
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && call.args.len() == 1
            && call.args[0].spread.is_none()
        {
            let method = match property.sym.as_ref() {
                "forEach" => Some(ArrayIterationMethod::ForEach),
                "filter" => Some(ArrayIterationMethod::Filter),
                "map" => Some(ArrayIterationMethod::Map),
                _ => None,
            };
            if let Some(method) = method {
                self.array_iterations.insert(
                    call.span.into(),
                    ArrayIteration {
                        method,
                        source: ValueExpr::from_expr(&member.obj),
                        callback: ValueExpr::from_expr(&call.args[0].expr),
                        result: None,
                    },
                );
            }
        }
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && matches!(member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object" || id.sym == "Reflect")
            && static_prop(&member.prop).as_deref().is_none_or(|method| {
                matches!(
                    method,
                    "set"
                        | "defineProperty"
                        | "defineProperties"
                        | "assign"
                        | "setPrototypeOf"
                        | "deleteProperty"
                )
            })
            && let Some(argument) = call.args.first()
        {
            let receiver = ValueExpr::from_expr(&argument.expr);
            let method = static_prop(&member.prop);
            let target = if matches!(
                method.as_deref(),
                Some("set" | "defineProperty" | "deleteProperty")
            ) {
                let property =
                    call.args
                        .get(1)
                        .and_then(|argument| match argument.expr.unwrap_parens() {
                            Expr::Lit(Lit::Str(value)) => {
                                Some(value.value.to_string_lossy().into_owned())
                            }
                            _ => None,
                        });
                ValueExpr::Member(Box::new(receiver), property, false)
            } else {
                receiver
            };
            self.intrinsic_mutation_receivers.push(target);
        }

        let callback_argument = self.inline_factory_callback_argument(call);
        let is_factory_member_callee = self.literal_depth > 0
            && self.factory_callback_depth > 0
            && matches!(&call.callee, Callee::Expr(callee)
                if self.is_callback_factory_member_callee(callee));
        if callback_argument.is_some() || is_factory_member_callee {
            match &call.callee {
                Callee::Expr(callee) if is_factory_member_callee => {
                    self.factory_member_value_depth += 1;
                    callee.visit_with(self);
                    self.factory_member_value_depth -= 1;
                }
                callee => callee.visit_with(self),
            }
            for (index, argument) in call.args.iter().enumerate() {
                if callback_argument == Some(index) {
                    self.factory_callback_depth += 1;
                    argument.visit_with(self);
                    self.factory_callback_depth -= 1;
                } else {
                    argument.visit_with(self);
                }
            }
            call.type_args.visit_with(self);
        } else {
            call.visit_children_with(self);
        }
        if call.args.iter().any(|arg| arg.spread.is_some()) {
            self.mark_opaque();
        }
        if let Some(proof) = from_entries_mapped_proof(call, &self.plain_array_bindings) {
            self.mapped_array_map_calls
                .insert(proof.map_call_site, proof.clone());
            self.from_entries_map_calls.insert(call.span.into(), proof);
        }
        if let Callee::Expr(callee) = &call.callee {
            self.record_call(Call {
                callee: ValueExpr::from_expr(callee),
                args: call
                    .args
                    .iter()
                    .map(|arg| ValueExpr::from_expr(&arg.expr))
                    .collect(),
                kind: CallKind::Call,
                span: call.span,
                has_spread: call.args.iter().any(|arg| arg.spread.is_some()),
                no_reentry_candidate: call
                    .args
                    .iter()
                    .all(|arg| arg.spread.is_none() && no_reentry_primitive_argument(&arg.expr)),
                no_reentry_record_candidate: call
                    .args
                    .iter()
                    .all(|arg| arg.spread.is_none() && no_reentry_inert_argument(&arg.expr))
                    && call
                        .args
                        .iter()
                        .any(|arg| matches!(arg.expr.unwrap_parens(), Expr::Object(_))),
                no_sync_arguments: is_no_sync_callback_member_call(
                    callee,
                    &self.no_sync_callback_members,
                ) || is_static_event_listener_registration(callee, &call.args),
                caller: self.current,
                owner: self.owner,
                plain_object_sources: call.args.len() > 1
                    && call.args.iter().skip(1).all(|arg| {
                        matches!(arg.expr.unwrap_parens(), Expr::Object(object)
                            if object.props.iter().all(plain_assign_source_property))
                    }),
            });
        } else {
            self.mark_opaque();
        }
    }

    fn visit_tpl(&mut self, template: &Tpl) {
        for expression in &template.exprs {
            expression.visit_with(self);
            self.record_implicit_coercion(expression, expression.span());
        }
    }

    fn visit_tagged_tpl(&mut self, template: &TaggedTpl) {
        template.tag.visit_with(self);
        for expression in &template.tpl.exprs {
            expression.visit_with(self);
        }
        template.type_params.visit_with(self);
        self.mark_opaque();
    }

    fn visit_opt_chain_expr(&mut self, chain: &OptChainExpr) {
        // Only the optional operation's own lookup is unsupported. Namespace
        // reads in its argument callbacks still receive their ordinary context.
        let member = match chain.base.as_ref() {
            OptChainBase::Member(member) => Some(member),
            OptChainBase::Call(call) => match call.callee.unwrap_parens() {
                Expr::Member(member) => Some(member),
                _ => None,
            },
        };
        if !self.namespace_privacy.members.is_empty()
            && let Some(member) = member
        {
            self.namespace_privacy
                .optional_members
                .insert(member.span.lo.0);
        }
        let before = self.past_await;
        chain.visit_children_with(self);
        self.past_await = before;
        self.mark_opaque();
    }

    fn visit_new_expr(&mut self, new_expr: &NewExpr) {
        if let Some(span) = map_entry_array_span(new_expr) {
            self.map_entry_arrays.insert(span);
        }
        let previous_target = self.safe_class_target.take();
        self.safe_class_target = match new_expr.callee.unwrap_parens() {
            Expr::Ident(id) => Some(id.to_id()),
            _ => None,
        };
        new_expr.callee.visit_with(self);
        self.safe_class_target = previous_target;
        new_expr.args.visit_with(self);
        if new_expr
            .args
            .as_deref()
            .unwrap_or_default()
            .iter()
            .any(|arg| arg.spread.is_some())
        {
            self.mark_opaque();
        }
        self.record_call(Call {
            callee: ValueExpr::from_expr(&new_expr.callee),
            args: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .map(|arg| ValueExpr::from_expr(&arg.expr))
                .collect(),
            kind: CallKind::New,
            span: new_expr.span,
            has_spread: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .any(|arg| arg.spread.is_some()),
            no_sync_arguments: false,
            no_reentry_candidate: false,
            no_reentry_record_candidate: false,
            caller: self.current,
            owner: self.owner,
            plain_object_sources: false,
        });
    }

    fn visit_await_expr(&mut self, await_expr: &AwaitExpr) {
        await_expr.arg.visit_with(self);
        if self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}

fn from_flow(values: FlowValues) -> Values {
    values
        .into_iter()
        .map(|value| match value {
            ValueOrigin::Unknown => AbstractValue::Unknown,
            other => AbstractValue::Origin(other),
        })
        .collect()
}

fn unknown() -> Values {
    BTreeSet::from([AbstractValue::Unknown])
}

#[derive(Debug, Clone, Default)]
struct Outcome {
    effects: SynchronousCalls,
    returns: Values,
    complete: bool,
    executed_owners: BTreeSet<StatementOrdinal>,
    unknown_call: bool,
}

#[derive(Debug, Clone, Default)]
struct ReachabilityState {
    unknown_call_seen: bool,
    incomplete: bool,
    revision: u64,
    expanded: BTreeSet<StatementOrdinal>,
    owners: BTreeSet<StatementOrdinal>,
}

/// Callback roots retained by a fallback stay escaped for the rest of the
/// chunk. Cache their synchronous read closure once; initialization timing is
/// checked against the current prefix when the summary is applied.
#[derive(Default)]
struct CallbackFallbackSummary {
    expanded: BTreeSet<StatementOrdinal>,
    incomplete_expanded: bool,
    reads: BTreeSet<Id>,
    rebinds: BTreeSet<Id>,
    read_additions: Vec<Id>,
    rebind_additions: Vec<Id>,
}

type InvocationSignature = (
    u32,
    Vec<Values>,
    Option<CapturedContext>,
    bool,
    u64,
    u64,
    Option<StatementOrdinal>,
);

type ActiveInvocation = (u32, Option<StatementOrdinal>);

// Callable cycles can keep changing their argument/capture snapshots and thus
// evade the state-specific memoization key. Bound recursive analysis separately
// from that cache so a finite source program cannot consume the Rust stack.
const MAX_SYNCHRONOUS_INVOCATION_DEPTH: usize = 64;
// Count actual expansions, including those whose changing prefix state defeats
// memoization. A cache-size limit alone does not bound a branching call tree.
const MAX_SYNCHRONOUS_INVOCATIONS: usize = 256;
// Giving every callback the entire pool as an argument creates a quadratic
// value cross product, even without recursion. Summarize large pools before
// manufacturing capture snapshots that retain that cross product.
const MAX_SYNCHRONOUS_CALLBACK_POOL: usize = 64;

struct Executor<'a> {
    index: &'a ExecutionIndex,
    facts: &'a [StatementFacts],
    flow: &'a ChunkValueFlow,
    set_iterable_proof: &'a SetIterableProof,
    shadowed: &'a BTreeSet<&'static str>,
    top_bindings: BTreeSet<Id>,
    binding_owner: BTreeMap<Id, StatementOrdinal>,
    owner_read_closure: FactsOwnerReadClosure,
    synchronous_owner_adjacency: Vec<Vec<StatementOrdinal>>,
    synchronous_owner_reads: Vec<Option<&'a BTreeSet<Id>>>,
    class_member_effects: BTreeMap<StatementOrdinal, (BTreeSet<Id>, BTreeSet<Id>)>,
    exported_owners: BTreeSet<StatementOrdinal>,
    prior_reachability_roots: Option<BTreeSet<StatementOrdinal>>,
    current_top_owner: Option<StatementOrdinal>,
    prior_effect_owners: BTreeSet<StatementOrdinal>,
    reachability: ReachabilityState,
    mutable_captures: BTreeSet<Id>,
    prefix_values: Context,
    initialized_bindings: BTreeSet<Id>,
    var_bindings: BTreeSet<Id>,
    changed_member_roots: BTreeSet<Id>,
    global_escaped_callbacks: Values,
    prefix_object_properties: BTreeMap<u32, BTreeMap<Option<String>, Values>>,
    dense_arrays: BTreeMap<u32, DenseArray>,
    ordinary_spread_copies: BTreeSet<u32>,
    array_iteration_depth: usize,
    array_iteration_protected_objects: BTreeSet<u32>,
    array_iteration_unsupported: bool,
    array_intrinsic_mutated: bool,
    tainted_objects: BTreeSet<u32>,
    tainted_functions: BTreeSet<u32>,
    tainted_classes: BTreeSet<u32>,
    escaped_class_capabilities: BTreeSet<u32>,
    exported_runtime_values: Values,
    export_reentry_enabled: bool,
    fresh_mutation_callables: BTreeSet<u32>,
    prefix_revision: u64,
    invocation_depth: usize,
    invocation_count: usize,
    callback_fallback: CallbackFallbackSummary,
    fallback_outcome: Outcome,
    fallback_callback_sizes: Option<(usize, usize)>,
    fallback_local_owners: BTreeSet<StatementOrdinal>,
    capture_contexts: BTreeMap<Arc<Context>, usize>,
    callback_sets: BTreeMap<Arc<Values>, usize>,
    active: BTreeSet<ActiveInvocation>,
    cache: BTreeMap<InvocationSignature, Outcome>,
}

impl Executor<'_> {
    fn ordinary_data_properties(
        &self,
        values: &Values,
    ) -> Option<BTreeMap<Option<String>, Values>> {
        let [AbstractValue::Origin(ValueOrigin::Object(object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if self.tainted_objects.contains(object)
            || (!self.index.plain_data_objects.contains(object)
                && !self.ordinary_spread_copies.contains(object))
        {
            return None;
        }
        let properties = self.prefix_object_properties.get(object)?;
        if properties.contains_key(&None) {
            return None;
        }
        Some(properties.clone())
    }

    fn array_intrinsics_intact(&self) -> bool {
        let array = ("Array".into(), SyntaxContext::empty());
        !self.array_intrinsic_mutated
            && !self.flow.is_declared(&array)
            && !self.shadowed.contains("Array")
            && self.index.dynamic_scope_owners.is_empty()
            && !self.index.mutated_intrinsic_roots.iter().any(|root| {
                matches!(
                    root,
                    IntrinsicRoot::GlobalObject | IntrinsicRoot::Array | IntrinsicRoot::Object
                )
            })
    }

    fn dense_source(&self, values: &Values) -> Option<(u32, DenseArray)> {
        let [AbstractValue::Origin(ValueOrigin::Object(object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self.array_intrinsics_intact() || self.tainted_objects.contains(object) {
            return None;
        }
        Some((*object, self.dense_arrays.get(object)?.clone()))
    }

    fn retain_dense_array(&mut self, object: u32, slots: Vec<Values>) {
        let before = self.prefix_object_properties.get(&object).cloned();
        let dense = self
            .dense_arrays
            .entry(object)
            .or_insert_with(|| DenseArray {
                slots: slots.clone(),
            });
        let length = dense.slots.len().max(slots.len());
        dense
            .slots
            .resize_with(length, || BTreeSet::from([AbstractValue::Undefined]));
        for (index, current) in dense.slots.iter_mut().enumerate() {
            if let Some(values) = slots.get(index) {
                current.extend(values.iter().cloned());
            } else {
                current.insert(AbstractValue::Undefined);
            }
        }
        let properties = self.prefix_object_properties.entry(object).or_default();
        properties
            .entry(Some("length".into()))
            .or_default()
            .insert(AbstractValue::NonCallable);
        for (index, values) in dense.slots.iter().enumerate() {
            properties
                .entry(Some(index.to_string()))
                .or_default()
                .extend(values.iter().cloned());
            properties
                .entry(None)
                .or_default()
                .extend(values.iter().cloned());
        }
        if before.as_ref() != Some(properties) {
            self.prefix_revision = self.prefix_revision.wrapping_add(1);
        }
    }

    fn evaluate_array(&mut self, object: u32, env: &Context, captures: &Context) -> Values {
        let elements = self.index.array_elements[&object].clone();
        let mut slots = Vec::new();
        let mut containment = Values::new();
        let mut dense = true;
        let mut may_throw = false;
        let mut can_complete = true;
        for element in elements {
            if !can_complete {
                break;
            }
            let Some(element) = element else {
                dense = false;
                continue;
            };
            let mut values = self.evaluate(&element.value, env, captures);
            may_throw |= values.remove(&AbstractValue::Unavailable);
            if values.is_empty() {
                can_complete = false;
                continue;
            }
            containment.extend(values.iter().cloned());
            if element.spread {
                if let Some((_, source)) = self.dense_source(&values) {
                    slots.extend(source.slots);
                } else {
                    dense = false;
                }
            } else {
                slots.push(values);
            }
        }
        if !can_complete {
            // Do not publish this array origin or its retained callbacks when
            // evaluation can only throw before the literal is constructed.
            return if may_throw {
                BTreeSet::from([AbstractValue::Unavailable])
            } else {
                Values::new()
            };
        }
        self.prefix_object_properties
            .entry(object)
            .or_default()
            .entry(None)
            .or_default()
            .extend(containment);
        if dense {
            self.retain_dense_array(object, slots);
        } else {
            self.tainted_objects.insert(object);
        }
        let mut result = BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(object))]);
        if may_throw {
            result.insert(AbstractValue::Unavailable);
        }
        result
    }

    fn array_iteration_plan(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<(
        ArrayIteration,
        u32,
        DenseArray,
        u32,
        Option<CapturedContext>,
    )> {
        let proof = self.index.array_iterations.get(&span)?.clone();
        proof.result?;
        let values = self.evaluate(&proof.source, env, captures);
        let (source, dense) = self.dense_source(&values)?;
        if dense.slots.len() > MAX_SYNCHRONOUS_INVOCATIONS {
            return None;
        }
        for values in &dense.slots {
            if values.is_empty()
                || values.iter().any(|value| match value {
                    AbstractValue::Origin(ValueOrigin::Object(object)) => {
                        (!self.index.plain_data_objects.contains(object)
                            && !self.ordinary_spread_copies.contains(object))
                            || self.tainted_objects.contains(object)
                    }
                    AbstractValue::Origin(ValueOrigin::Function(key))
                    | AbstractValue::Closure(key, _) => self.tainted_functions.contains(key),
                    AbstractValue::NonCallable | AbstractValue::Undefined => false,
                    _ => true,
                })
            {
                return None;
            }
        }
        let callback = self.evaluate(&proof.callback, env, captures);
        let (key, captured) = match callback.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Origin(ValueOrigin::Function(key))] => (*key, None),
            [AbstractValue::Closure(key, captured)] => (*key, Some(captured.clone())),
            _ => return None,
        };
        let callable = self.index.callables.get(&key)?;
        if callable.generator
            || self.index.async_callables.contains(&key)
            || callable.params.iter().any(|param| param.binding.is_none())
        {
            return None;
        }
        Some((proof, source, dense, key, captured))
    }

    fn merge_array_effects(&mut self, outcome: &mut Outcome, nested: Outcome) {
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.effects.reads.extend(nested.effects.reads);
        outcome.effects.rebinds.extend(nested.effects.rebinds);
        outcome
            .effects
            .uninitialized_reads
            .extend(nested.effects.uninitialized_reads);
        outcome.executed_owners.extend(nested.executed_owners);
        outcome.unknown_call |= nested.unknown_call;
        outcome.complete &= nested.complete;
    }

    fn execute_array_iteration(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> bool {
        let Some((proof, source, dense, key, captured)) =
            self.array_iteration_plan(span, env, captures)
        else {
            return false;
        };
        let source_values = BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(source))]);
        let original_callbacks = self.callback_values(&source_values);
        self.array_iteration_protected_objects
            .extend(self.reachable_objects(&source_values));
        let mut slots = Vec::new();
        for values in &dense.slots {
            let previous_unsupported = self.array_iteration_unsupported;
            self.array_iteration_unsupported = false;
            self.array_iteration_depth += 1;
            let nested = self.invoke(
                key,
                vec![
                    values.clone(),
                    BTreeSet::from([AbstractValue::NonCallable]),
                    source_values.clone(),
                ],
                captured.clone(),
                false,
            );
            self.array_iteration_depth -= 1;
            let unsupported = self.array_iteration_unsupported;
            self.array_iteration_unsupported |= previous_unsupported;
            let returned = nested.returns.clone();
            self.merge_array_effects(outcome, nested);
            let current_source = self.evaluate(&proof.source, env, captures);
            let valid = !unsupported && self.dense_source(&current_source).is_some_and(|(object, current)| object == source && current.slots == dense.slots)
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Origin(ValueOrigin::Object(object)) if self.tainted_objects.contains(object)))
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Origin(ValueOrigin::Function(key)) | AbstractValue::Closure(key, _) if self.tainted_functions.contains(key)));
            let valid = valid
                && (proof.method != ArrayIterationMethod::Map
                    || returned.iter().all(|value| match value {
                        AbstractValue::NonCallable
                        | AbstractValue::Undefined
                        | AbstractValue::Origin(ValueOrigin::Function(_))
                        | AbstractValue::Closure(_, _) => true,
                        AbstractValue::Origin(ValueOrigin::Object(object)) => {
                            !self.tainted_objects.contains(object)
                                && (self.index.plain_data_objects.contains(object)
                                    || self.ordinary_spread_copies.contains(object)
                                    || self.dense_arrays.contains_key(object))
                        }
                        _ => false,
                    }));
            if !valid {
                let callback_owner = self.index.callables[&key].owner;
                self.promote_lazy_owner(
                    callback_owner,
                    self.current_top_owner.unwrap_or(callback_owner),
                    outcome,
                );
                self.tainted_objects.insert(proof.result.unwrap());
                self.prefix_object_properties
                    .entry(proof.result.unwrap())
                    .or_default()
                    .entry(None)
                    .or_default()
                    .extend(original_callbacks.iter().cloned());
                self.invoke_possible_callbacks(&[original_callbacks], false, outcome);
                return false;
            }
            slots.push(if proof.method == ArrayIterationMethod::Map {
                returned
            } else {
                values.clone()
            });
        }
        if proof.method != ArrayIterationMethod::ForEach {
            self.retain_dense_array(proof.result.unwrap(), slots);
        }
        true
    }

    fn is_proven_map_entry_array(
        &self,
        callee: &ValueExpr,
        args: &[ValueExpr],
        env: &Context,
        captures: &Context,
        owner: StatementOrdinal,
    ) -> bool {
        let ValueExpr::Binding(id) = callee else {
            return false;
        };
        id.0.as_ref() == "Map"
            && !self.flow.is_declared(id)
            && !self.shadowed.contains("Map")
            && !env.contains_key(id)
            && !captures.contains_key(id)
            && !self.index.reassigned_map
            && self.index.dynamic_scope_owners.is_empty()
            && !self.index.dynamic_scope_owners.contains(&owner)
            && matches!(args, [ValueExpr::ObjectLiteral(span, _)]
                if self.index.map_entry_arrays.contains(span))
    }

    fn proven_from_entries_callbacks(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let array_intrinsic = ("Array".into(), SyntaxContext::empty());
        if !self.index.dynamic_scope_owners.is_empty()
            || self.flow.is_declared(&proof.object_intrinsic)
            || self.shadowed.contains("Object")
            || self.flow.is_declared(&array_intrinsic)
            || self.shadowed.contains("Array")
            || self.index.mutated_intrinsic_roots.iter().any(|root| {
                matches!(
                    root,
                    IntrinsicRoot::GlobalObject | IntrinsicRoot::Object | IntrinsicRoot::Array
                )
            })
        {
            return None;
        }
        let ValueExpr::Binding(source_id) = &proof.source else {
            return None;
        };
        // This path deliberately accepts only a chunk-top plain-array binding.
        // Local parameters, aliases from opaque calls, and synthetic map results
        // stay on the ordinary unknown-call path.
        if !self.top_bindings.contains(source_id)
            || !self
                .index
                .plain_array_bindings
                .contains(source_id.0.as_ref())
        {
            return None;
        }
        let source_values = self.evaluate(&proof.source, env, captures);
        let [AbstractValue::Origin(ValueOrigin::Object(array))] =
            source_values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if self.tainted_objects.contains(array) {
            return None;
        }
        let count = *self.index.dense_array_literals.get(array)?;
        let mut expected_entries = Values::new();
        for index in 0..count {
            let slot = self.flow.values_of_property(*array, &index.to_string())?;
            if slot.iter().next().is_none()
                || slot.iter().any(|origin| origin == ValueOrigin::Unknown)
            {
                return None;
            }
            for origin in slot {
                let ValueOrigin::Object(entry) = origin else {
                    return None;
                };
                if !self.index.plain_data_objects.contains(&entry)
                    || !self
                        .index
                        .primitive_data_properties
                        .contains(&(entry, proof.key.clone()))
                    || self.tainted_objects.contains(&entry)
                {
                    return None;
                }
                let key_values = self
                    .prefix_object_properties
                    .get(&entry)
                    .and_then(|properties| properties.get(&Some(proof.key.clone())))?;
                if key_values.is_empty()
                    || key_values.iter().any(|value| {
                        !matches!(value, AbstractValue::NonCallable | AbstractValue::Undefined)
                    })
                {
                    return None;
                }
                expected_entries.insert(AbstractValue::Origin(ValueOrigin::Object(entry)));
            }
        }
        let actual_entries = self
            .prefix_object_properties
            .get(array)
            .and_then(|properties| properties.get(&None))?;
        if actual_entries != &expected_entries {
            return None;
        }
        let mut retained_values = self.callback_values(&source_values);
        // Retain the source identity so returned registry aliases taint entries.
        retained_values.extend(source_values);
        Some(retained_values)
    }

    fn callbacks_in_from_entries_source(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let source_values = self.evaluate(&proof.source, env, captures);
        Some(self.callback_values(&source_values))
    }

    fn summarize_metadata_decoration(
        &mut self,
        application: &MetadataDecorateCall,
        env: &Context,
        captures: &Context,
        owner: StatementOrdinal,
        outcome: &mut Outcome,
    ) -> bool {
        if !self.index.dynamic_scope_owners.is_empty()
            || !self.initialized_bindings.contains(&application.helper)
            || !self.initialized_bindings.contains(&application.target)
        {
            return false;
        }
        let helper = self.evaluate(
            &ValueExpr::Binding(application.helper.clone()),
            env,
            captures,
        );
        let target = self.evaluate(
            &ValueExpr::Binding(application.target.clone()),
            env,
            captures,
        );
        if helper.len() != 1 || target.len() != 1 {
            return false;
        }
        let helper_key = match helper.first() {
            Some(AbstractValue::Origin(ValueOrigin::Function(key))) => *key,
            Some(AbstractValue::Closure(key, captured)) if captured.values.is_empty() => *key,
            _ => return false,
        };
        let Some(AbstractValue::Origin(ValueOrigin::Class(target_key))) = target.first() else {
            return false;
        };
        if application.stable_origins(self.flow) != Some((helper_key, *target_key)) {
            return false;
        }
        if application.member_decorators.iter().any(|root| {
            !self.index.imported_bindings.contains(root) || self.changed_member_roots.contains(root)
        }) {
            return false;
        }
        // The author certifies the helper's dispatch and every decorator's
        // metadata-only behavior, not purity. Preserve all static dependencies
        // of their bodies without manufacturing calls into the target methods.
        for binding in std::iter::once(&application.helper)
            .chain(&application.decorators)
            .chain(&application.member_decorators)
        {
            if let Some(&binding_owner) = self.binding_owner.get(binding) {
                let (reads, rebinds) = self
                    .owner_read_closure
                    .lazy_effects_for_owner(binding_owner);
                self.promote_lazy_effects(reads, rebinds, owner, outcome);
                outcome.executed_owners.insert(binding_owner);
            }
        }
        self.apply_rebind_effects(&outcome.effects.rebinds);
        true
    }

    fn all_owners(&self) -> impl Iterator<Item = StatementOrdinal> + '_ {
        self.facts.iter().map(|fact| fact.ordinal)
    }

    fn promote_lazy_owner(
        &self,
        owner: StatementOrdinal,
        effect_owner: StatementOrdinal,
        outcome: &mut Outcome,
    ) {
        let fact = &self.facts[owner.0];
        let prefix_reads = self.index.owner_callable_prefix_reads[owner.0].as_ref();
        let reads = prefix_reads
            .filter(|reads| {
                // Without complete coverage, retain the original whole-body
                // fallback rather than dropping a possible setter read.
                !self.reachability.incomplete
                    && !self.index.dynamic_scope_owners.contains(&owner)
                    && !self.index.unindexed_accessor_owners.contains(&owner)
                    && fact.reads.first_order_lazy.is_subset(reads)
            })
            .unwrap_or(&fact.reads.lazy);
        // The per-Callable rebind index does not yet cover every update and
        // destructuring target. Keep the complete raw set until it does.
        self.promote_lazy_effects(reads, &fact.rebinds.lazy, effect_owner, outcome);
    }

    fn promote_lazy_effects(
        &self,
        reads: &BTreeSet<Id>,
        rebinds: &BTreeSet<Id>,
        effect_owner: StatementOrdinal,
        outcome: &mut Outcome,
    ) {
        // Large fallback summaries are already sorted. Building and merging
        // their sets in bulk avoids a tree lookup for every repeated read.
        // Small per-owner additions still use insertion into the larger set.
        if reads.len() >= 64 && reads.len().saturating_mul(2) >= outcome.effects.reads.len() {
            let mut uninitialized = reads
                .difference(&self.initialized_bindings)
                .filter(|id| self.top_bindings.contains(*id))
                .cloned()
                .collect();
            outcome
                .effects
                .uninitialized_reads
                .append(&mut uninitialized);
            let mut promoted = reads
                .intersection(&self.top_bindings)
                .cloned()
                .map(|id| (id, effect_owner))
                .collect();
            outcome.effects.reads.append(&mut promoted);
        } else {
            for id in reads.iter().filter(|id| self.top_bindings.contains(*id)) {
                outcome.effects.reads.insert((id.clone(), effect_owner));
                if !self.initialized_bindings.contains(id) {
                    outcome.effects.uninitialized_reads.insert(id.clone());
                }
            }
        }
        if rebinds.len() >= 64 && rebinds.len().saturating_mul(2) >= outcome.effects.rebinds.len() {
            let mut promoted = rebinds
                .intersection(&self.top_bindings)
                .cloned()
                .map(|id| (id, effect_owner))
                .collect();
            outcome.effects.rebinds.append(&mut promoted);
        } else {
            outcome.effects.rebinds.extend(
                rebinds
                    .iter()
                    .filter(|id| self.top_bindings.contains(*id))
                    .cloned()
                    .map(|id| (id, effect_owner)),
            );
        }
    }

    fn roots_for_owner(&self, owner: StatementOrdinal) -> BTreeSet<StatementOrdinal> {
        let mut roots = BTreeSet::from([owner]);
        if let Some(fact) = self.facts.get(owner.0) {
            roots.extend(
                self.owner_read_closure
                    .owners_for_bindings(fact.reads.eager.iter().chain(fact.rebinds.eager.iter())),
            );
        }
        roots
    }

    fn fact_roots(&self, fact: &StatementFacts) -> BTreeSet<StatementOrdinal> {
        let mut roots = BTreeSet::new();
        // Export lists link bindings; evaluating one does not expose a value
        // whose declaration has not run yet.
        if self.index.linkage_owners.contains(&fact.ordinal) {
            return roots;
        }
        if self.exported_owners.contains(&fact.ordinal)
            || !(fact.purity.is_pure()
                && (fact.declares_direct_function
                    || matches!(fact.kind, StatementKind::ClassDecl)
                    || self.closed_callback_factory_result(fact)))
        {
            roots.insert(fact.ordinal);
        }
        // A pure class definition evaluates its superclass value, but merely
        // reading a known ordinary class does not run that class's constructor
        // or methods. The eager read remains in StatementFacts for the owner
        // graph. Calls/static initializers and actual capability publication
        // still enter reachability through their own executor paths.
        let inert_heritage = self.inert_class_heritage_binding(fact);
        roots.extend(
            self.owner_read_closure.owners_for_bindings(
                fact.reads
                    .eager
                    .iter()
                    .filter(|id| Some(*id) != inert_heritage)
                    .chain(fact.rebinds.eager.iter()),
            ),
        );
        roots
    }

    /// A validated factory retains the inline callback only in its fresh
    /// returned graph. Creating that graph does not publish the callback;
    /// later calls, writes, and exports still flow through the value model.
    fn closed_callback_factory_result(&self, fact: &StatementFacts) -> bool {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || self.index.opaque_top.contains(&fact.ordinal)
        {
            return false;
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return false;
        };
        let [call] = self.index.top_calls[fact.ordinal.0].as_slice() else {
            return false;
        };
        matches!(callee.as_ref(), ValueExpr::Binding(factory)
            if self.index.callback_factory_imports.contains(factory))
            && matches!(
                args.as_slice(),
                [ValueExpr::Origin(ValueOrigin::Function(_))]
            )
            && fact.declared.contains(binding)
            && self.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            && call.kind == CallKind::Call
            && !call.has_spread
            && CallSite::from(call.span) == *site
    }

    fn inert_class_heritage_binding<'a>(&'a self, fact: &StatementFacts) -> Option<&'a Id> {
        if !matches!(fact.kind, StatementKind::ClassDecl)
            || !fact.purity.is_pure()
            || self.exported_owners.contains(&fact.ordinal)
            || self.reachability.incomplete
        {
            return None;
        }
        let (class_key, _) = self.index.class_bindings.iter().find(|(key, binding)| {
            self.index.class_owners.get(key) == Some(&fact.ordinal)
                && fact.declared.contains(binding)
        })?;
        let ValueExpr::Binding(base) = self
            .index
            .class_values
            .get(class_key)?
            .super_class
            .as_ref()?
        else {
            return None;
        };
        let values = self.prefix_values.get(base)?;
        let Some(AbstractValue::Origin(ValueOrigin::Class(base_key))) = values.first() else {
            return None;
        };
        let base_owner = *self.index.class_owners.get(base_key)?;
        if !self.initialized_bindings.contains(base)
            || !fact.reads.eager.contains(base)
            || self.index.class_bindings.get(base_key) != Some(base)
            || base_owner.0 >= fact.ordinal.0
            || !self.facts[base_owner.0].purity.is_pure()
            || self.tainted_classes.contains(base_key)
            || self.escaped_class_capabilities.contains(base_key)
            || values.len() != 1
        {
            return None;
        }
        Some(base)
    }

    fn extend_reachable_owners(
        &mut self,
        roots: impl IntoIterator<Item = StatementOrdinal>,
    ) -> bool {
        let before = self.reachability.owners.len();
        // A reachable callback can read only its synchronous prefix while the
        // chunk is initializing. In particular, a binding used after a
        // definite await cannot make the body of its future value executable
        // during an opaque call at this prefix.
        let reached = FactsOwnerReadClosure::extend_synchronous_owners(
            &self.synchronous_owner_adjacency,
            roots,
            &mut self.reachability.expanded,
        );
        self.reachability.owners.extend(reached.iter().copied());
        let changed = self.reachability.owners.len() != before;
        if changed {
            self.reachability.revision = self.reachability.revision.wrapping_add(1);
        }
        if reached
            .iter()
            .any(|owner| self.index.dynamic_scope_owners.contains(owner))
        {
            self.mark_reachability_incomplete();
        }
        changed
    }

    fn mark_reachability_incomplete(&mut self) {
        if !self.reachability.incomplete {
            self.reachability.incomplete = true;
            self.reachability.revision = self.reachability.revision.wrapping_add(1);
        }
        // Incomplete reachability always falls back to the whole chunk.
        self.prior_reachability_roots = None;
    }

    fn note_unknown_execution(&mut self, owner: StatementOrdinal) {
        // Exports expose live capabilities only to code that can reenter the
        // module. Builtin iteration without such reentry remains precise.
        if self.export_reentry_enabled {
            let mut exported = self.exported_runtime_values.clone();
            exported.extend(self.index.exported_bindings.iter().flat_map(|binding| {
                self.prefix_values
                    .get(binding)
                    .into_iter()
                    .flatten()
                    .cloned()
            }));
            self.expose_class_capabilities(&exported);
            self.taint_objects(&exported);
        }
        let mut changed = !self.reachability.unknown_call_seen;
        self.reachability.unknown_call_seen = true;
        let roots = self
            .prior_reachability_roots
            .take()
            .into_iter()
            .flatten()
            .chain(self.roots_for_owner(owner));
        changed |= self.extend_reachable_owners(roots);
        if changed {
            self.reachability.revision = self.reachability.revision.wrapping_add(1);
        }
    }

    fn note_unknown_call(&mut self, call: &Call) {
        self.note_unknown_execution(call.owner);
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.mark_reachability_incomplete();
        }
    }

    fn record_executed_owner(&mut self, owner: StatementOrdinal) {
        if self.reachability.unknown_call_seen {
            self.extend_reachable_owners([owner]);
        } else if let Some(roots) = &mut self.prior_reachability_roots {
            roots.insert(owner);
        }
    }

    fn promote_property_writes(
        &self,
        writes: &[(Option<Id>, Option<String>)],
        owner: StatementOrdinal,
        outcome: &mut Outcome,
    ) {
        if writes.is_empty() {
            return;
        }
        let unknown_call = outcome.unknown_call
            || self.reachability.unknown_call_seen
            || self.fallback_outcome.unknown_call;
        if self.index.setter_owners.is_empty() && !unknown_call {
            return;
        }
        let mut possible_owners = outcome.executed_owners.clone();
        possible_owners.insert(owner);
        possible_owners.extend(self.prior_effect_owners.iter().copied());
        for (receiver, _) in writes {
            if let Some(receiver) = receiver
                && let Some(binding_owner) = self.binding_owner.get(receiver)
            {
                possible_owners.insert(*binding_owner);
            }
        }
        let unknown_receiver = writes.iter().any(|(receiver, _)| receiver.is_none());
        if unknown_call {
            possible_owners.extend(
                self.reachability
                    .owners
                    .iter()
                    .copied()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
            if self.reachability.incomplete {
                possible_owners.extend(self.all_owners());
            }
        } else if unknown_receiver {
            possible_owners.extend(
                self.all_owners()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
        }
        let mut accessor_owners = BTreeSet::new();
        if unknown_call {
            accessor_owners.extend(possible_owners);
        } else {
            for (_, property) in writes {
                if let Some(property) = property {
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .get(&Some(property.clone()))
                            .into_iter()
                            .flatten()
                            .copied(),
                    );
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .get(&None)
                            .into_iter()
                            .flatten()
                            .copied(),
                    );
                } else {
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .values()
                            .flat_map(|owners| owners.iter().copied()),
                    );
                }
            }
            accessor_owners = accessor_owners
                .intersection(&possible_owners)
                .copied()
                .collect();
        }
        if !accessor_owners.is_empty() {
            let effect_owner = if unknown_call {
                self.current_top_owner.unwrap_or(owner)
            } else {
                owner
            };
            for accessor_owner in accessor_owners {
                self.promote_lazy_owner(accessor_owner, effect_owner, outcome);
            }
            outcome.complete = false;
        }
    }

    fn may_be_available_accessor_owner(&self, candidate: StatementOrdinal) -> bool {
        if self.reachability.incomplete {
            // Direct eval and `with` can manufacture references to lexical
            // values outside the statically known read closure.
            return true;
        }
        let Some(current) = self.current_top_owner else {
            return true;
        };
        // Reachability follows lexical reads, including declarations whose
        // initializer has not executed. Such a future let/const/class value
        // cannot yet have been installed as a setter. Function declarations
        // are hoisted, and a later var declaration can name a value assigned
        // earlier, so retain both. Directly executed owners are added by the
        // caller independently of this broad reachability filter.
        candidate.0 <= current.0
            || self.index.hoisted_function_owners.contains(&candidate)
            || !self.facts[candidate.0].var_declared.is_empty()
    }

    fn safe_object_assign_target(&self, call: &Call, id: &Id) -> bool {
        if call.kind == CallKind::New
            || !call.plain_object_sources
            || !matches!(call.args.first(), Some(ValueExpr::Binding(target)) if target == id)
        {
            return false;
        }
        let ValueExpr::Member(object, Some(property), _) = &call.callee else {
            return false;
        };
        matches!(object.as_ref(), ValueExpr::Binding(object_id)
            if object_id.0.as_ref() == "Object"
                && !self.flow.is_declared(object_id)
                && !self.shadowed.contains("Object")
                && property == "assign")
    }

    fn stable_fresh_function_target(&self, key: u32, callable: &Callable, id: &Id) -> bool {
        callable.local_functions.contains(id)
            && !callable.rebinds.contains(id)
            && !callable.literal_captures.contains(id)
            && !callable.escaped_bindings.contains(id)
            && !callable.dynamic_property_effect
            && callable
                .member_writes
                .iter()
                .all(|(receiver, property)| receiver != id || property != "__proto__")
            && !callable.local_assignments.iter().any(|(target, value)| {
                if target == id {
                    return false;
                }
                let mut mentioned = BTreeSet::new();
                value.bindings(&mut mentioned);
                mentioned.contains(id)
            })
            && !self
                .index
                .callables
                .iter()
                .any(|(other, nested)| *other != key && nested.reads.contains(id))
            && !callable.calls.iter().any(|call| {
                let mut mentioned = BTreeSet::new();
                call.callee.bindings(&mut mentioned);
                for arg in &call.args {
                    arg.bindings(&mut mentioned);
                }
                mentioned.contains(id) && !self.safe_object_assign_target(call, id)
            })
    }

    fn callback_values(&self, values: &Values) -> Values {
        let mut callbacks = Values::new();
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited_objects = BTreeSet::new();
        let mut expanded_escaped = false;
        while let Some(value) = pending.pop() {
            match &value {
                AbstractValue::Origin(ValueOrigin::Function(_)) | AbstractValue::Closure(_, _) => {
                    // Capture snapshots form a DAG: multiple bindings can
                    // contain the same closure. Expand each closure once,
                    // rather than revisit it along every path through captures.
                    if callbacks.insert(value.clone())
                        && let AbstractValue::Closure(_, captured) = &value
                    {
                        pending.extend(
                            captured
                                .values
                                .values()
                                .flat_map(|values| values.iter().cloned()),
                        );
                    }
                }
                AbstractValue::Origin(ValueOrigin::Class(key))
                | AbstractValue::ClassMembers(key) => {
                    callbacks.insert(AbstractValue::ClassMembers(*key));
                }
                AbstractValue::Instance(key, captured) => {
                    callbacks.insert(AbstractValue::ClassMembers(*key));
                    if callbacks.insert(value.clone()) {
                        pending.extend(captured.values.iter().cloned());
                    }
                }
                AbstractValue::DeferredCallbacks(captured) => {
                    if callbacks.insert(value.clone()) {
                        pending.extend(captured.values.iter().cloned());
                    }
                }
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    if visited_objects.insert(*object)
                        && let Some(properties) = self.prefix_object_properties.get(object)
                    {
                        pending.extend(
                            properties
                                .values()
                                .flat_map(|values| values.iter().cloned()),
                        );
                    }
                }
                AbstractValue::GlobalObject | AbstractValue::Unknown if !expanded_escaped => {
                    // Unknown external data cannot expose an unescaped local
                    // callback. Preserve tracked escapes; exports are added
                    // separately at opaque call sites, and known object
                    // identities are expanded through their property maps.
                    expanded_escaped = true;
                    pending.extend(
                        self.global_escaped_callbacks
                            .iter()
                            .filter(|value| !matches!(value, AbstractValue::Unknown))
                            .cloned(),
                    );
                }
                _ => {}
            }
        }
        callbacks
    }

    fn fresh_primitive_write_receivers(
        &mut self,
        callable: &Callable,
        env: &Context,
    ) -> BTreeSet<u32> {
        let writes = &callable.callback_result_writes;
        if self.array_iteration_depth == 0
            || self.array_iteration_unsupported
            || !self.array_intrinsics_intact()
            || callable.opaque
            || writes.is_empty()
            || writes.len() != callable.member_value_writes.len()
            || writes.len()
                != callable.member_writes.len() + callable.dynamic_property_receivers.len()
            || callable
                .calls
                .iter()
                .any(|call| call.kind != CallKind::LocalMemberRead)
        {
            return BTreeSet::new();
        }
        let mut objects = BTreeSet::new();
        for write in writes {
            if !write.simple_assignment
                || !write.data_read_operands
                || !matches!(write.receiver, ValueExpr::Binding(_))
            {
                return BTreeSet::new();
            }
            for expression in [&write.property_key, &write.value] {
                let values = self.evaluate(expression, env, env);
                if values.is_empty()
                    || !values.iter().all(|value| {
                        matches!(value, AbstractValue::NonCallable | AbstractValue::Undefined)
                    })
                {
                    return BTreeSet::new();
                }
            }
            let receivers = self.evaluate(&write.receiver, env, env);
            let [AbstractValue::Origin(ValueOrigin::Object(object))] =
                receivers.iter().collect::<Vec<_>>().as_slice()
            else {
                return BTreeSet::new();
            };
            if self.tainted_objects.contains(object)
                || self.array_iteration_protected_objects.contains(object)
                || (!self.index.plain_data_objects.contains(object)
                    && !self.ordinary_spread_copies.contains(object))
                || self.dense_arrays.contains_key(object)
            {
                return BTreeSet::new();
            }
            objects.insert(*object);
        }
        objects
    }

    fn apply_member_value_writes(
        &mut self,
        writes: &[(Id, Option<String>, ValueExpr)],
        env: &Context,
        captures: &Context,
        certified_data_receivers: &BTreeSet<u32>,
    ) {
        for (receiver, property, expression) in writes {
            let receivers = self.evaluate(&ValueExpr::Binding(receiver.clone()), env, captures);
            let values = self.evaluate(expression, env, captures);
            for receiver in receivers {
                match receiver {
                    AbstractValue::GlobalObject | AbstractValue::ArrayIntrinsic => {
                        self.array_intrinsic_mutated |= receiver == AbstractValue::ArrayIntrinsic;
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        let before = self.global_escaped_callbacks.len();
                        self.global_escaped_callbacks.extend(callbacks);
                        if self.global_escaped_callbacks.len() != before {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    AbstractValue::Origin(ValueOrigin::Object(object)) => {
                        if !certified_data_receivers.contains(&object) {
                            self.tainted_objects.insert(object);
                        }
                        let properties = self.prefix_object_properties.entry(object).or_default();
                        let values_for_property = properties.entry(property.clone()).or_default();
                        let before = values_for_property.len();
                        values_for_property.extend(values.iter().cloned());
                        if values_for_property.len() != before {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    AbstractValue::Instance(key, captured) => {
                        self.tainted_classes.insert(key);
                        self.taint_objects(&captured.values);
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        if !callbacks.is_subset(&self.global_escaped_callbacks) {
                            self.global_escaped_callbacks.extend(callbacks);
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    AbstractValue::Unknown | AbstractValue::DeferredCallbacks(_) => {
                        self.taint_objects(&values);
                        // A write through an untracked object may publish the
                        // callback through that object before it is passed to
                        // opaque code. Retain it as an escaped callback root.
                        let callbacks = self.callback_values(&values);
                        let before = self.global_escaped_callbacks.len();
                        self.global_escaped_callbacks.extend(callbacks);
                        if self.global_escaped_callbacks.len() != before {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    fn promote_callback_result_writes(
        &mut self,
        writes: &[CallbackResultWrite],
        env: &Context,
        captures: &Context,
        owner: StatementOrdinal,
        outcome: &mut Outcome,
        certified_data_receivers: &BTreeSet<u32>,
    ) {
        for write in writes {
            let receivers = self.evaluate(&write.receiver, env, captures);
            let uncertified = receivers.iter().filter(|value| !matches!(value,
                AbstractValue::Origin(ValueOrigin::Object(object)) if certified_data_receivers.contains(object))).cloned().collect();
            self.taint_objects(&uncertified);
            let callbacks = self.callback_values(&receivers);
            if !callbacks
                .iter()
                .any(|value| matches!(value, AbstractValue::DeferredCallbacks(_)))
            {
                continue;
            }
            if write
                .property
                .as_deref()
                .is_some_and(|property| property != "__proto__")
                && matches!(write.value, ValueExpr::NonCallable | ValueExpr::Undefined)
            {
                continue;
            }
            // Installing an opaque value can add an accessor anywhere in the
            // returned graph. Include its effects now: a later plain member
            // read can execute a getter without another explicit Call node.
            outcome.complete = false;
            outcome.unknown_call = true;
            self.note_unknown_execution(owner);
            let values = self.evaluate(&write.value, env, captures);
            self.promote_deferred_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[callbacks, values], false, outcome);
        }
    }

    fn intern_captures(&mut self, captures: Context) -> CapturedContext {
        if let Some((values, &identity)) = self.capture_contexts.get_key_value(&captures) {
            return CapturedContext {
                identity,
                values: Arc::clone(values),
            };
        }
        let identity = self.capture_contexts.len();
        let values = Arc::new(captures);
        self.capture_contexts.insert(Arc::clone(&values), identity);
        CapturedContext { identity, values }
    }

    fn intern_callback_set(&mut self, values: Values) -> Captured<Values> {
        if let Some((values, &identity)) = self.callback_sets.get_key_value(&values) {
            return Captured {
                identity,
                values: Arc::clone(values),
            };
        }
        let identity = self.callback_sets.len();
        let values = Arc::new(values);
        self.callback_sets.insert(Arc::clone(&values), identity);
        Captured { identity, values }
    }

    fn deferred_callbacks(&mut self, values: Values) -> AbstractValue {
        AbstractValue::DeferredCallbacks(self.intern_callback_set(values))
    }

    fn taint_objects(&mut self, values: &Values) {
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            match value {
                AbstractValue::ArrayIntrinsic => {
                    self.array_intrinsic_mutated = true;
                }
                AbstractValue::Origin(ValueOrigin::Object(object))
                    if self.tainted_objects.insert(object) =>
                {
                    if let Some(properties) = self.prefix_object_properties.get(&object) {
                        pending.extend(
                            properties
                                .values()
                                .flat_map(|values| values.iter().cloned()),
                        );
                    }
                }
                AbstractValue::Instance(_, captured)
                | AbstractValue::DeferredCallbacks(captured) => {
                    pending.extend(captured.values.iter().cloned());
                }
                AbstractValue::Origin(ValueOrigin::Function(key)) => {
                    self.tainted_functions.insert(key);
                }
                AbstractValue::Closure(key, captured) => {
                    self.tainted_functions.insert(key);
                    pending.extend(
                        captured
                            .values
                            .values()
                            .flat_map(|values| values.iter().cloned()),
                    );
                }
                _ => {}
            }
        }
    }

    fn construct_local_class(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Option<(Outcome, AbstractValue)> {
        let constructor = self.index.local_constructors.get(&key)?.clone();
        let binding = self.index.class_bindings.get(&key)?;
        // A constructorless, fieldless class only allocates an instance for a
        // direct zero-argument `new`. Even if its class object escaped, a
        // modified prototype or deferred method cannot run during allocation.
        // Any unknown/reassigned callee alternative still takes the ordinary
        // opaque path in execute_call. Other constructors need the class and
        // prototype to remain unexposed to avoid setters and method reentry.
        let inert_allocation = constructor.inert_allocation && args.is_empty();
        if !inert_allocation
            && (self.index.other_class_uses.contains(binding)
                || (self.export_reentry_enabled && self.index.exported_bindings.contains(binding))
                || self.tainted_classes.contains(&key)
                || self
                    .global_escaped_callbacks
                    .contains(&AbstractValue::ClassMembers(key)))
        {
            return None;
        }
        if constructor
            .binding_reads
            .iter()
            .any(|id| !self.flow.is_declared(id))
        {
            return None;
        }
        if self.export_reentry_enabled {
            let exported: Values = self
                .index
                .exported_bindings
                .iter()
                .flat_map(|id| self.prefix_values.get(id).into_iter().flatten().cloned())
                .collect();
            self.taint_objects(&exported);
        }
        for index in &constructor.property_reads {
            let values = args.get(*index)?;
            if values.is_empty()
                || !values.iter().all(|value| {
                    matches!(value,
                    AbstractValue::Origin(ValueOrigin::Object(object))
                        if self.index.plain_data_objects.contains(object)
                            && !self.tainted_objects.contains(object))
                })
            {
                return None;
            }
        }
        let outcome = constructor.key.map_or_else(
            || Outcome {
                complete: true,
                ..Outcome::default()
            },
            |constructor_key| self.invoke(constructor_key, args.to_vec(), None, true),
        );
        let values: Values = args.iter().flatten().cloned().collect();
        let instance = AbstractValue::Instance(key, self.intern_callback_set(values));
        Some((outcome, instance))
    }

    /// The foreign wrapper can invoke its captured callbacks and any values
    /// they return synchronously. It cannot resume them past an await.
    fn reachable_objects(&self, values: &Values) -> BTreeSet<u32> {
        let mut objects = BTreeSet::new();
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            match value {
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    objects.insert(object);
                    if let Some(properties) = self.prefix_object_properties.get(&object) {
                        pending.extend(properties.values().flatten().cloned());
                    }
                }
                AbstractValue::Instance(_, captured)
                | AbstractValue::DeferredCallbacks(captured) => {
                    pending.extend(captured.values.iter().cloned())
                }
                AbstractValue::Closure(_, captured) => {
                    pending.extend(captured.values.values().flatten().cloned())
                }
                _ => {}
            }
        }
        objects
    }

    fn expose_class_capabilities(&mut self, values: &Values) {
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            match value {
                AbstractValue::Origin(ValueOrigin::Class(key))
                | AbstractValue::ClassMembers(key) => {
                    self.escaped_class_capabilities.insert(key);
                }
                AbstractValue::Instance(key, captured) => {
                    self.escaped_class_capabilities.insert(key);
                    pending.extend(captured.values.iter().cloned());
                }
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    if let Some(properties) = self.prefix_object_properties.get(&object) {
                        pending.extend(properties.values().flatten().cloned());
                    }
                }
                AbstractValue::DeferredCallbacks(captured) => {
                    pending.extend(captured.values.iter().cloned())
                }
                AbstractValue::Closure(_, captured) => {
                    pending.extend(captured.values.values().flatten().cloned())
                }
                _ => {}
            }
        }
    }

    fn default_class_has_instance(&self, values: &Values) -> bool {
        let [AbstractValue::Origin(ValueOrigin::Class(key))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return false;
        };
        self.index
            .class_values
            .get(key)
            .is_some_and(|class| class.default_has_instance)
            && !self.tainted_classes.contains(key)
            && !self.escaped_class_capabilities.contains(key)
            && !self
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(*key))
            && self.index.class_bindings.get(key).is_some_and(|binding| {
                !self.index.other_class_uses.contains(binding)
                    && !self.index.exported_bindings.contains(binding)
            })
    }

    fn ordinary_prototype_chain(&self, values: &Values) -> bool {
        !values.is_empty()
            && values.iter().all(|value| match value {
                AbstractValue::NonCallable | AbstractValue::Undefined => true,
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    !self.tainted_objects.contains(object)
                        && self.array_intrinsics_intact()
                        && (self.index.plain_data_objects.contains(object)
                            || self.ordinary_spread_copies.contains(object)
                            || self.dense_arrays.contains_key(object))
                }
                _ => false,
            })
    }

    fn contains_dense_array(&self, values: &Values) -> bool {
        self.reachable_objects(values)
            .iter()
            .any(|object| self.dense_arrays.contains_key(object))
    }

    fn promote_raw_callbacks(&mut self, values: &Values, outcome: &mut Outcome) {
        let effect_owner = self.current_top_owner.unwrap();
        let mut owners = BTreeSet::new();
        for callback in self.callback_values(values) {
            let owner = match callback {
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => self
                    .index
                    .callables
                    .get(&key)
                    .map(|callable| callable.owner),
                AbstractValue::ClassMembers(key) => self
                    .flow
                    .callable_owner
                    .get(&ValueOrigin::Class(key))
                    .or_else(|| self.index.class_owners.get(&key))
                    .copied(),
                _ => None,
            };
            owners.extend(owner);
        }
        if self.reachability.incomplete {
            for owner in owners {
                let (reads, rebinds) = self.owner_read_closure.lazy_effects_for_owner(owner);
                let reads = reads.clone();
                let rebinds = rebinds.clone();
                let referenced = reads
                    .iter()
                    .flat_map(|id| self.prefix_values.get(id).into_iter().flatten().cloned())
                    .collect();
                self.taint_objects(&referenced);
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
            }
            return;
        }
        let reached = FactsOwnerReadClosure::extend_synchronous_owners(
            &self.synchronous_owner_adjacency,
            owners,
            &mut BTreeSet::new(),
        );
        for owner in reached {
            let fact = &self.facts[owner.0];
            // This coverage-checked union includes every retained callable's
            // synchronous prefix, including returned callbacks and getters.
            // Bodies past a definite await cannot run during this protocol.
            let reads = self.synchronous_owner_reads[owner.0]
                .unwrap_or(&fact.reads.lazy)
                .clone();
            let rebinds = fact.rebinds.lazy.clone();
            let referenced = reads
                .iter()
                .flat_map(|id| self.prefix_values.get(id).into_iter().flatten().cloned())
                .collect();
            self.taint_objects(&referenced);
            // Write coverage is not complete: retain all lazy rebinds.
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
        }
    }

    fn promote_deferred_callbacks(&mut self, captured: &Values, outcome: &mut Outcome) {
        let effect_owner = self.current_top_owner.unwrap();
        let mut roots = BTreeSet::new();
        for callback in self.callback_values(captured) {
            let key = match callback {
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => key,
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    continue;
                }
                _ => continue,
            };
            if let Some(callable) = self.index.callables.get(&key) {
                roots.insert(callable.owner);
            }
        }
        if self.reachability.incomplete {
            for owner in roots {
                let (reads, rebinds) = self.owner_read_closure.lazy_effects_for_owner(owner);
                self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
            }
        } else {
            let reached = FactsOwnerReadClosure::extend_synchronous_owners(
                &self.synchronous_owner_adjacency,
                roots,
                &mut BTreeSet::new(),
            );
            for owner in reached {
                let fact = &self.facts[owner.0];
                let reads = self.synchronous_owner_reads[owner.0].unwrap_or(&fact.reads.lazy);
                // Retain complete rebinds: prefix coverage currently applies
                // only to reads, not every update or destructuring write.
                self.promote_lazy_effects(reads, &fact.rebinds.lazy, effect_owner, outcome);
            }
        }
    }

    /// Opaque code holding a class or its prototype can retrieve and run any
    /// method, getter, field callback, or constructor synchronously. Cached
    /// owner traversal includes those bodies and their synchronous callees,
    /// while stopping reads after a definite await. Owners without complete
    /// callable coverage retain their whole lazy read set.
    fn promote_class_members(&mut self, key: u32, outcome: &mut Outcome) {
        let Some(&owner) = self.flow.callable_owner.get(&ValueOrigin::Class(key)) else {
            return;
        };
        let effect_owner = self.current_top_owner.unwrap_or(owner);
        if self.reachability.incomplete {
            let (reads, rebinds) = self.owner_read_closure.lazy_effects_for_owner(owner);
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        } else {
            if !self.class_member_effects.contains_key(&owner) {
                let reached = FactsOwnerReadClosure::extend_synchronous_owners(
                    &self.synchronous_owner_adjacency,
                    [owner],
                    &mut BTreeSet::new(),
                );
                let mut reads = BTreeSet::new();
                let mut rebinds = BTreeSet::new();
                for reached_owner in reached {
                    let fact = &self.facts[reached_owner.0];
                    reads.extend(
                        self.synchronous_owner_reads[reached_owner.0]
                            .unwrap_or(&fact.reads.lazy)
                            .iter()
                            .cloned(),
                    );
                    // Prefix coverage is only proved for reads. Preserve all
                    // lazy writes, including destructuring and updates.
                    rebinds.extend(fact.rebinds.lazy.iter().cloned());
                }
                self.class_member_effects.insert(owner, (reads, rebinds));
            }
            let (reads, rebinds) = &self.class_member_effects[&owner];
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        }
        // The opaque caller may invoke a method that changes a top-level
        // binding. Subsequent prefix calls must include its new possible
        // value, just as they do after an ordinary callback invocation.
        self.apply_rebind_effects(&outcome.effects.rebinds);
    }

    fn closure_captures(&mut self, key: u32, captures: &Context) -> CapturedContext {
        let Some(callable) = self.index.callables.get(&key) else {
            return self.intern_captures(Context::new());
        };
        let free_bindings = callable.reads.iter().chain(callable.rebinds.iter());
        let mut captured = Context::new();
        for id in free_bindings {
            if self.top_bindings.contains(id) {
                continue;
            }
            if let Some(values) = captures.get(id) {
                captured.insert(
                    id.clone(),
                    if self.mutable_captures.contains(id) || captured.len() >= 64 {
                        unknown()
                    } else {
                        values.clone()
                    },
                );
            }
        }
        self.intern_captures(captured)
    }

    fn available_export_callbacks(&self) -> Values {
        if !self.export_reentry_enabled {
            return Values::new();
        }
        // Discover the whole export surface in one traversal. Exported aliases,
        // unknown values, and shared objects must not each re-expand the same
        // escaped callback/capture graph independently.
        let mut values = Values::new();
        for binding in &self.index.exported_bindings {
            if let Some(exported) = self.prefix_values.get(binding) {
                values.extend(exported.iter().cloned());
            }
        }
        values.extend(
            self.index
                .exported_hoisted_functions
                .iter()
                .copied()
                .map(|key| AbstractValue::Origin(ValueOrigin::Function(key))),
        );
        values.extend(self.exported_runtime_values.iter().cloned());
        self.callback_values(&values)
    }

    fn invoke_possible_callbacks(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        outcome: &mut Outcome,
    ) {
        let mut callbacks = self.global_escaped_callbacks.clone();
        callbacks.extend(self.available_export_callbacks());
        if !no_sync_arguments {
            let values = args.iter().flatten().cloned().collect();
            callbacks.extend(self.callback_values(&values));
        }
        // Unknown host code may invoke any callback it received with another
        // currently available callback, so each formal gets the conservative
        // callback set plus a non-callable/unknown possibility.
        let mut possible_argument = callbacks.clone();
        possible_argument.insert(AbstractValue::Unknown);
        let args_for = |key: u32, index: &ExecutionIndex| {
            let count = index
                .callables
                .get(&key)
                .map_or(0, |callable| callable.params.len());
            vec![possible_argument.clone(); count]
        };
        let reentrant_pool = callbacks.iter().any(|callback| {
            let key = match callback {
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => *key,
                _ => return false,
            };
            self.active.contains(&(key, self.current_top_owner))
        });
        for callback in callbacks.clone() {
            if reentrant_pool
                || callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL
                || self.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
                || self.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            {
                // A recursive member already falls back to a closure over the
                // entire pool in its arguments. Summarize that pool once rather
                // than enumerate permutations of callbacks around the cycle.
                // The same batch fallback bounds exhausted acyclic expansions.
                let nested = self.bounded_invocation_fallback(
                    self.current_top_owner
                        .expect("callback call has a top owner"),
                    std::slice::from_ref(&callbacks),
                    &Context::new(),
                );
                self.apply_rebind_effects(&nested.effects.rebinds);
                outcome.effects.reads.extend(nested.effects.reads);
                outcome.effects.rebinds.extend(nested.effects.rebinds);
                outcome
                    .effects
                    .uninitialized_reads
                    .extend(nested.effects.uninitialized_reads);
                outcome.executed_owners.extend(nested.executed_owners);
                outcome.unknown_call |= nested.unknown_call;
                outcome.complete &= nested.complete;
                break;
            }
            let nested = match callback {
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    continue;
                }
                AbstractValue::DeferredCallbacks(captured) => {
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    continue;
                }
                AbstractValue::Origin(ValueOrigin::Function(key)) => {
                    self.invoke(key, args_for(key, self.index), None, false)
                }
                AbstractValue::Closure(key, captured) => {
                    self.invoke(key, args_for(key, self.index), Some(captured), false)
                }
                _ => continue,
            };
            self.apply_rebind_effects(&nested.effects.rebinds);
            outcome.effects.reads.extend(nested.effects.reads);
            outcome.effects.rebinds.extend(nested.effects.rebinds);
            outcome
                .effects
                .uninitialized_reads
                .extend(nested.effects.uninitialized_reads);
            outcome.executed_owners.extend(nested.executed_owners);
            outcome.unknown_call |= nested.unknown_call;
            outcome.complete &= nested.complete;
        }
        // An opaque callee can retain any callback passed to it and invoke it
        // during a later prefix event.
        let before = self.global_escaped_callbacks.len();
        let values = args.iter().flatten().cloned().collect();
        self.taint_objects(&values);
        self.global_escaped_callbacks
            .extend(self.callback_values(&values));
        if self.global_escaped_callbacks.len() != before {
            self.prefix_revision = self.prefix_revision.wrapping_add(1);
        }
    }

    fn apply_rebind_effects(&mut self, rebinds: &BTreeSet<(Id, StatementOrdinal)>) {
        for (id, _) in rebinds {
            if !self.top_bindings.contains(id) {
                continue;
            }
            let changed = self
                .prefix_values
                .entry(id.clone())
                .or_default()
                .insert(AbstractValue::Unknown);
            if changed {
                self.prefix_revision = self.prefix_revision.wrapping_add(1);
            }
        }
    }

    fn bounded_invocation_fallback(
        &mut self,
        owner: StatementOrdinal,
        args: &[Values],
        captured: &Context,
    ) -> Outcome {
        let effect_owner = self.current_top_owner.unwrap_or(owner);
        if self.array_iteration_depth > 0 {
            self.array_iteration_unsupported = true;
        }
        // Within one source prefix initialization is fixed. Keep a monotone
        // summary so each reachable owner's lazy effects are promoted once,
        // even when recursion or changing abstract arguments hit the bound
        // repeatedly. Reusing earlier fallback roots only adds constraints.
        let mut outcome = std::mem::take(&mut self.fallback_outcome);
        let rebinds_before = outcome.effects.rebinds.len();
        outcome.complete = false;
        outcome.unknown_call = true;
        outcome.executed_owners.insert(owner);

        // The recursive summary is no longer being expanded. Preserve reads
        // from this callable and callbacks actually present in its arguments,
        // captures, exports, or escaped globals. Do not turn ordinary
        // recursion into a whole-chunk reachability root.
        let mut possible_values: Values = args.iter().flatten().cloned().collect();
        possible_values.extend(captured.values().flatten().cloned());
        possible_values.extend(self.global_escaped_callbacks.iter().cloned());
        possible_values.extend(self.available_export_callbacks());
        let callbacks = self.callback_values(&possible_values);
        let mut roots = BTreeSet::new();
        for callback in &callbacks {
            let key = match callback {
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => Some(*key),
                _ => None,
            };
            if let Some(callback_owner) =
                key.and_then(|key| self.index.callables.get(&key).map(|c| c.owner))
            {
                roots.insert(callback_owner);
            }
            if let AbstractValue::ClassMembers(key) = callback
                && let Some(&owner) = self.flow.callable_owner.get(&ValueOrigin::Class(*key))
            {
                roots.insert(owner);
            }
        }
        self.extend_reachable_owners(roots.iter().copied().chain([owner]));
        let mut reached = FactsOwnerReadClosure::extend_synchronous_owners(
            &self.synchronous_owner_adjacency,
            roots.iter().copied(),
            &mut self.callback_fallback.expanded,
        );
        if self.reachability.incomplete {
            // Dynamic lexical scope defeats the normal binding-read closure.
            // Revisit even previously expanded owners: their cached prefix
            // effects may have omitted reads that eval or with can reach.
            if !self.callback_fallback.incomplete_expanded {
                self.callback_fallback.incomplete_expanded = true;
                for fact in self.facts {
                    self.callback_fallback.expanded.insert(fact.ordinal);
                    reached.push(fact.ordinal);
                }
            }
        }
        for reached_owner in reached {
            let fact = &self.facts[reached_owner.0];
            let reads = if self.reachability.incomplete {
                &fact.reads.lazy
            } else {
                self.synchronous_owner_reads[reached_owner.0].unwrap_or(&fact.reads.lazy)
            };
            for id in reads.iter().filter(|id| self.top_bindings.contains(*id)) {
                if self.callback_fallback.reads.insert(id.clone()) {
                    self.callback_fallback.read_additions.push(id.clone());
                }
            }
            for id in fact
                .rebinds
                .lazy
                .iter()
                .filter(|id| self.top_bindings.contains(*id))
            {
                if self.callback_fallback.rebinds.insert(id.clone()) {
                    self.callback_fallback.rebind_additions.push(id.clone());
                }
            }
        }
        let callback_sizes = (
            self.callback_fallback.reads.len(),
            self.callback_fallback.rebinds.len(),
        );
        if let Some((reads_before, rebinds_before)) = self.fallback_callback_sizes {
            // New escaped roots can appear during a statement. Apply only
            // their newly discovered IDs, rather than repeatedly reapplying
            // the entire growing closure at the same initialization prefix.
            let reads = self.callback_fallback.read_additions[reads_before..]
                .iter()
                .cloned()
                .collect();
            let rebinds = self.callback_fallback.rebind_additions[rebinds_before..]
                .iter()
                .cloned()
                .collect();
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
        } else {
            self.promote_lazy_effects(
                &self.callback_fallback.reads,
                &self.callback_fallback.rebinds,
                effect_owner,
                &mut outcome,
            );
        }
        self.fallback_callback_sizes = Some(callback_sizes);
        // A directly truncated callable need not escape. Follow its same
        // synchronous prefix graph without publishing private roots into the
        // persistent callback cache. Expand each local owner once per source
        // statement; earlier promotions remain in fallback_outcome.
        if !self.callback_fallback.expanded.contains(&owner) {
            let reached = FactsOwnerReadClosure::extend_synchronous_owners(
                &self.synchronous_owner_adjacency,
                [owner],
                &mut self.fallback_local_owners,
            );
            for reached_owner in reached {
                let fact = &self.facts[reached_owner.0];
                let reads = self.synchronous_owner_reads[reached_owner.0]
                    .unwrap_or(&fact.reads.lazy)
                    .difference(&self.callback_fallback.reads)
                    .cloned()
                    .collect();
                let rebinds = fact
                    .rebinds
                    .lazy
                    .difference(&self.callback_fallback.rebinds)
                    .cloned()
                    .collect();
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
            }
        }

        // The truncated body may have passed one of these callback values to
        // opaque code. Retain only this proven escape set for later calls.
        let before = self.global_escaped_callbacks.len();
        self.global_escaped_callbacks.extend(callbacks);
        if self.global_escaped_callbacks.len() != before {
            self.prefix_revision = self.prefix_revision.wrapping_add(1);
        }
        if outcome.effects.rebinds.len() != rebinds_before {
            self.apply_rebind_effects(&outcome.effects.rebinds);
        }
        // Fallback effects are attributed to the active top-level statement,
        // not the lexical callee. Accumulate them there once instead of copying
        // and re-merging the whole summary through every nested invocation.
        self.fallback_outcome = outcome;
        Outcome {
            returns: unknown(),
            complete: false,
            unknown_call: true,
            executed_owners: BTreeSet::from([owner]),
            ..Outcome::default()
        }
    }

    fn evaluate(&mut self, expr: &ValueExpr, env: &Context, captures: &Context) -> Values {
        match expr {
            ValueExpr::Binding(id) => {
                if self.index.callback_factory_imports.contains(id) {
                    return BTreeSet::from([AbstractValue::CallbackFactory]);
                }
                if self.index.inline_callback_factory_imports.contains(id) {
                    return BTreeSet::from([AbstractValue::InlineCallbackFactory]);
                }
                if self.top_bindings.contains(id) {
                    if let Some(prefix_values) = self.prefix_values.get(id) {
                        let mut values = prefix_values.clone();
                        if !self.initialized_bindings.contains(id) {
                            values.insert(if self.var_bindings.contains(id) {
                                AbstractValue::Undefined
                            } else {
                                AbstractValue::Unavailable
                            });
                        }
                        values
                    } else if self.var_bindings.contains(id) {
                        BTreeSet::from([AbstractValue::Undefined])
                    } else {
                        // Whole-chunk flow includes future initializers. A
                        // missing prefix value for a local binding is TDZ.
                        BTreeSet::from([AbstractValue::Unavailable])
                    }
                } else if let Some(values) = env.get(id) {
                    values.clone()
                } else if self.index.global_object_names.contains(id.0.as_ref())
                    && !self.flow.is_declared(id)
                    && !self.shadowed.contains(id.0.as_ref())
                {
                    BTreeSet::from([AbstractValue::GlobalObject])
                } else {
                    let values = from_flow(self.flow.values_of_binding(id));
                    if values.is_empty() { unknown() } else { values }
                }
            }
            ValueExpr::Origin(ValueOrigin::Function(key)) => {
                BTreeSet::from([AbstractValue::Closure(
                    *key,
                    self.closure_captures(*key, captures),
                )])
            }
            ValueExpr::Origin(origin) => BTreeSet::from([AbstractValue::Origin(*origin)]),
            ValueExpr::ObjectLiteral(key, properties) => {
                if self.index.exact_object_spread_copies.contains(key)
                    && let [(None, source)] = properties.as_slice()
                {
                    let source = self.evaluate(source, env, captures);
                    if let Some(properties) = self.ordinary_data_properties(&source) {
                        let previously_evaluated = self.ordinary_spread_copies.contains(key);
                        let current = self.prefix_object_properties.entry(*key).or_default();
                        let mut changed = false;
                        if previously_evaluated {
                            for (property, values) in current.iter_mut() {
                                if !properties.contains_key(property) {
                                    changed |= values.insert(AbstractValue::Undefined);
                                }
                            }
                        }
                        for (property, values) in properties {
                            let new_property = !current.contains_key(&property);
                            let slot = current.entry(property).or_default();
                            let before = slot.len();
                            if previously_evaluated && new_property {
                                slot.insert(AbstractValue::Undefined);
                            }
                            slot.extend(values);
                            changed |= slot.len() != before;
                        }
                        if changed {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                        self.ordinary_spread_copies.insert(*key);
                        return BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(*key))]);
                    }
                    self.tainted_objects.insert(*key);
                    if self.array_iteration_depth > 0 {
                        self.array_iteration_unsupported = true;
                    }
                }
                if self.index.array_elements.contains_key(key) {
                    return self.evaluate_array(*key, env, captures);
                }
                let mut evaluated = BTreeMap::<Option<String>, Values>::new();
                let mut may_throw = false;
                let mut can_complete = true;
                for (property, expression) in properties {
                    let mut values = self.evaluate(expression, env, captures);
                    if values.remove(&AbstractValue::Unavailable) {
                        may_throw = true;
                        can_complete &= !values.is_empty();
                    }
                    evaluated
                        .entry(property.clone())
                        .or_default()
                        .extend(values);
                }
                if can_complete {
                    let current = self.prefix_object_properties.entry(*key).or_default();
                    let mut changed = false;
                    for (property, values) in evaluated {
                        let current_values = current.entry(property).or_default();
                        let before = current_values.len();
                        current_values.extend(values);
                        changed |= current_values.len() != before;
                    }
                    if changed {
                        self.prefix_revision = self.prefix_revision.wrapping_add(1);
                    }
                }
                let mut result = Values::new();
                if can_complete {
                    result.insert(AbstractValue::Origin(ValueOrigin::Object(*key)));
                }
                if may_throw || !can_complete {
                    result.insert(AbstractValue::Unavailable);
                }
                result
            }
            ValueExpr::Undefined => BTreeSet::from([AbstractValue::Undefined]),
            ValueExpr::NonCallable => BTreeSet::from([AbstractValue::NonCallable]),
            ValueExpr::Member(object, property, is_static_identifier) => {
                if let ValueExpr::Binding(id) = object.as_ref()
                    && *is_static_identifier
                    && property.as_ref().is_some_and(|property| {
                        self.index
                            .callback_factory_members
                            .get(id)
                            .is_some_and(|members| members.contains(property))
                    })
                {
                    return BTreeSet::from([AbstractValue::CallbackFactory]);
                }
                if matches!(object.as_ref(), ValueExpr::Binding(id) if id.0.as_ref() == "Object" && !self.flow.is_declared(id))
                    && property.as_deref() == Some("assign")
                    && !self.shadowed.contains("Object")
                {
                    return BTreeSet::from([AbstractValue::ObjectAssign]);
                }
                let objects = self.evaluate(object, env, captures);
                let mut values = Values::new();
                for object in objects {
                    match object {
                        AbstractValue::ArrayIntrinsic => {
                            values.insert(AbstractValue::ArrayIntrinsic);
                        }
                        AbstractValue::GlobalObject => {
                            values.extend(self.global_escaped_callbacks.iter().cloned());
                            values.insert(AbstractValue::Unknown);
                        }
                        AbstractValue::Unavailable => {
                            values.insert(AbstractValue::Unavailable);
                        }
                        AbstractValue::DeferredCallbacks(captured) => {
                            values.insert(AbstractValue::DeferredCallbacks(captured));
                        }
                        AbstractValue::Origin(ValueOrigin::Class(key))
                        | AbstractValue::ClassMembers(key) => {
                            values.insert(AbstractValue::ClassMembers(key));
                        }
                        AbstractValue::Instance(key, captured) => {
                            if matches!(property.as_deref(), Some("constructor" | "__proto__")) {
                                self.tainted_classes.insert(key);
                            }
                            values.insert(AbstractValue::Instance(key, captured));
                        }
                        AbstractValue::Origin(ValueOrigin::Object(object_id)) => {
                            if self.dense_arrays.contains_key(&object_id)
                                && (property.is_none()
                                    || matches!(
                                        property.as_deref(),
                                        Some("constructor" | "__proto__")
                                    ))
                            {
                                values.insert(AbstractValue::ArrayIntrinsic);
                            }

                            if let Some(properties) = self.prefix_object_properties.get(&object_id)
                            {
                                if let Some(property) = property {
                                    if let Some(found) = properties.get(&Some(property.clone())) {
                                        values.extend(found.iter().cloned());
                                    } else {
                                        values.insert(AbstractValue::Unknown);
                                    }
                                    values.extend(
                                        properties
                                            .get(&None)
                                            .into_iter()
                                            .flat_map(|found| found.iter().cloned()),
                                    );
                                } else {
                                    values.extend(
                                        properties.values().flat_map(|found| found.iter().cloned()),
                                    );
                                }
                            } else if let Some(property) = property {
                                if let Some(found) =
                                    self.flow.values_of_property(object_id, property)
                                {
                                    values.extend(from_flow(found.clone()));
                                } else {
                                    values.insert(AbstractValue::Unknown);
                                }
                            } else {
                                values.insert(AbstractValue::Unknown);
                            }
                        }
                        _ => {
                            values.insert(AbstractValue::Unknown);
                        }
                    }
                }
                values
            }
            ValueExpr::Call(callee, args, span) => {
                if let Some((proof, _, _, _, _)) = self.array_iteration_plan(*span, env, captures) {
                    return if proof.method == ArrayIterationMethod::ForEach {
                        BTreeSet::from([AbstractValue::Undefined])
                    } else {
                        BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(
                            proof.result.unwrap(),
                        ))])
                    };
                }
                let targets = self.evaluate(callee, env, captures);
                if targets.len() == 1 && targets.contains(&AbstractValue::Unavailable) {
                    return BTreeSet::from([AbstractValue::Unavailable]);
                }
                let inline_callback = matches!(
                    args.as_slice(),
                    [ValueExpr::Origin(ValueOrigin::Function(_))]
                );
                let mut args: Vec<Values> = args
                    .iter()
                    .map(|arg| self.evaluate(arg, env, captures))
                    .collect();
                let args_may_throw = args
                    .iter()
                    .any(|values| values.contains(&AbstractValue::Unavailable));
                if args
                    .iter()
                    .any(|values| values.len() == 1 && values.contains(&AbstractValue::Unavailable))
                {
                    return BTreeSet::from([AbstractValue::Unavailable]);
                }
                for values in &mut args {
                    values.remove(&AbstractValue::Unavailable);
                }
                if let Some(callbacks) = self.proven_from_entries_callbacks(*span, env, captures) {
                    return BTreeSet::from([self.deferred_callbacks(callbacks)]);
                }
                let deferred_callback = matches!(args.as_slice(), [values] if !values.is_empty()
                    && values.iter().all(|value| matches!(value, AbstractValue::DeferredCallbacks(_))));
                let mut returns = Values::new();
                let no_targets = targets.is_empty();
                let may_throw = args_may_throw
                    || no_targets
                    || targets.iter().any(|target| {
                        matches!(
                            target,
                            AbstractValue::Unavailable
                                | AbstractValue::Undefined
                                | AbstractValue::NonCallable
                        )
                    });
                for target in targets {
                    match target {
                        AbstractValue::CallbackFactory if inline_callback || deferred_callback => {
                            let callbacks = self.callback_values(&args[0]);
                            returns.insert(self.deferred_callbacks(callbacks));
                        }
                        AbstractValue::InlineCallbackFactory if inline_callback => {
                            let callbacks = self.callback_values(&args[0]);
                            returns.insert(self.deferred_callbacks(callbacks));
                        }
                        AbstractValue::DeferredCallbacks(captured) => {
                            returns.insert(AbstractValue::DeferredCallbacks(captured));
                            returns.insert(AbstractValue::Unknown);
                        }
                        AbstractValue::Unavailable
                        | AbstractValue::Undefined
                        | AbstractValue::NonCallable => {}
                        AbstractValue::Origin(ValueOrigin::Function(key)) => {
                            returns.extend(self.invoke(key, args.clone(), None, false).returns);
                        }
                        AbstractValue::Closure(key, captured) => {
                            returns.extend(
                                self.invoke(key, args.clone(), Some(captured), false)
                                    .returns,
                            );
                        }
                        AbstractValue::ObjectAssign => {
                            returns.extend(args.first().cloned().unwrap_or_else(unknown));
                        }
                        AbstractValue::Unknown => {
                            returns.insert(AbstractValue::Unknown);
                        }
                        _ => {
                            returns.insert(AbstractValue::Unknown);
                        }
                    }
                }
                if returns.is_empty() && may_throw {
                    return BTreeSet::from([AbstractValue::Unavailable]);
                }
                if may_throw {
                    returns.insert(AbstractValue::Unavailable);
                }
                returns
            }
            ValueExpr::New(callee, args) => {
                let targets = self.evaluate(callee, env, captures);
                let evaluated_args: Vec<_> = args
                    .iter()
                    .map(|arg| self.evaluate(arg, env, captures))
                    .collect();
                let mut results = Values::new();
                if self.current_top_owner.is_some_and(|owner| {
                    self.is_proven_map_entry_array(callee, args, env, captures, owner)
                }) {
                    let mut can_complete = true;
                    let mut may_throw = false;
                    let mut successful_args = Values::new();
                    for values in &evaluated_args {
                        if values.contains(&AbstractValue::Unavailable) {
                            may_throw = true;
                            can_complete &= values.len() > 1;
                        }
                        successful_args.extend(
                            values
                                .iter()
                                .filter(|value| **value != AbstractValue::Unavailable)
                                .cloned(),
                        );
                    }
                    if can_complete {
                        let callbacks = self.callback_values(&successful_args);
                        results.insert(self.deferred_callbacks(callbacks));
                    }
                    if may_throw || !can_complete {
                        results.insert(AbstractValue::Unavailable);
                    }
                    return results;
                }
                for target in targets {
                    match target {
                        AbstractValue::Origin(ValueOrigin::Class(key)) => {
                            if let Some((_, instance)) =
                                self.construct_local_class(key, &evaluated_args)
                            {
                                results.insert(instance);
                            } else {
                                results.insert(AbstractValue::Unknown);
                            }
                        }
                        AbstractValue::Unavailable => {
                            results.insert(AbstractValue::Unavailable);
                        }
                        _ => {
                            results.insert(AbstractValue::Unknown);
                        }
                    }
                }
                if results.is_empty() {
                    unknown()
                } else {
                    results
                }
            }
            ValueExpr::Primitive(operands) => {
                let mut can_complete = true;
                let mut may_throw = false;
                for operand in operands {
                    let mut values = self.evaluate(operand, env, captures);
                    if values.remove(&AbstractValue::Unavailable) {
                        may_throw = true;
                    }
                    if values.is_empty() {
                        can_complete = false;
                        break;
                    }
                }
                let mut results = Values::new();
                if can_complete {
                    results.insert(AbstractValue::NonCallable);
                }
                if may_throw || !can_complete {
                    results.insert(AbstractValue::Unavailable);
                }
                results
            }
            ValueExpr::Alternatives(options) => options
                .iter()
                .flat_map(|option| self.evaluate(option, env, captures))
                .collect(),
            ValueExpr::Unknown => unknown(),
        }
    }

    fn invoke(
        &mut self,
        key: u32,
        args: Vec<Values>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let Some((callable_owner, callable_opaque, uses_arguments)) =
            self.index.callables.get(&key).map(|callable| {
                (
                    callable.owner,
                    callable.opaque,
                    callable.reads.iter().any(|id| id.0.as_ref() == "arguments"),
                )
            })
        else {
            return Outcome::default();
        };
        let unsupported_inputs = uses_arguments
            || self.index.callables[&key]
                .params
                .iter()
                .any(|parameter| parameter.binding.is_none());
        // Implicit arguments can alias any actual input even without a
        // corresponding formal parameter. Invalidate input capabilities before
        // consulting cached outcomes, including calls outside array iteration.
        if unsupported_inputs {
            for values in &args {
                self.taint_objects(values);
            }
            if self.array_iteration_depth > 0 {
                self.array_iteration_unsupported = true;
            }
        }
        let empty_context = Context::new();
        let captured_context = captured
            .as_ref()
            .map_or(&empty_context, |captured| captured.values.as_ref());
        self.record_executed_owner(callable_owner);
        if callable_opaque {
            self.note_unknown_execution(callable_owner);
        }
        let signature = (
            key,
            args.clone(),
            captured.clone(),
            fresh_checked_constructor,
            self.reachability.revision,
            self.prefix_revision,
            self.current_top_owner,
        );
        if self.array_iteration_depth == 0
            && let Some(cached) = self.cache.get(&signature)
        {
            return cached.clone();
        }
        let active_invocation = (key, self.current_top_owner);
        if self.active.contains(&active_invocation)
            || self.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            || self.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
        {
            return self.bounded_invocation_fallback(callable_owner, &args, captured_context);
        }
        self.active.insert(active_invocation);
        self.invocation_depth += 1;
        self.invocation_count += 1;
        let Some(callable) = self.index.callables.get(&key) else {
            self.active.remove(&active_invocation);
            self.invocation_depth -= 1;
            return Outcome::default();
        };
        let mut env = captured_context.clone();
        for (index, param) in callable.params.iter().enumerate() {
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| BTreeSet::from([AbstractValue::Undefined]));
            if let Some(default) = &param.default
                && values.contains(&AbstractValue::Undefined)
            {
                values.remove(&AbstractValue::Undefined);
                values.extend(self.evaluate(default, &env, &env));
            }
            if let Some(binding) = &param.binding {
                env.insert(binding.clone(), values);
            }
        }
        let unsupported_before = self.array_iteration_unsupported;
        if self.array_iteration_depth > 0 {
            if callable.reads.iter().any(|id| id.0.as_ref() == "arguments") {
                self.array_iteration_unsupported = true;
            }
            if let Some(reads) = self.index.callable_member_reads.get(&key) {
                for (receiver, property) in reads.clone() {
                    let values = self.evaluate(&receiver, &env, &env);
                    if property.is_none()
                        || values.is_empty()
                        || values.iter().any(|value| match value {
                            AbstractValue::NonCallable | AbstractValue::Undefined => false,
                            AbstractValue::Origin(ValueOrigin::Object(object)) => {
                                (!self.index.plain_data_objects.contains(object)
                                    && !self.ordinary_spread_copies.contains(object)
                                    && !self.dense_arrays.contains_key(object))
                                    || self.tainted_objects.contains(object)
                                    || !self.prefix_object_properties.get(object).is_some_and(
                                        |properties| properties.contains_key(&property),
                                    )
                            }
                            _ => true,
                        })
                    {
                        self.array_iteration_unsupported = true;
                    }
                }
            }
        }
        for (id, value) in &callable.local_assignments {
            let values = self.evaluate(value, &env, &env);
            env.entry(id.clone()).or_default().extend(values);
        }
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        outcome.executed_owners.insert(callable.owner);
        if callable.generator {
            // Generator parameter initializers run at call time, although
            // the body waits for the first iteration. Their effects are not
            // separated in the index, so retain the conservative path.
            outcome.complete = false;
            self.active.remove(&active_invocation);
            self.invocation_depth -= 1;
            self.cache.insert(signature, outcome.clone());
            return outcome;
        }
        outcome.complete &= !callable.opaque;
        for id in &callable.reads {
            if self.top_bindings.contains(id) {
                outcome.effects.reads.insert((id.clone(), callable.owner));
                if !self.initialized_bindings.contains(id) {
                    outcome.effects.uninitialized_reads.insert(id.clone());
                }
            }
        }
        for id in &callable.rebinds {
            if self.top_bindings.contains(id) {
                outcome.effects.rebinds.insert((id.clone(), callable.owner));
            }
        }
        // The visitor stores value-bearing member writes separately from call
        // nodes. Make same-owner callback publication visible before nested
        // opaque calls; if source order put a write afterward, this only adds
        // a conservative callback possibility.
        let mut certified_data_receivers = self.fresh_primitive_write_receivers(callable, &env);
        self.apply_member_value_writes(
            &callable.member_value_writes,
            &env,
            &env,
            &certified_data_receivers,
        );
        // local_constructor checked every assignment as a static property
        // write to a fresh `this`, with a safe right-hand value and no
        // prototype getter collision. That receiver cannot publish a stored
        // callback or invoke a setter. The generic lanes represent `this` as
        // unknown and would otherwise promote lazy callback bodies after any
        // earlier opaque call.
        if !fresh_checked_constructor {
            self.promote_callback_result_writes(
                &callable.callback_result_writes,
                &env,
                &env,
                callable.owner,
                &mut outcome,
                &certified_data_receivers,
            );
        }
        for call in &callable.calls {
            self.execute_call(call, &env, &env, &mut outcome);
        }
        if !certified_data_receivers.is_empty()
            && (self.array_iteration_unsupported
                || !self.array_intrinsics_intact()
                || certified_data_receivers
                    .iter()
                    .any(|object| self.tainted_objects.contains(object)))
        {
            self.tainted_objects
                .extend(certified_data_receivers.iter().copied());
            certified_data_receivers.clear();
        }
        for expr in &callable.returns {
            outcome.returns.extend(self.evaluate(expr, &env, &env));
        }
        if callable.may_fall_through {
            outcome.returns.insert(AbstractValue::Undefined);
        }

        let mut writes: Vec<_> = callable
            .member_writes
            .iter()
            .filter(|(receiver, _)| !self.stable_fresh_function_target(key, callable, receiver))
            .map(|(receiver, property)| (Some(receiver.clone()), Some(property.clone())))
            .collect();
        if callable.dynamic_property_effect {
            writes.push((None, None));
        }
        if !fresh_checked_constructor
            && !self.fresh_mutation_callables.contains(&key)
            && certified_data_receivers.is_empty()
        {
            self.promote_property_writes(&writes, callable.owner, &mut outcome);
        }
        if unsupported_inputs
            || (self.array_iteration_depth > 0
                && self.array_iteration_unsupported
                && !unsupported_before)
        {
            let fact = &self.facts[callable.owner.0];
            self.promote_lazy_effects(
                &fact.reads.lazy,
                &fact.rebinds.lazy,
                self.current_top_owner.unwrap_or(callable.owner),
                &mut outcome,
            );
            outcome.complete = false;
        }
        self.active.remove(&active_invocation);
        self.invocation_depth -= 1;
        self.cache.insert(signature, outcome.clone());
        outcome
    }

    fn execute_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) {
        if matches!(
            call.kind,
            CallKind::InProtocol
                | CallKind::InstanceOfProtocol
                | CallKind::DeepDestructuringProtocol
        ) {
            let mut operands: Vec<Values> = call
                .args
                .iter()
                .map(|argument| self.evaluate(argument, env, captures))
                .collect();
            if call.kind == CallKind::InProtocol
                && self.ordinary_prototype_chain(&operands[0])
                && operands[0]
                    .iter()
                    .all(|value| matches!(value, AbstractValue::Origin(ValueOrigin::Object(_))))
            {
                return;
            }
            if call.kind == CallKind::InstanceOfProtocol
                && self.default_class_has_instance(&operands[0])
            {
                // OrdinaryHasInstance consults the LHS prototype chain, never
                // the constructor's deferred method bodies.
                if self.ordinary_prototype_chain(&operands[1]) {
                    return;
                }
                operands.remove(0);
            }
            let values: Values = operands.into_iter().flatten().collect();
            self.expose_class_capabilities(&values);
            self.taint_objects(&values);
            self.promote_raw_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[values], false, outcome);
            self.note_unknown_call(call);
            outcome.complete = false;
            outcome.unknown_call = true;
            return;
        }
        if call.kind == CallKind::LocalMemberRead {
            let ValueExpr::Member(receiver, property, _) = &call.callee else {
                unreachable!()
            };
            let values = self.evaluate(receiver, env, captures);
            let unsupported_local = values.iter().any(|value| match value {
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    self.tainted_objects.contains(object)
                        || ((!self.index.plain_data_objects.contains(object)
                            && !self.ordinary_spread_copies.contains(object)
                            && !self.dense_arrays.contains_key(object))
                            || !self.prefix_object_properties.get(object).is_some_and(
                                |properties| {
                                    property.is_some() && properties.contains_key(property)
                                },
                            ))
                }
                AbstractValue::Instance(key, _) => {
                    self.tainted_classes.contains(key)
                        || !property.as_ref().is_some_and(|property| {
                            self.index.class_values.get(key).is_some_and(|class| {
                                class.ordinary_instance_methods.contains(property)
                                    || class
                                        .instance_fields
                                        .get(property)
                                        .is_some_and(|value| !matches!(value, ValueExpr::Unknown))
                            }) || self.index.local_constructors.get(key).is_some_and(
                                |constructor| constructor.own_fields.contains(property),
                            )
                        })
                }
                _ => false,
            });
            if unsupported_local {
                self.taint_objects(&values);
                let selected = self.evaluate(&call.callee, env, captures);
                self.promote_raw_callbacks(&selected, outcome);
                if self.array_iteration_depth > 0 {
                    self.array_iteration_unsupported = true;
                }
                outcome.complete = false;
            }
            return;
        }
        if call.kind == CallKind::ObjectSpread {
            let values = self.evaluate(&call.args[0], env, captures);
            if self.ordinary_data_properties(&values).is_some() {
                return;
            }
            if self.array_iteration_depth > 0 {
                self.array_iteration_unsupported = true;
            }
            outcome.complete = false;
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.promote_raw_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[values], false, outcome);
            return;
        }
        if call.kind == CallKind::ArraySpread {
            let values = self.evaluate(&call.args[0], env, captures);
            if self.dense_source(&values).is_some() {
                return;
            }
            outcome.complete = false;
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            // Consuming a custom iterator can execute deferred generator bodies.
            self.promote_raw_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[values], false, outcome);
            return;
        }
        if call.kind == CallKind::ImplicitCoercion {
            let values = call
                .args
                .first()
                .map(|argument| self.evaluate(argument, env, captures))
                .unwrap_or_else(unknown);
            if values.iter().all(|value| {
                matches!(
                    value,
                    AbstractValue::NonCallable
                        | AbstractValue::Undefined
                        | AbstractValue::Unavailable
                )
            }) {
                return;
            }
            let mut possible_values = values;
            possible_values.remove(&AbstractValue::Unavailable);
            if possible_values.is_empty() {
                return;
            }
            // ToPrimitive may call an own [Symbol.toPrimitive], valueOf, or
            // toString method. Treat that protocol step like an opaque
            // synchronous call and expose callbacks carried by the operand;
            // this covers both the exotic and ordinary conversion paths
            // without assuming which method succeeds first.
            outcome.complete = false;
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            // A conversion property may be an accessor that returns a
            // conversion function. Promote the operand's retained callback
            // closure as well as invoking its directly visible callbacks so
            // effects in such returned functions remain visible.
            self.promote_deferred_callbacks(&possible_values, outcome);
            self.invoke_possible_callbacks(&[possible_values], false, outcome);
            return;
        }
        // Only a fresh primitive literal or a checked const alias to one is
        // safe to omit from callback effects. A pure object/function element
        // can be retained by the Set and invoked by a later opaque consumer.
        if call.kind == CallKind::New
            && call.caller.is_none()
            && self.set_iterable_proof.contains_constructor(call.span)
        {
            return;
        }
        // Empty intrinsic collections return before consulting an adder or
        // iterator. This proves only their construction has no callbacks;
        // later operations and any constructor arguments remain conservative.
        if call.kind == CallKind::New
            && call.args.is_empty()
            && matches!(&call.callee, ValueExpr::Binding(id)
                if matches!(id.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet")
                    && !self.flow.is_declared(id)
                    && !self.shadowed.contains(id.0.as_ref()))
        {
            return;
        }
        let targets = self.evaluate(&call.callee, env, captures);
        if targets.len() == 1 && targets.contains(&AbstractValue::Unavailable) {
            return;
        }
        let mut args: Vec<Values> = call
            .args
            .iter()
            .map(|arg| self.evaluate(arg, env, captures))
            .collect();
        if args
            .iter()
            .any(|values| values.len() == 1 && values.contains(&AbstractValue::Unavailable))
        {
            return;
        }
        for values in &mut args {
            values.remove(&AbstractValue::Unavailable);
        }
        // Distinct author assertions cover primitive and fresh-record inputs.
        // Both use the same member identity and lookup guards. The provider
        // may have private effects; this only suppresses callback reentry.
        if call.kind == CallKind::Call
            && let ValueExpr::Member(receiver, Some(property), true) = &call.callee
            && let ValueExpr::Binding(namespace) = receiver.as_ref()
            && ((call.no_reentry_candidate
                && self
                    .index
                    .no_reentry_members
                    .get(namespace)
                    .is_some_and(|members| members.contains(property)))
                || (call.no_reentry_record_candidate
                    && self
                        .index
                        .no_reentry_record_members
                        .get(namespace)
                        .is_some_and(|members| members.contains(property))))
        {
            return;
        }
        let from_entries_source_callbacks = if call.kind == CallKind::Call {
            if self
                .proven_from_entries_callbacks(call.span.into(), env, captures)
                .is_some()
            {
                return;
            }
            self.callbacks_in_from_entries_source(call.span.into(), env, captures)
        } else {
            None
        };
        if call.kind == CallKind::New
            && self.is_proven_map_entry_array(&call.callee, &call.args, env, captures, call.owner)
        {
            return;
        }
        if call.kind == CallKind::Call
            && self.execute_array_iteration(call.span.into(), env, captures, outcome)
        {
            return;
        }
        // Known local methods still receive an implicit `this` capability.
        // Its effects are not resolved by invoke, so it can mutate any retained
        // array reachable from the receiver before a later intrinsic iteration.
        if call.kind == CallKind::Call
            && let ValueExpr::Member(receiver, _, _) = &call.callee
        {
            let values = self.evaluate(receiver, env, captures);
            let contains_array = self.contains_dense_array(&values);
            self.taint_objects(&values);
            if contains_array {
                self.promote_raw_callbacks(&targets, outcome);
            }
        }
        let mut opaque_args = args.clone();
        if let Some(proof) = self.index.array_iterations.get(&call.span.into()) {
            let source = self.evaluate(&proof.source.clone(), env, captures);
            self.promote_raw_callbacks(&source, outcome);
            opaque_args.push(source);
        }

        if let Some(callbacks) = from_entries_source_callbacks {
            opaque_args.push(callbacks);
        }
        if targets.is_empty()
            || targets.iter().any(|target| {
                matches!(
                    target,
                    AbstractValue::Unknown
                        | AbstractValue::DeferredCallbacks(_)
                        | AbstractValue::ClassMembers(_)
                        | AbstractValue::Instance(_, _)
                )
            })
        {
            // Opaque calls can mutate objects passed as arguments or used as
            // method receivers. Preserve that possibility for later narrow
            // construction proofs (including changes to an entry's own data
            // properties or an array's iterator/species surface).
            let mut possible_objects: Values = args.iter().flatten().cloned().collect();
            if let ValueExpr::Member(receiver, _, _) = &call.callee {
                possible_objects.extend(self.evaluate(receiver, env, captures));
            }
            self.expose_class_capabilities(&possible_objects);
            self.taint_objects(&possible_objects);
        }
        let retained_callback = matches!(args.as_slice(), [values] if !values.is_empty()
            && values.iter().all(|value| matches!(value, AbstractValue::DeferredCallbacks(_))));
        if targets.is_empty() {
            outcome.complete = false;
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.invoke_possible_callbacks(&opaque_args, call.no_sync_arguments, outcome);
        }
        let mut invoked_unknown_callbacks = targets.is_empty();
        for target in targets {
            match target {
                AbstractValue::CallbackFactory
                    if call.kind == CallKind::Call
                        && !call.has_spread
                        && (retained_callback
                            || matches!(
                                call.args.as_slice(),
                                [ValueExpr::Origin(ValueOrigin::Function(_))]
                            )) => {}
                AbstractValue::InlineCallbackFactory
                    if call.kind == CallKind::Call
                        && !call.has_spread
                        && matches!(
                            call.args.as_slice(),
                            [ValueExpr::Origin(ValueOrigin::Function(_))]
                        ) => {}
                AbstractValue::DeferredCallbacks(captured) => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    self.invoke_possible_callbacks(&possible_args, false, outcome);
                }
                AbstractValue::ClassMembers(key) => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.promote_class_members(key, outcome);
                    self.invoke_possible_callbacks(&opaque_args, false, outcome);
                }
                AbstractValue::Instance(key, captured) => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.promote_class_members(key, outcome);
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.invoke_possible_callbacks(&possible_args, false, outcome);
                }
                AbstractValue::Unavailable
                | AbstractValue::Undefined
                | AbstractValue::NonCallable => {}
                AbstractValue::ObjectAssign
                    if call.kind == CallKind::Call
                        && call.plain_object_sources
                        && matches!(call.args.first(), Some(ValueExpr::Binding(id)) if call.caller
                        .and_then(|caller| self.index.callables.get(&caller).map(|body| (caller, body)))
                        .is_some_and(|(caller, body)| self.stable_fresh_function_target(caller, body, id))) =>
                    {}
                AbstractValue::ObjectAssign if call.kind == CallKind::Call => {
                    // Once the target's descriptor/prototype integrity is
                    // unproven, copying a property may synchronously call a
                    // setter. Include nested lazy bodies of owners that ran
                    // before this call, as well as the owners of the source
                    // and target values. The first-order fallback excludes
                    // those bodies because merely creating a closure does
                    // not execute it.
                    self.note_unknown_call(call);
                    let possible_objects = args.iter().flatten().cloned().collect();
                    self.taint_objects(&possible_objects);
                    let mut possible_accessors: BTreeSet<_> = self
                        .reachability
                        .owners
                        .iter()
                        .copied()
                        .filter(|candidate| self.may_be_available_accessor_owner(*candidate))
                        .collect();
                    possible_accessors.extend(outcome.executed_owners.iter().copied());
                    if self.reachability.incomplete {
                        possible_accessors.extend(self.all_owners());
                    }
                    let effect_owner = self.current_top_owner.unwrap_or(call.owner);
                    for accessor_owner in possible_accessors {
                        self.promote_lazy_owner(accessor_owner, effect_owner, outcome);
                    }
                    outcome.complete = false;
                    outcome.unknown_call = true;
                }
                AbstractValue::Origin(ValueOrigin::Class(key)) if call.kind == CallKind::New => {
                    if let Some((nested, _)) = (!call.has_spread)
                        .then(|| self.construct_local_class(key, &args))
                        .flatten()
                    {
                        self.apply_rebind_effects(&nested.effects.rebinds);
                        outcome.effects.reads.extend(nested.effects.reads);
                        outcome.effects.rebinds.extend(nested.effects.rebinds);
                        outcome
                            .effects
                            .uninitialized_reads
                            .extend(nested.effects.uninitialized_reads);
                        outcome.executed_owners.extend(nested.executed_owners);
                        outcome.unknown_call |= nested.unknown_call;
                        outcome.complete &= nested.complete;
                    } else {
                        outcome.complete = false;
                        outcome.unknown_call = true;
                        self.note_unknown_call(call);
                        self.escaped_class_capabilities.insert(key);
                        // Unsupported constructors still execute their
                        // instance fields and body at `new`, even without a
                        // callback argument to seed the opaque fallback.
                        self.promote_class_members(key, outcome);
                        if !invoked_unknown_callbacks {
                            self.invoke_possible_callbacks(&args, false, outcome);
                            invoked_unknown_callbacks = true;
                        }
                    }
                }
                AbstractValue::Origin(ValueOrigin::Function(key))
                    if call.kind == CallKind::Call =>
                {
                    let nested = self.invoke(key, args.clone(), None, false);
                    self.apply_rebind_effects(&nested.effects.rebinds);
                    outcome.effects.reads.extend(nested.effects.reads);
                    outcome.effects.rebinds.extend(nested.effects.rebinds);
                    outcome
                        .effects
                        .uninitialized_reads
                        .extend(nested.effects.uninitialized_reads);
                    outcome.executed_owners.extend(nested.executed_owners);
                    outcome.unknown_call |= nested.unknown_call;
                    outcome.complete &= nested.complete;
                }
                AbstractValue::Closure(key, captured) if call.kind == CallKind::Call => {
                    let nested = self.invoke(key, args.clone(), Some(captured), false);
                    self.apply_rebind_effects(&nested.effects.rebinds);
                    outcome.effects.reads.extend(nested.effects.reads);
                    outcome.effects.rebinds.extend(nested.effects.rebinds);
                    outcome
                        .effects
                        .uninitialized_reads
                        .extend(nested.effects.uninitialized_reads);
                    outcome.executed_owners.extend(nested.executed_owners);
                    outcome.unknown_call |= nested.unknown_call;
                    outcome.complete &= nested.complete;
                }
                _ => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    if !invoked_unknown_callbacks {
                        self.invoke_possible_callbacks(
                            &opaque_args,
                            call.no_sync_arguments,
                            outcome,
                        );
                        invoked_unknown_callbacks = true;
                    }
                }
            }
        }
    }
}

/// Prove the narrow return-only wrapper shape using the same execution index
/// as call promotion. Whole-chunk flow must contain exactly that function;
/// aliases with unknown/reassigned targets and dynamic scopes remain opaque.
pub(crate) fn infer_returned_callback_factories(
    body: &[TopLevelItemView<'_>],
    exported_locals: &BTreeSet<String>,
) -> BTreeSet<String> {
    let index = ExecutionIndex::collect(body, &AnalysisHints::default(), None, &BTreeSet::new());
    if !index.dynamic_scope_owners.is_empty() {
        return BTreeSet::new();
    }
    let candidates: Vec<_> = index
        .hoisted_bindings
        .iter()
        .filter(|(id, key)| {
            exported_locals.contains(id.0.as_ref())
                && index.callback_factory_functions.contains(key)
        })
        .collect();
    if candidates.is_empty() {
        return BTreeSet::new();
    }
    let flow = ChunkValueFlow::analyze(body);
    candidates
        .into_iter()
        .filter_map(|(id, key)| {
            let values = flow.values_of_binding(id);
            values
                .iter()
                .eq([ValueOrigin::Function(*key)])
                .then(|| id.0.to_string())
        })
        .collect()
}

pub(crate) struct SynchronousCallProofs<'a> {
    pub(crate) set_iterable: &'a SetIterableProof,
    pub(crate) graph: &'a ChunkCodeGraph,
}

pub(crate) fn annotate_synchronous_calls(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    proofs: SynchronousCallProofs<'_>,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
    export_reentry_policy: ExportReentryPolicy,
    facts: &mut [StatementFacts],
) {
    let index =
        ExecutionIndex::collect(body, hints, Some(flow), proofs.graph.plain_array_bindings());
    // Every top-level call is executed by `top_calls`, regardless of whether
    // its result is retained. Keep an initializer's value only if a later
    // call may read it, directly or through another initializer/function.
    // This avoids re-evaluating thousands of unused factory results.
    let mut needed: BTreeSet<Id> = index
        .callables
        .values()
        .flat_map(|callable| callable.reads.iter().cloned())
        .collect();
    for calls in &index.top_calls {
        for call in calls {
            call.callee.bindings(&mut needed);
            for arg in &call.args {
                arg.bindings(&mut needed);
            }
        }
    }
    // The class heritage reachability check consumes the current value of a
    // direct superclass binding. Keep that value even when no call or export
    // otherwise needs it; assignments and alternate values then follow the
    // same prefix tracking as every other binding.
    needed.extend(index.class_values.values().filter_map(|class| {
        if let Some(ValueExpr::Binding(binding)) = class.super_class.as_ref() {
            Some(binding.clone())
        } else {
            None
        }
    }));
    needed.extend(index.exported_bindings.iter().cloned());
    for exported in &index.top_exported_values {
        for expression in exported {
            expression.bindings(&mut needed);
        }
    }
    let mut dependencies: BTreeMap<Id, BTreeSet<Id>> = BTreeMap::new();
    for assignments in &index.top_assignments {
        for (id, expression) in assignments {
            expression.bindings(dependencies.entry(id.clone()).or_default());
        }
    }
    let mut member_write_receivers = BTreeMap::<ValueOrigin, BTreeSet<Id>>::new();
    for writes in &index.top_member_value_writes {
        for (receiver, _, expression) in writes {
            if index.global_object_names.contains(receiver.0.as_ref()) {
                // Global-object writes are potential callback escape roots at
                // every later opaque call, even when the receiver alias is
                // not mentioned by a statically visible callee.
                expression.bindings(&mut needed);
            } else {
                expression.bindings(dependencies.entry(receiver.clone()).or_default());
                // A later call may read an object through a different local
                // alias than the one used for this property write. Retain
                // that receiver's prefix value whenever its known object
                // identity also reaches a needed binding.
                for origin in flow.values_of_binding(receiver) {
                    if matches!(origin, ValueOrigin::Object(_)) {
                        member_write_receivers
                            .entry(origin)
                            .or_default()
                            .insert(receiver.clone());
                    }
                }
            }
        }
    }
    let mut pending: Vec<Id> = needed.iter().cloned().collect();
    let mut visited_objects = BTreeSet::new();
    while let Some(id) = pending.pop() {
        if let Some(inputs) = dependencies.get(&id) {
            for input in inputs {
                if needed.insert(input.clone()) {
                    pending.push(input.clone());
                }
            }
        }
        for origin in flow.values_of_binding(&id) {
            if !matches!(origin, ValueOrigin::Object(_)) || !visited_objects.insert(origin) {
                continue;
            }
            if let Some(receivers) = member_write_receivers.get(&origin) {
                for receiver in receivers {
                    if needed.insert(receiver.clone()) {
                        pending.push(receiver.clone());
                    }
                }
            }
        }
    }
    let owner_read_closure = FactsOwnerReadClosure::new(facts);
    let prefix_reads: Vec<_> = facts
        .iter()
        .map(|fact| {
            index.owner_callable_prefix_reads[fact.ordinal.0]
                .as_ref()
                .filter(|reads| {
                    !index.dynamic_scope_owners.contains(&fact.ordinal)
                        && !index.unindexed_accessor_owners.contains(&fact.ordinal)
                        && fact.reads.first_order_lazy.is_subset(reads)
                })
        })
        .collect();
    let synchronous_owner_adjacency =
        owner_read_closure.synchronous_adjacency(facts, &prefix_reads);
    let mut exported_owners = BTreeSet::new();
    if export_reentry_policy == ExportReentryPolicy::Conservative {
        exported_owners.extend(index.exported_declaration_owners.iter().copied());
        exported_owners.extend(owner_read_closure.owners_for_bindings(&index.exported_bindings));
    }
    let initial_export_roots = exported_owners
        .intersection(&index.hoisted_function_owners)
        .copied()
        .collect();
    let top_bindings: BTreeSet<_> = facts
        .iter()
        .flat_map(|fact| fact.declared.iter().cloned())
        .collect();
    let var_bindings: BTreeSet<_> = facts
        .iter()
        .flat_map(|fact| fact.var_declared.iter().cloned())
        .collect();
    let changed_member_roots: BTreeSet<_> = if index.top_metadata_decorations.iter().any(|call| {
        call.as_ref()
            .is_some_and(|call| !call.member_decorators.is_empty())
    }) {
        facts
            .iter()
            .flat_map(|fact| {
                fact.member_write_roots
                    .iter()
                    .chain(&fact.rebinds.eager)
                    .chain(&fact.rebinds.lazy)
                    .cloned()
            })
            .collect()
    } else {
        BTreeSet::new()
    };
    let mut initial_values = Context::new();
    let mut initialized_bindings = BTreeSet::new();
    for (binding, key) in &index.hoisted_bindings {
        initial_values.insert(
            binding.clone(),
            BTreeSet::from([AbstractValue::Origin(ValueOrigin::Function(*key))]),
        );
        initialized_bindings.insert(binding.clone());
    }
    let mut executor = Executor {
        index: &index,
        facts,
        flow,
        set_iterable_proof: proofs.set_iterable,
        shadowed,
        top_bindings,
        binding_owner: facts
            .iter()
            .flat_map(|fact| fact.declared.iter().cloned().map(|id| (id, fact.ordinal)))
            .collect(),
        owner_read_closure,
        synchronous_owner_adjacency,
        synchronous_owner_reads: prefix_reads,
        class_member_effects: BTreeMap::new(),
        exported_owners,
        prior_reachability_roots: Some(initial_export_roots),
        current_top_owner: None,
        prior_effect_owners: BTreeSet::new(),
        reachability: ReachabilityState::default(),
        mutable_captures: index
            .callables
            .values()
            .flat_map(|callable| callable.rebinds.iter().cloned())
            .collect(),
        prefix_values: initial_values,
        initialized_bindings,
        var_bindings,
        changed_member_roots,
        global_escaped_callbacks: Values::new(),
        prefix_object_properties: BTreeMap::new(),
        dense_arrays: BTreeMap::new(),
        ordinary_spread_copies: BTreeSet::new(),
        array_iteration_depth: 0,
        array_iteration_protected_objects: BTreeSet::new(),
        array_iteration_unsupported: false,
        array_intrinsic_mutated: false,
        tainted_objects: BTreeSet::new(),
        tainted_functions: BTreeSet::new(),
        tainted_classes: BTreeSet::new(),
        escaped_class_capabilities: BTreeSet::new(),
        exported_runtime_values: index
            .exported_hoisted_functions
            .iter()
            .copied()
            .map(|key| AbstractValue::Origin(ValueOrigin::Function(key)))
            .collect(),
        export_reentry_enabled: export_reentry_policy == ExportReentryPolicy::Conservative,
        fresh_mutation_callables: super::fresh_receiver::fresh_mutation_callables(
            body,
            facts,
            !index.dynamic_scope_owners.is_empty(),
        ),
        prefix_revision: 0,
        invocation_depth: 0,
        invocation_count: 0,
        callback_fallback: CallbackFallbackSummary::default(),
        fallback_outcome: Outcome::default(),
        fallback_callback_sizes: None,
        fallback_local_owners: BTreeSet::new(),
        capture_contexts: BTreeMap::new(),
        callback_sets: BTreeMap::new(),
        active: BTreeSet::new(),
        cache: BTreeMap::new(),
    };
    let mut top_env = Context::new();
    let mut top_captures = Context::new();
    let mut effects = Vec::with_capacity(facts.len());
    for (fact, (calls, assignments)) in facts
        .iter()
        .zip(index.top_calls.iter().zip(&index.top_assignments))
    {
        executor.current_top_owner = Some(fact.ordinal);
        // Cache keys include this owner; previous statement snapshots cannot
        // be reused and should not consume this statement's bounded budget.
        executor.cache.clear();
        executor.invocation_count = 0;
        executor.fallback_outcome = Outcome::default();
        executor.fallback_callback_sizes = None;
        executor.fallback_local_owners.clear();
        let fact_roots = executor.fact_roots(fact);
        if executor.reachability.unknown_call_seen {
            executor.extend_reachable_owners(fact_roots.iter().copied());
        } else if let Some(roots) = &mut executor.prior_reachability_roots {
            // Include the active call site before any nested unknown call can
            // consume the prefix roots.
            roots.extend(fact_roots.iter().copied());
        }
        if index.opaque_top.contains(&fact.ordinal) {
            executor.note_unknown_execution(fact.ordinal);
        }
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        // A source owner is indivisible, but can assign an existing binding
        // before one of its calls, e.g. `(reader = () => later, host())`.
        // Join these possible values before the flattened calls. Keep the
        // previous values and do not mark initialization definite until the
        // owner finishes; assignments on skipped/later paths are only may
        // values. A lexical TDZ still blocks publication through that binding.
        for (id, expression) in assignments {
            if !needed.contains(id)
                || !fact.rebinds.eager.contains(id)
                || !(executor.var_bindings.contains(id)
                    || executor.initialized_bindings.contains(id))
            {
                continue;
            }
            let values = executor.evaluate(expression, &top_env, &top_captures);
            top_env
                .entry(id.clone())
                .or_default()
                .extend(values.iter().cloned());
            let possible = executor.prefix_values.entry(id.clone()).or_default();
            let before = possible.len();
            possible.extend(values);
            if possible.len() != before {
                executor.prefix_revision = executor.prefix_revision.wrapping_add(1);
            }
        }
        // Like callable writes above, top-level property writes are kept in a
        // separate visitor lane. Applying all of this statement's writes
        // before its flattened call list may expose a later write early, which
        // is conservative and avoids missing a callback assigned before an
        // opaque call in the same statement.
        let member_writes = &index.top_member_value_writes[fact.ordinal.0];
        executor.apply_member_value_writes(
            member_writes,
            &top_env,
            &top_captures,
            &BTreeSet::new(),
        );
        executor.promote_callback_result_writes(
            &index.top_callback_result_writes[fact.ordinal.0],
            &top_env,
            &top_captures,
            fact.ordinal,
            &mut outcome,
            &BTreeSet::new(),
        );
        let metadata_only = index.top_metadata_decorations[fact.ordinal.0]
            .as_ref()
            .is_some_and(|application| {
                executor.summarize_metadata_decoration(
                    application,
                    &top_env,
                    &top_captures,
                    fact.ordinal,
                    &mut outcome,
                )
            });
        if !metadata_only {
            for call in calls {
                executor.execute_call(call, &top_env, &top_captures, &mut outcome);
            }
        }
        executor.promote_property_writes(
            &index.top_property_writes[fact.ordinal.0],
            fact.ordinal,
            &mut outcome,
        );
        let fallback = std::mem::take(&mut executor.fallback_outcome);
        if fallback.unknown_call {
            outcome.effects.reads.extend(fallback.effects.reads);
            outcome.effects.rebinds.extend(fallback.effects.rebinds);
            outcome
                .effects
                .uninitialized_reads
                .extend(fallback.effects.uninitialized_reads);
            outcome.executed_owners.extend(fallback.executed_owners);
            outcome.unknown_call = true;
            outcome.complete = false;
        }
        outcome.complete &= !index.opaque_top.contains(&fact.ordinal);
        outcome.effects.complete = outcome.complete;
        let mut prior_effect_owners = outcome.executed_owners.clone();
        if !fact.purity.is_pure() {
            prior_effect_owners.insert(fact.ordinal);
        }
        executor.prior_effect_owners.extend(prior_effect_owners);
        if !executor.reachability.unknown_call_seen
            && let Some(roots) = &mut executor.prior_reachability_roots
        {
            roots.extend(outcome.executed_owners.iter().copied());
        }
        effects.push(outcome.effects);
        for (id, expression) in assignments {
            if !needed.contains(id) {
                continue;
            }
            let values = executor.evaluate(expression, &top_env, &top_captures);
            if !executor.top_bindings.contains(id) {
                top_captures
                    .entry(id.clone())
                    .or_default()
                    .extend(values.clone());
            }
            top_env
                .entry(id.clone())
                .or_default()
                .extend(values.clone());
            if executor.top_bindings.contains(id) {
                executor
                    .prefix_values
                    .entry(id.clone())
                    .or_default()
                    .extend(values);
                executor.prefix_revision = executor.prefix_revision.wrapping_add(1);
            }
        }
        executor.initialized_bindings.extend(
            index.top_definite_initializers[fact.ordinal.0]
                .iter()
                .cloned(),
        );
        for expression in &index.top_exported_values[fact.ordinal.0] {
            let values = executor.evaluate(expression, &top_env, &top_captures);
            executor.exported_runtime_values.extend(values);
            executor.prefix_revision = executor.prefix_revision.wrapping_add(1);
        }
    }
    for (fact, effect) in facts.iter_mut().zip(effects) {
        fact.synchronous_calls = Some(effect);
    }
}
