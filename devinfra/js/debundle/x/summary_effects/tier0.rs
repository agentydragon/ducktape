//! Tier 0: ordered impure statements plus reference reach, from syntax alone.
//!
//! Each top-level source unit is a *pure definition* or *impure*.
//!
//! - A pure definition only allocates and initializes its own binding. Its
//!   initializer is drawn from forms that run no user code and cannot throw
//!   except by reading an uninitialized binding: literals, function and
//!   class expressions without eager parts, object and array literals of
//!   such, references to top-level bindings, and operators over literals.
//! - Everything else is impure: it may call unknown code and may throw.
//!
//! Impure statements keep their mutual order, so two of them always
//! conflict. An impure statement `H` may run any function unknown code holds
//! by then. Since an impure statement can leak anything it mentions, that is
//! the reference closure of every binding mentioned by an impure statement
//! at or before `H` in source order: `reach(H)`. A pure definition leaks
//! nothing, so moving one never changes `reach`, and the stamps taken from
//! source order hold in every order that keeps impure statements ordered.
//!
//! - `H` and a pure definition `D` conflict when `D`'s binding is in
//!   `reach(H)` (`H` can observe it uninitialized), when a function in
//!   `reach(H)` or `H` itself assigns `D`'s binding, or when `D`'s
//!   initializer reads a binding `H` may write.
//! - Two pure definitions conflict when one's initializer reads the other's
//!   binding.

use rustc_hash::{FxHashMap, FxHashSet};
use swc_common::SyntaxContext;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::helpers::assigned_ids;
use crate::intrinsics::{Census, census};

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize, clap::ValueEnum,
)]
#[serde(rename_all = "snake_case")]
pub enum ImpureCause {
    /// A call whose callee is a top-level binding of this chunk.
    CallLocal,
    CallImport,
    CallGlobal,
    /// A call through a member, e.g. `ns.jsx(...)`.
    CallMember,
    CallOther,
    New,
    MemberRead,
    /// A read of an unresolved global or of another chunk's binding.
    ForeignRead,
    /// A spread, computed key, template interpolation or operator on a
    /// non-literal: each may run a conversion or iteration.
    Conversion,
    /// A class with `extends`, a static block, a static initializer or a
    /// computed key.
    ClassEagerPart,
    /// A destructuring declaration.
    Destructuring,
    /// An assignment or update at the top level of an expression statement.
    Assignment,
    /// Any other expression statement.
    ExpressionStatement,
    /// A block, loop, conditional, `try`, `throw` or other statement.
    ControlFlow,
    /// `await`, `yield`, `eval`, `with`, or syntax the tier does not model.
    Unmodeled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// An import, a re-export, an export list: runs nothing.
    Inert,
    /// A function declaration: its binding holds the function before any
    /// statement runs.
    Hoisted,
    Pure,
    Impure(ImpureCause),
}

/// Why a swapped pair is not proven to commute.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Conflict {
    BothImpure,
    /// The definition's binding is reachable from the impure statement's own
    /// code.
    ReachOwn,
    /// It is reachable only through what earlier impure statements leaked.
    ReachEscaped,
    /// A function the impure statement may run assigns the definition's
    /// binding, or the definition's initializer reads a binding the impure
    /// statement may write.
    Write,
    /// One definition's initializer reads the other's binding.
    EagerRead,
}

type Binding = u32;

pub struct Unit {
    pub kind: Kind,
    declared: Vec<Binding>,
    /// Bindings the initializer of a pure definition reads when it runs.
    eager: Vec<Binding>,
    mentions: Vec<Binding>,
    /// Bindings some assignment inside the unit targets.
    assigns: Vec<Binding>,
}

pub struct Tier0 {
    pub units: Vec<Unit>,
    /// Per binding: the first impure ordinal whose reach holds it.
    reach_stamp: Vec<u32>,
    /// Per binding: the first impure ordinal that may assign it.
    write_stamp: Vec<u32>,
    /// Per impure unit: the closure of its own mentions.
    own_reach: FxHashMap<usize, FxHashSet<Binding>>,
    pub bindings: usize,
    pub refused: bool,
    /// Per intrinsic global: the pure definitions that depend on it being
    /// unmodified.
    pub families_used: std::collections::BTreeMap<&'static str, usize>,
    /// What the chunk itself writes on globals.
    pub census: Census,
}

const NEVER: u32 = u32::MAX;

struct Scan<'a> {
    bindings: &'a FxHashMap<Id, Binding>,
    unresolved: SyntaxContext,
    mentions: FxHashSet<Binding>,
    refused: bool,
}

impl Visit for Scan<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        if let Some(binding) = self.bindings.get(&ident.to_id()) {
            self.mentions.insert(*binding);
        }
        if ident.ctxt == self.unresolved && &*ident.sym == "eval" {
            self.refused = true;
        }
    }

    fn visit_with_stmt(&mut self, _: &WithStmt) {
        self.refused = true;
    }
}

/// A bounded widening of what counts as a pure definition.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, clap::ValueEnum, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Upgrade {
    /// Operators and template interpolation over bindings that only ever
    /// hold a primitive: defined once, never assigned, by a literal or by
    /// such an expression. Converting a primitive runs no code, and no
    /// operator throws on numbers, strings, booleans, `null` and
    /// `undefined`.
    PrimitiveBindings,
    /// Allocations by intrinsic constructors and functions from a fixed
    /// list, with arguments that cannot run code (`native_allocation`).
    NativeAllocators,
    /// A class extending an intrinsic constructor from `HERITAGE`.
    IntrinsicHeritage,
    /// A read of a data property of an intrinsic namespace or prototype,
    /// such as `Object.prototype.toString` or `Math.PI`.
    IntrinsicMemberReads,
}

/// The global a recognized intrinsic use depends on, with the properties of
/// it or of its prototype that must be untouched.
type Dependency = (&'static str, &'static [&'static str]);

/// Intrinsic constructors a class may extend: the definition reads the
/// global binding and its `prototype`, a non-writable data property.
const HERITAGE: [&str; 11] = [
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
const NAMESPACES: [&str; 14] = [
    "Object", "Array", "Function", "String", "Number", "Boolean", "Symbol", "Math", "JSON",
    "Reflect", "Date", "Promise", "Map", "Set",
];

/// Globals whose `prototype` holds only data properties, apart from the
/// names in `PROTOTYPE_ACCESSORS`.
const DATA_PROTOTYPES: [&str; 6] = ["Object", "Array", "Function", "String", "Number", "Boolean"];
const PROTOTYPE_ACCESSORS: [&str; 3] = ["__proto__", "caller", "arguments"];

const TYPED_ARRAYS: [&str; 9] = [
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
fn small_length(expr: &Expr) -> bool {
    matches!(expr, Expr::Lit(Lit::Num(number))
        if number.value.fract() == 0.0 && (0.0..=65_536.0).contains(&number.value))
}

struct Classifier<'a> {
    bindings: &'a FxHashMap<Id, Binding>,
    imports: &'a FxHashSet<Binding>,
    /// Bindings proven to only ever hold a primitive.
    primitives: &'a FxHashSet<Binding>,
    upgrades: &'a FxHashSet<Upgrade>,
    census: &'a Census,
    unresolved: SyntaxContext,
    eager: Vec<Binding>,
    /// The globals the accepted intrinsic uses depend on.
    intrinsics: Vec<&'static str>,
}

/// Whether `expr` evaluates to a number, string, boolean, `null` or
/// `undefined` without running code or throwing, given the primitive
/// bindings; collects the bindings it reads.
fn primitive(
    expr: &Expr,
    bindings: &FxHashMap<Id, Binding>,
    primitives: &FxHashSet<Binding>,
    reads: &mut Vec<Binding>,
) -> bool {
    let mut recurse = |expr: &Expr| primitive(expr, bindings, primitives, reads);
    match expr {
        Expr::Paren(paren) => recurse(&paren.expr),
        Expr::Lit(Lit::Num(_) | Lit::Str(_) | Lit::Bool(_) | Lit::Null(_)) => true,
        Expr::Ident(ident) => match bindings.get(&ident.to_id()) {
            Some(binding) if primitives.contains(binding) => {
                reads.push(*binding);
                true
            }
            _ => false,
        },
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

fn literal_only(expr: &Expr) -> bool {
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
    fn call_cause(&self, callee: &Expr) -> ImpureCause {
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

    fn key(&self, key: &PropName) -> Result<(), ImpureCause> {
        match key {
            PropName::Ident(name) if &*name.sym == "__proto__" => Err(ImpureCause::Unmodeled),
            PropName::Str(value) if js_ast::str_value(value) == "__proto__" => {
                Err(ImpureCause::Unmodeled)
            }
            PropName::Str(_) => Ok(()),
            PropName::Ident(_) | PropName::Num(_) | PropName::BigInt(_) => Ok(()),
            PropName::Computed(_) => Err(ImpureCause::Conversion),
        }
    }

    fn global(&self, expr: &Expr) -> Option<String> {
        match expr {
            Expr::Ident(ident) if ident.ctxt == self.unresolved => Some(ident.sym.to_string()),
            _ => None,
        }
    }

    fn primitive_arg(&mut self, expr: &Expr) -> bool {
        let mut reads = Vec::new();
        let ok = primitive(expr, self.bindings, self.primitives, &mut reads);
        if ok {
            self.eager.extend(reads);
        }
        ok
    }

    /// An array literal without holes or spreads whose elements all pass.
    fn array_of(&mut self, expr: &Expr, mut element: impl FnMut(&mut Self, &Expr) -> bool) -> bool {
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
    fn native_allocation(&mut self, expr: &Expr) -> Option<Vec<Dependency>> {
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
    fn intrinsic_member_read(&self, expr: &Expr) -> Option<Vec<Dependency>> {
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
    fn untouched(&mut self, dependencies: Vec<Dependency>, extra_prop: Option<&str>) -> bool {
        let ok = dependencies.iter().all(|(global, props)| {
            let mut props: Vec<&str> = props.to_vec();
            props.extend(extra_prop);
            self.census.untouched(global, &props)
        });
        if ok {
            self.intrinsics
                .extend(dependencies.iter().map(|(global, _)| *global));
        }
        ok
    }

    /// A class whose definition runs nothing: no `extends`, static block,
    /// static initializer, computed key or decorator.
    fn class(&mut self, class: &Class) -> Result<(), ImpureCause> {
        let heritage = match &class.super_class {
            None => true,
            Some(heritage) if self.upgrades.contains(&Upgrade::IntrinsicHeritage) => self
                .global(heritage)
                .and_then(|name| HERITAGE.iter().find(|known| **known == name).copied())
                .is_some_and(|global| self.untouched(vec![(global, &[])], None)),
            Some(_) => false,
        };
        if !heritage || !class.decorators.is_empty() {
            return Err(ImpureCause::ClassEagerPart);
        }
        for member in &class.body {
            let (key, is_static_value) = match member {
                ClassMember::Constructor(_) | ClassMember::Empty(_) => continue,
                ClassMember::Method(method) => (Some(&method.key), false),
                ClassMember::PrivateMethod(_) => (None, false),
                ClassMember::ClassProp(prop) => (Some(&prop.key), prop.is_static),
                ClassMember::PrivateProp(prop) => (None, prop.is_static),
                _ => return Err(ImpureCause::ClassEagerPart),
            };
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
    fn pure(&mut self, expr: &Expr) -> Result<(), ImpureCause> {
        if literal_only(expr) {
            return Ok(());
        }
        let mut reads = Vec::new();
        if primitive(expr, self.bindings, self.primitives, &mut reads) {
            self.eager.extend(reads);
            return Ok(());
        }
        if let Some(dependencies) = self.native_allocation(expr)
            && self.untouched(dependencies, None)
        {
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
            if self.untouched(dependencies, Some(&prop)) {
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
                Some(binding) if self.imports.contains(binding) => Err(ImpureCause::ForeignRead),
                Some(binding) => {
                    self.eager.push(*binding);
                    Ok(())
                }
                None => Err(ImpureCause::Unmodeled),
            },
            Expr::Array(array) => {
                for element in array.elems.iter().flatten() {
                    if element.spread.is_some() {
                        return Err(ImpureCause::Conversion);
                    }
                    self.pure(&element.expr)?;
                }
                Ok(())
            }
            Expr::Object(object) => {
                for prop in &object.props {
                    let PropOrSpread::Prop(prop) = prop else {
                        return Err(ImpureCause::Conversion);
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
            Expr::Call(call) => Err(match &call.callee {
                Callee::Expr(callee) => self.call_cause(callee),
                _ => ImpureCause::CallOther,
            }),
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

    fn statement_cause(&self, expr: &Expr) -> ImpureCause {
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

    fn declarator(&mut self, declarator: &VarDeclarator) -> Kind {
        if !matches!(declarator.name, Pat::Ident(_)) {
            return Kind::Impure(ImpureCause::Destructuring);
        }
        match &declarator.init {
            None => Kind::Pure,
            Some(init) => match self.pure(init) {
                Ok(()) => Kind::Pure,
                Err(cause) => Kind::Impure(cause),
            },
        }
    }

    fn decl(&mut self, decl: &Decl) -> Kind {
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

    fn item(&mut self, item: &ModuleItem) -> Kind {
        match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => self.decl(decl),
            ModuleItem::Stmt(Stmt::Empty(_)) => Kind::Inert,
            ModuleItem::Stmt(Stmt::Expr(statement)) => {
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

fn pattern_ids(pat: &Pat, out: &mut Vec<Id>) {
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

fn declared_ids(decl: &Decl, out: &mut Vec<Id>) {
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

enum Source<'a> {
    Item(&'a ModuleItem),
    Declarator(&'a VarDeclarator),
}

pub struct Options {
    /// Treat exported bindings as reachable by unknown code from the start:
    /// needed when another chunk that imports from this one can run during
    /// this chunk's initialization (an import cycle).
    pub exports_reachable: bool,
    pub upgrades: FxHashSet<Upgrade>,
    /// An estimate, not an analysis: count statements impure for these
    /// causes as pure definitions, to size what refining a cause could buy.
    pub what_if_pure: FxHashSet<ImpureCause>,
}

pub fn analyze(module: &Module, unresolved: SyntaxContext, options: &Options) -> Tier0 {
    let mut sources = Vec::new();
    for item in &module.body {
        match js_ast::split_var_declarators(item) {
            Some(declarators) => sources.extend(declarators.iter().map(Source::Declarator)),
            None => sources.push(Source::Item(item)),
        }
    }
    let mut bindings: FxHashMap<Id, Binding> = FxHashMap::default();
    let mut imports = FxHashSet::default();
    let mut exported = FxHashSet::default();
    let mut declared_by_unit = Vec::new();
    let intern = |id: Id, bindings: &mut FxHashMap<Id, Binding>| -> Binding {
        let next = bindings.len() as Binding;
        *bindings.entry(id).or_insert(next)
    };
    for source in &sources {
        let mut ids = Vec::new();
        let mut is_export = false;
        match source {
            Source::Declarator(declarator) => pattern_ids(&declarator.name, &mut ids),
            Source::Item(ModuleItem::Stmt(Stmt::Decl(decl))) => declared_ids(decl, &mut ids),
            Source::Item(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export))) => {
                declared_ids(&export.decl, &mut ids);
                is_export = true;
            }
            Source::Item(ModuleItem::ModuleDecl(ModuleDecl::Import(import))) => {
                for specifier in &import.specifiers {
                    let local = match specifier {
                        ImportSpecifier::Named(named) => &named.local,
                        ImportSpecifier::Default(default) => &default.local,
                        ImportSpecifier::Namespace(namespace) => &namespace.local,
                    };
                    let binding = intern(local.to_id(), &mut bindings);
                    imports.insert(binding);
                }
            }
            Source::Item(_) => {}
        }
        let declared: Vec<Binding> = ids
            .into_iter()
            .map(|id| intern(id, &mut bindings))
            .collect();
        if is_export {
            exported.extend(declared.iter().copied());
        }
        declared_by_unit.push(declared);
    }
    // A declarator split out of `export var a = …, b = …` is exported too.
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                let mut ids = Vec::new();
                declared_ids(&export.decl, &mut ids);
                exported.extend(ids.iter().filter_map(|id| bindings.get(id)));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none() => {
                for specifier in &named.specifiers {
                    if let ExportSpecifier::Named(ExportNamedSpecifier {
                        orig: ModuleExportName::Ident(ident),
                        ..
                    }) = specifier
                    {
                        exported.extend(bindings.get(&ident.to_id()));
                    }
                }
            }
            _ => {}
        }
    }

    let mut assigned_anywhere: FxHashSet<Binding> = FxHashSet::default();
    for source in &sources {
        let assigned = match source {
            Source::Item(item) => assigned_ids(*item),
            Source::Declarator(declarator) => assigned_ids(*declarator),
        };
        assigned_anywhere.extend(assigned.iter().filter_map(|id| bindings.get(id)));
    }
    let mut declarations = vec![0u32; bindings.len()];
    for binding in declared_by_unit.iter().flatten() {
        declarations[*binding as usize] += 1;
    }
    let mut primitives: FxHashSet<Binding> = FxHashSet::default();
    if options.upgrades.contains(&Upgrade::PrimitiveBindings) {
        // The initializer of each binding declared by one identifier
        // declarator and never assigned.
        let mut candidates: Vec<(Binding, Option<&Expr>)> = Vec::new();
        for (source, declared) in sources.iter().zip(&declared_by_unit) {
            let declarator = match source {
                Source::Declarator(declarator) => Some(*declarator),
                Source::Item(
                    ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
                    | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                        decl: Decl::Var(var),
                        ..
                    })),
                ) => var.decls.first(),
                Source::Item(_) => None,
            };
            if let (Some(declarator), [binding]) = (declarator, declared.as_slice())
                && matches!(declarator.name, Pat::Ident(_))
                && declarations[*binding as usize] == 1
                && !assigned_anywhere.contains(binding)
            {
                candidates.push((*binding, declarator.init.as_deref()));
            }
        }
        loop {
            let before = primitives.len();
            for (binding, init) in &candidates {
                let holds_primitive = init
                    .is_none_or(|init| primitive(init, &bindings, &primitives, &mut Vec::new()));
                if holds_primitive {
                    primitives.insert(*binding);
                }
            }
            if primitives.len() == before {
                break;
            }
        }
    }

    let mut refused = false;
    let mut families_used: std::collections::BTreeMap<&'static str, usize> = Default::default();
    let object_aliases = crate::helpers::prepass(module, unresolved).object_aliases;
    let census = census(module, unresolved, &object_aliases);
    let mut units = Vec::with_capacity(sources.len());
    for (source, declared) in sources.iter().zip(declared_by_unit) {
        let mut scan = Scan {
            bindings: &bindings,
            unresolved,
            mentions: FxHashSet::default(),
            refused: false,
        };
        let mut classifier = Classifier {
            bindings: &bindings,
            imports: &imports,
            primitives: &primitives,
            upgrades: &options.upgrades,
            census: &census,
            unresolved,
            eager: Vec::new(),
            intrinsics: Vec::new(),
        };
        let (kind, assigned) = match source {
            Source::Item(item) => {
                item.visit_with(&mut scan);
                (classifier.item(item), assigned_ids(*item))
            }
            Source::Declarator(declarator) => {
                declarator.visit_with(&mut scan);
                (classifier.declarator(declarator), assigned_ids(*declarator))
            }
        };
        let kind = match kind {
            Kind::Impure(cause) if options.what_if_pure.contains(&cause) => Kind::Pure,
            kind => kind,
        };
        refused |= scan.refused;
        if kind == Kind::Pure {
            for global in &classifier.intrinsics {
                *families_used.entry(*global).or_default() += 1;
            }
        }
        let mut mentions: Vec<Binding> = scan.mentions.into_iter().collect();
        mentions.sort_unstable();
        // A default export hands its value to other chunks.
        let default_export = matches!(
            source,
            Source::Item(ModuleItem::ModuleDecl(
                ModuleDecl::ExportDefaultDecl(_) | ModuleDecl::ExportDefaultExpr(_)
            ))
        );
        if default_export {
            exported.extend(mentions.iter().copied());
        }
        units.push(Unit {
            kind,
            declared,
            eager: classifier.eager,
            mentions,
            assigns: assigned
                .iter()
                .filter_map(|id| bindings.get(id).copied())
                .collect(),
        });
    }
    if refused {
        // `eval` or `with` can name any binding: nothing is a pure definition.
        for unit in &mut units {
            if unit.kind != Kind::Inert {
                unit.kind = Kind::Impure(ImpureCause::Unmodeled);
            }
        }
    }

    // What a binding's value can reference: everything mentioned by the
    // unit that declares it and by every unit that assigns it.
    let binding_count = bindings.len();
    let mut edges: Vec<Vec<Binding>> = vec![Vec::new(); binding_count];
    for unit in &units {
        for binding in unit.declared.iter().chain(&unit.assigns) {
            edges[*binding as usize].extend(&unit.mentions);
        }
    }
    let closure = |seeds: &[Binding], visit: &mut dyn FnMut(Binding) -> bool| {
        let mut pending: Vec<Binding> = seeds.to_vec();
        while let Some(binding) = pending.pop() {
            if visit(binding) {
                pending.extend(&edges[binding as usize]);
            }
        }
    };

    let mut reach_stamp = vec![NEVER; binding_count];
    if options.exports_reachable {
        let seeds: Vec<Binding> = exported.iter().copied().collect();
        closure(&seeds, &mut |binding| {
            let fresh = reach_stamp[binding as usize] == NEVER;
            reach_stamp[binding as usize] = 0;
            fresh
        });
    }
    let mut own_reach = FxHashMap::default();
    for (ordinal, unit) in units.iter().enumerate() {
        if !matches!(unit.kind, Kind::Impure(_)) {
            continue;
        }
        closure(&unit.mentions, &mut |binding| {
            let fresh = reach_stamp[binding as usize] == NEVER;
            if fresh {
                reach_stamp[binding as usize] = ordinal as u32;
            }
            fresh
        });
        let mut own = FxHashSet::default();
        closure(&unit.mentions, &mut |binding| own.insert(binding));
        own_reach.insert(ordinal, own);
    }
    // An assignment can run once its unit runs, if the unit is impure, or
    // once a binding the unit declares is reachable.
    let mut write_stamp = vec![NEVER; binding_count];
    for (ordinal, unit) in units.iter().enumerate() {
        let enabled = match unit.kind {
            Kind::Impure(_) => ordinal as u32,
            _ => unit
                .declared
                .iter()
                .map(|binding| reach_stamp[*binding as usize])
                .min()
                .unwrap_or(NEVER),
        };
        let own_declarations: &[Binding] = match unit.kind {
            Kind::Impure(_) => &unit.declared,
            _ => &[],
        };
        for binding in unit.assigns.iter().chain(own_declarations) {
            let stamp = &mut write_stamp[*binding as usize];
            *stamp = (*stamp).min(enabled);
        }
    }
    Tier0 {
        units,
        reach_stamp,
        write_stamp,
        own_reach,
        bindings: binding_count,
        refused,
        families_used,
        census,
    }
}

impl Tier0 {
    fn impure_with_definition(&self, impure: usize, definition: usize) -> Option<Conflict> {
        let at = impure as u32;
        let unit = &self.units[definition];
        let initializes = unit.kind == Kind::Pure;
        if initializes {
            for binding in &unit.declared {
                if self.reach_stamp[*binding as usize] <= at {
                    let own = self.own_reach[&impure].contains(binding);
                    return Some(if own {
                        Conflict::ReachOwn
                    } else {
                        Conflict::ReachEscaped
                    });
                }
            }
        }
        let written = |binding: &Binding| self.write_stamp[*binding as usize] <= at;
        if unit.eager.iter().any(written) || unit.declared.iter().any(written) {
            return Some(Conflict::Write);
        }
        None
    }

    /// Why the two units are not proven to commute, if they are not.
    pub fn conflict(&self, a: usize, b: usize) -> Option<Conflict> {
        let (first, second) = (&self.units[a], &self.units[b]);
        match (first.kind, second.kind) {
            (Kind::Inert, _) | (_, Kind::Inert) => None,
            (Kind::Impure(_), Kind::Impure(_)) => Some(Conflict::BothImpure),
            (Kind::Impure(_), _) => self.impure_with_definition(a, b),
            (_, Kind::Impure(_)) => self.impure_with_definition(b, a),
            _ => {
                let reads = |reader: &Unit, definition: &Unit| {
                    definition.kind == Kind::Pure
                        && reader
                            .eager
                            .iter()
                            .any(|binding| definition.declared.contains(binding))
                };
                let redeclares = first
                    .declared
                    .iter()
                    .any(|binding| second.declared.contains(binding));
                (reads(first, second) || reads(second, first) || redeclares)
                    .then_some(Conflict::EagerRead)
            }
        }
    }
}
