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
use crate::premises::{Callee as PremisedCallee, PremiseKind, Premises, SiteKind};
use crate::program::ProgramFacts;

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
    /// A definition that calls into a provider with state of its own: it
    /// reads and writes nothing of this chunk beyond its eager reads, but
    /// keeps its order among statements that may run the same provider.
    Contained,
    /// A premised helper call whose effects are confined to its target
    /// object: it leaks nothing and writes no binding but a replaced target.
    Confined,
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
    /// The impure statement can reach an object a premised call reads, or a
    /// confined statement's target.
    ObjectState,
    /// Two confined statements share a target, or one involves a binding the
    /// other replaces.
    SharedTarget,
    /// Both statements may run code of one provider that keeps private
    /// state, or one is impure and may call any provider.
    ProviderState,
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
    /// Bindings whose objects a premised call may read, with everything
    /// those objects were built from.
    deep: FxHashSet<Binding>,
    /// The first impure ordinal that can reach a `deep` binding.
    deep_stamp: u32,
    /// For a confined statement: the bindings holding its target object, and
    /// the bindings it replaces.
    targets: Vec<Binding>,
    replaces: Vec<Binding>,
    /// Providers with private state the statement may run: `0` is this
    /// chunk, the rest are import sources, `ANY_PROVIDER` is all of them.
    providers: Vec<u32>,
    /// Lazy-module thunks the statement calls when it runs, and those of
    /// them no earlier definition calls: this statement may run the module.
    lazy_uses: Vec<Binding>,
    lazy_first: Vec<Binding>,
    /// Literals the definition reads a property of: it is a pure definition
    /// only while no impure statement can have reached them before it.
    pristine: Vec<Binding>,
}

/// A lazy module's body may run code of any provider.
const ANY_PROVIDER: u32 = u32::MAX;

pub struct Tier0 {
    pub units: Vec<Unit>,
    /// Per binding: the first impure ordinal whose reach holds it.
    reach_stamp: Vec<u32>,
    /// Per binding: the first impure ordinal that may assign it.
    write_stamp: Vec<u32>,
    /// Per binding: the impure unit that declares it.
    declarer: Vec<u32>,
    /// Per impure unit: the closure of its own mentions.
    own_reach: FxHashMap<usize, FxHashSet<Binding>>,
    pub bindings: usize,
    /// Per source unit: the names it declares.
    pub declared_names: Vec<Vec<String>>,
    /// `eval` and `with` sites; any makes every statement impure.
    pub refused: usize,
    /// `await` outside any function: other modules may run in between, so
    /// exports count as reachable.
    pub top_level_awaits: usize,
    /// Imports that only ever hold one string, by the program's chunks.
    pub imported_strings: usize,
    /// Whether exported bindings were treated as reachable from the start.
    pub exports_reachable: bool,
    /// Intrinsic uses left impure because the census found a write.
    /// The object literals of the chunk that the program does more with
    /// than read named members, as far as derivation saw.
    pub settled_refusals: Vec<SettledRefusalRow>,
    pub intrinsics_denied: usize,
    /// The same per family the use depends on and why the census refused.
    pub intrinsic_refusals:
        std::collections::BTreeMap<(&'static str, crate::intrinsics::Refusal), usize>,
    pub premises_given: usize,
    /// Premises whose callee the chunk names.
    pub premises_matched: usize,
    /// Per premise kind: the statements it made pure or confined.
    pub premise_uses: std::collections::BTreeMap<PremiseKind, usize>,
    /// Premised statements that read an import, and so stay ordered against
    /// every impure statement.
    pub premised_reading_imports: usize,
    /// Site premises that changed a statement's class.
    pub site_premise_uses: usize,
    pub derived: DerivedCounts,
    /// Definitions that may run a lazy module's body, and those that call a
    /// thunk an earlier definition already called.
    pub lazy_first_uses: usize,
    pub lazy_later_uses: usize,
    commuting: FxHashSet<(usize, usize)>,
    /// Per intrinsic global: the pure definitions that depend on it being
    /// unmodified.
    pub families_used: std::collections::BTreeMap<&'static str, usize>,
    /// What the chunk itself writes on globals.
    pub census: Census,
}

const NEVER: u32 = u32::MAX;

/// The bindings mentioned inside the functions and classes of an
/// expression: what running them at once would read.
struct Bodies<'a> {
    bindings: &'a FxHashMap<Id, Binding>,
    depth: usize,
    mentions: FxHashSet<Binding>,
}

impl Visit for Bodies<'_> {
    fn visit_function(&mut self, function: &Function) {
        self.depth += 1;
        function.visit_children_with(self);
        self.depth -= 1;
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.depth += 1;
        arrow.visit_children_with(self);
        self.depth -= 1;
    }

    fn visit_class(&mut self, class: &Class) {
        self.depth += 1;
        class.visit_children_with(self);
        self.depth -= 1;
    }

    fn visit_ident(&mut self, ident: &Ident) {
        if self.depth > 0
            && let Some(binding) = self.bindings.get(&ident.to_id())
        {
            self.mentions.insert(*binding);
        }
    }
}

struct Scan<'a> {
    bindings: &'a FxHashMap<Id, Binding>,
    unresolved: SyntaxContext,
    mentions: FxHashSet<Binding>,
    /// Those outside any function body.
    eager: FxHashSet<Binding>,
    /// `eval` and `with` sites.
    refused: usize,
    /// `await` and `for await` outside any function.
    top_level_awaits: usize,
    function_depth: usize,
}

impl Visit for Scan<'_> {
    fn visit_function(&mut self, function: &Function) {
        self.function_depth += 1;
        function.visit_children_with(self);
        self.function_depth -= 1;
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.function_depth += 1;
        arrow.visit_children_with(self);
        self.function_depth -= 1;
    }

    fn visit_await_expr(&mut self, awaited: &AwaitExpr) {
        if self.function_depth == 0 {
            self.top_level_awaits += 1;
        }
        awaited.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, looped: &ForOfStmt) {
        if looped.is_await && self.function_depth == 0 {
            self.top_level_awaits += 1;
        }
        looped.visit_children_with(self);
    }

    fn visit_ident(&mut self, ident: &Ident) {
        if let Some(binding) = self.bindings.get(&ident.to_id()) {
            self.mentions.insert(*binding);
            if self.function_depth == 0 {
                self.eager.insert(*binding);
            }
        }
        if ident.ctxt == self.unresolved && &*ident.sym == "eval" {
            self.refused += 1;
        }
    }

    fn visit_with_stmt(&mut self, with: &WithStmt) {
        self.refused += 1;
        with.visit_children_with(self);
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
    /// A class extending a local binding that only ever holds a class: the
    /// definition reads that binding and runs nothing.
    LocalHeritage,
    /// Static class fields whose initializer is a pure-definition
    /// expression.
    StaticFields,
    /// `table.key` where `table` only ever holds one plain object literal
    /// that has `key`, and no impure statement can have reached the object
    /// before the read.
    LiteralMemberReads,
    /// A read of an imported binding: the definition stays ordered against
    /// every impure statement.
    ImportReads,
    /// `(function (e) { e[e.A = 0] = "A"; e.B = "b"; })(E || (E = {}))` for a
    /// binding declared without a value and assigned nowhere else: its
    /// effects are confined to `E` (`enum_definition`).
    EnumDefinitions,
    /// `f(primitives…)` for a local function that only ever returns an
    /// expression of operators and templates over its parameters, literals
    /// and primitive-only bindings (`primitive_function`).
    PrimitiveCalls,
}

/// The primitive-only bindings a function reads, if it does nothing but
/// return an expression of operators and templates over its plain
/// parameters, literals and such bindings: called with primitives it runs no
/// other code, cannot throw and returns a primitive.
fn primitive_function(
    function: &Expr,
    bindings: &FxHashMap<Id, Binding>,
    primitives: &FxHashSet<Binding>,
) -> Option<Vec<Binding>> {
    let (params, body): (Vec<&Pat>, &Expr) = match function {
        Expr::Arrow(arrow) if !arrow.is_async && !arrow.is_generator => (
            arrow.params.iter().collect(),
            match &*arrow.body {
                ArrowFunctionBody::Expr(body) => body,
                ArrowFunctionBody::FunctionBody(block) => returned(&block.stmts)?,
            },
        ),
        Expr::Fn(FnExpr { function, .. }) if !function.is_async && !function.is_generator => (
            function.params.iter().map(|param| &param.pat).collect(),
            returned(&function.body.as_ref()?.stmts)?,
        ),
        _ => return None,
    };
    let params: FxHashSet<Id> = params
        .into_iter()
        .map(|param| match param {
            Pat::Ident(name) => Some(name.id.to_id()),
            _ => None,
        })
        .collect::<Option<_>>()?;
    let mut reads = Vec::new();
    over_primitives(body, &params, bindings, primitives, &mut reads).then_some(reads)
}

/// The expression of a body that is one `return`.
fn returned(statements: &[Stmt]) -> Option<&Expr> {
    match statements {
        [
            Stmt::Return(ReturnStmt {
                arg: Some(value), ..
            }),
        ] => Some(value),
        _ => None,
    }
}

/// The grammar of `primitive`, with parameters standing for primitives.
fn over_primitives(
    expr: &Expr,
    params: &FxHashSet<Id>,
    bindings: &FxHashMap<Id, Binding>,
    primitives: &FxHashSet<Binding>,
    reads: &mut Vec<Binding>,
) -> bool {
    let mut recurse = |expr: &Expr| over_primitives(expr, params, bindings, primitives, reads);
    match expr {
        Expr::Paren(paren) => recurse(&paren.expr),
        Expr::Lit(Lit::Num(_) | Lit::Str(_) | Lit::Bool(_) | Lit::Null(_)) => true,
        Expr::Ident(ident) if params.contains(&ident.to_id()) => true,
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

/// The named data properties of an object literal: the key, whether the
/// value is written as a primitive, and the value.
fn literal_keys(object: &ObjectLit) -> Vec<(String, bool, Option<&Expr>)> {
    object
        .props
        .iter()
        .filter_map(|prop| match prop {
            PropOrSpread::Prop(prop) => match &**prop {
                Prop::Shorthand(key) => Some((key.sym.to_string(), false, None)),
                Prop::KeyValue(pair) => {
                    let primitive = literal_only(&pair.value);
                    let value = Some(&*pair.value);
                    match &pair.key {
                        PropName::Ident(key) => Some((key.sym.to_string(), primitive, value)),
                        PropName::Str(key) => Some((js_ast::str_value(key), primitive, value)),
                        _ => None,
                    }
                }
                _ => None,
            },
            PropOrSpread::Spread(_) => None,
        })
        .collect()
}

fn unparen(mut expr: &Expr) -> &Expr {
    while let Expr::Paren(paren) = expr {
        expr = &paren.expr;
    }
    expr
}

/// An enum-shaped call: an inline function or arrow of one parameter whose
/// body only stores literals on the parameter under literal names, called
/// with `E || (E = {})` or `E || {}`.
struct EnumCall<'a> {
    target: &'a Ident,
    /// The argument assigns the binding: `E || (E = {})`.
    assigns: bool,
    /// The call returns the parameter.
    returns: bool,
}

fn enum_call(expr: &Expr) -> Option<EnumCall<'_>> {
    let Expr::Call(CallExpr {
        callee: Callee::Expr(callee),
        args,
        ..
    }) = unparen(expr)
    else {
        return None;
    };
    let [
        ExprOrSpread {
            spread: None,
            expr: argument,
        },
    ] = args.as_slice()
    else {
        return None;
    };
    let Expr::Bin(BinExpr {
        op: BinaryOp::LogicalOr,
        left,
        right,
        ..
    }) = unparen(argument)
    else {
        return None;
    };
    let Expr::Ident(target) = unparen(left) else {
        return None;
    };
    let empty =
        |expr: &Expr| matches!(unparen(expr), Expr::Object(object) if object.props.is_empty());
    let assigns = match unparen(right) {
        Expr::Assign(assign) => {
            let fresh = assign.op == AssignOp::Assign
                && matches!(&assign.left, AssignTarget::Simple(SimpleAssignTarget::Ident(assigned))
                    if assigned.id.to_id() == target.to_id())
                && empty(&assign.right);
            if !fresh {
                return None;
            }
            true
        }
        other if empty(other) => false,
        _ => return None,
    };
    // The parameter, and the body as statements or as one expression.
    let (param, statements, expression): (&Pat, &[Stmt], Option<&Expr>) = match unparen(callee) {
        Expr::Fn(FnExpr {
            ident: None,
            function,
        }) if !function.is_async && !function.is_generator => match function.params.as_slice() {
            [param] if param.decorators.is_empty() => {
                (&param.pat, &function.body.as_ref()?.stmts, None)
            }
            _ => return None,
        },
        Expr::Arrow(arrow) if !arrow.is_async && !arrow.is_generator => {
            let [param] = arrow.params.as_slice() else {
                return None;
            };
            match &*arrow.body {
                ArrowFunctionBody::FunctionBody(block) => (param, &block.stmts, None),
                ArrowFunctionBody::Expr(body) => (param, &[], Some(body)),
            }
        }
        _ => return None,
    };
    let Pat::Ident(param) = param else {
        return None;
    };
    let param = param.id.to_id();
    let on_param =
        |expr: &Expr| matches!(unparen(expr), Expr::Ident(ident) if ident.to_id() == param);
    // `e.name = literal`, `e["name"] = literal`.
    let named = |assign: &AssignExpr| {
        let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left else {
            return false;
        };
        let key = match &member.prop {
            MemberProp::Ident(_) => true,
            MemberProp::Computed(computed) => matches!(&*computed.expr, Expr::Lit(Lit::Str(_))),
            MemberProp::PrivateName(_) => false,
        };
        assign.op == AssignOp::Assign && key && on_param(&member.obj) && literal_only(&assign.right)
    };
    let store = |expr: &Expr| {
        let Expr::Assign(assign) = unparen(expr) else {
            return false;
        };
        if named(assign) {
            return true;
        }
        // `e[e.name = literal] = "name"`.
        let AssignTarget::Simple(SimpleAssignTarget::Member(MemberExpr {
            obj,
            prop: MemberProp::Computed(computed),
            ..
        })) = &assign.left
        else {
            return false;
        };
        assign.op == AssignOp::Assign
            && on_param(obj)
            && matches!(&*assign.right, Expr::Lit(Lit::Str(_)))
            && matches!(unparen(&computed.expr), Expr::Assign(inner) if named(inner))
    };
    // Stores, then optionally the parameter itself.
    let mut parts: Vec<&Expr> = Vec::new();
    let mut returns = false;
    fn spread<'e>(expr: &'e Expr, parts: &mut Vec<&'e Expr>) {
        match unparen(expr) {
            Expr::Seq(seq) => parts.extend(seq.exprs.iter().map(|part| unparen(part))),
            other => parts.push(other),
        }
    }
    for (index, statement) in statements.iter().enumerate() {
        match statement {
            Stmt::Expr(statement) => spread(&statement.expr, &mut parts),
            Stmt::Return(ReturnStmt {
                arg: Some(value), ..
            }) if index + 1 == statements.len() => {
                spread(value, &mut parts);
                returns = true;
            }
            _ => return None,
        }
    }
    if let Some(expression) = expression {
        spread(expression, &mut parts);
        returns = true;
    }
    if returns {
        // What is returned must be the parameter.
        if !on_param(parts.pop()?) {
            return None;
        }
    }
    parts.iter().all(|part| store(part)).then_some(EnumCall {
        target,
        assigns,
        returns,
    })
}

/// The binding an enum-shaped statement defines: `enum_call` with the
/// argument `E || (E = {})`.
fn enum_definition(expr: &Expr) -> Option<&Ident> {
    enum_call(expr)
        .filter(|call| call.assigns)
        .map(|call| call.target)
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

/// Own properties of intrinsic namespaces. A read of one is not answered by
/// any prototype object; any other name is looked up on `Function.prototype`
/// or `Object.prototype`.
const OWN_STATICS: [(&str, &[&str]); 9] = [
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
    /// Bindings that only ever hold one class.
    classes: &'a FxHashSet<Binding>,
    /// Bindings that only ever hold one plain object literal, with its keys.
    /// Each key says whether its value is written as a primitive literal.
    literals: &'a FxHashMap<Binding, FxHashMap<String, bool>>,
    /// Bindings that only ever hold one `Symbol(…)` or `Symbol.for(…)`.
    symbols: &'a FxHashSet<Binding>,
    /// Bindings that only ever hold one array literal.
    arrays: &'a FxHashSet<Binding>,
    settled: &'a Settled,
    /// Members of such literals written as plain object literals, with
    /// their keys, where the inner object cannot change either.
    sealed_members: &'a FxHashSet<(Binding, String)>,
    nested_literals: &'a FxHashMap<(Binding, String), FxHashMap<String, bool>>,
    /// Bindings that only ever hold a function pure over primitives, with
    /// the bindings a call of it reads: see `primitive_function`.
    primitive_functions: &'a FxHashMap<Binding, Vec<Binding>>,
    /// Bindings only enum-shaped statements assign: see `enum_definition`.
    enums: &'a FxHashSet<Binding>,
    /// Bindings a `var` declares by an enum-shaped call on itself.
    enum_declarations: &'a FxHashSet<Binding>,
    /// Literals nothing in the chunk can change: see `sealed_literals`.
    sealed: &'a FxHashSet<Binding>,
    /// Literals the statement reads a property of.
    pristine: Vec<Binding>,
    upgrades: &'a FxHashSet<Upgrade>,
    census: &'a Census,
    unresolved: SyntaxContext,
    eager: Vec<Binding>,
    /// The globals the accepted intrinsic uses depend on.
    intrinsics: Vec<&'static str>,
    /// Intrinsic uses the census refused.
    denied: Vec<(&'static str, crate::intrinsics::Refusal)>,
    premises: &'a ResolvedPremises,
    /// Bindings read by the arguments of premised calls.
    deep: Vec<Binding>,
    targets: Vec<Binding>,
    replaces: Vec<Binding>,
    premise_uses: Vec<PremiseKind>,
    /// Positions in `eager` that hold the callee of a premised call.
    callee_slots: FxHashSet<usize>,
    /// Nesting depth in the arguments of premised calls.
    premised_args: usize,
    providers: Vec<u32>,
    /// Lazy-module thunks the statement calls when it runs.
    lazy_uses: Vec<Binding>,
    /// A site premise waives the census for this statement.
    native_site: bool,
    never_written: &'a FxHashSet<String>,
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
    callees: FxHashMap<Binding, (Vec<PremiseKind>, usize)>,
    members: FxHashMap<(Binding, String), (Vec<PremiseKind>, usize)>,
    /// Per imported binding: its source, numbered from 1.
    providers: FxHashMap<Binding, u32>,
    /// Per derived allocator: the bindings a call of it reads.
    allocator_reads: FxHashMap<Binding, Vec<Binding>>,
    /// Per derived allocator: argument positions that must be fresh object
    /// literals.
    literal_params: FxHashMap<Binding, Vec<usize>>,
    /// Per derived allocator: the intrinsic collections it constructs.
    allocator_intrinsics: FxHashMap<Binding, Vec<&'static str>>,
    /// Derived: bindings holding the helper that defines getters on its
    /// first argument from the functions of its second.
    exporters: FxHashSet<Binding>,
    /// Derived: bindings holding the helper that returns a fresh object
    /// with a getter per function of its argument.
    exporter_allocators: FxHashSet<Binding>,
    /// Derived: imports whose value is fixed once their chunk has run, for
    /// a chunk off any import cycle.
    stable_imports: FxHashSet<Binding>,
    /// Derived: those of them whose exporting chunk declares a class: its
    /// `prototype` cannot be replaced.
    stable_class_imports: FxHashSet<Binding>,
    /// Derived: literals of this chunk the whole program only reads named
    /// members of.
    settled_locals: FxHashSet<Binding>,
    pub given: usize,
    pub matched: usize,
}

/// What a call or construction of a callee with these kinds is. A callee
/// that is both pure and a callback factory only allocates and retains its
/// arguments.
fn call_kind(kinds: &[PremiseKind], constructs: bool) -> Option<PremiseKind> {
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
    fn provider(&self, binding: Binding) -> u32 {
        self.providers.get(&binding).copied().unwrap_or(0)
    }
}

/// Whether `expr` evaluates to a number, string, boolean, `null` or
/// `undefined` without running code or throwing, given the primitive
/// bindings; collects the bindings it reads.
fn primitive(
    expr: &Expr,
    bindings: &FxHashMap<Id, Binding>,
    primitives: &FxHashSet<Binding>,
    settled: &Settled,
    reads: &mut Vec<Binding>,
) -> bool {
    let mut recurse = |expr: &Expr| primitive(expr, bindings, primitives, settled, reads);
    match expr {
        // A member of a settled import, written as a primitive.
        Expr::Member(MemberExpr {
            obj,
            prop: MemberProp::Ident(key),
            ..
        }) => {
            let Expr::Ident(table) = &**obj else {
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

/// Imports holding an object literal that nothing in the program handles
/// except to read a named member: per key, whether its value is written as
/// a primitive.
type Settled = FxHashMap<Binding, FxHashMap<String, bool>>;

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

    fn key(&mut self, key: &PropName) -> Result<(), ImpureCause> {
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
    fn computed_key(&mut self, expr: &Expr) -> Result<(), ImpureCause> {
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

    fn global(&self, expr: &Expr) -> Option<String> {
        match expr {
            Expr::Ident(ident) if ident.ctxt == self.unresolved => Some(ident.sym.to_string()),
            _ => None,
        }
    }

    /// A call of a function pure over primitives with primitive arguments.
    fn primitive_call(&mut self, call: &CallExpr) -> bool {
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

    fn primitive_arg(&mut self, expr: &Expr) -> bool {
        let mut reads = Vec::new();
        let ok = primitive(
            expr,
            self.bindings,
            self.primitives,
            self.settled,
            &mut reads,
        );
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
    /// `prototype_members` says the properties are members of the
    /// intrinsic's prototype object, not of the constructor or namespace.
    fn untouched(
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
    fn read_literal(&mut self, table: Binding) {
        self.eager.push(table);
        if !self.sealed.contains(&table) {
            self.deep.push(table);
            self.pristine.push(table);
        }
    }

    /// `table.member.key` where `table.member` is written as a plain object
    /// literal with that key and neither object can change.
    fn nested_member_read(&self, expr: &Expr) -> Option<Binding> {
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
    fn literal_member_read(&self, expr: &Expr) -> Option<Binding> {
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
    fn premise(&self, callee: &Expr) -> Option<(Vec<PremiseKind>, usize, Binding)> {
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
    fn premised_allocation(&mut self, expr: &Expr) -> bool {
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
    fn premised_argument(&mut self, expr: &Expr) -> bool {
        self.premised_args += 1;
        let pure = self.pure(expr).is_ok();
        self.premised_args -= 1;
        pure
    }

    /// `dec` or `dec(args)` under a decorator premise on `dec`.
    fn decorator(&mut self, expr: &Expr) -> bool {
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
    fn export_getters(&mut self, getters: &Expr, tagged: bool) -> bool {
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
    fn export_allocation(&mut self, call: &CallExpr) -> bool {
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
    fn export_call(&mut self, expr: &Expr) -> bool {
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
    fn reads_only(&mut self, expr: &Expr) -> bool {
        match expr {
            Expr::Paren(paren) => self.reads_only(&paren.expr),
            Expr::Seq(seq) => seq.exprs.iter().all(|part| self.reads_only(part)),
            Expr::Unary(unary) if unary.op == UnaryOp::Void => self.reads_only(&unary.arg),
            Expr::Member(_) | Expr::Ident(_) | Expr::Lit(_) => self.pure(expr).is_ok(),
            _ => false,
        }
    }

    fn confined(&mut self, expr: &Expr) -> bool {
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
    fn class(&mut self, class: &Class) -> Result<(), ImpureCause> {
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
    fn pure(&mut self, expr: &Expr) -> Result<(), ImpureCause> {
        if literal_only(expr) {
            return Ok(());
        }
        let mut reads = Vec::new();
        if primitive(
            expr,
            self.bindings,
            self.primitives,
            self.settled,
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

/// An object literal that is not unchangeable program-wide.
#[derive(Debug, Clone, serde::Serialize)]
pub struct SettledRefusalRow {
    /// The source unit declaring it.
    pub unit: Option<usize>,
    pub reason: crate::derive::SettledRefusal,
    /// How often the chunk reads a named member of it.
    pub named_reads: usize,
    pub exported: bool,
}

/// How often each binding is the object of a named member read.
#[derive(Default)]
struct NamedReads(FxHashMap<Id, usize>);

impl Visit for NamedReads {
    fn visit_member_expr(&mut self, member: &MemberExpr) {
        if let (Expr::Ident(binding), MemberProp::Ident(_)) = (&*member.obj, &member.prop) {
            *self.0.entry(binding.to_id()).or_default() += 1;
        }
        member.visit_children_with(self);
    }
}

/// How often each binding is assigned by a plain `=`, and the bindings
/// assigned any other way.
#[derive(Default)]
struct AssignmentCounts {
    plain: FxHashMap<Id, usize>,
    other: FxHashSet<Id>,
}

impl Visit for AssignmentCounts {
    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        match &assign.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(target))
                if assign.op == AssignOp::Assign =>
            {
                *self.plain.entry(target.id.to_id()).or_default() += 1;
            }
            _ => self.other.extend(crate::helpers::assigned_ids(assign)),
        }
        assign.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        self.other.extend(crate::helpers::assigned_ids(update));
        update.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, looped: &ForInStmt) {
        self.other
            .extend(crate::helpers::assigned_ids(&looped.left));
        looped.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, looped: &ForOfStmt) {
        self.other
            .extend(crate::helpers::assigned_ids(&looped.left));
        looped.visit_children_with(self);
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
    /// Facts about the program's other chunks, when they were read.
    pub program: Option<ProgramFacts>,
    pub premises: Premises,
    /// Derive callee contracts from their bodies.
    pub derive: bool,
}

fn resolve_premises(
    module: &Module,
    premises: &Premises,
    bindings: &FxHashMap<Id, Binding>,
    declared_by_unit: &[Vec<Binding>],
) -> ResolvedPremises {
    let mut imported: FxHashMap<(String, String), Vec<Binding>> = FxHashMap::default();
    let mut sources: FxHashMap<String, u32> = FxHashMap::default();
    let mut providers = FxHashMap::default();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        let specifier = js_ast::str_value(&import.src);
        for import_specifier in &import.specifiers {
            let (export, local) = match import_specifier {
                ImportSpecifier::Named(named) => (
                    match &named.imported {
                        Some(ModuleExportName::Ident(name)) => name.sym.to_string(),
                        Some(ModuleExportName::Str(name)) => js_ast::str_value(name),
                        None => named.local.sym.to_string(),
                    },
                    &named.local,
                ),
                ImportSpecifier::Default(default) => ("default".to_string(), &default.local),
                ImportSpecifier::Namespace(namespace) => ("*".to_string(), &namespace.local),
            };
            if let Some(binding) = bindings.get(&local.to_id()) {
                let next = sources.len() as u32 + 1;
                providers.insert(*binding, *sources.entry(specifier.clone()).or_insert(next));
                imported
                    .entry((specifier.clone(), export))
                    .or_default()
                    .push(*binding);
            }
        }
    }
    let mut resolved = ResolvedPremises {
        given: premises.callees.len(),
        providers,
        ..ResolvedPremises::default()
    };
    for premise in &premises.callees {
        let add = |entry: &mut (Vec<PremiseKind>, usize)| {
            entry.0.push(premise.kind);
            if premise.kind == PremiseKind::DecoratorHelper {
                entry.1 = premise.target_arg;
            }
        };
        let (targets, member): (Vec<Binding>, Option<&String>) = match &premise.callee {
            PremisedCallee::Import { specifier, export } => (
                imported
                    .get(&(specifier.clone(), export.clone()))
                    .cloned()
                    .unwrap_or_default(),
                None,
            ),
            PremisedCallee::ImportMember {
                specifier,
                export,
                member,
            } => (
                imported
                    .get(&(specifier.clone(), export.clone()))
                    .cloned()
                    .unwrap_or_default(),
                Some(member),
            ),
            PremisedCallee::Local { unit } => (
                declared_by_unit.get(*unit).cloned().unwrap_or_default(),
                None,
            ),
            PremisedCallee::LocalMember { unit, member } => (
                declared_by_unit.get(*unit).cloned().unwrap_or_default(),
                Some(member),
            ),
        };
        if !targets.is_empty() {
            resolved.matched += 1;
        }
        for binding in targets {
            match member {
                Some(member) => {
                    add(resolved
                        .members
                        .entry((binding, member.clone()))
                        .or_default());
                }
                None => {
                    add(resolved.callees.entry(binding).or_default());
                }
            }
        }
    }
    resolved
}

pub fn analyze(module: &Module, unresolved: SyntaxContext, options: &Options) -> Tier0 {
    let mut index =
        crate::derive::ProgramIndex::new(options.program.as_ref().filter(|_| options.derive));
    analyze_indexed(module, unresolved, options, &mut index)
}

/// `analyze` with a derivation index of `options.program` that outlives the
/// call, for analysing several chunks of one program.
pub fn analyze_indexed(
    module: &Module,
    unresolved: SyntaxContext,
    options: &Options,
    index: &mut crate::derive::ProgramIndex<'_>,
) -> Tier0 {
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
    // Bindings declared once and never assigned, by a class or by a plain
    // object literal.
    // Bindings declared once without a value and assigned only as the
    // argument of enum-shaped statements.
    let mut enum_statements: FxHashMap<Binding, usize> = FxHashMap::default();
    let mut valueless: FxHashSet<Binding> = FxHashSet::default();
    for (source, declared) in sources.iter().zip(&declared_by_unit) {
        match source {
            Source::Item(ModuleItem::Stmt(Stmt::Expr(statement))) => {
                if let Some(binding) = enum_definition(&statement.expr)
                    .and_then(|target| bindings.get(&target.to_id()))
                {
                    *enum_statements.entry(*binding).or_default() += 1;
                }
            }
            Source::Declarator(VarDeclarator { init: None, .. }) => {
                valueless.extend(declared.iter().copied());
            }
            Source::Item(
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
                | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                    decl: Decl::Var(var),
                    ..
                })),
            ) if var.decls.iter().all(|declarator| declarator.init.is_none()) => {
                valueless.extend(declared.iter().copied());
            }
            _ => {}
        }
    }
    let mut assignments = AssignmentCounts::default();
    module.visit_with(&mut assignments);
    let enums: FxHashSet<Binding> = if options.upgrades.contains(&Upgrade::EnumDefinitions) {
        bindings
            .iter()
            .filter(|(id, binding)| {
                valueless.contains(*binding)
                    && declarations[**binding as usize] == 1
                    && !assignments.other.contains(*id)
                    && enum_statements.get(*binding).copied().unwrap_or(0) > 0
                    && assignments.plain.get(*id) == enum_statements.get(*binding)
            })
            .map(|(_, binding)| *binding)
            .collect()
    } else {
        FxHashSet::default()
    };
    // `var E = (e => (…, e))(E || {})`: a `var` declared once and assigned
    // nowhere reads as `undefined` in its own initializer.
    let mut enum_declarations: FxHashSet<Binding> = FxHashSet::default();
    if options.upgrades.contains(&Upgrade::EnumDefinitions) {
        for item in &module.body {
            let (ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                decl: Decl::Var(var),
                ..
            }))) = item
            else {
                continue;
            };
            if var.kind != VarDeclKind::Var {
                continue;
            }
            for declarator in &var.decls {
                let Pat::Ident(name) = &declarator.name else {
                    continue;
                };
                let id = name.id.to_id();
                let Some(binding) = bindings.get(&id) else {
                    continue;
                };
                let own = declarator
                    .init
                    .as_deref()
                    .and_then(enum_call)
                    .is_some_and(|call| !call.assigns && call.returns && call.target.to_id() == id);
                if own
                    && declarations[*binding as usize] == 1
                    && !assignments.plain.contains_key(&id)
                    && !assignments.other.contains(&id)
                {
                    enum_declarations.insert(*binding);
                }
            }
        }
    }
    let mut classes: FxHashSet<Binding> = FxHashSet::default();
    let mut literals: FxHashMap<Binding, FxHashMap<String, bool>> = FxHashMap::default();
    let mut nested_literals: FxHashMap<(Binding, String), FxHashMap<String, bool>> =
        FxHashMap::default();
    let mut symbols: FxHashSet<Binding> = FxHashSet::default();
    let mut arrays: FxHashSet<Binding> = FxHashSet::default();
    for (source, declared) in sources.iter().zip(&declared_by_unit) {
        let [binding] = declared.as_slice() else {
            continue;
        };
        if declarations[*binding as usize] != 1 || assigned_anywhere.contains(binding) {
            continue;
        }
        let declarator = match source {
            Source::Declarator(declarator) => *declarator,
            Source::Item(
                ModuleItem::Stmt(Stmt::Decl(Decl::Class(_)))
                | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                    decl: Decl::Class(_),
                    ..
                })),
            ) => {
                classes.insert(*binding);
                continue;
            }
            Source::Item(
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
                | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                    decl: Decl::Var(var),
                    ..
                })),
            ) => match var.decls.as_slice() {
                [declarator] => declarator,
                _ => continue,
            },
            Source::Item(_) => continue,
        };
        if !matches!(declarator.name, Pat::Ident(_)) {
            continue;
        }
        let init = declarator.init.as_deref();
        match init {
            Some(Expr::Class(_)) => {
                classes.insert(*binding);
            }
            Some(Expr::Array(_)) => {
                arrays.insert(*binding);
            }
            // `Symbol('tag')`, `Symbol.for('tag')`.
            Some(Expr::Call(CallExpr {
                callee: Callee::Expr(callee),
                ..
            })) if options.upgrades.contains(&Upgrade::NativeAllocators) => {
                let symbol = |expr: &Expr| {
                    matches!(expr, Expr::Ident(ident)
                        if ident.ctxt == unresolved && &*ident.sym == "Symbol")
                };
                let makes_symbol = symbol(callee)
                    || matches!(&**callee, Expr::Member(MemberExpr {
                        obj,
                        prop: MemberProp::Ident(method),
                        ..
                    }) if &*method.sym == "for" && symbol(obj));
                if makes_symbol {
                    symbols.insert(*binding);
                }
            }
            Some(Expr::Object(object))
                if crate::derive::plain_literal(init.expect("an object")) =>
            {
                let keys = literal_keys(object);
                for (member, _, value) in &keys {
                    if let Some(value) = value
                        && let Expr::Object(inner) = unparen(value)
                        && crate::derive::plain_literal(unparen(value))
                    {
                        let inner_keys = literal_keys(inner)
                            .into_iter()
                            .map(|(key, primitive, _)| (key, primitive));
                        nested_literals.insert((*binding, member.clone()), inner_keys.collect());
                    }
                }
                let keys = keys.into_iter().map(|(key, primitive, _)| (key, primitive));
                literals.insert(*binding, keys.collect());
            }
            _ => {}
        }
    }
    let mut derived_primitive_imports = 0;
    let found = options
        .derive
        .then(|| crate::derive::derive(module, unresolved, options.program.as_ref(), index));
    // Off an import cycle the exporting chunk has run, and no code of the
    // program touches these objects except to read named members.
    let mut settled = Settled::default();
    let off_cycle = options
        .program
        .as_ref()
        .is_some_and(|program| !program.on_import_cycle());
    if let (Some(found), true) = (&found, off_cycle) {
        for (id, keys) in &found.settled_imports {
            if let Some(binding) = bindings.get(id) {
                settled.insert(*binding, keys.iter().cloned().collect());
            }
        }
    }
    let mut primitives: FxHashSet<Binding> = FxHashSet::default();
    let mut function_candidates: Vec<(Binding, Option<&Expr>)> = Vec::new();
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
        function_candidates = candidates.clone();
        // An import that only ever holds a primitive, by its exporting
        // chunk's declaration. Off an import cycle that chunk has run before
        // this one starts, so the binding already holds its one value.
        if let (Some(found), true) = (&found, off_cycle) {
            let imported = found.primitive_imports.iter();
            primitives.extend(imported.filter_map(|id| bindings.get(id).copied()));
            derived_primitive_imports = found.primitive_imports.len();
        }
        loop {
            let before = primitives.len();
            for (binding, init) in &candidates {
                let holds_primitive = init.is_none_or(|init| {
                    primitive(init, &bindings, &primitives, &settled, &mut Vec::new())
                });
                if holds_primitive {
                    primitives.insert(*binding);
                }
            }
            if primitives.len() == before {
                break;
            }
        }
    }
    let primitive_functions: FxHashMap<Binding, Vec<Binding>> =
        if options.upgrades.contains(&Upgrade::PrimitiveCalls) {
            function_candidates
                .iter()
                .filter_map(|(binding, init)| {
                    let reads = primitive_function(unparen((*init)?), &bindings, &primitives)?;
                    Some((*binding, reads))
                })
                .collect()
        } else {
            FxHashMap::default()
        };

    let mut refused = 0;
    let mut top_level_awaits = 0;
    let mut intrinsics_denied = 0;
    let mut intrinsic_refusals: std::collections::BTreeMap<
        (&'static str, crate::intrinsics::Refusal),
        usize,
    > = Default::default();
    let mut premise_uses: std::collections::BTreeMap<PremiseKind, usize> = Default::default();
    let mut premises = resolve_premises(module, &options.premises, &bindings, &declared_by_unit);
    let mut settled_refusals: Vec<SettledRefusalRow> = Vec::new();
    let mut derived = DerivedCounts {
        primitive_imports: derived_primitive_imports,
        settled_imports: settled.len(),
        ..DerivedCounts::default()
    };
    if let Some(found) = &found {
        // An author premise on the same callee wins.
        let binding = |id: &Id| bindings.get(id).copied();
        let mut callee = |id: &Id, kind: PremiseKind, count: &mut usize| {
            if let Some(binding) = binding(id) {
                *count += 1;
                premises.callees.entry(binding).or_insert((vec![kind], 0));
            }
        };
        for allocator in &found.allocators {
            // One that touches private state is a callback factory of its
            // provider.
            let (kind, count) = if allocator.private {
                (PremiseKind::CallbackFactory, &mut derived.contained)
            } else {
                (PremiseKind::Allocator, &mut derived.allocators)
            };
            callee(&allocator.binding, kind, count);
            if let Some(binding) = bindings.get(&allocator.binding) {
                let reads = allocator
                    .reads
                    .iter()
                    .filter_map(|id| bindings.get(id).copied());
                premises.allocator_reads.insert(*binding, reads.collect());
                premises
                    .literal_params
                    .insert(*binding, allocator.literal_params.clone());
                premises
                    .allocator_intrinsics
                    .insert(*binding, allocator.intrinsics.clone());
            }
        }
        for (id, reads, intrinsics) in &found.classes {
            callee(
                id,
                PremiseKind::AllocatorNew,
                &mut derived.allocator_classes,
            );
            if let Some(class) = bindings.get(id) {
                let reads = reads.iter().filter_map(|id| bindings.get(id).copied());
                premises.allocator_reads.insert(*class, reads.collect());
                premises
                    .allocator_intrinsics
                    .insert(*class, intrinsics.clone());
            }
        }
        for id in &found.exporters {
            if let Some(binding) = binding(id) {
                derived.export_helpers += 1;
                premises.exporters.insert(binding);
            }
        }
        for id in &found.exporter_allocators {
            if let Some(binding) = binding(id) {
                derived.export_helpers += 1;
                premises.exporter_allocators.insert(binding);
            }
        }
        if off_cycle {
            let stable = found.stable_imports.iter().filter_map(binding);
            premises.stable_imports.extend(stable);
            derived.stable_imports = premises.stable_imports.len();
            let classes = found.stable_class_imports.iter().filter_map(binding);
            premises.stable_class_imports.extend(classes);
        }
        let locals = found.settled_locals.iter().filter_map(binding);
        premises.settled_locals.extend(locals);
        derived.settled_locals = premises.settled_locals.len();
        let mut reads = NamedReads::default();
        module.visit_with(&mut reads);
        for (id, refusal) in &found.settled_refusals {
            let Some(binding) = binding(id) else {
                continue;
            };
            settled_refusals.push(SettledRefusalRow {
                unit: declared_by_unit
                    .iter()
                    .position(|declared| declared.contains(&binding)),
                reason: *refusal,
                named_reads: reads.0.get(id).copied().unwrap_or(0),
                exported: exported.contains(&binding),
            });
        }
        for id in &found.namers {
            callee(id, PremiseKind::FunctionNamer, &mut derived.function_namers);
        }
        for id in &found.wrappers {
            callee(id, PremiseKind::Allocator, &mut derived.lazy_wrappers);
        }
        for id in &found.interop {
            callee(
                id,
                PremiseKind::InteropNamespace,
                &mut derived.interop_helpers,
            );
        }
        for (id, _, local) in &found.lazy {
            callee(id, PremiseKind::LazyModule, &mut derived.lazy_modules);
            derived.local_lazy_modules += usize::from(*local);
        }
        for (id, path) in &found.members {
            if let Some(binding) = binding(id) {
                derived.namespace_members += 1;
                premises
                    .members
                    .entry((binding, path.clone()))
                    .or_insert((vec![PremiseKind::Allocator], 0));
            }
        }
    }
    let mut families_used: std::collections::BTreeMap<&'static str, usize> = Default::default();
    let object_aliases = crate::helpers::prepass(module, unresolved).object_aliases;
    let imported_strings = options
        .program
        .as_ref()
        .map(|program| program.imported_strings(&program.chunk(), module))
        .unwrap_or_default();
    let mut census = census(module, unresolved, &object_aliases, &imported_strings);
    if let Some(program) = &options.program {
        // A write by any chunk of the program counts.
        census.merge(program.census.clone());
    }
    // A literal that is not exported and that no statement of the chunk
    // mentions other than to read a named member is never held by anything
    // but its binding: no code can change it. Nor can the uses of the whole
    // binding that `derive::mentions` passes over, if `Object`'s statics are
    // the intrinsics. A premise says the same of a binding outright.
    let unexported_literal = |binding: &Binding| {
        literals
            .get(binding)
            .is_some_and(|keys| !keys.contains_key("__proto__"))
            && !exported.contains(binding)
    };
    let strict = crate::derive::mentions(module, None);
    let relaxed = crate::derive::mentions(module, Some(unresolved)).filter(|mentions| {
        !mentions.object_statics
            || census.untouched("Object", &["keys", "values", "entries"], false)
    });
    let unhandled = |mentions: &Option<crate::derive::Mentions>| -> FxHashSet<Binding> {
        let Some(mentions) = mentions else {
            return FxHashSet::default();
        };
        bindings
            .iter()
            .filter(|(id, binding)| unexported_literal(binding) && !mentions.handled.contains(*id))
            .map(|(_, binding)| *binding)
            .collect()
    };
    let premised: FxHashSet<Binding> = options
        .premises
        .immutable_bindings
        .iter()
        .filter_map(|unit| declared_by_unit.get(*unit))
        .flatten()
        .copied()
        .filter(|binding| literals.contains_key(binding))
        .collect();
    // The object a member of such a literal holds is as written while every
    // use of that member reads a named member of it.
    let mut sealed_members: FxHashSet<(Binding, String)> = FxHashSet::default();
    if let Some(mentions) = &strict {
        let whole = unhandled(&strict);
        for (binding, member) in nested_literals.keys() {
            let id = bindings
                .iter()
                .find(|(_, candidate)| *candidate == binding)
                .map(|(id, _)| id.clone());
            let untouched = id.is_some_and(|id| !mentions.inner.contains(&(id, member.clone())));
            if untouched && (whole.contains(binding) || premised.contains(binding)) {
                sealed_members.insert((*binding, member.clone()));
            }
        }
    }
    let mut sealed = unhandled(&strict);
    sealed.extend(unhandled(&relaxed));
    sealed.extend(&premised);
    // The whole program only reads named members of these, exported or not.
    let program_wide = premises.settled_locals.iter().copied();
    sealed.extend(program_wide.filter(|binding| {
        literals
            .get(binding)
            .is_some_and(|keys| !keys.contains_key("__proto__"))
    }));
    let mut units = Vec::with_capacity(sources.len());
    let never_written: FxHashSet<String> = options
        .premises
        .intrinsics_never_written
        .iter()
        .cloned()
        .collect();
    let sites: FxHashMap<usize, SiteKind> = options
        .premises
        .sites
        .iter()
        .map(|site| (site.unit, site.kind))
        .collect();
    let mut site_premise_uses = 0;
    for (ordinal, (source, declared)) in sources.iter().zip(declared_by_unit).enumerate() {
        let site = sites.get(&ordinal).copied();
        let mut scan = Scan {
            bindings: &bindings,
            unresolved,
            mentions: FxHashSet::default(),
            eager: FxHashSet::default(),
            refused: 0,
            top_level_awaits: 0,
            function_depth: 0,
        };
        let mut classifier = Classifier {
            bindings: &bindings,
            imports: &imports,
            primitives: &primitives,
            classes: &classes,
            literals: &literals,
            symbols: &symbols,
            arrays: &arrays,
            settled: &settled,
            sealed: &sealed,
            sealed_members: &sealed_members,
            nested_literals: &nested_literals,
            enums: &enums,
            enum_declarations: &enum_declarations,
            primitive_functions: &primitive_functions,
            pristine: Vec::new(),
            upgrades: &options.upgrades,
            census: &census,
            unresolved,
            eager: Vec::new(),
            intrinsics: Vec::new(),
            denied: Vec::new(),
            premises: &premises,
            deep: Vec::new(),
            targets: Vec::new(),
            replaces: Vec::new(),
            premise_uses: Vec::new(),
            callee_slots: FxHashSet::default(),
            premised_args: 0,
            providers: Vec::new(),
            lazy_uses: Vec::new(),
            native_site: site == Some(SiteKind::NativeOperation),
            never_written: &never_written,
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
        let kind = match kind {
            Kind::Impure(_) if site == Some(SiteKind::Pure) => {
                site_premise_uses += 1;
                classifier.eager = scan.eager.iter().copied().collect();
                classifier.deep = classifier.eager.clone();
                classifier.providers.clear();
                Kind::Pure
            }
            // The site premise says more than a contained callee's kind.
            Kind::Pure if site == Some(SiteKind::Pure) && !classifier.providers.is_empty() => {
                site_premise_uses += 1;
                classifier.providers.clear();
                classifier.lazy_uses.clear();
                Kind::Pure
            }
            Kind::Pure if !classifier.providers.is_empty() => Kind::Contained,
            kind => kind,
        };
        site_premise_uses +=
            usize::from(classifier.native_site && !classifier.intrinsics.is_empty());
        refused += scan.refused;
        top_level_awaits += scan.top_level_awaits;
        intrinsics_denied += classifier.denied.len();
        for refusal in classifier.denied {
            *intrinsic_refusals.entry(refusal).or_default() += 1;
        }
        if matches!(kind, Kind::Pure | Kind::Contained) {
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
        if matches!(kind, Kind::Pure | Kind::Contained | Kind::Confined) {
            for used in &classifier.premise_uses {
                *premise_uses.entry(*used).or_default() += 1;
            }
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
            deep: classifier.deep.into_iter().collect(),
            deep_stamp: NEVER,
            targets: classifier.targets,
            replaces: classifier.replaces,
            providers: classifier.providers,
            lazy_uses: classifier.lazy_uses,
            lazy_first: Vec::new(),
            pristine: classifier.pristine,
        });
    }
    // The first definition to call a lazy module's thunk may run the module
    // body. An imported module's body is another chunk's code; a local one
    // is this chunk's, and the statement stays impure.
    let mut initialized: FxHashSet<Binding> = FxHashSet::default();
    let (mut lazy_first_uses, mut lazy_later_uses) = (0, 0);
    for unit in &mut units {
        if !matches!(unit.kind, Kind::Pure | Kind::Contained) {
            unit.lazy_uses.clear();
            continue;
        }
        let firsts: Vec<Binding> = unit
            .lazy_uses
            .iter()
            .copied()
            .filter(|thunk| initialized.insert(*thunk))
            .collect();
        if firsts.is_empty() {
            lazy_later_uses += usize::from(!unit.lazy_uses.is_empty());
            continue;
        }
        lazy_first_uses += 1;
        if firsts.iter().all(|thunk| imports.contains(thunk)) {
            unit.kind = Kind::Contained;
            unit.providers.push(ANY_PROVIDER);
        } else {
            unit.kind = Kind::Impure(ImpureCause::CallLocal);
        }
        unit.lazy_first = firsts;
    }
    if refused > 0 {
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
        // A decorator helper may store what it was given in its target.
        for binding in &unit.targets {
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

    let exports_reachable = options.exports_reachable
        || top_level_awaits > 0
        || options
            .program
            .as_ref()
            .is_some_and(|program| program.on_import_cycle());
    // Per binding: the first impure ordinal whose reach holds it.
    let reach = |units: &[Unit]| {
        let mut reach_stamp = vec![NEVER; binding_count];
        if exports_reachable {
            let seeds: Vec<Binding> = exported.iter().copied().collect();
            closure(&seeds, &mut |binding| {
                let fresh = reach_stamp[binding as usize] == NEVER;
                reach_stamp[binding as usize] = 0;
                fresh
            });
        }
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
        }
        reach_stamp
    };
    // A property read of a literal runs no code only while the literal is as
    // written. Assume every such read is a pure definition, then make impure
    // each one an earlier impure statement can reach, until none is left:
    // making one impure only widens what is reached.
    let reach_stamp = loop {
        let reach_stamp = reach(&units);
        let mut changed = false;
        for (ordinal, unit) in units.iter_mut().enumerate() {
            let reached = unit
                .pristine
                .iter()
                .any(|table| reach_stamp[*table as usize] <= ordinal as u32);
            if reached && !matches!(unit.kind, Kind::Impure(_)) {
                unit.kind = Kind::Impure(ImpureCause::MemberRead);
                unit.providers.clear();
                changed = true;
            }
        }
        if !changed {
            break reach_stamp;
        }
    };
    let mut own_reach = FxHashMap::default();
    for (ordinal, unit) in units.iter().enumerate() {
        if !matches!(unit.kind, Kind::Impure(_)) {
            continue;
        }
        let mut own = FxHashSet::default();
        closure(&unit.mentions, &mut |binding| own.insert(binding));
        own_reach.insert(ordinal, own);
    }
    // An assignment can run once its unit runs, if the unit is impure, or
    // once a binding the unit declares is reachable.
    let mut write_stamp = vec![NEVER; binding_count];
    let mut declarer = vec![NEVER; binding_count];
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
        for binding in &unit.assigns {
            let stamp = &mut write_stamp[*binding as usize];
            *stamp = (*stamp).min(enabled);
        }
        // An impure declaration writes its bindings when it runs, and only
        // then. A binding with two of them is treated as assigned.
        if matches!(unit.kind, Kind::Impure(_)) {
            for binding in &unit.declared {
                let declarer = &mut declarer[*binding as usize];
                if *declarer == NEVER {
                    *declarer = ordinal as u32;
                } else {
                    let stamp = &mut write_stamp[*binding as usize];
                    *stamp = (*stamp).min(*declarer);
                }
            }
        }
    }
    // A premised call may read, and through a function it is given run,
    // everything its arguments can reference. What an import references is
    // another chunk's, which any impure statement may change.
    let mut premised_reading_imports = 0;
    for unit in &mut units {
        if !matches!(unit.kind, Kind::Pure | Kind::Contained | Kind::Confined) {
            unit.providers.clear();
            unit.deep.clear();
        }
        let seeds: Vec<Binding> = unit.deep.drain().collect();
        closure(&seeds, &mut |binding| unit.deep.insert(binding));
        unit.deep_stamp = unit
            .deep
            .iter()
            .map(|binding| {
                if imports.contains(binding) {
                    0
                } else {
                    reach_stamp[*binding as usize]
                }
            })
            .min()
            .unwrap_or(NEVER);
        premised_reading_imports +=
            usize::from(unit.deep.iter().any(|binding| imports.contains(binding)));
    }
    let declared_names = {
        let mut names = vec![String::new(); binding_count];
        for (id, binding) in &bindings {
            names[*binding as usize] = id.0.to_string();
        }
        units
            .iter()
            .map(|unit| {
                unit.declared
                    .iter()
                    .map(|binding| names[*binding as usize].clone())
                    .collect()
            })
            .collect()
    };
    Tier0 {
        premised_reading_imports,
        site_premise_uses,
        derived,
        lazy_first_uses,
        lazy_later_uses,
        commuting: options
            .premises
            .commuting
            .iter()
            .map(|(a, b)| (*a.min(b), *a.max(b)))
            .collect(),
        premises_given: premises.given,
        premises_matched: premises.matched,
        premise_uses,
        units,
        reach_stamp,
        write_stamp,
        declarer,
        own_reach,
        bindings: binding_count,
        declared_names,
        refused,
        top_level_awaits,
        imported_strings: imported_strings.len(),
        exports_reachable,
        intrinsics_denied,
        intrinsic_refusals,
        settled_refusals,
        families_used,
        census,
    }
}

impl Tier0 {
    fn impure_with_definition(&self, impure: usize, definition: usize) -> Option<Conflict> {
        let at = impure as u32;
        let unit = &self.units[definition];
        let initializes = matches!(unit.kind, Kind::Pure | Kind::Contained);
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
        let written = |binding: &Binding| {
            self.write_stamp[*binding as usize] <= at || self.declarer[*binding as usize] == at
        };
        if unit
            .eager
            .iter()
            .chain(&unit.declared)
            .chain(&unit.deep)
            .any(written)
        {
            return Some(Conflict::Write);
        }
        // The impure statement may change an object a premised call reads.
        if unit.deep_stamp <= at {
            return Some(Conflict::ObjectState);
        }
        None
    }

    fn impure_with_confined(&self, impure: usize, confined: usize) -> Option<Conflict> {
        let at = impure as u32;
        let unit = &self.units[confined];
        let reached = |binding: &Binding| self.reach_stamp[*binding as usize] <= at;
        if unit.targets.iter().chain(&unit.replaces).any(reached) || unit.deep_stamp <= at {
            return Some(Conflict::ObjectState);
        }
        let written = |binding: &Binding| {
            self.write_stamp[*binding as usize] <= at || self.declarer[*binding as usize] == at
        };
        (unit
            .eager
            .iter()
            .chain(&unit.targets)
            .chain(&unit.deep)
            .any(written))
        .then_some(Conflict::Write)
    }

    fn confined_with_definition(&self, confined: usize, definition: usize) -> Option<Conflict> {
        let (statement, unit) = (&self.units[confined], &self.units[definition]);
        // A lazy module's body may call what earlier impure statements
        // leaked, the target among it.
        if !unit.lazy_first.is_empty()
            && statement
                .targets
                .iter()
                .chain(&statement.replaces)
                .any(|binding| self.reach_stamp[*binding as usize] <= definition as u32)
        {
            return Some(Conflict::ObjectState);
        }
        let involved = |binding: &Binding| {
            statement.eager.contains(binding)
                || statement.deep.contains(binding)
                || statement.targets.contains(binding)
                || statement.replaces.contains(binding)
        };
        // A hoisted function is readable throughout; only replacing it matters.
        let reads_definition = match unit.kind {
            Kind::Hoisted => unit
                .declared
                .iter()
                .any(|binding| statement.replaces.contains(binding)),
            _ => unit.declared.iter().any(involved),
        };
        if reads_definition
            || unit
                .eager
                .iter()
                .any(|binding| statement.replaces.contains(binding))
        {
            return Some(Conflict::EagerRead);
        }
        // A premised call in the definition reads the target's state.
        statement
            .targets
            .iter()
            .chain(&statement.replaces)
            .any(|binding| unit.deep.contains(binding))
            .then_some(Conflict::ObjectState)
    }

    fn confined_pair(&self, a: usize, b: usize) -> Option<Conflict> {
        let (first, second) = (&self.units[a], &self.units[b]);
        let touches = |unit: &Unit, binding: &Binding| {
            unit.targets.contains(binding)
                || unit.replaces.contains(binding)
                || unit.deep.contains(binding)
        };
        let shared = first
            .targets
            .iter()
            .chain(&first.replaces)
            .any(|binding| touches(second, binding))
            || second
                .targets
                .iter()
                .chain(&second.replaces)
                .any(|binding| touches(first, binding))
            || first
                .replaces
                .iter()
                .any(|binding| second.eager.contains(binding))
            || second
                .replaces
                .iter()
                .any(|binding| first.eager.contains(binding));
        shared.then_some(Conflict::SharedTarget)
    }

    /// Why the two units are not proven to commute, if they are not.
    pub fn conflict(&self, a: usize, b: usize) -> Option<Conflict> {
        let (first, second) = (&self.units[a], &self.units[b]);
        if self.commuting.contains(&(a.min(b), a.max(b))) {
            return None;
        }
        // A contained statement is a definition that also runs a provider.
        let impure = |unit: &Unit| matches!(unit.kind, Kind::Impure(_));
        let any = |unit: &Unit| unit.providers.contains(&ANY_PROVIDER);
        let shares_provider = first
            .providers
            .iter()
            .any(|provider| second.providers.contains(provider))
            || (any(first) && !second.providers.is_empty())
            || (any(second) && !first.providers.is_empty())
            // A later use relies on the module having run.
            || first.lazy_first.iter().any(|thunk| second.lazy_uses.contains(thunk))
            || second.lazy_first.iter().any(|thunk| first.lazy_uses.contains(thunk));
        if (impure(first) && !second.providers.is_empty())
            || (impure(second) && !first.providers.is_empty())
            || shares_provider
        {
            return Some(Conflict::ProviderState);
        }
        let definition = |kind: Kind| match kind {
            Kind::Contained => Kind::Pure,
            kind => kind,
        };
        match (definition(first.kind), definition(second.kind)) {
            (Kind::Inert, _) | (_, Kind::Inert) => None,
            (Kind::Impure(_), Kind::Impure(_)) => Some(Conflict::BothImpure),
            (Kind::Impure(_), Kind::Confined) => self.impure_with_confined(a, b),
            (Kind::Confined, Kind::Impure(_)) => self.impure_with_confined(b, a),
            (Kind::Impure(_), _) => self.impure_with_definition(a, b),
            (_, Kind::Impure(_)) => self.impure_with_definition(b, a),
            (Kind::Confined, Kind::Confined) => self.confined_pair(a, b),
            (Kind::Confined, _) => self.confined_with_definition(a, b),
            (_, Kind::Confined) => self.confined_with_definition(b, a),
            _ => {
                let reads = |reader: &Unit, definition: &Unit| {
                    matches!(definition.kind, Kind::Pure | Kind::Contained)
                        && reader
                            .eager
                            .iter()
                            .chain(&reader.deep)
                            .any(|binding| definition.declared.contains(binding))
                };
                let redeclares = first
                    .declared
                    .iter()
                    .any(|binding| second.declared.contains(binding));
                if reads(first, second) || reads(second, first) || redeclares {
                    return Some(Conflict::EagerRead);
                }
                // A lazy module's body may call what earlier impure
                // statements leaked.
                let leaked = |runner: usize, definition: &Unit| {
                    !self.units[runner].lazy_first.is_empty()
                        && matches!(definition.kind, Kind::Pure | Kind::Contained)
                        && definition
                            .declared
                            .iter()
                            .any(|binding| self.reach_stamp[*binding as usize] <= runner as u32)
                };
                (leaked(a, second) || leaked(b, first)).then_some(Conflict::ReachEscaped)
            }
        }
    }
}
