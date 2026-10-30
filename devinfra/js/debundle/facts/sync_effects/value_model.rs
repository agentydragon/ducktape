//! Abstract value vocabulary and value relationships used by synchronous execution analysis.

use crate::facts::value_flow::{ValueOrigin, static_prop as static_string_prop};
use imbl::{OrdMap, OrdSet};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::{BytePos, Span};
use swc_ecma_ast::*;

pub(super) fn static_prop(property: &MemberProp) -> Option<String> {
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

#[derive(Debug, Clone, Default, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Values(OrdSet<AbstractValue>);

impl Values {
    pub(super) fn new() -> Self {
        Self::default()
    }

    pub(super) fn first(&self) -> Option<&AbstractValue> {
        self.0.get_min()
    }

    pub(super) fn insert(&mut self, value: AbstractValue) -> bool {
        self.0.insert(value).is_none()
    }

    pub(super) fn remove(&mut self, value: &AbstractValue) -> bool {
        self.0.remove(value).is_some()
    }

    pub(super) fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub(super) fn is_disjoint(&self, other: &Self) -> bool {
        self.iter().all(|value| !other.contains(value))
    }

    pub(super) fn is_subset(&self, other: &Self) -> bool {
        self.iter().all(|value| other.contains(value))
    }
}

impl std::ops::Deref for Values {
    type Target = OrdSet<AbstractValue>;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl Extend<AbstractValue> for Values {
    fn extend<T: IntoIterator<Item = AbstractValue>>(&mut self, values: T) {
        self.0.extend(values);
    }
}

impl FromIterator<AbstractValue> for Values {
    fn from_iter<T: IntoIterator<Item = AbstractValue>>(values: T) -> Self {
        Self(values.into_iter().collect())
    }
}

impl<const N: usize> From<[AbstractValue; N]> for Values {
    fn from(values: [AbstractValue; N]) -> Self {
        values.into_iter().collect()
    }
}

impl IntoIterator for Values {
    type Item = AbstractValue;
    type IntoIter = <OrdSet<AbstractValue> as IntoIterator>::IntoIter;

    fn into_iter(self) -> Self::IntoIter {
        self.0.into_iter()
    }
}

impl<'a> IntoIterator for &'a Values {
    type Item = &'a AbstractValue;
    type IntoIter = <&'a OrdSet<AbstractValue> as IntoIterator>::IntoIter;

    fn into_iter(self) -> Self::IntoIter {
        self.0.iter()
    }
}

pub(super) type Context = OrdMap<Id, Values>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct CallSite {
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

impl AbstractValue {
    /// Whether this value is one of the exact primitive abstractions.
    /// `Unknown` and `Unavailable` are intentionally excluded: either can
    /// represent a non-primitive result or a throwing evaluation.
    pub(super) fn is_primitive(&self) -> bool {
        matches!(
            self,
            Self::NonCallable | Self::Undefined | Self::StringPrimitive(_)
        )
    }
}

impl CallSite {
    pub(super) fn allocation_id(self) -> u32 {
        self.start
    }

    pub(super) fn source_span(self) -> Span {
        Span::new(BytePos(self.start), BytePos(self.end))
    }
}

/// Contexts are interned bottom-up by the executor. Structural equality of
/// captures becomes identity equality, so comparing value sets and cache keys
/// never recursively compares a shared capture DAG along every path.
#[derive(Debug, Clone)]
pub(super) struct Captured<T> {
    identity: usize,
    pub(super) values: Arc<T>,
}

impl<T> Captured<T> {
    pub(super) fn with_identity(identity: usize, values: Arc<T>) -> Self {
        Self { identity, values }
    }
}

pub(super) type CapturedContext = Captured<Context>;

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
pub(super) enum AbstractValue {
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
    /// Exact own-descriptor entries captured from one source-proved ordinary
    /// object at a native call site. The executor may apply the certificate
    /// through the paired defineProperties call; all other uses keep the
    /// carrier opaque.
    OwnDescriptorMap(CallSite),
    /// An exact primitive string retained for own-name arrays and keyed reads.
    StringPrimitive(String),
    /// A foreign source factory's deferred object graph. Provider captures are
    /// retained in its imported summary; using this token activates the full
    /// conservative importer binding/callback pool, never foreign SWC IDs.
    ForeignDeferredRecord,
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
pub(super) enum ArrayIterationMethod {
    ForEach,
    Filter,
    Map,
}

#[derive(Debug, Clone)]
pub(super) struct ArrayIteration {
    pub(super) method: ArrayIterationMethod,
    pub(super) source: ValueExpr,
    pub(super) callback: ValueExpr,
    pub(super) result: Option<u32>,
}

#[derive(Debug, Clone)]
pub(super) struct ArrayElement {
    pub(super) spread: bool,
    pub(super) value: ValueExpr,
}

/// Slot values are an ordered overapproximation. The ordinary Object origin
/// remains the identity used by every mutation, publication and taint lane.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct DenseArray {
    pub(super) slots: Vec<Values>,
}

#[derive(Debug, Clone)]
pub(super) struct FromEntriesMappedProof {
    pub(super) source: ValueExpr,
    pub(super) key: String,
    pub(super) map_call_site: CallSite,
    pub(super) object_intrinsic: Id,
}

/// A fresh, private Vite/Rollup style facade. Both intrinsic calls return the
/// same ordinary object; its stored callable fields remain reachable if the
/// facade is later published or consumed.
pub(super) struct FrozenNamespaceProof {
    pub(super) freeze_site: CallSite,
    pub(super) define_site: CallSite,
    pub(super) stored_bindings: BTreeSet<Id>,
}

/// Syntax-only shape of one statically named own property. Evaluated values
/// stay in the executor's ordinary object identity store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum OwnPropertyDescriptorSyntax {
    Data,
    Accessor {
        getter: Option<u32>,
        setter: Option<u32>,
    },
}

/// Source-ordered string properties of one supported ordinary object literal.
/// The map carries descriptor syntax; `creation_order` preserves insertion
/// order for ECMAScript own-key enumeration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyShape {
    pub(super) properties: BTreeMap<String, OwnPropertyDescriptorSyntax>,
    pub(super) creation_order: Vec<String>,
}

/// Returns ECMAScript's own string-key order for a source-ordered ordinary
/// object shape: array-index keys numerically first, followed by other string
/// keys in their original insertion order. `BTreeMap` order is never a proxy
/// for JavaScript property order.
pub(super) fn ordered_own_property_names(creation_order: &[String]) -> Vec<String> {
    let mut indices = Vec::new();
    let mut strings = Vec::new();
    let mut seen = BTreeSet::new();
    for name in creation_order {
        if !seen.insert(name.as_str()) {
            continue;
        }
        let array_index = name
            .parse::<u32>()
            .ok()
            .filter(|index| *index != u32::MAX && index.to_string() == name.as_str());
        if let Some(index) = array_index {
            indices.push((index, name.clone()));
        } else {
            strings.push(name.clone());
        }
    }
    indices.sort_unstable_by_key(|(index, _)| *index);
    indices
        .into_iter()
        .map(|(_, name)| name)
        .chain(strings)
        .collect()
}

#[derive(Debug, Clone)]
pub(super) enum ValueExpr {
    Binding(Id),
    Origin(ValueOrigin),
    ObjectLiteral(u32, Vec<(Option<String>, Self)>),
    Undefined,
    NonCallable,
    Member(Box<Self>, Option<String>, bool),
    Call(Box<Self>, Vec<Self>, CallSite),
    New(Box<Self>, Vec<Self>, CallSite),
    /// Source spelling of `Object.getPrototypeOf([][Symbol.iterator]())`.
    /// Native identity remains conditional on Object, Array, and Symbol.
    ArrayIteratorPrototype(Box<Self>),
    /// Source spelling of `Object.getPrototypeOf(new Set()[Symbol.iterator]())`.
    /// Its path is conditional on the Set constructor and iterator protocol.
    SetIteratorPrototype(Box<Self>),
    /// An operator result is primitive on successful evaluation. This is a
    /// value-kind fact, not a purity claim: keep operands so calls and
    /// unavailable reads inside them still flow through the evaluator.
    Primitive(Vec<Self>),
    Alternatives(Vec<Self>),
    Unknown,
}

#[derive(Debug, Clone, Default)]
pub(super) struct ClassValueFacts {
    pub(super) instance_fields: BTreeMap<String, ValueExpr>,
    pub(super) ordinary_instance_methods: BTreeSet<String>,
    pub(super) default_has_instance: bool,
    pub(super) static_fields: BTreeMap<String, ValueExpr>,
    pub(super) super_class: Option<ValueExpr>,
    pub(super) opaque_instance: bool,
    pub(super) opaque_static: bool,
    pub(super) allocation_without_user_code: bool,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum ClassSurface {
    Constructor,
    Instance,
}

#[derive(Debug, Clone, Default)]
pub(super) struct NamespaceValueFacts {
    classes: BTreeMap<u32, ClassValueFacts>,
    verified_data_objects: BTreeSet<u32>,
    /// A binding is resolved only when the retained syntax has exactly one
    /// assignment for it. Reassignments and alternatives remain opaque.
    bindings: BTreeMap<Id, ValueExpr>,
    binding_assignments: BTreeMap<Id, Vec<ValueExpr>>,
    unstable_class_bindings: BTreeSet<Id>,
}

fn array_iterator_prototype_expression(call: &CallExpr) -> bool {
    let Callee::Expr(outer_callee) = &call.callee else {
        return false;
    };
    let Expr::Member(outer_member) = outer_callee.unwrap_parens() else {
        return false;
    };
    if !matches!(outer_member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object")
        || !matches!(&outer_member.prop, MemberProp::Ident(id) if id.sym == "getPrototypeOf")
    {
        return false;
    }
    let [argument] = call.args.as_slice() else {
        return false;
    };
    if argument.spread.is_some() {
        return false;
    }
    let Expr::Call(iterator_call) = argument.expr.unwrap_parens() else {
        return false;
    };
    if !iterator_call.args.is_empty() {
        return false;
    }
    let Callee::Expr(iterator_callee) = &iterator_call.callee else {
        return false;
    };
    let Expr::Member(iterator_member) = iterator_callee.unwrap_parens() else {
        return false;
    };
    let Expr::Array(array) = iterator_member.obj.unwrap_parens() else {
        return false;
    };
    if !array.elems.is_empty() {
        return false;
    }
    matches!(&iterator_member.prop, MemberProp::Computed(key)
        if matches!(key.expr.unwrap_parens(), Expr::Member(symbol)
            if matches!(symbol.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Symbol")
                && matches!(&symbol.prop, MemberProp::Ident(id) if id.sym == "iterator")))
}

fn set_iterator_prototype_expression(call: &CallExpr) -> bool {
    let Callee::Expr(outer_callee) = &call.callee else {
        return false;
    };
    let Expr::Member(outer_member) = outer_callee.unwrap_parens() else {
        return false;
    };
    if !matches!(outer_member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object")
        || !matches!(&outer_member.prop, MemberProp::Ident(id) if id.sym == "getPrototypeOf")
    {
        return false;
    }
    let [argument] = call.args.as_slice() else {
        return false;
    };
    if argument.spread.is_some() {
        return false;
    }
    let Expr::Call(iterator_call) = argument.expr.unwrap_parens() else {
        return false;
    };
    if !iterator_call.args.is_empty() {
        return false;
    }
    let Callee::Expr(iterator_callee) = &iterator_call.callee else {
        return false;
    };
    let Expr::Member(iterator_member) = iterator_callee.unwrap_parens() else {
        return false;
    };
    let Expr::New(set) = iterator_member.obj.unwrap_parens() else {
        return false;
    };
    if !matches!(set.callee.unwrap_parens(), Expr::Ident(id) if id.sym == "Set")
        || !set.args.as_deref().is_none_or(|args| args.is_empty())
    {
        return false;
    }
    matches!(&iterator_member.prop, MemberProp::Computed(key)
        if matches!(key.expr.unwrap_parens(), Expr::Member(symbol)
            if matches!(symbol.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Symbol")
                && matches!(&symbol.prop, MemberProp::Ident(id) if id.sym == "iterator")))
}

impl ValueExpr {
    pub(super) fn from_expr(expr: &Expr) -> Self {
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
                OptChainBase::Call(call) if call.args.iter().all(|arg| arg.spread.is_none()) => {
                    // Keep a known local call's returned callable reachable.
                    // The undefined alternative overapproximates optional
                    // short-circuiting without dropping the invocation path.
                    Self::Alternatives(vec![
                        Self::Undefined,
                        // Preserve the prior unknown publication capability;
                        // timing precision must not admit namespace contracts.
                        Self::Unknown,
                        Self::Call(
                            Box::new(Self::from_expr(&call.callee)),
                            call.args
                                .iter()
                                .map(|arg| Self::from_expr(&arg.expr))
                                .collect(),
                            call.span.into(),
                        ),
                    ])
                }
                OptChainBase::Member(member) => {
                    let mut options = vec![
                        Self::Undefined,
                        Self::Member(
                            Box::new(Self::from_expr(&member.obj)),
                            static_prop(&member.prop),
                            matches!(member.prop, MemberProp::Ident(_)),
                        ),
                    ];
                    if !matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                        || !matches!(member.prop, MemberProp::Ident(_))
                    {
                        options.push(Self::Unknown);
                    }
                    Self::Alternatives(options)
                }
                // Spread calls have no modeled result shape.
                _ => Self::Unknown,
            },
            Expr::Call(call) if array_iterator_prototype_expression(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    unreachable!("recognized iterator-prototype expression is an ordinary call")
                };
                Self::ArrayIteratorPrototype(Box::new(Self::Call(
                    Box::new(Self::from_expr(callee)),
                    call.args
                        .iter()
                        .map(|argument| Self::from_expr(&argument.expr))
                        .collect(),
                    call.span.into(),
                )))
            }
            Expr::Call(call) if set_iterator_prototype_expression(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    unreachable!("recognized iterator-prototype expression is an ordinary call")
                };
                Self::SetIteratorPrototype(Box::new(Self::Call(
                    Box::new(Self::from_expr(callee)),
                    call.args
                        .iter()
                        .map(|argument| Self::from_expr(&argument.expr))
                        .collect(),
                    call.span.into(),
                )))
            }
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
                    new_expr.span.into(),
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

    pub(super) fn bindings(&self, result: &mut BTreeSet<Id>) {
        match self {
            Self::Binding(id) => {
                result.insert(id.clone());
            }
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.bindings(result)
            }
            Self::Member(object, _, _) => object.bindings(result),
            Self::Call(callee, args, _) | Self::New(callee, args, _) => {
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
    pub(super) fn carries_namespace(&self, id: &Id, aliases: &BTreeSet<Id>) -> bool {
        match self {
            Self::Binding(binding) => binding == id || aliases.contains(binding),
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.carries_namespace(id, aliases)
            }
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
            Self::New(callee, args, _) => {
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
    pub(super) fn may_publish_namespace(
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
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }
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
            Self::New(callee, args, _) => {
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
    pub(super) fn may_be_namespace_mutation_receiver(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        receiver_aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        self.may_be_namespace_mutation_receiver_with_alias_query(
            id,
            aliases,
            &mut |binding| receiver_aliases.contains(binding),
            facts,
        )
    }

    pub(super) fn may_be_namespace_mutation_receiver_with_alias_query(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        receiver_alias: &mut impl FnMut(&Id) -> bool,
        facts: &NamespaceValueFacts,
    ) -> bool {
        match self {
            Self::Binding(binding) => {
                binding == id || aliases.contains(binding) || receiver_alias(binding)
            }
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.may_be_namespace_mutation_receiver_with_alias_query(
                    id,
                    aliases,
                    receiver_alias,
                    facts,
                )
            }
            Self::Primitive(_) => false,
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.carries_namespace(id, aliases)),
            Self::Alternatives(options) => options.iter().any(|value| {
                value.may_be_namespace_mutation_receiver_with_alias_query(
                    id,
                    aliases,
                    receiver_alias,
                    facts,
                )
            }),
            Self::Member(object, property, is_static) => {
                if let Some(value) = property
                    .as_deref()
                    .filter(|_| *is_static)
                    .and_then(|property| facts.own_data_member(object, property, id, aliases))
                {
                    return value.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
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
                    || matches!(object.as_ref(), Self::Unknown | Self::New(..))
                    || object.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
                        facts,
                    )
            }
            Self::Call(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || !Self::is_direct_namespace_member_callee(callee, id)
            }
            Self::New(..) | Self::Unknown => true,
            Self::Origin(_) | Self::Undefined | Self::NonCallable => false,
        }
    }

    /// A callback result may contain one-hop namespace member reads as
    /// ordinary data-property values. That exception applies only to the
    /// direct returned object graph; aliases and other unknown shapes remain
    /// subject to the ordinary publication check.
    pub(super) fn may_publish_namespace_in_factory_result(
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

pub(super) fn infer_namespace_aliases(
    namespace: &Id,
    assignments: &[&(Id, ValueExpr)],
    facts: &NamespaceValueFacts,
) -> BTreeSet<Id> {
    use crate::inference::{FactContext, IterativeInference, IterativeRules};
    struct AliasRules<'a> {
        namespace: &'a Id,
        assignments: &'a [&'a (Id, ValueExpr)],
        facts: &'a NamespaceValueFacts,
    }
    impl IterativeRules for AliasRules<'_> {
        type Key = ();
        type Fact = BTreeSet<Id>;
        type Rule = usize;
        fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Self::Key, Self::Fact>) {
            let (target, value) = self.assignments[*rule];
            if target == self.namespace {
                return;
            }
            let is_alias = context.read(()).is_some_and(|aliases| {
                value.may_equal_namespace(self.namespace, aliases, self.facts, &mut BTreeSet::new())
            });
            if is_alias {
                context.propose((), BTreeSet::from([target.clone()]));
            }
        }
        fn merge(_: &(), current: &mut BTreeSet<Id>, contribution: BTreeSet<Id>) -> bool {
            let before = current.len();
            current.extend(contribution);
            current.len() != before
        }
    }
    let mut inference = IterativeInference::new(AliasRules {
        namespace,
        assignments,
        facts,
    });
    inference.seed((), BTreeSet::new());
    inference
        .solve(0..assignments.len())
        .into_values()
        .remove(&())
        .expect("alias seed")
}

impl NamespaceValueFacts {
    pub(super) fn class_allocation_without_user_code(&self, span: u32) -> bool {
        self.classes
            .get(&span)
            .is_some_and(|class| class.allocation_without_user_code)
    }

    pub(super) fn has_instance_field(&self, span: u32, property: &str) -> bool {
        self.classes
            .get(&span)
            .is_some_and(|class| class.instance_fields.contains_key(property))
    }

    pub(super) fn from_assignments(
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

    pub(super) fn resolve_class(
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
            ValueExpr::New(callee, _, _) => self
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
pub(super) fn static_property_name(name: &PropName) -> Option<String> {
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

pub(super) fn object_data_property_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) if ident.sym == "__proto__" => None,
        PropName::Str(string) if string.value.to_string_lossy() == "__proto__" => None,
        _ => static_property_name(name),
    }
}
