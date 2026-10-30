//! Derived call summaries for exported provider callables: the provider body
//! evaluated once under this domain with opaque arguments (plan step 5c,
//! narrowed to bodies whose whole effect set is allocation).
//!
//! A body qualifies when every operation it performs at call time is one of:
//! reading a parameter or a body-local `const`/`let`, creating a primitive,
//! closure, object or array literal, constructing an empty census-intact keyed
//! collection, defining a class field on the fresh instance, or writing a
//! declared private field of the fresh instance. Each of those runs no code
//! outside the body (ECMA-262 CreateDataProperty, PrivateFieldSet, closure
//! and literal evaluation). Anything else is `Havoc` and yields no summary,
//! including any read of a provider binding: an importer may hold and mutate
//! what that binding denotes.

use std::collections::{BTreeMap, BTreeSet};

use swc_ecma_ast::*;

use crate::analysis_inputs::{ForeignInvocation, ForeignReturn, NativeIntrinsic};

/// The exported declaration being summarized.
pub(crate) enum ForeignBody<'a> {
    Function(&'a Function),
    Class(&'a Class),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DerivedForeignCall {
    pub(crate) invocation: ForeignInvocation,
    pub(crate) returns: BTreeSet<ForeignReturn>,
    pub(crate) retained_arguments: BTreeSet<usize>,
    pub(crate) native_constructions: BTreeSet<NativeIntrinsic>,
}

/// A value at one program point of the body.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct Value {
    /// The value may be exactly argument `i`.
    arguments: BTreeSet<usize>,
    /// The value may be an allocation of this call, from which these
    /// arguments may be reachable.
    fresh: Option<BTreeSet<usize>>,
    primitive: bool,
}

impl Value {
    fn primitive() -> Self {
        Self {
            primitive: true,
            ..Self::default()
        }
    }

    fn fresh(reaches: BTreeSet<usize>) -> Self {
        Self {
            fresh: Some(reaches),
            ..Self::default()
        }
    }

    /// Every argument an allocation storing this value can reach.
    fn reaches(&self) -> BTreeSet<usize> {
        self.arguments
            .iter()
            .chain(self.fresh.iter().flatten())
            .copied()
            .collect()
    }
}

struct Evaluator<'a, F> {
    env: BTreeMap<Id, Value>,
    parameters: usize,
    /// Arguments the fresh `this` can reach; `None` outside a constructor.
    this: Option<BTreeSet<usize>>,
    /// Declared private fields (not methods or accessors) of the class.
    private_fields: BTreeSet<&'a str>,
    collection_intact: F,
    shadowed: &'a BTreeSet<&'static str>,
    native_constructions: BTreeSet<NativeIntrinsic>,
}

/// Every argument a closure created in this body can later reach. Closures
/// capture the whole environment, so this is every parameter.
fn all_parameters(count: usize) -> BTreeSet<usize> {
    (0..count).collect()
}

/// Summarize `body` or return `None` (the call stays unknown). The caller
/// establishes stable binding identity and dynamic-scope absence;
/// `collection_intact` answers the closed census for an empty keyed
/// collection's global constructor slot.
pub(crate) fn derive_foreign_call(
    body: ForeignBody<'_>,
    shadowed: &BTreeSet<&'static str>,
    collection_intact: impl Fn(NativeIntrinsic) -> bool,
) -> Option<DerivedForeignCall> {
    match body {
        ForeignBody::Function(function) => derive_function(function, shadowed, collection_intact),
        ForeignBody::Class(class) => derive_class(class, shadowed, collection_intact),
    }
}

fn simple_parameters<'a>(patterns: impl Iterator<Item = &'a Pat>) -> Option<Vec<Id>> {
    patterns
        .map(|pattern| match pattern {
            Pat::Ident(binding) => Some(binding.id.to_id()),
            _ => None,
        })
        .collect()
}

fn derive_function(
    function: &Function,
    shadowed: &BTreeSet<&'static str>,
    collection_intact: impl Fn(NativeIntrinsic) -> bool,
) -> Option<DerivedForeignCall> {
    if function.is_async
        || function.is_generator
        || !function.decorators.is_empty()
        || function
            .params
            .iter()
            .any(|param| !param.decorators.is_empty())
    {
        return None;
    }
    let parameters = simple_parameters(function.params.iter().map(|param| &param.pat))?;
    let mut evaluator = Evaluator::new(
        &parameters,
        None,
        BTreeSet::new(),
        shadowed,
        collection_intact,
    );
    let returned = evaluator.block(&function.body.as_ref()?.stmts, false)?;
    let mut returns = BTreeSet::new();
    let mut retained_arguments = BTreeSet::new();
    returns.extend(
        returned
            .arguments
            .iter()
            .map(|&index| ForeignReturn::Argument { index }),
    );
    if let Some(reaches) = returned.fresh {
        returns.insert(ForeignReturn::FreshAllocation);
        retained_arguments.extend(reaches);
    }
    if returned.primitive {
        returns.insert(ForeignReturn::Primitive);
    }
    Some(DerivedForeignCall {
        invocation: ForeignInvocation::Call,
        returns,
        retained_arguments,
        native_constructions: evaluator.native_constructions,
    })
}

fn derive_class(
    class: &Class,
    shadowed: &BTreeSet<&'static str>,
    collection_intact: impl Fn(NativeIntrinsic) -> bool,
) -> Option<DerivedForeignCall> {
    // A derived constructor runs the superclass constructor; decorators run
    // arbitrary code at definition or construction.
    if class.super_class.is_some() || !class.decorators.is_empty() {
        return None;
    }
    let mut constructor = None;
    let mut private_fields = BTreeSet::new();
    let mut instance_fields = Vec::new();
    for member in &class.body {
        match member {
            ClassMember::Constructor(definition) => constructor = Some(definition),
            ClassMember::PrivateProp(field) => {
                if !field.decorators.is_empty() {
                    return None;
                }
                if !field.is_static {
                    private_fields.insert(field.key.name.as_ref());
                    instance_fields.push(field.value.as_deref());
                }
            }
            ClassMember::ClassProp(field) => {
                if !field.decorators.is_empty() {
                    return None;
                }
                if !field.is_static {
                    // A computed key is evaluated at class definition; the
                    // field is still defined with CreateDataPropertyOrThrow.
                    // A literal key keeps the definition free of coercion.
                    if !matches!(
                        field.key,
                        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                    ) {
                        return None;
                    }
                    instance_fields.push(field.value.as_deref());
                }
            }
            // Instance accessors run when initialized, i.e. at construction.
            ClassMember::AutoAccessor(accessor) if !accessor.is_static => return None,
            // Methods are installed at class definition; static members and
            // blocks already ran when the provider evaluated.
            ClassMember::Method(method) if !method.function.decorators.is_empty() => {
                return None;
            }
            ClassMember::PrivateMethod(method) if !method.function.decorators.is_empty() => {
                return None;
            }
            _ => {}
        }
    }
    let parameters = match constructor {
        Some(constructor) => {
            let mut patterns = Vec::new();
            for param in &constructor.params {
                // Parameter properties write public fields through [[Set]].
                let ParamOrTsParamProp::Param(param) = param else {
                    return None;
                };
                if !param.decorators.is_empty() {
                    return None;
                }
                patterns.push(&param.pat);
            }
            simple_parameters(patterns.into_iter())?
        }
        None => Vec::new(),
    };
    let mut evaluator = Evaluator::new(
        &parameters,
        Some(BTreeSet::new()),
        private_fields,
        shadowed,
        collection_intact,
    );
    // Base-class construction initializes fields before the constructor body.
    for initializer in instance_fields.into_iter().flatten() {
        let value = evaluator.expr(initializer)?;
        evaluator.store_on_this(&value);
    }
    if let Some(constructor) = constructor {
        let returned = evaluator.block(&constructor.body.as_ref()?.stmts, true)?;
        // `return` in a constructor can replace the result; only the implicit
        // completion is admitted.
        if returned != Value::primitive() {
            return None;
        }
    }
    Some(DerivedForeignCall {
        invocation: ForeignInvocation::Construct,
        returns: BTreeSet::from([ForeignReturn::FreshAllocation]),
        retained_arguments: evaluator.this.unwrap_or_default(),
        native_constructions: evaluator.native_constructions,
    })
}

impl<'a, F: Fn(NativeIntrinsic) -> bool> Evaluator<'a, F> {
    fn new(
        parameters: &[Id],
        this: Option<BTreeSet<usize>>,
        private_fields: BTreeSet<&'a str>,
        shadowed: &'a BTreeSet<&'static str>,
        collection_intact: F,
    ) -> Self {
        Self {
            parameters: parameters.len(),
            env: parameters
                .iter()
                .enumerate()
                .map(|(index, parameter)| {
                    (
                        parameter.clone(),
                        Value {
                            arguments: BTreeSet::from([index]),
                            ..Value::default()
                        },
                    )
                })
                .collect(),
            this,
            private_fields,
            collection_intact,
            shadowed,
            native_constructions: BTreeSet::new(),
        }
    }

    fn parameter_count(&self) -> usize {
        self.parameters
    }

    fn store_on_this(&mut self, value: &Value) {
        if let Some(this) = &mut self.this {
            this.extend(value.reaches());
        }
    }

    /// Straight-line statements up to the first `return`. Returns the
    /// completion value (`undefined` when the block falls through).
    fn block(&mut self, statements: &[Stmt], constructor: bool) -> Option<Value> {
        for statement in statements {
            match statement {
                Stmt::Empty(_) => {}
                Stmt::Return(ReturnStmt { arg, .. }) => {
                    return match arg {
                        Some(arg) => self.expr(arg),
                        None => Some(Value::primitive()),
                    };
                }
                Stmt::Decl(Decl::Var(declaration))
                    if matches!(declaration.kind, VarDeclKind::Const | VarDeclKind::Let) =>
                {
                    for declarator in &declaration.decls {
                        let Pat::Ident(binding) = &declarator.name else {
                            return None;
                        };
                        let value = match &declarator.init {
                            Some(init) => self.expr(init)?,
                            None => Value::primitive(),
                        };
                        self.env.insert(binding.id.to_id(), value);
                    }
                }
                Stmt::Expr(ExprStmt { expr, .. }) if constructor => {
                    self.private_field_write(expr)?;
                }
                _ => return None,
            }
        }
        Some(Value::primitive())
    }

    /// `this.#field = value` on a declared private field of the fresh
    /// instance (PrivateFieldSet: no setter, no proxy trap).
    fn private_field_write(&mut self, expression: &Expr) -> Option<()> {
        let Expr::Assign(AssignExpr {
            op: AssignOp::Assign,
            left: AssignTarget::Simple(SimpleAssignTarget::Member(member)),
            right,
            ..
        }) = expression.unwrap_parens()
        else {
            return None;
        };
        let MemberProp::PrivateName(name) = &member.prop else {
            return None;
        };
        if !matches!(member.obj.unwrap_parens(), Expr::This(_))
            || !self.private_fields.contains(name.name.as_ref())
        {
            return None;
        }
        let value = self.expr(right)?;
        self.store_on_this(&value);
        Some(())
    }

    fn expr(&mut self, expression: &Expr) -> Option<Value> {
        match expression.unwrap_parens() {
            Expr::Ident(binding) => self.env.get(&binding.to_id()).cloned(),
            Expr::Lit(Lit::Regex(_)) => Some(Value::fresh(BTreeSet::new())),
            Expr::Lit(_) => Some(Value::primitive()),
            Expr::Tpl(template) if template.exprs.is_empty() => Some(Value::primitive()),
            Expr::Unary(UnaryExpr {
                op: UnaryOp::Void,
                arg,
                ..
            }) if matches!(arg.unwrap_parens(), Expr::Lit(_)) => Some(Value::primitive()),
            Expr::This(_) => self
                .this
                .as_ref()
                .map(|reaches| Value::fresh(reaches.clone())),
            Expr::Arrow(arrow) if !arrow.is_async && !arrow.is_generator => {
                Some(Value::fresh(all_parameters(self.parameter_count())))
            }
            Expr::Fn(function)
                if !function.function.is_async
                    && !function.function.is_generator
                    && function.function.decorators.is_empty() =>
            {
                Some(Value::fresh(all_parameters(self.parameter_count())))
            }
            Expr::Array(array) => {
                let mut reaches = BTreeSet::new();
                for element in array.elems.iter().flatten() {
                    if element.spread.is_some() {
                        return None;
                    }
                    reaches.extend(self.expr(&element.expr)?.reaches());
                }
                Some(Value::fresh(reaches))
            }
            Expr::Object(object) => {
                let mut reaches = BTreeSet::new();
                for property in &object.props {
                    let PropOrSpread::Prop(property) = property else {
                        return None;
                    };
                    match property.as_ref() {
                        Prop::Shorthand(binding) => {
                            reaches.extend(self.env.get(&binding.to_id())?.reaches());
                        }
                        Prop::KeyValue(KeyValueProp { key, value }) => {
                            if !literal_key(key) {
                                return None;
                            }
                            reaches.extend(self.expr(value)?.reaches());
                        }
                        Prop::Method(MethodProp { key, function })
                            if literal_key(key) && function.decorators.is_empty() =>
                        {
                            reaches.extend(all_parameters(self.parameter_count()));
                        }
                        Prop::Getter(GetterProp { key, .. })
                        | Prop::Setter(SetterProp { key, .. })
                            if literal_key(key) =>
                        {
                            reaches.extend(all_parameters(self.parameter_count()));
                        }
                        _ => return None,
                    }
                }
                Some(Value::fresh(reaches))
            }
            Expr::New(NewExpr { callee, args, .. })
                if args.as_deref().is_none_or(<[ExprOrSpread]>::is_empty) =>
            {
                let Expr::Ident(constructor) = callee.unwrap_parens() else {
                    return None;
                };
                let family = match constructor.sym.as_ref() {
                    "Map" => NativeIntrinsic::Map,
                    "Set" => NativeIntrinsic::Set,
                    "WeakMap" => NativeIntrinsic::WeakMap,
                    "WeakSet" => NativeIntrinsic::WeakSet,
                    _ => return None,
                };
                if self.env.contains_key(&constructor.to_id())
                    || self.shadowed.contains(constructor.sym.as_ref())
                    || !(self.collection_intact)(family)
                {
                    return None;
                }
                self.native_constructions.insert(family);
                Some(Value::fresh(BTreeSet::new()))
            }
            _ => None,
        }
    }
}

/// A non-computed key other than `__proto__`, whose `:` form sets the
/// prototype instead of defining a property.
fn literal_key(key: &PropName) -> bool {
    match key {
        PropName::Ident(name) => name.sym.as_ref() != "__proto__",
        PropName::Str(name) => name.value.as_str() != Some("__proto__"),
        PropName::Num(_) => true,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn derive(source: &str, export: &str) -> Option<DerivedForeignCall> {
        let module = js_ast::parse_js_module("provider.js", source)
            .expect("fixture parses")
            .module;
        let body = module.body.iter().find_map(|item| match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration)))
                if declaration.ident.sym.as_ref() == export =>
            {
                Some(ForeignBody::Function(&declaration.function))
            }
            ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration)))
                if declaration.ident.sym.as_ref() == export =>
            {
                Some(ForeignBody::Class(&declaration.class))
            }
            _ => None,
        })?;
        derive_foreign_call(body, &BTreeSet::new(), |_| true)
    }

    fn call(returns: &[ForeignReturn], retained: &[usize]) -> Option<DerivedForeignCall> {
        Some(DerivedForeignCall {
            invocation: ForeignInvocation::Call,
            returns: returns.iter().copied().collect(),
            retained_arguments: retained.iter().copied().collect(),
            native_constructions: BTreeSet::new(),
        })
    }

    #[test]
    fn allocation_only_bodies_are_summarized() {
        js_ast::with_swc_globals(|| {
            assert_eq!(
                derive("function keep(value, reset) { return value; }", "keep"),
                call(&[ForeignReturn::Argument { index: 0 }], &[])
            );
            assert_eq!(
                derive(
                    "function object(shape, options) { const tag = 'object'; return { tag, shape, [1]: 0 }; }",
                    "object"
                ),
                None,
                "a computed key may coerce"
            );
            assert_eq!(
                derive(
                    "function object(shape, options) { const tag = 'object'; return { tag, shape }; }",
                    "object"
                ),
                call(&[ForeignReturn::FreshAllocation], &[0])
            );
            assert_eq!(
                derive(
                    "function cell() { let t; return { set: (v) => { t = v; }, get: () => t }; }",
                    "cell"
                ),
                call(&[ForeignReturn::FreshAllocation], &[])
            );
            assert_eq!(
                derive(
                    "class Box { #callback; #cache = new WeakMap(); constructor(callback, label) { this.#callback = callback; } of(key) { return this.#callback(key); } }",
                    "Box"
                ),
                Some(DerivedForeignCall {
                    invocation: ForeignInvocation::Construct,
                    returns: BTreeSet::from([ForeignReturn::FreshAllocation]),
                    retained_arguments: BTreeSet::from([0]),
                    native_constructions: BTreeSet::from([NativeIntrinsic::WeakMap]),
                })
            );
        });
    }

    #[test]
    fn any_operation_that_can_run_other_code_has_no_summary() {
        js_ast::with_swc_globals(|| {
            for (source, export) in [
                ("function run(callback) { return callback(); }", "run"),
                ("function read(input) { return input.value; }", "read"),
                ("function spread(input) { return { ...input }; }", "spread"),
                ("function add(a, b) { return a + b; }", "add"),
                ("function text(a) { return `${a}`; }", "text"),
                (
                    "const registry = { lazy: (e) => e };\nfunction lazy(e) { return registry.lazy(e); }",
                    "lazy",
                ),
                ("function global(e) { return window; }", "global"),
                ("function defaults(e = later()) { return e; }", "defaults"),
                ("function pick({ value }) { return value; }", "pick"),
                ("async function later(e) { return e; }", "later"),
                ("function* steps(e) { yield e; }", "steps"),
                ("function setter(e) { e = 1; return e; }", "setter"),
                ("function make() { return new Date(); }", "make"),
                ("function make() { return new Map([[1, 2]]); }", "make"),
                ("function proto(e) { return { __proto__: e }; }", "proto"),
                (
                    "class Base {}\nclass Derived extends Base { constructor() { super(); } }",
                    "Derived",
                ),
                (
                    "class Public { constructor(e) { this.callback = e; } }",
                    "Public",
                ),
                (
                    "class Accessor { set #value(v) {} constructor(e) { this.#value = e; } }",
                    "Accessor",
                ),
                (
                    "class Replaced { constructor(e) { return e; } }",
                    "Replaced",
                ),
                ("class Field { value = compute(); }", "Field"),
                ("class Params { constructor({ e }) {} }", "Params"),
            ] {
                assert_eq!(derive(source, export), None, "{source}");
            }
        });
    }

    #[test]
    fn census_and_shadowing_gate_collection_construction() {
        js_ast::with_swc_globals(|| {
            let module =
                js_ast::parse_js_module("provider.js", "function make() { return new WeakMap(); }")
                    .expect("fixture parses")
                    .module;
            let ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) = &module.body[0] else {
                unreachable!()
            };
            let body = || ForeignBody::Function(&declaration.function);
            assert!(derive_foreign_call(body(), &BTreeSet::new(), |_| true).is_some());
            assert!(derive_foreign_call(body(), &BTreeSet::new(), |_| false).is_none());
            assert!(derive_foreign_call(body(), &BTreeSet::from(["WeakMap"]), |_| true).is_none());
        });
    }
}
