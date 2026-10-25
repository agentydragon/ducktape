//! Call-site execution effects. Function values and function bodies are
//! deliberately separate: returning a closure never executes that closure.

use super::statement_facts::SynchronousCalls;
use super::value_flow::{Values as FlowValues, static_prop};
use super::*;
use std::cmp::Ordering;
use std::sync::Arc;

type Values = BTreeSet<AbstractValue>;
type Context = BTreeMap<Id, Values>;

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
    /// A fresh opaque return value retaining callbacks without publishing them.
    DeferredCallbacks(Captured<Values>),
    ObjectAssign,
    GlobalObject,
    Undefined,
    NonCallable,
    /// Evaluating an expression throws before producing a value (for example,
    /// a lexical binding read in its temporal dead zone).
    Unavailable,
    Unknown,
}

#[derive(Debug, Clone)]
enum ValueExpr {
    Binding(Id),
    Origin(ValueOrigin),
    ObjectLiteral(u32, Vec<(Option<String>, Self)>),
    Undefined,
    NonCallable,
    Member(Box<Self>, Option<String>, bool),
    Call(Box<Self>, Vec<Self>),
    New(Box<Self>, Vec<Self>),
    /// An operator result is primitive on successful evaluation. This is a
    /// value-kind fact, not a purity claim: keep operands so calls and
    /// unavailable reads inside them still flow through the evaluator.
    Primitive(Vec<Self>),
    Alternatives(Vec<Self>),
    Unknown,
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
            Expr::Call(call) if call.args.iter().all(|arg| arg.spread.is_none()) => {
                match &call.callee {
                    Callee::Expr(callee) => Self::Call(
                        Box::new(Self::from_expr(callee)),
                        call.args
                            .iter()
                            .map(|arg| Self::from_expr(&arg.expr))
                            .collect(),
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
            Self::Call(callee, args) | Self::New(callee, args) => {
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

    /// Whether this value may be the namespace itself or an alias to it.
    /// Direct static identifier-property namespace call results are allowed
    /// only under the audited callback-factory identity contract; all other
    /// unknown call results stay conservative.
    fn may_expose_namespace(&self, id: &Id) -> bool {
        match self {
            Self::Binding(binding) => binding == id,
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.may_expose_namespace(id)),
            Self::Primitive(_) => false,
            Self::Alternatives(options) => {
                options.iter().any(|value| value.may_expose_namespace(id))
            }
            Self::Member(object, _, _) => object.may_expose_namespace(id),
            Self::Call(callee, args) => {
                args.iter().any(|value| value.may_expose_namespace(id))
                    || !matches!(callee.as_ref(), Self::Member(object, Some(_), true)
                        if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id))
            }
            // Constructor calls can return a caller-visible object explicitly.
            // Keep their result opaque for namespace alias/escape checks.
            Self::New(_, _) => true,
            Self::Unknown => true,
            Self::Origin(_) | Self::Undefined | Self::NonCallable => false,
        }
    }

    /// Whether the expression explicitly carries the namespace through its
    /// value inputs. A call's callee is not a value passed to the call; its
    /// arguments are. This narrower check is used for call arguments, while
    /// assignment/receiver checks keep unknown call results conservative.
    fn directly_exposes_namespace(&self, id: &Id) -> bool {
        match self {
            Self::Binding(binding) => binding == id,
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.directly_exposes_namespace(id)),
            Self::Primitive(_) => false,
            Self::Alternatives(options) => options
                .iter()
                .any(|value| value.directly_exposes_namespace(id)),
            Self::Member(object, _, _) => object.directly_exposes_namespace(id),
            Self::Call(_, args) => args
                .iter()
                .any(|value| value.directly_exposes_namespace(id)),
            Self::New(_, args) => args
                .iter()
                .any(|value| value.directly_exposes_namespace(id)),
            Self::Unknown => true,
            Self::Origin(_) | Self::Undefined | Self::NonCallable => false,
        }
    }

    /// A callback result may contain one-hop namespace member reads as
    /// ordinary data-property values. That exception applies only to the
    /// direct returned object graph; assigning an alias locally or passing it
    /// elsewhere still uses `may_expose_namespace` above.
    fn may_expose_namespace_in_factory_result(&self, id: &Id) -> bool {
        match self {
            Self::ObjectLiteral(_, properties) => properties.iter().any(|(name, value)| {
                let direct_member_value = name.is_some()
                    && matches!(value, Self::Member(object, Some(_), true)
                        if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id));
                !direct_member_value && value.may_expose_namespace_in_factory_result(id)
            }),
            _ => self.may_expose_namespace(id),
        }
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
    callee: ValueExpr,
    args: Vec<ValueExpr>,
    span: Span,
    is_new: bool,
    caller: Option<u32>,
    owner: StatementOrdinal,
    plain_object_sources: bool,
    no_sync_arguments: bool,
    has_spread: bool,
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
    property: Option<String>,
    value: ValueExpr,
}

#[derive(Debug, Clone)]
struct Callable {
    owner: StatementOrdinal,
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
    member_writes: Vec<(Id, String)>,
    member_value_writes: Vec<(Id, Option<String>, ValueExpr)>,
    callback_result_writes: Vec<CallbackResultWrite>,
    dynamic_property_effect: bool,
    dynamic_property_receivers: Vec<ValueExpr>,
    opaque: bool,
    generator: bool,
}

#[derive(Debug, Clone)]
struct LocalConstructor {
    key: Option<u32>,
    /// No constructor body or instance fields run during direct allocation.
    inert_allocation: bool,
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
        property_reads,
        binding_reads,
    })
}

impl Default for Callable {
    fn default() -> Self {
        Self {
            owner: StatementOrdinal(0),
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
            member_writes: Vec::new(),
            member_value_writes: Vec::new(),
            callback_result_writes: Vec::new(),
            dynamic_property_effect: false,
            dynamic_property_receivers: Vec::new(),
            opaque: false,
            generator: false,
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
    class_bindings: BTreeMap<u32, Id>,
    class_ids: BTreeSet<Id>,
    other_class_uses: BTreeSet<Id>,
    safe_class_target: Option<Id>,
    plain_data_objects: BTreeSet<u32>,
    setter_owners: BTreeMap<Option<String>, BTreeSet<StatementOrdinal>>,
    owner_callable_prefix_reads: Vec<Option<BTreeSet<Id>>>,
    unindexed_accessor_owners: BTreeSet<StatementOrdinal>,
    top_calls: Vec<Vec<Call>>,
    top_property_writes: Vec<Vec<(Option<Id>, Option<String>)>>,
    top_assignments: Vec<Vec<(Id, ValueExpr)>>,
    top_definite_initializers: Vec<BTreeSet<Id>>,
    top_member_value_writes: Vec<Vec<(Id, Option<String>, ValueExpr)>>,
    top_callback_result_writes: Vec<Vec<CallbackResultWrite>>,
    top_exported_values: Vec<Vec<ValueExpr>>,
    top_metadata_decorations: Vec<Option<MetadataDecorateCall>>,
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
    callback_factory_members: BTreeMap<Id, BTreeSet<String>>,
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
            class_bindings: BTreeMap::new(),
            class_ids: BTreeSet::new(),
            other_class_uses: BTreeSet::new(),
            safe_class_target: None,
            plain_data_objects: BTreeSet::new(),
            setter_owners: BTreeMap::new(),
            owner_callable_prefix_reads: Vec::new(),
            unindexed_accessor_owners: BTreeSet::new(),
            top_calls: Vec::new(),
            top_property_writes: Vec::new(),
            top_assignments: Vec::new(),
            top_definite_initializers: Vec::new(),
            top_member_value_writes: Vec::new(),
            top_callback_result_writes: Vec::new(),
            top_exported_values: Vec::new(),
            top_metadata_decorations: Vec::new(),
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
            callback_factory_members: BTreeMap::new(),
            callback_factory_functions: BTreeSet::new(),
        }
    }
}

impl ExecutionIndex {
    fn collect(body: &[TopLevelItemView<'_>], hints: &AnalysisHints) -> Self {
        let mut index = Self {
            top_calls: vec![Vec::new(); body.len()],
            owner_callable_prefix_reads: vec![None; body.len()],
            top_property_writes: vec![Vec::new(); body.len()],
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
            no_sync_callback_members: hints.no_sync_callback_members.clone(),
            ..Self::default()
        };
        for item in body {
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
                    if hints.callback_factories.contains(local.sym.as_ref()) {
                        index.callback_factory_imports.insert(local.to_id());
                    }
                    if let Some(members) = hints.callback_factory_members.get(local.sym.as_ref()) {
                        index
                            .callback_factory_members
                            .insert(local.to_id(), members.clone());
                    }
                }
            }
            item.as_module_item().visit_with(&mut index);
        }
        // An accessor fallback may run any callable owned by this item,
        // including a nested function installed as the setter. Union each
        // retained synchronous prefix instead of using whole lazy bodies.
        for callable in index.callables.values() {
            index.owner_callable_prefix_reads[callable.owner.0]
                .get_or_insert_with(BTreeSet::new)
                .extend(callable.reads.iter().cloned());
        }
        // A namespace member is an audited target only while its binding is
        // unchanged. Any local write could replace a member with an eager
        // callback invoker before the apparent factory call.
        index.callback_factory_members.retain(|id, members| {
            let top_member_write =
                index
                    .top_property_writes
                    .iter()
                    .flatten()
                    .any(|(receiver, property)| {
                        receiver.as_ref() == Some(id)
                            && property
                                .as_ref()
                                .is_none_or(|property| members.contains(property))
                    });
            let callable_write = index.callables.values().any(|callable| {
                callable
                    .member_writes
                    .iter()
                    .any(|(receiver, property)| receiver == id && members.contains(property))
                    || callable
                        .dynamic_property_receivers
                        .iter()
                        .any(|receiver| receiver.may_expose_namespace(id))
                    || callable.literal_exposures.contains(id)
                    || callable.escaped_bindings.contains(id)
                    || callable
                        .local_assignments
                        .iter()
                        .any(|(_, value)| value.may_expose_namespace(id))
                    || callable
                        .returns
                        .iter()
                        .any(|value| value.may_expose_namespace_in_factory_result(id))
                    || callable.calls.iter().any(|call| {
                        call.args
                            .iter()
                            .any(|value| value.directly_exposes_namespace(id))
                    })
            });
            let top_assignment = index
                .top_assignments
                .iter()
                .flatten()
                .any(|(_, value)| value.may_expose_namespace(id));
            let top_member_value = index
                .top_member_value_writes
                .iter()
                .flatten()
                .any(|(_, _, value)| value.may_expose_namespace(id));
            let top_call_argument = index.top_calls.iter().flatten().any(|call| {
                call.args
                    .iter()
                    .any(|value| value.directly_exposes_namespace(id))
            });
            let exported_value = index
                .top_exported_values
                .iter()
                .flatten()
                .any(|value| value.may_expose_namespace(id));
            !(top_member_write
                || callable_write
                || top_assignment
                || top_member_value
                || top_call_argument
                || exported_value)
        });
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
        let saved_current = self.current.replace(key);
        let saved_await = std::mem::replace(&mut self.past_await, false);
        let saved_may_skip = std::mem::replace(&mut self.may_skip_await_depth, 0);
        let saved_conditional = std::mem::replace(&mut self.conditional_depth, 0);
        let opaque_params = params.iter().any(|param| param.binding.is_none());
        self.callables.entry(key).or_insert_with(|| Callable {
            owner: self.owner,
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
            .filter(|id| value.may_expose_namespace(id))
            .cloned()
            .collect::<Vec<_>>();
        self.callables
            .get_mut(&key)
            .unwrap()
            .escaped_bindings
            .extend(exposed);
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
    fn visit_named_export(&mut self, _export: &NamedExport) {
        // Linkage is captured by collect_export_roots. Specifiers do not run
        // at module initialization or expose a class in a closed module.
    }

    fn visit_class(&mut self, class: &Class) {
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
        if object.props.iter().all(|property| match property {
            PropOrSpread::Spread(_) => false,
            PropOrSpread::Prop(property) => match property.as_ref() {
                Prop::KeyValue(pair) => {
                    static_property_name(&pair.key).is_some()
                        && static_property_name(&pair.key).as_deref() != Some("__proto__")
                }
                Prop::Shorthand(id) => id.sym.as_ref() != "__proto__",
                _ => false,
            },
        }) {
            self.plain_data_objects.insert(object.span.lo.0);
        }
        self.literal_depth += 1;
        for property in &object.props {
            match property {
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
        self.literal_depth += 1;
        array.visit_children_with(self);
        self.literal_depth -= 1;
    }

    fn visit_fn_decl(&mut self, declaration: &FnDecl) {
        if let Some(key) = self.current {
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
        let params = arrow.params.iter().map(Parameter::from_pattern).collect();
        self.with_callable(arrow.span.lo.0, params, |index| {
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

    fn visit_binding_ident(&mut self, _binding: &BindingIdent) {}

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        declarator.visit_children_with(self);
        if let (Pat::Ident(binding), Some(init)) = (&declarator.name, &declarator.init) {
            let assignment = (binding.id.to_id(), ValueExpr::from_expr(init));
            if let Some(key) = self.current {
                self.callables
                    .get_mut(&key)
                    .unwrap()
                    .local_assignments
                    .push(assignment);
            } else {
                self.top_assignments[self.owner.0].push(assignment);
            }
        }
    }

    fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
        let before_assignment = self.past_await;
        assignment.visit_children_with(self);
        if matches!(
            assignment.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            self.past_await = before_assignment;
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
        } else {
            self.mark_dynamic_property_effect(None);
        }
    }

    fn visit_update_expr(&mut self, expression: &UpdateExpr) {
        expression.visit_children_with(self);
        if let Expr::Member(member) = expression.arg.unwrap_parens() {
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
        self.mark_opaque();
    }

    fn visit_unary_expr(&mut self, expression: &UnaryExpr) {
        expression.visit_children_with(self);
        if expression.op == UnaryOp::Delete
            && let Expr::Member(member) = expression.arg.unwrap_parens()
        {
            self.mark_dynamic_property_effect(Some(&member.obj));
        }
    }

    fn visit_return_stmt(&mut self, ret: &ReturnStmt) {
        ret.visit_children_with(self);
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
        if let Callee::Expr(callee) = &call.callee {
            self.record_call(Call {
                callee: ValueExpr::from_expr(callee),
                args: call
                    .args
                    .iter()
                    .map(|arg| ValueExpr::from_expr(&arg.expr))
                    .collect(),
                is_new: false,
                span: call.span,
                has_spread: call.args.iter().any(|arg| arg.spread.is_some()),
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

    fn visit_tagged_tpl(&mut self, template: &TaggedTpl) {
        template.visit_children_with(self);
        self.mark_opaque();
    }

    fn visit_opt_chain_expr(&mut self, chain: &OptChainExpr) {
        let before = self.past_await;
        chain.visit_children_with(self);
        self.past_await = before;
        self.mark_opaque();
    }

    fn visit_new_expr(&mut self, new_expr: &NewExpr) {
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
            is_new: true,
            span: new_expr.span,
            has_spread: new_expr
                .args
                .as_deref()
                .unwrap_or_default()
                .iter()
                .any(|arg| arg.spread.is_some()),
            no_sync_arguments: false,
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
/// chunk. Cache their static read closure once; initialization timing is still
/// checked against the current prefix when the summary is applied.
#[derive(Default)]
struct CallbackFallbackSummary {
    expanded: BTreeSet<StatementOrdinal>,
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
    exported_owners: BTreeSet<StatementOrdinal>,
    prior_reachability_roots: Option<BTreeSet<StatementOrdinal>>,
    current_top_owner: Option<StatementOrdinal>,
    prior_effect_owners: BTreeSet<StatementOrdinal>,
    reachability: ReachabilityState,
    mutable_captures: BTreeSet<Id>,
    prefix_values: Context,
    initialized_bindings: BTreeSet<Id>,
    var_bindings: BTreeSet<Id>,
    global_escaped_callbacks: Values,
    prefix_object_properties: BTreeMap<u32, BTreeMap<Option<String>, Values>>,
    tainted_objects: BTreeSet<u32>,
    tainted_classes: BTreeSet<u32>,
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
        // The author certifies the helper's dispatch and every decorator's
        // metadata-only behavior, not purity. Preserve all static dependencies
        // of their bodies without manufacturing calls into the target methods.
        for binding in std::iter::once(&application.helper).chain(&application.decorators) {
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
            || !(fact.purity.is_pure() && fact.declares_direct_function)
        {
            roots.insert(fact.ordinal);
        }
        roots.extend(
            self.owner_read_closure
                .owners_for_bindings(fact.reads.eager.iter().chain(fact.rebinds.eager.iter())),
        );
        roots
    }

    fn extend_reachable_owners(
        &mut self,
        roots: impl IntoIterator<Item = StatementOrdinal>,
    ) -> bool {
        let before = self.reachability.owners.len();
        let reached = self.owner_read_closure.extend_reachable_owners(
            roots,
            &mut self.reachability.expanded,
            &mut self.reachability.owners,
        );
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
        if call.is_new
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

    fn apply_member_value_writes(
        &mut self,
        writes: &[(Id, Option<String>, ValueExpr)],
        env: &Context,
        captures: &Context,
    ) {
        for (receiver, property, expression) in writes {
            let receivers = self.evaluate(&ValueExpr::Binding(receiver.clone()), env, captures);
            let values = self.evaluate(expression, env, captures);
            for receiver in receivers {
                match receiver {
                    AbstractValue::GlobalObject => {
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        let before = self.global_escaped_callbacks.len();
                        self.global_escaped_callbacks.extend(callbacks);
                        if self.global_escaped_callbacks.len() != before {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    AbstractValue::Origin(ValueOrigin::Object(object)) => {
                        self.tainted_objects.insert(object);
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
    ) {
        for write in writes {
            let receivers = self.evaluate(&write.receiver, env, captures);
            self.taint_objects(&receivers);
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
                AbstractValue::Closure(_, captured) => {
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

    /// The foreign wrapper's body is opaque once invoked. Include the full
    /// lazy closure, including callbacks returned by the original callback.
    fn promote_deferred_callbacks(&mut self, captured: &Values, outcome: &mut Outcome) {
        let effect_owner = self.current_top_owner.unwrap();
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
                let (reads, rebinds) = self
                    .owner_read_closure
                    .lazy_effects_for_owner(callable.owner);
                self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
            }
        }
    }

    /// Opaque code holding a class or its prototype can retrieve and run any
    /// method, getter, or static callback. The class owner's lazy closure also
    /// covers calls made by those deferred bodies.
    fn promote_class_members(&mut self, key: u32, outcome: &mut Outcome) {
        let Some(&owner) = self.flow.callable_owner.get(&ValueOrigin::Class(key)) else {
            return;
        };
        let (reads, rebinds) = self.owner_read_closure.lazy_effects_for_owner(owner);
        let effect_owner = self.current_top_owner.unwrap_or(owner);
        self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
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
        let mut owners = BTreeSet::new();
        let mut reached = self.owner_read_closure.extend_reachable_owners(
            roots.iter().copied(),
            &mut self.callback_fallback.expanded,
            &mut owners,
        );
        if self.reachability.incomplete {
            // Dynamic lexical scope defeats the normal binding-read closure.
            for fact in self.facts {
                if self.callback_fallback.expanded.insert(fact.ordinal) {
                    reached.push(fact.ordinal);
                }
            }
        }
        for reached_owner in reached {
            let fact = &self.facts[reached_owner.0];
            for id in fact
                .reads
                .lazy
                .iter()
                .filter(|id| self.top_bindings.contains(*id))
            {
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
        // The directly truncated callable need not escape. Its static SCC
        // summary already contains the same transitive lazy effects, without
        // adding its private roots to the persistent callback cache.
        if !self.callback_fallback.expanded.contains(&owner)
            && self.fallback_local_owners.insert(owner)
        {
            let (reads, rebinds) = self.owner_read_closure.lazy_effects_for_owner(owner);
            let reads = reads
                .difference(&self.callback_fallback.reads)
                .cloned()
                .collect();
            let rebinds = rebinds
                .difference(&self.callback_fallback.rebinds)
                .cloned()
                .collect();
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
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
            ValueExpr::Call(callee, args) => {
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
        let Some((callable_owner, callable_opaque)) = self
            .index
            .callables
            .get(&key)
            .map(|callable| (callable.owner, callable.opaque))
        else {
            return Outcome::default();
        };
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
        if let Some(cached) = self.cache.get(&signature) {
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
        self.apply_member_value_writes(&callable.member_value_writes, &env, &env);
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
            );
        }
        for call in &callable.calls {
            self.execute_call(call, &env, &env, &mut outcome);
        }
        for expr in &callable.returns {
            outcome.returns.extend(self.evaluate(expr, &env, &env));
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
        if !fresh_checked_constructor && !self.fresh_mutation_callables.contains(&key) {
            self.promote_property_writes(&writes, callable.owner, &mut outcome);
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
        // Only a fresh primitive literal or a checked const alias to one is
        // safe to omit from callback effects. A pure object/function element
        // can be retained by the Set and invoked by a later opaque consumer.
        if call.is_new
            && call.caller.is_none()
            && self.set_iterable_proof.contains_constructor(call.span)
        {
            return;
        }
        // Empty intrinsic collections return before consulting an adder or
        // iterator. This proves only their construction has no callbacks;
        // later operations and any constructor arguments remain conservative.
        if call.is_new
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
        let retained_callback = matches!(args.as_slice(), [values] if !values.is_empty()
            && values.iter().all(|value| matches!(value, AbstractValue::DeferredCallbacks(_))));
        if targets.is_empty() {
            outcome.complete = false;
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.invoke_possible_callbacks(&args, call.no_sync_arguments, outcome);
        }
        let mut invoked_unknown_callbacks = targets.is_empty();
        for target in targets {
            match target {
                AbstractValue::CallbackFactory
                    if !call.is_new
                        && !call.has_spread
                        && (retained_callback
                            || matches!(
                                call.args.as_slice(),
                                [ValueExpr::Origin(ValueOrigin::Function(_))]
                            )) => {}
                AbstractValue::DeferredCallbacks(captured) => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    let mut possible_args = args.clone();
                    possible_args.push((*captured.values).clone());
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    self.invoke_possible_callbacks(&possible_args, false, outcome);
                }
                AbstractValue::ClassMembers(key) => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.promote_class_members(key, outcome);
                    self.invoke_possible_callbacks(&args, false, outcome);
                }
                AbstractValue::Instance(key, captured) => {
                    outcome.complete = false;
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.promote_class_members(key, outcome);
                    let mut possible_args = args.clone();
                    possible_args.push((*captured.values).clone());
                    self.invoke_possible_callbacks(&possible_args, false, outcome);
                }
                AbstractValue::Unavailable
                | AbstractValue::Undefined
                | AbstractValue::NonCallable => {}
                AbstractValue::ObjectAssign
                    if !call.is_new
                        && call.plain_object_sources
                        && matches!(call.args.first(), Some(ValueExpr::Binding(id)) if call.caller
                        .and_then(|caller| self.index.callables.get(&caller).map(|body| (caller, body)))
                        .is_some_and(|(caller, body)| self.stable_fresh_function_target(caller, body, id))) =>
                    {}
                AbstractValue::ObjectAssign if !call.is_new => {
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
                AbstractValue::Origin(ValueOrigin::Class(key)) if call.is_new => {
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
                AbstractValue::Origin(ValueOrigin::Function(key)) if !call.is_new => {
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
                AbstractValue::Closure(key, captured) if !call.is_new => {
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
                        self.invoke_possible_callbacks(&args, call.no_sync_arguments, outcome);
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
    let index = ExecutionIndex::collect(body, &AnalysisHints::default());
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

pub(crate) fn annotate_synchronous_calls(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    set_iterable_proof: &SetIterableProof,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisHints,
    export_reentry_policy: ExportReentryPolicy,
    facts: &mut [StatementFacts],
) {
    let index = ExecutionIndex::collect(body, hints);
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
        set_iterable_proof,
        shadowed,
        top_bindings,
        binding_owner: facts
            .iter()
            .flat_map(|fact| fact.declared.iter().cloned().map(|id| (id, fact.ordinal)))
            .collect(),
        owner_read_closure,
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
        global_escaped_callbacks: Values::new(),
        prefix_object_properties: BTreeMap::new(),
        tainted_objects: BTreeSet::new(),
        tainted_classes: BTreeSet::new(),
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
        executor.apply_member_value_writes(member_writes, &top_env, &top_captures);
        executor.promote_callback_result_writes(
            &index.top_callback_result_writes[fact.ordinal.0],
            &top_env,
            &top_captures,
            fact.ordinal,
            &mut outcome,
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
