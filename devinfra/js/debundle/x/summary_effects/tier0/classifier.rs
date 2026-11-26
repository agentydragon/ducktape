use super::*;

/// The global a recognized intrinsic use depends on, with the properties of
/// it or of its prototype that must be untouched.
pub(super) type Dependency = (&'static str, &'static [&'static str]);

/// Intrinsic constructors a class may extend: the definition reads the
/// global binding and its `prototype`, a non-writable data property.
pub(super) const HERITAGE: [&str; 11] = [
    "Error",
    "TypeError",
    "RangeError",
    "SyntaxError",
    "ReferenceError",
    "EvalError",
    "URIError",
    "Array",
    "Map",
    "Set",
    "Promise",
];

/// Globals whose own properties are all data properties.
pub(super) const NAMESPACES: [&str; 14] = [
    "Object", "Array", "Function", "String", "Number", "Boolean", "Symbol", "Math", "JSON",
    "Reflect", "Date", "Promise", "Map", "Set",
];

/// Own properties of intrinsic namespaces. A read of one is not answered by
/// any prototype object; any other name is looked up on `Function.prototype`
/// or `Object.prototype`.
pub(super) const OWN_STATICS: [(&str, &[&str]); 9] = [
    (
        "Object",
        &[
            "assign",
            "create",
            "defineProperties",
            "defineProperty",
            "entries",
            "freeze",
            "fromEntries",
            "getOwnPropertyDescriptor",
            "getOwnPropertyDescriptors",
            "getOwnPropertyNames",
            "getOwnPropertySymbols",
            "getPrototypeOf",
            "is",
            "isFrozen",
            "keys",
            "setPrototypeOf",
            "values",
        ],
    ),
    ("Array", &["from", "isArray", "of"]),
    (
        "Symbol",
        &[
            "asyncIterator",
            "for",
            "hasInstance",
            "iterator",
            "toPrimitive",
            "toStringTag",
        ],
    ),
    (
        "Math",
        &[
            "PI", "abs", "ceil", "cos", "floor", "max", "min", "pow", "random", "round", "sin",
            "sqrt", "trunc",
        ],
    ),
    (
        "Number",
        &[
            "EPSILON",
            "MAX_SAFE_INTEGER",
            "MIN_SAFE_INTEGER",
            "isFinite",
            "isInteger",
            "isNaN",
            "parseFloat",
            "parseInt",
        ],
    ),
    ("String", &["fromCharCode", "fromCodePoint", "raw"]),
    ("JSON", &["parse", "stringify"]),
    (
        "Reflect",
        &[
            "apply",
            "construct",
            "defineProperty",
            "get",
            "getPrototypeOf",
            "has",
            "ownKeys",
            "set",
        ],
    ),
    ("Date", &["UTC", "now", "parse"]),
];

/// Globals whose `prototype` holds only data properties, apart from the
/// names in `PROTOTYPE_ACCESSORS`.
pub(super) const DATA_PROTOTYPES: [&str; 6] =
    ["Object", "Array", "Function", "String", "Number", "Boolean"];
pub(super) const PROTOTYPE_ACCESSORS: [&str; 3] = ["__proto__", "caller", "arguments"];

pub(super) const TYPED_ARRAYS: [&str; 9] = [
    "Int8Array",
    "Uint8Array",
    "Uint8ClampedArray",
    "Int16Array",
    "Uint16Array",
    "Int32Array",
    "Uint32Array",
    "Float32Array",
    "Float64Array",
];

/// A length a typed array accepts without a `RangeError`.
pub(super) fn small_length(expr: &Expr) -> bool {
    matches!(expr, Expr::Lit(Lit::Num(number))
        if number.value.fract() == 0.0 && (0.0..=65_536.0).contains(&number.value))
}

pub(super) struct Classifier<'a> {
    pub(super) bindings: &'a FxHashMap<Id, Binding>,
    pub(super) imports: &'a FxHashSet<Binding>,
    /// Bindings proven to only ever hold a primitive.
    pub(super) primitives: &'a FxHashSet<Binding>,
    /// Bindings that only ever hold one class.
    pub(super) classes: &'a FxHashSet<Binding>,
    /// Bindings that only ever hold one plain object literal, with its keys.
    /// Each key says whether its value is written as a primitive literal.
    pub(super) literals: &'a FxHashMap<Binding, FxHashMap<String, bool>>,
    /// Bindings that only ever hold one `Symbol(…)` or `Symbol.for(…)`.
    pub(super) symbols: &'a FxHashSet<Binding>,
    /// Bindings that only ever hold one array literal.
    pub(super) arrays: &'a FxHashSet<Binding>,
    pub(super) settled: &'a Settled,
    /// Members of such literals written as plain object literals, with
    /// their keys, where the inner object cannot change either.
    pub(super) sealed_members: &'a FxHashSet<(Binding, String)>,
    pub(super) nested_literals: &'a FxHashMap<(Binding, String), FxHashMap<String, bool>>,
    /// Bindings that only ever hold a function pure over primitives, with
    /// the bindings a call of it reads: see `primitive_function`.
    pub(super) primitive_functions: &'a FxHashMap<Binding, Vec<Binding>>,
    /// Bindings only enum-shaped statements assign: see `enum_definition`.
    pub(super) enums: &'a FxHashSet<Binding>,
    /// Bindings a `var` declares by an enum-shaped call on itself.
    pub(super) enum_declarations: &'a FxHashSet<Binding>,
    /// Literals nothing in the chunk can change: see `sealed_literals`.
    pub(super) sealed: &'a FxHashSet<Binding>,
    /// Literals the statement reads a property of.
    pub(super) pristine: Vec<Binding>,
    pub(super) upgrades: &'a FxHashSet<Upgrade>,
    pub(super) census: &'a Census,
    pub(super) unresolved: SyntaxContext,
    pub(super) eager: Vec<Binding>,
    /// The globals the accepted intrinsic uses depend on.
    pub(super) intrinsics: Vec<&'static str>,
    /// Intrinsic uses the census refused.
    pub(super) denied: Vec<(&'static str, crate::intrinsics::Refusal)>,
    pub(super) premises: &'a ResolvedPremises,
    /// Bindings read by the arguments of premised calls.
    pub(super) deep: Vec<Binding>,
    pub(super) targets: Vec<Binding>,
    pub(super) replaces: Vec<Binding>,
    pub(super) premise_uses: Vec<PremiseKind>,
    /// Positions in `eager` that hold the callee of a premised call.
    pub(super) callee_slots: FxHashSet<usize>,
    /// Nesting depth in the arguments of premised calls.
    pub(super) premised_args: usize,
    pub(super) providers: Vec<u32>,
    /// Lazy-module thunks the statement calls when it runs.
    pub(super) lazy_uses: Vec<Binding>,
    /// A site premise waives the census for this statement.
    pub(super) native_site: bool,
    pub(super) never_written: &'a FxHashSet<String>,
}

/// Premises resolved to this chunk's bindings.
/// Bindings of the chunk whose contract derivation found.
#[derive(Default, Clone, serde::Serialize)]
pub struct DerivedCounts {
    pub allocators: usize,
    /// Allocators that also touch state private to their chunk.
    pub contained: usize,
    pub allocator_classes: usize,
    pub function_namers: usize,
    pub export_helpers: usize,
    pub stable_imports: usize,
    pub settled_locals: usize,
    pub namespace_members: usize,
    pub lazy_wrappers: usize,
    pub interop_helpers: usize,
    pub lazy_modules: usize,
    /// Those of `lazy_modules` whose body is in this chunk.
    pub local_lazy_modules: usize,
    /// Imports that only ever hold a primitive.
    pub primitive_imports: usize,
    /// Imports holding an object literal the program only reads named
    /// members of.
    pub settled_imports: usize,
}

#[derive(Default)]
pub struct ResolvedPremises {
    /// Per callee: every kind it was given, and its target argument.
    pub(super) callees: FxHashMap<Binding, (Vec<PremiseKind>, usize)>,
    pub(super) members: FxHashMap<(Binding, String), (Vec<PremiseKind>, usize)>,
    /// Per imported binding: its source, numbered from 1.
    pub(super) providers: FxHashMap<Binding, u32>,
    /// Per derived allocator: the bindings a call of it reads.
    pub(super) allocator_reads: FxHashMap<Binding, Vec<Binding>>,
    /// Per derived allocator: argument positions that must be fresh object
    /// literals.
    pub(super) literal_params: FxHashMap<Binding, Vec<usize>>,
    /// Per derived allocator: the intrinsic collections it constructs.
    pub(super) allocator_intrinsics: FxHashMap<Binding, Vec<&'static str>>,
    /// Derived: bindings holding the helper that defines getters on its
    /// first argument from the functions of its second.
    pub(super) exporters: FxHashSet<Binding>,
    /// Derived: bindings holding the helper that returns a fresh object
    /// with a getter per function of its argument.
    pub(super) exporter_allocators: FxHashSet<Binding>,
    /// Derived: imports whose value is fixed once their chunk has run, for
    /// a chunk off any import cycle.
    pub(super) stable_imports: FxHashSet<Binding>,
    /// Derived: those of them whose exporting chunk declares a class: its
    /// `prototype` cannot be replaced.
    pub(super) stable_class_imports: FxHashSet<Binding>,
    /// Derived: literals of this chunk the whole program only reads named
    /// members of.
    pub(super) settled_locals: FxHashSet<Binding>,
    pub given: usize,
    pub matched: usize,
}

/// What a call or construction of a callee with these kinds is. A callee
/// that is both pure and a callback factory only allocates and retains its
/// arguments.
pub(super) fn call_kind(kinds: &[PremiseKind], constructs: bool) -> Option<PremiseKind> {
    let has = |kind: &PremiseKind| kinds.contains(kind);
    if constructs {
        return [PremiseKind::PureNew, PremiseKind::AllocatorNew]
            .into_iter()
            .find(has);
    }
    if has(&PremiseKind::Allocator)
        || (has(&PremiseKind::PureCall) && has(&PremiseKind::CallbackFactory))
    {
        return Some(PremiseKind::Allocator);
    }
    [
        PremiseKind::PureCall,
        PremiseKind::CallbackFactory,
        PremiseKind::LazyModule,
        PremiseKind::InteropNamespace,
        PremiseKind::FunctionNamer,
    ]
    .into_iter()
    .find(has)
}

impl ResolvedPremises {
    /// Whose private state a call of `binding` may touch.
    pub(super) fn provider(&self, binding: Binding) -> u32 {
        self.providers.get(&binding).copied().unwrap_or(0)
    }
}

/// Which additional primitive leaves are known at the evaluation site.
#[derive(Clone, Copy)]
pub(super) enum PrimitiveScope<'a> {
    FunctionParameters(&'a FxHashSet<Id>),
    Initialization(&'a Settled),
}

/// Whether `expr` evaluates to a number, string, boolean, `null` or
/// `undefined` without running code or throwing, given the primitive
/// bindings; collects the bindings it reads.
pub(super) fn primitive(
    expr: &Expr,
    bindings: &FxHashMap<Id, Binding>,
    primitives: &FxHashSet<Binding>,
    scope: PrimitiveScope<'_>,
    reads: &mut Vec<Binding>,
) -> bool {
    let mut recurse = |expr: &Expr| primitive(expr, bindings, primitives, scope, reads);
    match expr.unwrap_parens() {
        // A member of a settled import, written as a primitive.
        Expr::Member(MemberExpr {
            obj,
            prop: MemberProp::Ident(key),
            ..
        }) => {
            let (PrimitiveScope::Initialization(settled), Expr::Ident(table)) = (scope, &**obj)
            else {
                return false;
            };
            let Some(binding) = bindings.get(&table.to_id()) else {
                return false;
            };
            let primitive = settled
                .get(binding)
                .and_then(|keys| keys.get(&*key.sym))
                .copied()
                == Some(true);
            if primitive {
                reads.push(*binding);
            }
            primitive
        }
        Expr::Lit(Lit::Num(_) | Lit::Str(_) | Lit::Bool(_) | Lit::Null(_)) => true,
        Expr::Ident(ident) => {
            let id = ident.to_id();
            if matches!(scope, PrimitiveScope::FunctionParameters(params) if params.contains(&id)) {
                return true;
            }
            let Some(binding) = bindings
                .get(&id)
                .filter(|binding| primitives.contains(binding))
            else {
                return false;
            };
            reads.push(*binding);
            true
        }
        Expr::Tpl(template) => template.exprs.iter().all(|expr| recurse(expr)),
        Expr::Unary(unary) => {
            matches!(
                unary.op,
                UnaryOp::Minus
                    | UnaryOp::Plus
                    | UnaryOp::Bang
                    | UnaryOp::Tilde
                    | UnaryOp::Void
                    | UnaryOp::TypeOf
            ) && recurse(&unary.arg)
        }
        Expr::Bin(binary) => {
            !matches!(binary.op, BinaryOp::In | BinaryOp::InstanceOf)
                && recurse(&binary.left)
                && recurse(&binary.right)
        }
        Expr::Cond(cond) => recurse(&cond.test) && recurse(&cond.cons) && recurse(&cond.alt),
        _ => false,
    }
}

/// Imports holding an object literal that nothing in the program handles
/// except to read a named member: per key, whether its value is written as
/// a primitive.
pub(super) type Settled = FxHashMap<Binding, FxHashMap<String, bool>>;

pub(super) fn literal_only(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => literal_only(&paren.expr),
        Expr::Lit(Lit::Num(_) | Lit::Str(_) | Lit::Bool(_) | Lit::Null(_)) => true,
        Expr::Tpl(template) => template.exprs.is_empty(),
        Expr::Unary(unary) => {
            matches!(
                unary.op,
                UnaryOp::Minus | UnaryOp::Plus | UnaryOp::Bang | UnaryOp::Tilde | UnaryOp::Void
            ) && literal_only(&unary.arg)
        }
        // Operators over numbers, strings, booleans and `null` convert
        // without user code and never throw.
        Expr::Bin(binary) => {
            !matches!(binary.op, BinaryOp::In | BinaryOp::InstanceOf)
                && literal_only(&binary.left)
                && literal_only(&binary.right)
        }
        Expr::Cond(cond) => {
            literal_only(&cond.test) && literal_only(&cond.cons) && literal_only(&cond.alt)
        }
        _ => false,
    }
}

impl Classifier<'_> {
    pub(super) fn call_cause(&self, callee: &Expr) -> ImpureCause {
        match callee {
            Expr::Paren(paren) => self.call_cause(&paren.expr),
            Expr::Seq(seq) => match seq.exprs.last() {
                Some(last) => self.call_cause(last),
                None => ImpureCause::CallOther,
            },
            Expr::Member(_) | Expr::OptChain(_) | Expr::SuperProp(_) => ImpureCause::CallMember,
            Expr::Ident(ident) if ident.ctxt == self.unresolved => ImpureCause::CallGlobal,
            Expr::Ident(ident) => match self.bindings.get(&ident.to_id()) {
                Some(binding) if self.imports.contains(binding) => ImpureCause::CallImport,
                Some(_) => ImpureCause::CallLocal,
                None => ImpureCause::CallOther,
            },
            _ => ImpureCause::CallOther,
        }
    }

    pub(super) fn key(&mut self, key: &PropName) -> Result<(), ImpureCause> {
        match key {
            PropName::Ident(name) if &*name.sym == "__proto__" => Err(ImpureCause::Unmodeled),
            PropName::Str(value) if js_ast::str_value(value) == "__proto__" => {
                Err(ImpureCause::Unmodeled)
            }
            PropName::Str(_) => Ok(()),
            PropName::Ident(_) | PropName::Num(_) | PropName::BigInt(_) => Ok(()),
            PropName::Computed(computed) => self.computed_key(&computed.expr),
        }
    }

    /// A computed key that converts to a property key without running
    /// code: a primitive, or a symbol.
    pub(super) fn computed_key(&mut self, expr: &Expr) -> Result<(), ImpureCause> {
        let converts = match expr {
            Expr::Paren(paren) => return self.computed_key(&paren.expr),
            // A binding that only ever holds one `Symbol(…)`.
            Expr::Ident(ident) => {
                let symbol = self
                    .bindings
                    .get(&ident.to_id())
                    .copied()
                    .filter(|binding| self.symbols.contains(binding));
                self.eager.extend(symbol);
                (symbol.is_some() && self.untouched(vec![("Symbol", &[])], None, false))
                    || self.primitive_arg(expr)
            }
            // `Symbol.iterator`, or a primitive entry of a literal table.
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(key),
                ..
            }) => {
                if matches!(self.global(obj).as_deref(), Some("Symbol")) {
                    self.pure(expr).is_ok()
                } else {
                    let primitive_entry = self
                        .literal_member_read(expr)
                        .filter(|table| self.literals[table].get(&*key.sym).copied() == Some(true));
                    if let Some(table) = primitive_entry {
                        self.read_literal(table);
                    }
                    // Or a primitive member of a settled import.
                    primitive_entry.is_some() || self.primitive_arg(expr)
                }
            }
            other => literal_only(other) || self.primitive_arg(other),
        };
        if converts {
            Ok(())
        } else {
            Err(ImpureCause::Conversion)
        }
    }

    pub(super) fn global(&self, expr: &Expr) -> Option<String> {
        match expr {
            Expr::Ident(ident) if ident.ctxt == self.unresolved => Some(ident.sym.to_string()),
            _ => None,
        }
    }

    /// A call of a function pure over primitives with primitive arguments.
    pub(super) fn primitive_call(&mut self, call: &CallExpr) -> bool {
        let Callee::Expr(callee) = &call.callee else {
            return false;
        };
        let Expr::Ident(callee) = unparen(callee) else {
            return false;
        };
        let Some(binding) = self.bindings.get(&callee.to_id()).copied() else {
            return false;
        };
        let Some(reads) = self.primitive_functions.get(&binding) else {
            return false;
        };
        let primitive_arguments = call
            .args
            .iter()
            .all(|arg| arg.spread.is_none() && self.primitive_arg(&arg.expr));
        if primitive_arguments {
            self.eager.push(binding);
            self.eager.extend(reads);
        }
        primitive_arguments
    }

    pub(super) fn primitive_arg(&mut self, expr: &Expr) -> bool {
        let mut reads = Vec::new();
        let ok = primitive(
            expr,
            self.bindings,
            self.primitives,
            PrimitiveScope::Initialization(self.settled),
            &mut reads,
        );
        if ok {
            self.eager.extend(reads);
        }
        ok
    }

    /// An array literal without holes or spreads whose elements all pass.
    pub(super) fn array_of(
        &mut self,
        expr: &Expr,
        mut element: impl FnMut(&mut Self, &Expr) -> bool,
    ) -> bool {
        let Expr::Array(array) = expr else {
            return false;
        };
        array.elems.iter().all(|slot| match slot {
            Some(ExprOrSpread { spread: None, expr }) => element(self, expr),
            _ => false,
        })
    }

    /// A `new` or call that only allocates, runs no user code and cannot
    /// throw, provided its intrinsic family is unmodified:
    ///
    /// - `new Map()`, `new Set()`, `new WeakMap()`, `new WeakSet()`: an empty
    ///   collection; no iterable is read.
    /// - `new Set([p, …])` and `new Map([[p, e], …])` over primitive keys
    ///   `p` and pure values `e`: iterate a fresh array literal with the
    ///   intrinsic array iterator and call the intrinsic adder.
    /// - `new <TypedArray>(n)` for a small non-negative integer literal `n`,
    ///   or over an array literal of primitives: converts numbers only.
    /// - `Object.freeze(<object or array literal>)`: defines no accessor and
    ///   calls none; the literal is fresh.
    /// - `Object.create(null)`: a fresh object without a prototype.
    /// - `Symbol()`, `Symbol(p)`, `Symbol.for(p)` over a primitive `p`:
    ///   `ToString` of a primitive.
    pub(super) fn native_allocation(&mut self, expr: &Expr) -> Option<Vec<Dependency>> {
        if !self.upgrades.contains(&Upgrade::NativeAllocators) {
            return None;
        }
        const ARRAY_ITERATION: Dependency = ("Array", &["@@iterator"]);
        match expr {
            Expr::New(new) => {
                let name = self.global(&new.callee)?;
                let args = new.args.as_deref().unwrap_or_default();
                if args.iter().any(|arg| arg.spread.is_some()) {
                    return None;
                }
                let args: Vec<&Expr> = args.iter().map(|arg| &*arg.expr).collect();
                match (name.as_str(), args.as_slice()) {
                    ("Map", []) => Some(vec![("Map", &[])]),
                    ("Set", []) => Some(vec![("Set", &[])]),
                    ("WeakMap", []) => Some(vec![("WeakMap", &[])]),
                    ("WeakSet", []) => Some(vec![("WeakSet", &[])]),
                    ("Set", [elements]) => self
                        .array_of(elements, |this, element| this.primitive_arg(element))
                        .then(|| vec![("Set", &["add"] as &[_]), ARRAY_ITERATION]),
                    ("Map", [entries]) => self
                        .array_of(entries, |this, entry| match entry {
                            Expr::Array(pair) => match pair.elems.as_slice() {
                                [
                                    Some(ExprOrSpread {
                                        spread: None,
                                        expr: key,
                                    }),
                                    Some(ExprOrSpread {
                                        spread: None,
                                        expr: value,
                                    }),
                                ] => this.primitive_arg(key) && this.pure(value).is_ok(),
                                _ => false,
                            },
                            _ => false,
                        })
                        .then(|| vec![("Map", &["set"] as &[_]), ARRAY_ITERATION]),
                    (name, [arg]) => {
                        let typed = *TYPED_ARRAYS.iter().find(|typed| **typed == name)?;
                        if small_length(arg) {
                            Some(vec![(typed, &[])])
                        } else {
                            self.array_of(arg, |this, element| this.primitive_arg(element))
                                .then(|| vec![(typed, &[] as &[_]), ARRAY_ITERATION])
                        }
                    }
                    _ => None,
                }
            }
            Expr::Call(CallExpr {
                callee: Callee::Expr(callee),
                args,
                ..
            }) => {
                if args.iter().any(|arg| arg.spread.is_some()) {
                    return None;
                }
                let args: Vec<&Expr> = args.iter().map(|arg| &*arg.expr).collect();
                let (object, method) = match &**callee {
                    Expr::Member(MemberExpr {
                        obj,
                        prop: MemberProp::Ident(method),
                        ..
                    }) => (self.global(obj)?, Some(method.sym.to_string())),
                    other => (self.global(other)?, None),
                };
                match (object.as_str(), method.as_deref(), args.as_slice()) {
                    ("Object", Some("freeze"), [literal @ (Expr::Object(_) | Expr::Array(_))]) => {
                        self.pure(literal)
                            .is_ok()
                            .then(|| vec![("Object", &["freeze"] as &[_])])
                    }
                    ("Object", Some("create"), [Expr::Lit(Lit::Null(_))]) => {
                        Some(vec![("Object", &["create"])])
                    }
                    ("Symbol", None, []) => Some(vec![("Symbol", &[])]),
                    ("Symbol", None, [description]) => self
                        .primitive_arg(description)
                        .then(|| vec![("Symbol", &[] as &[_])]),
                    ("Symbol", Some("for"), [description]) => self
                        .primitive_arg(description)
                        .then(|| vec![("Symbol", &["for"] as &[_])]),
                    _ => None,
                }
            }
            _ => None,
        }
    }

    /// A read of a data property of an intrinsic namespace (`Math.PI`,
    /// `Object.assign`) or prototype (`Object.prototype.toString`). The
    /// value is a primitive or an intrinsic function; reading it runs no
    /// code and cannot throw. The `prototype` object itself is not accepted:
    /// holding it would let later code write the intrinsic unseen.
    pub(super) fn intrinsic_member_read(&self, expr: &Expr) -> Option<Vec<Dependency>> {
        if !self.upgrades.contains(&Upgrade::IntrinsicMemberReads) {
            return None;
        }
        let Expr::Member(MemberExpr {
            obj,
            prop: MemberProp::Ident(prop),
            ..
        }) = expr
        else {
            return None;
        };
        let prop: &str = &prop.sym;
        let (global, on_prototype) = match &**obj {
            Expr::Member(MemberExpr {
                obj: root,
                prop: MemberProp::Ident(prototype),
                ..
            }) if &*prototype.sym == "prototype" => (self.global(root)?, true),
            other => (self.global(other)?, false),
        };
        let list: &[&'static str] = if on_prototype {
            &DATA_PROTOTYPES
        } else {
            &NAMESPACES
        };
        let global = *list.iter().find(|name| **name == global)?;
        let accepted = if on_prototype {
            !PROTOTYPE_ACCESSORS.contains(&prop)
        } else {
            prop != "prototype"
        };
        // The property name is checked against the census by the caller.
        accepted.then(|| vec![(global, &[] as &[_])])
    }

    /// Whether the chunk leaves every dependency untouched; records them.
    /// `prototype_members` says the properties are members of the
    /// intrinsic's prototype object, not of the constructor or namespace.
    pub(super) fn untouched(
        &mut self,
        dependencies: Vec<Dependency>,
        extra_prop: Option<&str>,
        prototype_members: bool,
    ) -> bool {
        let refusal = dependencies.iter().find_map(|(global, props)| {
            let mut props: Vec<&str> = props.to_vec();
            props.extend(extra_prop);
            if self.native_site || self.never_written.contains(*global) {
                return None;
            }
            let refusal = self.census.refusal(global, &props, prototype_members)?;
            Some((*global, refusal))
        });
        match refusal {
            None => self
                .intrinsics
                .extend(dependencies.iter().map(|(global, _)| *global)),
            Some(refusal) => self.denied.push(refusal),
        }
        refusal.is_none()
    }

    /// Records a read of a property of the literal `table` holds. The read
    /// depends on the state of the object unless nothing can change it.
    pub(super) fn read_literal(&mut self, table: Binding) {
        self.eager.push(table);
        if !self.sealed.contains(&table) {
            self.deep.push(table);
            self.pristine.push(table);
        }
    }

    /// `table.member.key` where `table.member` is written as a plain object
    /// literal with that key and neither object can change.
    pub(super) fn nested_member_read(&self, expr: &Expr) -> Option<Binding> {
        if !self.upgrades.contains(&Upgrade::LiteralMemberReads) {
            return None;
        }
        let Expr::Member(MemberExpr {
            obj,
            prop: MemberProp::Ident(key),
            ..
        }) = expr
        else {
            return None;
        };
        let Expr::Member(MemberExpr {
            obj: table,
            prop: MemberProp::Ident(member),
            ..
        }) = &**obj
        else {
            return None;
        };
        let Expr::Ident(table) = &**table else {
            return None;
        };
        let pair = (*self.bindings.get(&table.to_id())?, member.sym.to_string());
        (self.sealed_members.contains(&pair)
            && self.nested_literals.get(&pair)?.contains_key(&*key.sym))
        .then_some(pair.0)
    }

    /// `table.key` for a binding that only ever holds one plain object
    /// literal with that key.
    pub(super) fn literal_member_read(&self, expr: &Expr) -> Option<Binding> {
        if !self.upgrades.contains(&Upgrade::LiteralMemberReads) {
            return None;
        }
        let Expr::Member(MemberExpr {
            obj,
            prop: MemberProp::Ident(key),
            ..
        }) = expr
        else {
            return None;
        };
        let Expr::Ident(table) = &**obj else {
            return None;
        };
        let binding = *self.bindings.get(&table.to_id())?;
        self.literals
            .get(&binding)?
            .contains_key(&*key.sym)
            .then_some(binding)
    }

    /// The premise on a callee expression, with the binding it reads.
    pub(super) fn premise(&self, callee: &Expr) -> Option<(Vec<PremiseKind>, usize, Binding)> {
        match callee {
            Expr::Paren(paren) => self.premise(&paren.expr),
            // `(0, ns.member)(…)`.
            Expr::Seq(seq) => match seq.exprs.as_slice() {
                [zero, last] if literal_only(zero) => self.premise(last),
                _ => None,
            },
            Expr::Ident(ident) => {
                let binding = *self.bindings.get(&ident.to_id())?;
                let (kinds, target) = self.premises.callees.get(&binding)?;
                Some((kinds.clone(), *target, binding))
            }
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(member),
                ..
            }) => {
                // `ns.member` or `ns.default.member`.
                let (namespace, path) = match &**obj {
                    Expr::Ident(namespace) => (namespace, member.sym.to_string()),
                    Expr::Member(MemberExpr {
                        obj,
                        prop: MemberProp::Ident(first),
                        ..
                    }) => match &**obj {
                        Expr::Ident(namespace) => {
                            (namespace, format!("{}.{}", first.sym, member.sym))
                        }
                        _ => return None,
                    },
                    _ => return None,
                };
                let binding = *self.bindings.get(&namespace.to_id())?;
                let (kinds, target) = self.premises.members.get(&(binding, path))?;
                Some((kinds.clone(), *target, binding))
            }
            _ => None,
        }
    }

    /// A call or construction a premise makes a pure allocation.
    pub(super) fn premised_allocation(&mut self, expr: &Expr) -> bool {
        let (callee, args, constructs) = match expr {
            Expr::Call(CallExpr {
                callee: Callee::Expr(callee),
                args,
                ..
            }) => (&**callee, args.as_slice(), false),
            Expr::New(new) => (&*new.callee, new.args.as_deref().unwrap_or_default(), true),
            _ => return false,
        };
        let Some((kinds, _, callee_binding)) = self.premise(callee) else {
            return false;
        };
        let Some(kind) = call_kind(&kinds, constructs) else {
            return false;
        };
        let lazy_call = |expr: &Expr| {
            matches!(expr, Expr::Call(CallExpr { callee: Callee::Expr(callee), args, .. })
            if args.is_empty() && matches!(
                self.premise(callee),
                Some((kinds, _, _)) if kinds.contains(&PremiseKind::LazyModule)
            ))
        };
        let applies = match kind {
            PremiseKind::PureNew => constructs,
            PremiseKind::PureCall | PremiseKind::CallbackFactory | PremiseKind::Allocator => {
                !constructs
            }
            PremiseKind::AllocatorNew => constructs,
            PremiseKind::FunctionNamer => {
                !constructs
                    && matches!(args, [function, name]
                        if matches!(&*function.expr, Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_))
                            && matches!(&*name.expr, Expr::Lit(Lit::Str(_))))
            }
            PremiseKind::LazyModule => !constructs && args.is_empty(),
            PremiseKind::InteropNamespace => {
                !constructs && args.first().is_some_and(|arg| lazy_call(&arg.expr))
            }
            PremiseKind::DecoratorHelper
            | PremiseKind::Decorator
            | PremiseKind::ContainedDecorator => false,
        };
        if !applies || args.iter().any(|arg| arg.spread.is_some()) {
            return false;
        }
        // A derived allocator reads properties of these arguments.
        let literals = self.premises.literal_params.get(&callee_binding);
        if literals.into_iter().flatten().any(|position| {
            !args
                .get(*position)
                .is_some_and(|arg| crate::derive::plain_literal(&arg.expr))
        }) {
            return false;
        }
        // A derived allocator constructs these intrinsics.
        let needed: Vec<Dependency> = self
            .premises
            .allocator_intrinsics
            .get(&callee_binding)
            .into_iter()
            .flatten()
            .map(|global| (*global, &[] as &[_]))
            .collect();
        if !needed.is_empty() && !self.untouched(needed, None, false) {
            return false;
        }
        if let Some(reads) = self.premises.allocator_reads.get(&callee_binding) {
            self.eager.extend(reads);
        }
        if kind == PremiseKind::LazyModule {
            // The exports object is the provider's.
            self.lazy_uses.push(callee_binding);
            self.deep.push(callee_binding);
        }
        let before = self.eager.len();
        self.premised_args += 1;
        let impure = args.iter().any(|arg| self.pure(&arg.expr).is_err());
        self.premised_args -= 1;
        if impure {
            return false;
        }
        if kind == PremiseKind::CallbackFactory {
            self.providers.push(self.premises.provider(callee_binding));
        } else if !matches!(
            kind,
            PremiseKind::Allocator
                | PremiseKind::AllocatorNew
                | PremiseKind::FunctionNamer
                | PremiseKind::LazyModule
        ) {
            // The callee may call its function arguments at once, and read
            // the objects its arguments reference. The callees of nested
            // premised calls are not arguments.
            let mut bodies = Bodies {
                bindings: self.bindings,
                depth: 0,
                mentions: FxHashSet::default(),
            };
            args.visit_with(&mut bodies);
            self.eager.extend(bodies.mentions);
            let read: Vec<Binding> = (before..self.eager.len())
                .filter(|slot| !self.callee_slots.contains(slot))
                .map(|slot| self.eager[slot])
                .collect();
            self.deep.extend(read);
        }
        self.callee_slots.insert(self.eager.len());
        self.eager.push(callee_binding);
        self.premise_uses.push(kind);
        true
    }

    /// An argument that is itself a pure definition; it may read an import.
    pub(super) fn premised_argument(&mut self, expr: &Expr) -> bool {
        self.premised_args += 1;
        let pure = self.pure(expr).is_ok();
        self.premised_args -= 1;
        pure
    }

    /// `dec` or `dec(args)` under a decorator premise on `dec`.
    pub(super) fn decorator(&mut self, expr: &Expr) -> bool {
        let (callee, args) = match expr {
            Expr::Paren(paren) => return self.decorator(&paren.expr),
            Expr::Call(CallExpr {
                callee: Callee::Expr(callee),
                args,
                ..
            }) => (&**callee, args.as_slice()),
            other => (other, &[] as &[ExprOrSpread]),
        };
        let Some((kinds, _, binding)) = self.premise(callee) else {
            return false;
        };
        // The stronger kind wins.
        let kind = if kinds.contains(&PremiseKind::Decorator) {
            PremiseKind::Decorator
        } else if kinds.contains(&PremiseKind::ContainedDecorator) {
            self.providers.push(self.premises.provider(binding));
            PremiseKind::ContainedDecorator
        } else {
            return false;
        };
        if args
            .iter()
            .any(|arg| arg.spread.is_some() || !self.premised_argument(&arg.expr))
        {
            return false;
        }
        self.eager.push(binding);
        self.premise_uses.push(kind);
        true
    }

    /// `helper(decorators, Target.prototype, …)` or
    /// `Target = helper(decorators, Target)` under a decorator-helper
    /// premise.
    /// Whether every property of the literal is a function under a plain
    /// key, and the intrinsics the export helpers use are as defined: an
    /// enumerable member of `Object.prototype` would be enumerated too.
    pub(super) fn export_getters(&mut self, getters: &Expr, tagged: bool) -> bool {
        let Expr::Object(getters) = getters else {
            return false;
        };
        let functions = getters.props.iter().all(|prop| {
            matches!(prop, PropOrSpread::Prop(prop) if matches!(&**prop, Prop::KeyValue(pair)
                if !matches!(pair.key, PropName::Computed(_))
                    && matches!(unparen(&pair.value), Expr::Arrow(_) | Expr::Fn(_))))
        });
        functions
            && (self.native_site || self.census.prototype_unwritten("Object"))
            && self.untouched(vec![("Object", &["defineProperty"])], None, false)
            && (!tagged || self.untouched(vec![("Symbol", &["toStringTag"])], None, false))
    }

    /// `exporter({ name: () => value, … })` for the derived helper that
    /// returns a fresh object with one getter per property.
    pub(super) fn export_allocation(&mut self, call: &CallExpr) -> bool {
        let Callee::Expr(callee) = &call.callee else {
            return false;
        };
        let Expr::Ident(helper) = unparen(callee) else {
            return false;
        };
        let Some(helper) = self.bindings.get(&helper.to_id()).copied() else {
            return false;
        };
        let (getters, flag) = match call.args.as_slice() {
            [ExprOrSpread { spread: None, expr }] => (expr, None),
            [
                ExprOrSpread { spread: None, expr },
                ExprOrSpread {
                    spread: None,
                    expr: flag,
                },
            ] => (expr, Some(flag)),
            _ => return false,
        };
        if !self.premises.exporter_allocators.contains(&helper)
            || flag.is_some_and(|flag| !literal_only(flag))
            || !self.export_getters(getters, true)
        {
            return false;
        }
        self.eager.push(helper);
        true
    }

    /// `exporter(target, { name: () => value, … })` for the derived helper:
    /// it defines one getter on `target` per property and calls nothing. An
    /// enumerable member of `Object.prototype` would be enumerated too.
    pub(super) fn export_call(&mut self, expr: &Expr) -> bool {
        let Expr::Call(CallExpr {
            callee: Callee::Expr(callee),
            args,
            ..
        }) = unparen(expr)
        else {
            return false;
        };
        let Expr::Ident(helper) = unparen(callee) else {
            return false;
        };
        let Some(helper) = self.bindings.get(&helper.to_id()).copied() else {
            return false;
        };
        let [
            ExprOrSpread {
                spread: None,
                expr: target,
            },
            ExprOrSpread {
                spread: None,
                expr: getters,
            },
        ] = args.as_slice()
        else {
            return false;
        };
        let Expr::Ident(target) = &**target else {
            return false;
        };
        let Some(target) = self.bindings.get(&target.to_id()).copied() else {
            return false;
        };
        if !self.premises.exporters.contains(&helper)
            || self.imports.contains(&target)
            || !self.export_getters(getters, false)
        {
            return false;
        }
        self.targets.push(target);
        self.eager.push(helper);
        true
    }

    /// An expression statement of reads alone: named members of literals,
    /// bindings, literals, joined by sequences, `void` and parentheses.
    pub(super) fn reads_only(&mut self, expr: &Expr) -> bool {
        match expr {
            Expr::Paren(paren) => self.reads_only(&paren.expr),
            Expr::Seq(seq) => seq.exprs.iter().all(|part| self.reads_only(part)),
            Expr::Unary(unary) if unary.op == UnaryOp::Void => self.reads_only(&unary.arg),
            Expr::Member(_) | Expr::Ident(_) | Expr::Lit(_) => self.pure(expr).is_ok(),
            _ => false,
        }
    }

    pub(super) fn confined(&mut self, expr: &Expr) -> bool {
        if self.export_call(expr) {
            return true;
        }
        if let Some(binding) = enum_definition(expr)
            .and_then(|target| self.bindings.get(&target.to_id()))
            .filter(|binding| self.enums.contains(*binding))
        {
            self.targets.push(*binding);
            self.replaces.push(*binding);
            return true;
        }
        let (call, assigned) = match expr {
            Expr::Paren(paren) => return self.confined(&paren.expr),
            Expr::Call(call) => (call, None),
            Expr::Assign(AssignExpr {
                op: AssignOp::Assign,
                left: AssignTarget::Simple(SimpleAssignTarget::Ident(target)),
                right,
                ..
            }) => match &**right {
                Expr::Call(call) => (call, Some(&target.id)),
                _ => return false,
            },
            _ => return false,
        };
        let Callee::Expr(callee) = &call.callee else {
            return false;
        };
        let Some((kinds, target_arg, helper)) = self.premise(callee) else {
            return false;
        };
        if !kinds.contains(&PremiseKind::DecoratorHelper) {
            return false;
        }
        if call.args.iter().any(|arg| arg.spread.is_some()) {
            return false;
        }
        for (position, arg) in call.args.iter().enumerate() {
            if position == target_arg {
                // `Target` or `Target.prototype`.
                let target = match &*arg.expr {
                    Expr::Member(MemberExpr {
                        obj,
                        prop: MemberProp::Ident(prototype),
                        ..
                    }) if &*prototype.sym == "prototype" => &**obj,
                    other => other,
                };
                let Expr::Ident(target) = target else {
                    return false;
                };
                let Some(binding) = self.bindings.get(&target.to_id()) else {
                    return false;
                };
                if self.imports.contains(binding) {
                    return false;
                }
                self.targets.push(*binding);
            } else if position == 0 {
                // The decorators: each must have a premise of its own.
                let Expr::Array(decorators) = &*arg.expr else {
                    return false;
                };
                for decorator in &decorators.elems {
                    let Some(ExprOrSpread { spread: None, expr }) = decorator else {
                        return false;
                    };
                    if !self.decorator(expr) {
                        return false;
                    }
                }
            } else if !self.premised_argument(&arg.expr) {
                return false;
            }
        }
        if self.targets.is_empty() {
            return false;
        }
        if let Some(assigned) = assigned {
            match self.bindings.get(&assigned.to_id()) {
                Some(binding) => self.replaces.push(*binding),
                None => return false,
            }
        }
        self.eager.push(helper);
        self.premise_uses.push(PremiseKind::DecoratorHelper);
        true
    }

    /// A class whose definition runs nothing: no `extends`, static block,
    /// static initializer, computed key or decorator.
    pub(super) fn class(&mut self, class: &Class) -> Result<(), ImpureCause> {
        let local_base = |heritage: &Expr| match heritage {
            Expr::Ident(base) => self.bindings.get(&base.to_id()).copied().filter(|binding| {
                self.classes.contains(binding)
                    || self.premises.stable_class_imports.contains(binding)
            }),
            _ => None,
        };
        let heritage = match &class.super_class {
            None => true,
            Some(heritage)
                if self.upgrades.contains(&Upgrade::LocalHeritage)
                    && local_base(heritage).is_some() =>
            {
                self.eager.extend(local_base(heritage));
                true
            }
            Some(heritage) if self.upgrades.contains(&Upgrade::IntrinsicHeritage) => self
                .global(heritage)
                .and_then(|name| HERITAGE.iter().find(|known| **known == name).copied())
                .is_some_and(|global| self.untouched(vec![(global, &[])], None, false)),
            Some(_) => false,
        };
        if !heritage || !class.decorators.is_empty() {
            return Err(ImpureCause::ClassEagerPart);
        }
        for member in &class.body {
            let (key, static_value) = match member {
                ClassMember::Constructor(_) | ClassMember::Empty(_) => continue,
                ClassMember::Method(method) => (Some(&method.key), None),
                ClassMember::PrivateMethod(_) => (None, None),
                ClassMember::ClassProp(prop) => {
                    (Some(&prop.key), prop.is_static.then_some(&prop.value))
                }
                ClassMember::PrivateProp(prop) => (None, prop.is_static.then_some(&prop.value)),
                _ => return Err(ImpureCause::ClassEagerPart),
            };
            // A static field defines an own property of the new class.
            let is_static_value = static_value.is_some_and(|value| {
                !self.upgrades.contains(&Upgrade::StaticFields)
                    || value
                        .as_deref()
                        .is_some_and(|value| self.pure(value).is_err())
            });
            let decorated = match member {
                ClassMember::Method(method) => !method.function.decorators.is_empty(),
                ClassMember::ClassProp(prop) => !prop.decorators.is_empty(),
                _ => false,
            };
            if is_static_value || decorated || key.is_some_and(|key| self.key(key).is_err()) {
                return Err(ImpureCause::ClassEagerPart);
            }
        }
        Ok(())
    }

    /// An expression that runs no user code and cannot throw except by
    /// reading an uninitialized binding; collects the bindings it reads.
    pub(super) fn pure(&mut self, expr: &Expr) -> Result<(), ImpureCause> {
        if literal_only(expr) {
            return Ok(());
        }
        let mut reads = Vec::new();
        if primitive(
            expr,
            self.bindings,
            self.primitives,
            PrimitiveScope::Initialization(self.settled),
            &mut reads,
        ) {
            self.eager.extend(reads);
            return Ok(());
        }
        if let Some(dependencies) = self.native_allocation(expr)
            && self.untouched(dependencies, None, true)
        {
            return Ok(());
        }
        if self.premised_allocation(expr) {
            return Ok(());
        }
        // A member of a settled import: nothing in the program changes it.
        if let Expr::Member(MemberExpr {
            obj,
            prop: MemberProp::Ident(key),
            ..
        }) = expr
            && let Expr::Ident(table) = &**obj
            && let Some(binding) = self.bindings.get(&table.to_id())
            && self
                .settled
                .get(binding)
                .is_some_and(|keys| keys.contains_key(&*key.sym))
        {
            self.eager.push(*binding);
            return Ok(());
        }
        if let Some(table) = self.literal_member_read(expr) {
            self.read_literal(table);
            return Ok(());
        }
        if let Some(table) = self.nested_member_read(expr) {
            self.eager.push(table);
            return Ok(());
        }
        if let Some(dependencies) = self.intrinsic_member_read(expr) {
            let prop = match expr {
                Expr::Member(MemberExpr {
                    prop: MemberProp::Ident(prop),
                    ..
                }) => prop.sym.to_string(),
                _ => unreachable!("an intrinsic member read is a named member"),
            };
            // `G.prototype.x` is a member of the prototype object, and a
            // name `G` does not own is answered by one.
            let own_static = dependencies.iter().all(|(global, _)| {
                OWN_STATICS
                    .iter()
                    .any(|(owner, names)| owner == global && names.contains(&&*prop))
            });
            let on_prototype = matches!(expr, Expr::Member(MemberExpr { obj, .. })
                if matches!(&**obj, Expr::Member(_)));
            let mut dependencies = dependencies;
            if !own_static {
                dependencies.extend([("Object", &[] as &[_]), ("Function", &[] as &[_])]);
            }
            if self.untouched(dependencies, Some(&prop), on_prototype || !own_static) {
                return Ok(());
            }
        }
        match expr {
            Expr::Paren(paren) => self.pure(&paren.expr),
            Expr::Lit(Lit::Regex(_) | Lit::BigInt(_)) => Ok(()),
            Expr::Fn(function) => {
                if function.function.decorators.is_empty() {
                    Ok(())
                } else {
                    Err(ImpureCause::Unmodeled)
                }
            }
            Expr::Arrow(_) => Ok(()),
            Expr::Class(class) => self.class(&class.class),
            Expr::Ident(ident) if ident.ctxt == self.unresolved => match &*ident.sym {
                "undefined" | "NaN" | "Infinity" => Ok(()),
                _ => Err(ImpureCause::ForeignRead),
            },
            Expr::Ident(ident) => match self.bindings.get(&ident.to_id()) {
                // An argument of a premised call: the unit is then ordered
                // against every impure statement.
                Some(binding)
                    if self.imports.contains(binding)
                        && self.premised_args == 0
                        && !self.upgrades.contains(&Upgrade::ImportReads) =>
                {
                    Err(ImpureCause::ForeignRead)
                }
                // The exporting chunk has run and never assigns the binding
                // again: the read sees its one value.
                Some(binding)
                    if self.premised_args == 0
                        && self.premises.stable_imports.contains(binding) =>
                {
                    self.eager.push(*binding);
                    Ok(())
                }
                // Another chunk's code may change what the import holds.
                Some(binding) if self.imports.contains(binding) => {
                    self.eager.push(*binding);
                    self.deep.push(*binding);
                    Ok(())
                }
                Some(binding) => {
                    self.eager.push(*binding);
                    Ok(())
                }
                None => Err(ImpureCause::Unmodeled),
            },
            Expr::Array(array) => {
                for element in array.elems.iter().flatten() {
                    if element.spread.is_none() {
                        self.pure(&element.expr)?;
                        continue;
                    }
                    // Spreading an array literal no impure statement has
                    // reached reads its elements through the intrinsic
                    // array iterator.
                    let spread = match &*element.expr {
                        Expr::Ident(array)
                            if self.upgrades.contains(&Upgrade::LiteralMemberReads) =>
                        {
                            self.bindings
                                .get(&array.to_id())
                                .copied()
                                .filter(|binding| self.arrays.contains(binding))
                        }
                        _ => None,
                    };
                    let Some(spread) = spread else {
                        return Err(ImpureCause::Conversion);
                    };
                    if !self.untouched(vec![("Array", &["@@iterator"])], None, true) {
                        return Err(ImpureCause::Conversion);
                    }
                    self.eager.push(spread);
                    self.deep.push(spread);
                    self.pristine.push(spread);
                }
                Ok(())
            }
            Expr::Object(object) => {
                for prop in &object.props {
                    let prop = match prop {
                        PropOrSpread::Prop(prop) => prop,
                        // Spreading a plain literal no impure statement has
                        // reached copies data properties.
                        PropOrSpread::Spread(spread) => {
                            let table = match &*spread.expr {
                                Expr::Ident(table)
                                    if self.upgrades.contains(&Upgrade::LiteralMemberReads) =>
                                {
                                    self.bindings
                                        .get(&table.to_id())
                                        .copied()
                                        .filter(|binding| self.literals.contains_key(binding))
                                }
                                _ => None,
                            };
                            let Some(table) = table else {
                                return Err(ImpureCause::Conversion);
                            };
                            self.read_literal(table);
                            continue;
                        }
                    };
                    match &**prop {
                        Prop::Shorthand(ident) => self.pure(&Expr::Ident(ident.clone()))?,
                        Prop::KeyValue(key_value) => {
                            self.key(&key_value.key)?;
                            self.pure(&key_value.value)?;
                        }
                        Prop::Method(method) => self.key(&method.key)?,
                        Prop::Getter(getter) => self.key(&getter.key)?,
                        Prop::Setter(setter) => self.key(&setter.key)?,
                        Prop::Assign(_) => return Err(ImpureCause::Unmodeled),
                    }
                }
                Ok(())
            }
            // `ToBoolean` runs no code.
            Expr::Unary(unary) if matches!(unary.op, UnaryOp::Bang | UnaryOp::Void) => {
                self.pure(&unary.arg)
            }
            // `typeof` never throws on an initialized operand.
            Expr::Unary(unary) if unary.op == UnaryOp::TypeOf => self.pure(&unary.arg),
            Expr::Cond(cond) => {
                self.pure(&cond.test)?;
                self.pure(&cond.cons)?;
                self.pure(&cond.alt)
            }
            Expr::Bin(binary)
                if matches!(
                    binary.op,
                    BinaryOp::LogicalAnd
                        | BinaryOp::LogicalOr
                        | BinaryOp::NullishCoalescing
                        | BinaryOp::EqEqEq
                        | BinaryOp::NotEqEq
                ) =>
            {
                self.pure(&binary.left)?;
                self.pure(&binary.right)
            }
            Expr::Seq(seq) => seq.exprs.iter().try_for_each(|expr| self.pure(expr)),
            Expr::Call(call) => {
                if self.primitive_call(call) || self.export_allocation(call) {
                    return Ok(());
                }
                Err(match &call.callee {
                    Callee::Expr(callee) => self.call_cause(callee),
                    _ => ImpureCause::CallOther,
                })
            }
            Expr::TaggedTpl(tagged) => Err(self.call_cause(&tagged.tag)),
            Expr::OptChain(chain) => Err(match &*chain.base {
                OptChainBase::Call(call) => self.call_cause(&call.callee),
                OptChainBase::Member(_) => ImpureCause::MemberRead,
            }),
            Expr::New(_) => Err(ImpureCause::New),
            Expr::Member(_) | Expr::SuperProp(_) => Err(ImpureCause::MemberRead),
            Expr::Assign(_) | Expr::Update(_) => Err(ImpureCause::Assignment),
            Expr::Bin(_) | Expr::Unary(_) | Expr::Tpl(_) => Err(ImpureCause::Conversion),
            _ => Err(ImpureCause::Unmodeled),
        }
    }

    pub(super) fn statement_cause(&self, expr: &Expr) -> ImpureCause {
        match expr {
            Expr::Paren(paren) => self.statement_cause(&paren.expr),
            Expr::Call(CallExpr {
                callee: Callee::Expr(callee),
                ..
            }) => self.call_cause(callee),
            Expr::New(_) => ImpureCause::New,
            Expr::Assign(_) | Expr::Update(_) => ImpureCause::Assignment,
            Expr::Await(_) | Expr::Yield(_) => ImpureCause::Unmodeled,
            _ => ImpureCause::ExpressionStatement,
        }
    }

    pub(super) fn declarator(&mut self, declarator: &VarDeclarator) -> Kind {
        if !matches!(declarator.name, Pat::Ident(_)) {
            return Kind::Impure(ImpureCause::Destructuring);
        }
        // An enum-shaped call on the declared `var` itself allocates the
        // object it fills and reads nothing.
        if let Pat::Ident(name) = &declarator.name
            && let Some(binding) = self.bindings.get(&name.id.to_id())
            && self.enum_declarations.contains(binding)
        {
            return Kind::Pure;
        }
        match &declarator.init {
            None => Kind::Pure,
            Some(init) => match self.pure(init) {
                Ok(()) => Kind::Pure,
                Err(cause) => Kind::Impure(cause),
            },
        }
    }

    pub(super) fn decl(&mut self, decl: &Decl) -> Kind {
        match decl {
            Decl::Fn(function) if function.function.decorators.is_empty() => Kind::Hoisted,
            Decl::Class(class) => match self.class(&class.class) {
                Ok(()) => Kind::Pure,
                Err(cause) => Kind::Impure(cause),
            },
            Decl::Var(var) => match var.decls.as_slice() {
                [declarator] => self.declarator(declarator),
                [] => Kind::Inert,
                _ => unreachable!("multi-declarator declarations are split"),
            },
            _ => Kind::Impure(ImpureCause::Unmodeled),
        }
    }

    pub(super) fn item(&mut self, item: &ModuleItem) -> Kind {
        match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => self.decl(decl),
            ModuleItem::Stmt(Stmt::Empty(_)) => Kind::Inert,
            ModuleItem::Stmt(Stmt::Expr(statement)) => {
                if self.confined(&statement.expr) {
                    return Kind::Confined;
                }
                // An expression that is a pure definition's initializer
                // has no effect as a statement either: it only reads.
                let before = (self.eager.len(), self.deep.len(), self.pristine.len());
                if self.reads_only(&statement.expr) {
                    return Kind::Pure;
                }
                self.eager.truncate(before.0);
                self.deep.truncate(before.1);
                self.pristine.truncate(before.2);
                Kind::Impure(self.statement_cause(&statement.expr))
            }
            ModuleItem::Stmt(_) => Kind::Impure(ImpureCause::ControlFlow),
            ModuleItem::ModuleDecl(decl) => match decl {
                ModuleDecl::Import(_) | ModuleDecl::ExportNamed(_) | ModuleDecl::ExportAll(_) => {
                    Kind::Inert
                }
                ModuleDecl::ExportDecl(export) => self.decl(&export.decl),
                ModuleDecl::ExportDefaultDecl(default) => match &default.decl {
                    DefaultDecl::Fn(_) => Kind::Hoisted,
                    DefaultDecl::Class(class) => match self.class(&class.class) {
                        Ok(()) => Kind::Pure,
                        Err(cause) => Kind::Impure(cause),
                    },
                    DefaultDecl::TsInterfaceDecl(_) => Kind::Impure(ImpureCause::Unmodeled),
                },
                ModuleDecl::ExportDefaultExpr(default) => match self.pure(&default.expr) {
                    Ok(()) => Kind::Pure,
                    Err(cause) => Kind::Impure(cause),
                },
                _ => Kind::Impure(ImpureCause::Unmodeled),
            },
        }
    }
}

pub(super) fn pattern_ids(pat: &Pat, out: &mut Vec<Id>) {
    match pat {
        Pat::Ident(binding) => out.push(binding.id.to_id()),
        Pat::Array(array) => {
            for element in array.elems.iter().flatten() {
                pattern_ids(element, out);
            }
        }
        Pat::Rest(rest) => pattern_ids(&rest.arg, out),
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => pattern_ids(&key_value.value, out),
                    ObjectPatProp::Assign(assign) => out.push(assign.key.id.to_id()),
                    ObjectPatProp::Rest(rest) => pattern_ids(&rest.arg, out),
                }
            }
        }
        Pat::Assign(assign) => pattern_ids(&assign.left, out),
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

pub(super) fn declared_ids(decl: &Decl, out: &mut Vec<Id>) {
    match decl {
        Decl::Fn(function) => out.push(function.ident.to_id()),
        Decl::Class(class) => out.push(class.ident.to_id()),
        Decl::Var(var) => {
            for declarator in &var.decls {
                pattern_ids(&declarator.name, out);
            }
        }
        _ => {}
    }
}
