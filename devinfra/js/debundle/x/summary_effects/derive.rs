//! Callee contracts derived from bodies instead of asserted.
//!
//! Three derivations, each syntactic and deliberately small:
//!
//! - **Bundler helpers by shape.** The lazy CommonJS wrapper and the interop
//!   namespace helper are recognized by comparing their bodies, up to
//!   renaming, with the canonical sources below.
//! - **Allocators.** A function whose body only builds a fresh value from its
//!   parameters: literals, closures, parameters and constants as values,
//!   stores into objects it allocated itself, calls of other allocators. It
//!   reads no property of a parameter, calls no parameter, and cannot throw.
//! - **Members of a lazy module.** `ns.member` where `ns` is a lazy module's
//!   exports, directly or through the interop helper, resolved to the single
//!   `exports.member = …` of the module body.
//!
//! Imports are followed into the providing chunk when the program was read.

use rustc_hash::{FxHashMap, FxHashSet};
use swc_common::SyntaxContext;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::helpers::assigned_ids;
use crate::program::ProgramFacts;

/// `(cb, mod) => () => …`: calling the result runs `cb` at most once.
const COMMON_JS: &str =
    "(cb, mod) => () => (mod || cb((mod = { exports: {} }).exports, mod), mod.exports)";
const COPY_PROPS: &str = "(to, from, except, desc) => {
  if (from && typeof from == 'object' || typeof from == 'function')
    for (let key of Object.getOwnPropertyNames(from))
      !Object.prototype.hasOwnProperty.call(to, key) && key !== except &&
        Object.defineProperty(to, key, {
          get: () => from[key],
          enumerable: !(desc = Object.getOwnPropertyDescriptor(from, key)) || desc.enumerable
        });
  return to;
}";
const TO_ESM: &str = "(mod, isNodeMode, target) => (
  target = mod != null ? Object.create(Object.getPrototypeOf(mod)) : {},
  HELPER_copy_props(
    isNodeMode || !mod || !mod.__esModule
      ? Object.defineProperty(target, 'default', { value: mod, enumerable: true })
      : target,
    mod))";

/// `name(fn, "f")` gives a function its name and returns it.
const NAMER: &str = "(target, value) => Object.defineProperty(target, 'name', { value: value, configurable: true })";

/// A lazy module: the chunk and binding holding its thunk.
pub type LazyKey = (usize, Id);

#[derive(Clone, PartialEq, Eq)]
enum Role {
    Unknown,
    CommonJs,
    CopyProps,
    ToEsm,
    Namer,
    /// A function; `facts` says whether it also touches private state.
    Allocator,
    /// A class whose construction only stores into the new object.
    AllocatorClass,
    Lazy(LazyKey),
    Namespace {
        lazy: LazyKey,
        interop: bool,
    },
}

#[derive(Clone, Copy)]
enum Def<'a> {
    Function(&'a Function),
    Class(&'a Class),
    Value(&'a Expr),
}

/// What classifying an allocator body found.
#[derive(Default, Clone)]
struct Facts {
    /// Top-level bindings of its chunk a call reads, its callees included.
    reads: Vec<Id>,
    /// It reads or writes state private to its chunk: a counter, a list,
    /// a collection.
    private: bool,
    /// Parameter positions whose properties it reads: a call must pass a
    /// fresh object literal there.
    literal_params: Vec<usize>,
    /// Intrinsic collections it constructs empty.
    intrinsics: Vec<&'static str>,
    /// For a class and its bases: the names with an accessor, and the names
    /// a constructor stores under. A store under a name with a setter would
    /// run the setter.
    accessors: Vec<String>,
    stored: Vec<String>,
}

/// Collections an allocator may construct empty.
const COLLECTIONS: [&str; 4] = ["Map", "Set", "WeakMap", "WeakSet"];

/// State a chunk keeps for its own functions, by how it was declared.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Private {
    Counter,
    Array,
    Collection,
}

/// How the chunk assigns its top-level bindings.
#[derive(Default)]
struct Stores {
    /// Assigned with `=` at all.
    plain: FxHashSet<Id>,
    /// Assigned something other than an array literal or `x.concat(…)` of
    /// itself, or by a compound operator, a pattern or a loop head.
    other: FxHashSet<Id>,
}

impl Visit for Stores {
    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        match &assign.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(target)) => {
                let id = target.id.to_id();
                let own_concat = matches!(&*assign.right, Expr::Call(CallExpr {
                    callee: Callee::Expr(callee),
                    ..
                }) if matches!(&**callee, Expr::Member(MemberExpr {
                    obj,
                    prop: MemberProp::Ident(method),
                    ..
                }) if &*method.sym == "concat"
                    && matches!(&**obj, Expr::Ident(base) if base.to_id() == id)));
                let benign = assign.op == AssignOp::Assign
                    && (own_concat || matches!(&*assign.right, Expr::Array(_)));
                self.plain.insert(id.clone());
                if !benign {
                    self.other.insert(id);
                }
            }
            AssignTarget::Pat(pattern) => self.other.extend(assigned_ids(pattern)),
            _ => {}
        }
        assign.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, looped: &ForInStmt) {
        self.other.extend(assigned_ids(&looped.left));
        looped.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, looped: &ForOfStmt) {
        self.other.extend(assigned_ids(&looped.left));
        looped.visit_children_with(self);
    }
}

enum Export {
    Local(Id),
    From(String, String),
}

#[derive(Clone, Copy)]
enum Callable<'a> {
    Function(&'a Function),
    Arrow(&'a ArrowExpr),
}

fn callable(expr: &Expr) -> Option<Callable<'_>> {
    match expr {
        Expr::Paren(paren) => callable(&paren.expr),
        Expr::Fn(function) => Some(Callable::Function(&function.function)),
        Expr::Arrow(arrow) => Some(Callable::Arrow(arrow)),
        _ => None,
    }
}

struct Chunk<'a> {
    module: &'a Module,
    unresolved: SyntaxContext,
    /// Top-level bindings declared once and never assigned.
    defs: FxHashMap<Id, Def<'a>>,
    imports: FxHashMap<Id, (String, String)>,
    exports: FxHashMap<String, Export>,
    assigned: FxHashSet<Id>,
    /// Top-level variables that can hold private state.
    private: FxHashMap<Id, Private>,
    stores: Stores,
}

fn export_name(name: &ModuleExportName) -> String {
    match name {
        ModuleExportName::Ident(ident) => ident.sym.to_string(),
        ModuleExportName::Str(value) => js_ast::str_value(value),
    }
}

impl<'a> Chunk<'a> {
    fn new(module: &'a Module, unresolved: SyntaxContext) -> Self {
        let assigned = assigned_ids(module);
        let mut declared: FxHashMap<Id, usize> = FxHashMap::default();
        let mut defs: FxHashMap<Id, Def<'a>> = FxHashMap::default();
        let mut imports = FxHashMap::default();
        let mut exports = FxHashMap::default();
        let mut private = FxHashMap::default();
        let unresolved_global = |expr: &Expr, names: &[&str]| {
            matches!(expr, Expr::Ident(ident)
                if ident.ctxt == unresolved && names.contains(&&*ident.sym))
        };
        let mut declare = |decl: &'a Decl, exported: bool, exports: &mut FxHashMap<_, _>| {
            let mut bind = |ident: &Ident, def: Option<Def<'a>>| {
                *declared.entry(ident.to_id()).or_default() += 1;
                if let Some(def) = def {
                    defs.insert(ident.to_id(), def);
                }
                if exported {
                    exports.insert(ident.sym.to_string(), Export::Local(ident.to_id()));
                }
            };
            match decl {
                Decl::Fn(function) => {
                    bind(&function.ident, Some(Def::Function(&function.function)))
                }
                Decl::Class(class) => bind(&class.ident, Some(Def::Class(&class.class))),
                Decl::Var(var) => {
                    for declarator in &var.decls {
                        if let Pat::Ident(name) = &declarator.name {
                            let kind = match declarator.init.as_deref() {
                                None | Some(Expr::Lit(Lit::Num(_))) => Some(Private::Counter),
                                Some(Expr::Array(_)) => Some(Private::Array),
                                Some(Expr::New(new))
                                    if unresolved_global(
                                        &new.callee,
                                        &["Map", "Set", "WeakMap", "WeakSet"],
                                    ) =>
                                {
                                    Some(Private::Collection)
                                }
                                _ => None,
                            };
                            if let (Some(kind), false) = (kind, exported) {
                                private.insert(name.id.to_id(), kind);
                            }
                            bind(&name.id, declarator.init.as_deref().map(Def::Value));
                        }
                    }
                }
                _ => {}
            }
        };
        for item in &module.body {
            match item {
                ModuleItem::Stmt(Stmt::Decl(decl)) => declare(decl, false, &mut exports),
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    declare(&export.decl, true, &mut exports);
                }
                ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
                    let source = js_ast::str_value(&import.src);
                    for specifier in &import.specifiers {
                        let (local, export) = match specifier {
                            ImportSpecifier::Named(named) => (
                                &named.local,
                                named
                                    .imported
                                    .as_ref()
                                    .map_or_else(|| named.local.sym.to_string(), export_name),
                            ),
                            ImportSpecifier::Default(default) => {
                                (&default.local, "default".to_string())
                            }
                            ImportSpecifier::Namespace(namespace) => {
                                (&namespace.local, "*".to_string())
                            }
                        };
                        imports.insert(local.to_id(), (source.clone(), export));
                    }
                }
                ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
                    for specifier in &named.specifiers {
                        let ExportSpecifier::Named(specifier) = specifier else {
                            continue;
                        };
                        let exported = specifier.exported.as_ref().unwrap_or(&specifier.orig);
                        let export = match (&named.src, &specifier.orig) {
                            (Some(source), orig) => {
                                Export::From(js_ast::str_value(source), export_name(orig))
                            }
                            (None, ModuleExportName::Ident(orig)) => Export::Local(orig.to_id()),
                            (None, ModuleExportName::Str(_)) => continue,
                        };
                        exports.insert(export_name(exported), export);
                    }
                }
                _ => {}
            }
        }
        defs.retain(|id, _| declared[id] == 1 && !assigned.contains(id));
        private.retain(|id, _| declared[id] == 1);
        let mut stores = Stores::default();
        module.visit_with(&mut stores);
        Self {
            module,
            unresolved,
            defs,
            imports,
            exports,
            assigned,
            private,
            stores,
        }
    }
}

/// `f`, `(f)` or `(0, f)`.
fn callee_ident(expr: &Expr) -> Option<&Ident> {
    match expr {
        Expr::Paren(paren) => callee_ident(&paren.expr),
        Expr::Seq(seq) => match seq.exprs.as_slice() {
            [first, last] if matches!(&**first, Expr::Lit(_)) => callee_ident(last),
            _ => None,
        },
        Expr::Ident(ident) => Some(ident),
        _ => None,
    }
}

/// `expr` up to renaming of the bindings it declares, or `None` for syntax
/// the helper shapes do not use. `outer` names an identifier bound outside.
fn shape(
    expr: &Expr,
    names: &mut Vec<Id>,
    outer: &mut dyn FnMut(&Ident) -> Option<String>,
) -> Option<String> {
    Some(match expr {
        Expr::Paren(paren) => return shape(&paren.expr, names, outer),
        Expr::Arrow(arrow) => {
            let mut out = String::from("fn(");
            for param in &arrow.params {
                let Pat::Ident(name) = param else {
                    return None;
                };
                names.push(name.id.to_id());
                out += &format!("${},", names.len() - 1);
            }
            out += ")";
            out + &match &*arrow.body {
                ArrowFunctionBody::Expr(body) => shape(body, names, outer)?,
                ArrowFunctionBody::FunctionBody(block) => {
                    shape_statements(&block.stmts, names, outer)?
                }
            }
        }
        Expr::Ident(ident) => match names.iter().rposition(|name| *name == ident.to_id()) {
            Some(index) => format!("${index}"),
            None => outer(ident)?,
        },
        Expr::Lit(Lit::Str(value)) => format!("{:?}", js_ast::str_value(value)),
        Expr::Lit(Lit::Num(number)) => number.value.to_string(),
        Expr::Lit(Lit::Bool(value)) => value.value.to_string(),
        Expr::Lit(Lit::Null(_)) => "null".to_string(),
        Expr::Unary(unary) => match (unary.op, &*unary.arg) {
            (UnaryOp::Bang, Expr::Lit(Lit::Num(number))) => (number.value == 0.0).to_string(),
            (UnaryOp::Void, _) => "undefined".to_string(),
            (op, arg) => format!("{op:?}({})", shape(arg, names, outer)?),
        },
        Expr::Bin(binary) => format!(
            "({} {:?} {})",
            shape(&binary.left, names, outer)?,
            binary.op,
            shape(&binary.right, names, outer)?
        ),
        Expr::Cond(cond) => format!(
            "({}?{}:{})",
            shape(&cond.test, names, outer)?,
            shape(&cond.cons, names, outer)?,
            shape(&cond.alt, names, outer)?
        ),
        Expr::Seq(seq) => {
            let parts: Option<Vec<String>> = seq
                .exprs
                .iter()
                .map(|part| shape(part, names, outer))
                .collect();
            format!("[{}]", parts?.join(";"))
        }
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            let target = match &assign.left {
                AssignTarget::Simple(SimpleAssignTarget::Ident(target)) => {
                    shape(&Expr::Ident(target.id.clone()), names, outer)?
                }
                AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                    shape_member(member, names, outer)?
                }
                _ => return None,
            };
            format!("({target}={})", shape(&assign.right, names, outer)?)
        }
        Expr::Member(member) => shape_member(member, names, outer)?,
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            let mut out = format!("call({};", shape(callee, names, outer)?);
            for arg in &call.args {
                if arg.spread.is_some() {
                    return None;
                }
                out += &shape(&arg.expr, names, outer)?;
                out += ",";
            }
            out + ")"
        }
        Expr::Object(object) => {
            let mut out = String::from("{");
            for prop in &object.props {
                let PropOrSpread::Prop(prop) = prop else {
                    return None;
                };
                let (key, value) = match &**prop {
                    Prop::Shorthand(ident) => (
                        ident.sym.to_string(),
                        shape(&Expr::Ident(ident.clone()), names, outer)?,
                    ),
                    Prop::KeyValue(pair) => (
                        match &pair.key {
                            PropName::Ident(name) => name.sym.to_string(),
                            PropName::Str(value) => js_ast::str_value(value),
                            _ => return None,
                        },
                        shape(&pair.value, names, outer)?,
                    ),
                    _ => return None,
                };
                out += &format!("{key}:{value},");
            }
            out + "}"
        }
        _ => return None,
    })
}

fn shape_member(
    member: &MemberExpr,
    names: &mut Vec<Id>,
    outer: &mut dyn FnMut(&Ident) -> Option<String>,
) -> Option<String> {
    let object = shape(&member.obj, names, outer)?;
    Some(match &member.prop {
        MemberProp::Ident(name) => format!("{object}.{}", name.sym),
        MemberProp::Computed(computed) => {
            format!("{object}[{}]", shape(&computed.expr, names, outer)?)
        }
        MemberProp::PrivateName(_) => return None,
    })
}

fn shape_statements(
    statements: &[Stmt],
    names: &mut Vec<Id>,
    outer: &mut dyn FnMut(&Ident) -> Option<String>,
) -> Option<String> {
    let mut out = String::from("{");
    for statement in statements {
        out += &shape_statement(statement, names, outer)?;
        out += ";";
    }
    Some(out + "}")
}

fn shape_statement(
    statement: &Stmt,
    names: &mut Vec<Id>,
    outer: &mut dyn FnMut(&Ident) -> Option<String>,
) -> Option<String> {
    Some(match statement {
        Stmt::Block(block) => match block.stmts.as_slice() {
            [only] => return shape_statement(only, names, outer),
            statements => shape_statements(statements, names, outer)?,
        },
        Stmt::Expr(statement) => shape(&statement.expr, names, outer)?,
        Stmt::Return(returned) => {
            format!("return {}", shape(returned.arg.as_deref()?, names, outer)?)
        }
        Stmt::If(branch) if branch.alt.is_none() => format!(
            "if({}){}",
            shape(&branch.test, names, outer)?,
            shape_statement(&branch.cons, names, outer)?
        ),
        Stmt::ForOf(looped) if !looped.is_await => {
            let ForHead::VarDecl(head) = &looped.left else {
                return None;
            };
            let [
                VarDeclarator {
                    name: Pat::Ident(name),
                    init: None,
                    ..
                },
            ] = head.decls.as_slice()
            else {
                return None;
            };
            let iterated = shape(&looped.right, names, outer)?;
            names.push(name.id.to_id());
            format!(
                "for(${} of {iterated}){}",
                names.len() - 1,
                shape_statement(&looped.body, names, outer)?
            )
        }
        _ => return None,
    })
}

/// The shape of a canonical helper source.
fn canonical(source: &str) -> String {
    let parsed = js_ast::parse_js_module("helper", &format!("({source});"))
        .expect("the canonical helper parses");
    let [ModuleItem::Stmt(Stmt::Expr(statement))] = parsed.module.body.as_slice() else {
        panic!("the canonical helper is one expression");
    };
    shape(&statement.expr, &mut Vec::new(), &mut |ident| {
        Some(match ident.sym.strip_prefix("HELPER_") {
            Some(helper) => format!("#{helper}"),
            None => ident.sym.to_string(),
        })
    })
    .expect("the canonical helper is in the shape grammar")
}

/// Whether anything in the node names `__esModule`.
struct MentionsEsModule(bool);

impl Visit for MentionsEsModule {
    fn visit_ident_name(&mut self, name: &IdentName) {
        self.0 |= &*name.sym == "__esModule";
    }

    fn visit_str(&mut self, value: &Str) {
        self.0 |= js_ast::str_value(value) == "__esModule";
    }
}

struct Resolver<'a> {
    /// The analysed chunk, then the program's chunks in order.
    chunks: Vec<Chunk<'a>>,
    program: Option<&'a ProgramFacts>,
    roles: FxHashMap<(usize, Id), Role>,
    /// Per allocator binding or class.
    facts: FxHashMap<(usize, Id), Facts>,
    /// Those of the body being classified.
    current: Facts,
    namer: String,
    common_js: String,
    copy_props: String,
    to_esm: String,
}

/// Bindings an allocator body may use as values.
struct Scope<'s> {
    chunk: usize,
    params: FxHashSet<Id>,
    /// The position of each parameter.
    positions: FxHashMap<Id, usize>,
    /// Parameters the body assigns.
    reassigned: FxHashSet<Id>,
    /// Parameters and locals holding an object the body allocated.
    fresh: FxHashSet<Id>,
    /// Bindings of an enclosing lazy module body, declared once.
    enclosing: &'s FxHashSet<Id>,
}

impl<'s> Scope<'s> {
    fn new(chunk: usize, params: Vec<Id>, enclosing: &'s FxHashSet<Id>) -> Self {
        Self {
            chunk,
            positions: params
                .iter()
                .enumerate()
                .map(|(position, id)| (id.clone(), position))
                .collect(),
            params: params.into_iter().collect(),
            reassigned: FxHashSet::default(),
            fresh: FxHashSet::default(),
            enclosing,
        }
    }
}

/// `{ a: 1, b }`: a literal whose properties can be read without running
/// code.
pub fn plain_literal(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => plain_literal(&paren.expr),
        Expr::Object(object) => object.props.iter().all(|prop| match prop {
            PropOrSpread::Prop(prop) => match &**prop {
                Prop::Shorthand(_) => true,
                Prop::KeyValue(pair) => !matches!(pair.key, PropName::Computed(_)),
                _ => false,
            },
            PropOrSpread::Spread(_) => false,
        }),
        _ => false,
    }
}

impl<'a> Resolver<'a> {
    fn provider(&self, chunk: usize, specifier: &str) -> Option<usize> {
        let program = self.program?;
        let from = match chunk {
            0 => &program.chunk,
            other => &program.modules[other - 1].0,
        };
        Some(program.resolve(from, specifier)? + 1)
    }

    fn exported(&mut self, chunk: usize, specifier: &str, export: &str, depth: usize) -> Role {
        let Some(provider) = self.provider(chunk, specifier) else {
            return Role::Unknown;
        };
        match self.chunks[provider].exports.get(export) {
            Some(Export::Local(id)) => {
                let id = id.clone();
                self.role(provider, &id)
            }
            Some(Export::From(specifier, export)) if depth < 8 => {
                let (specifier, export) = (specifier.clone(), export.clone());
                self.exported(provider, &specifier, &export, depth + 1)
            }
            _ => Role::Unknown,
        }
    }

    fn role(&mut self, chunk: usize, id: &Id) -> Role {
        let key = (chunk, id.clone());
        if let Some(role) = self.roles.get(&key) {
            return role.clone();
        }
        // A binding defined through itself has no role.
        self.roles.insert(key.clone(), Role::Unknown);
        let role = self.compute(chunk, id);
        self.roles.insert(key, role.clone());
        role
    }

    fn compute(&mut self, chunk: usize, id: &Id) -> Role {
        if let Some((specifier, export)) = self.chunks[chunk].imports.get(id).cloned() {
            return self.exported(chunk, &specifier, &export, 0);
        }
        let Some(def) = self.chunks[chunk].defs.get(id).copied() else {
            return Role::Unknown;
        };
        let function = match def {
            Def::Function(function) => Some(Callable::Function(function)),
            Def::Value(value) => callable(value),
            Def::Class(_) => None,
        };
        if let Some(function) = function {
            if let (Def::Value(value), Some(Callable::Arrow(_))) = (def, callable_arrow(def)) {
                let found = shape(value, &mut Vec::new(), &mut |ident| {
                    self.outer(chunk, ident)
                });
                if let Some(found) = found {
                    if found == self.common_js {
                        return Role::CommonJs;
                    }
                    if found == self.copy_props {
                        return Role::CopyProps;
                    }
                    if found == self.to_esm {
                        return Role::ToEsm;
                    }
                    if found == self.namer {
                        return Role::Namer;
                    }
                }
            }
            let none = FxHashSet::default();
            let outer = std::mem::take(&mut self.current);
            let allocates = self.allocator(chunk, function, &none);
            let facts = std::mem::replace(&mut self.current, outer);
            return if allocates {
                self.facts.insert((chunk, id.clone()), facts);
                Role::Allocator
            } else {
                Role::Unknown
            };
        }
        let class = match def {
            Def::Class(class) => Some(class),
            Def::Value(Expr::Class(class)) => Some(&*class.class),
            _ => None,
        };
        if let Some(class) = class {
            let outer = std::mem::take(&mut self.current);
            let allocates = self.allocator_class(chunk, class);
            let facts = std::mem::replace(&mut self.current, outer);
            return if allocates && !facts.private && facts.literal_params.is_empty() {
                self.facts.insert((chunk, id.clone()), facts);
                Role::AllocatorClass
            } else {
                Role::Unknown
            };
        }
        let Def::Value(Expr::Call(call)) = def else {
            return Role::Unknown;
        };
        let Callee::Expr(callee) = &call.callee else {
            return Role::Unknown;
        };
        let Some(callee) = callee_ident(callee) else {
            return Role::Unknown;
        };
        let arguments: Vec<&Expr> = call
            .args
            .iter()
            .filter(|arg| arg.spread.is_none())
            .map(|arg| &*arg.expr)
            .collect();
        if arguments.len() != call.args.len() {
            return Role::Unknown;
        }
        match (self.role(chunk, &callee.to_id()), arguments.as_slice()) {
            (Role::CommonJs, [body]) if callable(body).is_some() => Role::Lazy((chunk, id.clone())),
            (Role::Lazy(lazy), []) => Role::Namespace {
                lazy,
                interop: false,
            },
            (Role::ToEsm, [inner, ..]) => match self.lazy_call(chunk, inner) {
                Some(lazy) => Role::Namespace {
                    lazy,
                    interop: true,
                },
                None => Role::Unknown,
            },
            _ => Role::Unknown,
        }
    }

    /// `lazy()` for a lazy module's thunk.
    fn lazy_call(&mut self, chunk: usize, expr: &Expr) -> Option<LazyKey> {
        let Expr::Call(call) = expr else {
            return None;
        };
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        if !call.args.is_empty() {
            return None;
        }
        match self.role(chunk, &callee_ident(callee)?.to_id()) {
            Role::Lazy(lazy) => Some(lazy),
            _ => None,
        }
    }

    /// The name a helper shape gives an identifier bound outside it.
    fn outer(&mut self, chunk: usize, ident: &Ident) -> Option<String> {
        if ident.ctxt == self.chunks[chunk].unresolved {
            return Some(ident.sym.to_string());
        }
        if let Some(Def::Value(value)) = self.chunks[chunk].defs.get(&ident.to_id()).copied()
            && let Some(path) = self.global_path(chunk, value)
        {
            return Some(path);
        }
        match self.role(chunk, &ident.to_id()) {
            Role::CommonJs => Some("#common_js".to_string()),
            Role::CopyProps => Some("#copy_props".to_string()),
            Role::ToEsm => Some("#to_esm".to_string()),
            _ => None,
        }
    }

    /// `Object.create` for a member chain on a global.
    fn global_path(&self, chunk: usize, expr: &Expr) -> Option<String> {
        match expr {
            Expr::Paren(paren) => self.global_path(chunk, &paren.expr),
            Expr::Ident(ident) if ident.ctxt == self.chunks[chunk].unresolved => {
                Some(ident.sym.to_string())
            }
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(name),
                ..
            }) => Some(format!("{}.{}", self.global_path(chunk, obj)?, name.sym)),
            _ => None,
        }
    }

    /// Whether the function only builds a fresh value from its parameters.
    fn allocator(
        &mut self,
        chunk: usize,
        function: Callable<'a>,
        enclosing: &FxHashSet<Id>,
    ) -> bool {
        let (params, body): (Vec<&Pat>, _) = match function {
            Callable::Function(function) => {
                if function.is_async || function.is_generator {
                    return false;
                }
                (
                    function.params.iter().map(|param| &param.pat).collect(),
                    function.body.as_ref().map(|body| body.stmts.as_slice()),
                )
            }
            Callable::Arrow(arrow) => {
                if arrow.is_async {
                    return false;
                }
                let params = arrow.params.iter().collect();
                match &*arrow.body {
                    ArrowFunctionBody::Expr(body) => {
                        let Some(params) = plain_params(params) else {
                            return false;
                        };
                        let mut scope = Scope::new(chunk, params, enclosing);
                        return self.value(body, &mut scope).is_some();
                    }
                    ArrowFunctionBody::FunctionBody(block) => {
                        (params, Some(block.stmts.as_slice()))
                    }
                }
            }
        };
        let (Some(params), Some(body)) = (plain_params(params), body) else {
            return false;
        };
        let mut scope = Scope::new(chunk, params, enclosing);
        // A function declared in the body is a closure, there before any
        // statement of the body runs.
        for statement in body {
            if let Stmt::Decl(Decl::Fn(function)) = statement {
                scope.params.insert(function.ident.to_id());
                scope.fresh.insert(function.ident.to_id());
            }
        }
        // Declarations after the `return` are hoisted above it.
        let trailing = body
            .iter()
            .rev()
            .take_while(|statement| matches!(statement, Stmt::Decl(Decl::Fn(_))))
            .count();
        let Some((last, rest)) = body[..body.len() - trailing].split_last() else {
            return false;
        };
        rest.iter()
            .all(|statement| self.statement(statement, &mut scope))
            && matches!(last, Stmt::Return(returned)
                if returned.arg.as_deref().is_some_and(|value| self.value(value, &mut scope).is_some()))
    }

    /// A statement of an allocator body before its `return`.
    fn statement(&mut self, statement: &Stmt, scope: &mut Scope<'_>) -> bool {
        match statement {
            Stmt::Expr(statement) => self.value(&statement.expr, scope).is_some(),
            // Declaring a function runs nothing, whatever its body, and an
            // `async` or generator body runs only when called.
            Stmt::Decl(Decl::Fn(_)) => true,
            Stmt::Decl(Decl::Var(var)) => var.decls.iter().all(|declarator| {
                let Pat::Ident(name) = &declarator.name else {
                    return false;
                };
                let fresh = match &declarator.init {
                    Some(init) => match self.value(init, scope) {
                        Some(fresh) => fresh,
                        None => return false,
                    },
                    None => false,
                };
                scope.params.insert(name.id.to_id());
                if fresh {
                    scope.fresh.insert(name.id.to_id());
                }
                true
            }),
            _ => false,
        }
    }

    /// Whether constructing the class only stores values into the new
    /// object: no accessor a store could run, no initializer that runs
    /// code, a base class of the same kind.
    fn allocator_class(&mut self, chunk: usize, class: &'a Class) -> bool {
        if !class.decorators.is_empty() {
            return false;
        }
        let derived = match class.super_class.as_deref() {
            None => false,
            Some(Expr::Ident(base)) => {
                if self.role(chunk, &base.to_id()) != Role::AllocatorClass {
                    return false;
                }
                let key = (chunk, base.to_id());
                self.current.reads.push(base.to_id());
                let inherited = self.facts.get(&key).cloned().unwrap_or_default();
                self.current.reads.extend(inherited.reads);
                self.current.intrinsics.extend(inherited.intrinsics);
                self.current.accessors.extend(inherited.accessors);
                self.current.stored.extend(inherited.stored);
                true
            }
            Some(_) => return false,
        };
        let none = FxHashSet::default();
        for member in &class.body {
            let ok = match member {
                ClassMember::Constructor(constructor) => {
                    let params: Option<Vec<Id>> = constructor
                        .params
                        .iter()
                        .map(|param| match param {
                            ParamOrTsParamProp::Param(Param {
                                pat: Pat::Ident(name),
                                ..
                            }) => Some(name.id.to_id()),
                            _ => None,
                        })
                        .collect();
                    let (Some(params), Some(body)) = (params, &constructor.body) else {
                        return false;
                    };
                    let mut scope = Scope::new(chunk, params, &none);
                    body.stmts.iter().enumerate().all(|(index, statement)| {
                        let Stmt::Expr(statement) = statement else {
                            return false;
                        };
                        match &*statement.expr {
                            // `super(values)` first, in a derived class.
                            Expr::Call(CallExpr {
                                callee: Callee::Super(_),
                                args,
                                ..
                            }) => {
                                derived
                                    && index == 0
                                    && args.iter().all(|arg| {
                                        arg.spread.is_none()
                                            && self.value(&arg.expr, &mut scope).is_some()
                                    })
                            }
                            // `this.name = value`.
                            Expr::Assign(AssignExpr {
                                op: AssignOp::Assign,
                                left:
                                    AssignTarget::Simple(SimpleAssignTarget::Member(MemberExpr {
                                        obj,
                                        prop: MemberProp::Ident(name),
                                        ..
                                    })),
                                right,
                                ..
                            }) => {
                                self.current.stored.push(name.sym.to_string());
                                matches!(&**obj, Expr::This(_))
                                    && self.value(right, &mut scope).is_some()
                            }
                            _ => false,
                        }
                    }) && (!derived
                        || matches!(body.stmts.first(), Some(Stmt::Expr(first))
                            if matches!(&*first.expr, Expr::Call(CallExpr { callee: Callee::Super(_), .. }))))
                }
                // Defining a method or an accessor runs nothing.
                ClassMember::Method(method) => {
                    let name = match &method.key {
                        PropName::Ident(name) => Some(name.sym.to_string()),
                        PropName::Str(name) => Some(js_ast::str_value(name)),
                        _ => None,
                    };
                    if let (Some(name), true) = (&name, method.kind != MethodKind::Method) {
                        self.current.accessors.push(name.clone());
                    }
                    name.is_some()
                }
                // A constructor store never reaches a private name.
                ClassMember::PrivateMethod(_) => true,
                // A field initializer runs at construction, without the
                // constructor's parameters.
                ClassMember::ClassProp(ClassProp {
                    is_static, value, ..
                })
                | ClassMember::PrivateProp(PrivateProp {
                    is_static, value, ..
                }) => {
                    *is_static
                        || value.as_deref().is_none_or(|value| {
                            let mut scope = Scope::new(chunk, Vec::new(), &none);
                            self.value(value, &mut scope).is_some()
                        })
                }
                ClassMember::StaticBlock(_) | ClassMember::Empty(_) => true,
                _ => false,
            };
            if !ok {
                return false;
            }
        }
        let (accessors, stored) = (&self.current.accessors, &self.current.stored);
        !stored.iter().any(|name| accessors.contains(name))
    }

    /// The private state `id` names in an allocator body, if the chunk only
    /// ever updates it in place.
    fn private(&self, id: &Id, scope: &Scope<'_>) -> Option<Private> {
        if scope.params.contains(id) || scope.enclosing.contains(id) {
            return None;
        }
        let chunk = &self.chunks[scope.chunk];
        let kind = *chunk.private.get(id)?;
        let untouched = match kind {
            Private::Array => !chunk.stores.other.contains(id),
            Private::Counter | Private::Collection => {
                !chunk.stores.plain.contains(id) && !chunk.stores.other.contains(id)
            }
        };
        untouched.then_some(kind)
    }

    /// Whether evaluating `expr` only allocates; `Some(true)` when its value
    /// is an object the body allocated.
    fn value(&mut self, expr: &Expr, scope: &mut Scope<'_>) -> Option<bool> {
        Some(match expr {
            Expr::Paren(paren) => return self.value(&paren.expr, scope),
            Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_)) => false,
            // `void 0`, `-1`, `!0`.
            Expr::Unary(unary)
                if matches!(
                    unary.op,
                    UnaryOp::Void | UnaryOp::Minus | UnaryOp::Plus | UnaryOp::Bang
                ) =>
            {
                matches!(&*unary.arg, Expr::Lit(Lit::Num(_))).then_some(false)?
            }
            Expr::Ident(ident) => {
                let id = ident.to_id();
                if scope.fresh.contains(&id) {
                    true
                } else if scope.params.contains(&id) || scope.enclosing.contains(&id) {
                    false
                } else if ident.ctxt == self.chunks[scope.chunk].unresolved {
                    (&*ident.sym == "undefined").then_some(false)?
                } else {
                    // A top-level binding that never changes after its
                    // declaration; the call reads it.
                    self.chunks[scope.chunk]
                        .defs
                        .contains_key(&id)
                        .then_some(false)?;
                    self.current.reads.push(id);
                    false
                }
            }
            Expr::Arrow(_) | Expr::Fn(_) => true,
            // `++counter` on a counter the chunk only ever steps.
            Expr::Update(update) => {
                let Expr::Ident(counter) = &*update.arg else {
                    return None;
                };
                (self.private(&counter.to_id(), scope)? == Private::Counter).then_some(())?;
                self.current.private = true;
                false
            }
            Expr::Tpl(template) => {
                for part in &template.exprs {
                    if !matches!(&**part, Expr::Update(_) | Expr::Lit(_)) {
                        return None;
                    }
                    self.value(part, scope)?;
                }
                false
            }
            // `param.name`, for a parameter that is a fresh literal.
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(_),
                ..
            }) => {
                let Expr::Ident(param) = &**obj else {
                    return None;
                };
                let id = param.to_id();
                let position = *scope.positions.get(&id)?;
                if scope.reassigned.contains(&id) {
                    return None;
                }
                if !self.current.literal_params.contains(&position) {
                    self.current.literal_params.push(position);
                }
                false
            }
            Expr::New(new) => {
                let Expr::Ident(class) = &*new.callee else {
                    return None;
                };
                // `new Map()`: an empty collection, if the intrinsic is as
                // the language defines it.
                if class.ctxt == self.chunks[scope.chunk].unresolved {
                    let collection = COLLECTIONS.iter().find(|name| **name == &*class.sym)?;
                    new.args.as_ref().is_none_or(Vec::is_empty).then_some(())?;
                    if !self.current.intrinsics.contains(collection) {
                        self.current.intrinsics.push(collection);
                    }
                    return Some(true);
                }
                if scope.params.contains(&class.to_id())
                    || self.role(scope.chunk, &class.to_id()) != Role::AllocatorClass
                {
                    return None;
                }
                let key = (scope.chunk, class.to_id());
                self.current.reads.push(class.to_id());
                let facts = self.facts.get(&key).cloned().unwrap_or_default();
                self.current.reads.extend(facts.reads);
                self.current.intrinsics.extend(facts.intrinsics);
                for arg in new.args.iter().flatten() {
                    if arg.spread.is_some() {
                        return None;
                    }
                    self.value(&arg.expr, scope)?;
                }
                true
            }
            Expr::Array(array) => {
                for element in &array.elems {
                    let ExprOrSpread { spread: None, expr } = element.as_ref()? else {
                        return None;
                    };
                    self.value(expr, scope)?;
                }
                true
            }
            Expr::Object(object) => {
                for prop in &object.props {
                    let PropOrSpread::Prop(prop) = prop else {
                        return None;
                    };
                    match &**prop {
                        Prop::Shorthand(ident) => {
                            self.value(&Expr::Ident(ident.clone()), scope)?;
                        }
                        Prop::KeyValue(pair) if !matches!(pair.key, PropName::Computed(_)) => {
                            self.value(&pair.value, scope)?;
                        }
                        _ => return None,
                    }
                }
                true
            }
            // `p === void 0 ? null : p`.
            Expr::Cond(cond) => {
                let Expr::Bin(test) = &*cond.test else {
                    return None;
                };
                if !matches!(test.op, BinaryOp::EqEqEq | BinaryOp::NotEqEq) {
                    return None;
                }
                self.value(&test.left, scope)?;
                self.value(&test.right, scope)?;
                // Either branch may be the value.
                let (cons, alt) = (
                    self.value(&cond.cons, scope)?,
                    self.value(&cond.alt, scope)?,
                );
                cons && alt
            }
            Expr::Seq(seq) => {
                let mut last = false;
                for part in &seq.exprs {
                    last = self.value(part, scope)?;
                }
                last
            }
            Expr::Assign(assign) if assign.op == AssignOp::Assign => match &assign.left {
                AssignTarget::Simple(SimpleAssignTarget::Ident(target)) => {
                    let id = target.id.to_id();
                    if !scope.params.contains(&id) {
                        // `list = list.concat(value)` on a private list.
                        (self.private(&id, scope)? == Private::Array).then_some(())?;
                        self.value(&assign.right, scope)?;
                        self.current.private = true;
                        return Some(false);
                    }
                    if let Some(position) = scope.positions.get(&id) {
                        if self.current.literal_params.contains(position) {
                            return None;
                        }
                        scope.reassigned.insert(id.clone());
                    }
                    let fresh = self.value(&assign.right, scope)?;
                    if fresh {
                        scope.fresh.insert(id);
                    } else {
                        scope.fresh.remove(&id);
                    }
                    fresh
                }
                // A store into an object the body allocated.
                AssignTarget::Simple(SimpleAssignTarget::Member(MemberExpr {
                    obj,
                    prop: MemberProp::Ident(_),
                    ..
                })) => {
                    let Expr::Ident(object) = &**obj else {
                        return None;
                    };
                    if !scope.fresh.contains(&object.to_id()) {
                        return None;
                    }
                    self.value(&assign.right, scope)?
                }
                _ => return None,
            },
            Expr::Call(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                if call.args.iter().any(|arg| arg.spread.is_some()) {
                    return None;
                }
                // `list.push(value)`, `registry.set(key, value)` on private
                // state.
                if let Expr::Member(MemberExpr {
                    obj,
                    prop: MemberProp::Ident(method),
                    ..
                }) = &**callee
                {
                    let Expr::Ident(state) = &**obj else {
                        return None;
                    };
                    let fresh = match (self.private(&state.to_id(), scope)?, &*method.sym) {
                        (Private::Array, "concat") => true,
                        (Private::Array, "push" | "unshift") => false,
                        (Private::Collection, "set" | "add") => false,
                        _ => return None,
                    };
                    for arg in &call.args {
                        self.value(&arg.expr, scope)?;
                    }
                    self.current.private = true;
                    return Some(fresh);
                }
                let callee = callee_ident(callee)?;
                if scope.params.contains(&callee.to_id())
                    || self.role(scope.chunk, &callee.to_id()) != Role::Allocator
                {
                    return None;
                }
                let key = (scope.chunk, callee.to_id());
                let facts = self.facts.get(&key).cloned().unwrap_or_default();
                self.current.reads.push(callee.to_id());
                self.current.reads.extend(facts.reads);
                self.current.private |= facts.private;
                self.current.intrinsics.extend(facts.intrinsics);
                for (position, arg) in call.args.iter().enumerate() {
                    if facts.literal_params.contains(&position) && !plain_literal(&arg.expr) {
                        return None;
                    }
                    self.value(&arg.expr, scope)?;
                }
                if facts
                    .literal_params
                    .iter()
                    .any(|position| *position >= call.args.len())
                {
                    return None;
                }
                true
            }
            _ => return None,
        })
    }

    /// The role of `exports.<name>` of a lazy module, when it is an
    /// allocator.
    fn member(&mut self, lazy: &LazyKey, name: &str, depth: usize) -> bool {
        let (chunk, id) = lazy;
        let Some(Def::Value(Expr::Call(call))) = self.chunks[*chunk].defs.get(id).copied() else {
            return false;
        };
        let Some(body) = call.args.first().and_then(|arg| callable(&arg.expr)) else {
            return false;
        };
        let (params, statements): (Vec<&Pat>, &[Stmt]) = match body {
            Callable::Function(function) => (
                function.params.iter().map(|param| &param.pat).collect(),
                function.body.as_ref().map_or(&[], |body| &body.stmts),
            ),
            Callable::Arrow(arrow) => match &*arrow.body {
                ArrowFunctionBody::FunctionBody(block) => {
                    (arrow.params.iter().collect(), &block.stmts)
                }
                ArrowFunctionBody::Expr(_) => return false,
            },
        };
        let param = |index: usize| match params.get(index) {
            Some(Pat::Ident(name)) => Some(name.id.to_id()),
            _ => None,
        };
        let (exports, module) = (param(0), param(1));
        // Bindings of the module body, and which are declared once.
        let mut declared: FxHashMap<Id, usize> = FxHashMap::default();
        let mut functions: FxHashMap<Id, Callable<'a>> = FxHashMap::default();
        let mut assignments: Vec<(&MemberExpr, &Expr)> = Vec::new();
        for statement in statements {
            match statement {
                Stmt::Decl(Decl::Fn(function)) => {
                    *declared.entry(function.ident.to_id()).or_default() += 1;
                    functions.insert(
                        function.ident.to_id(),
                        Callable::Function(&function.function),
                    );
                }
                Stmt::Decl(Decl::Var(var)) => {
                    for declarator in &var.decls {
                        if let Pat::Ident(name) = &declarator.name {
                            *declared.entry(name.id.to_id()).or_default() += 1;
                            if let Some(function) = declarator.init.as_deref().and_then(callable) {
                                functions.insert(name.id.to_id(), function);
                            }
                        }
                    }
                }
                Stmt::Expr(statement) => {
                    let mut parts = Vec::new();
                    match &*statement.expr {
                        Expr::Seq(seq) => parts.extend(seq.exprs.iter().map(|part| &**part)),
                        other => parts.push(other),
                    }
                    for part in parts {
                        if let Expr::Assign(AssignExpr {
                            op: AssignOp::Assign,
                            left: AssignTarget::Simple(SimpleAssignTarget::Member(target)),
                            right,
                            ..
                        }) = part
                        {
                            assignments.push((target, right));
                        }
                    }
                }
                _ => {}
            }
        }
        let assigned = &self.chunks[*chunk].assigned;
        let constants: FxHashSet<Id> = declared
            .iter()
            .filter(|(id, count)| **count == 1 && !assigned.contains(*id))
            .map(|(id, _)| id.clone())
            .collect();
        let on = |target: &MemberExpr, object: &Option<Id>, name: &str| {
            matches!((&*target.obj, &target.prop), (Expr::Ident(base), MemberProp::Ident(prop))
                if Some(base.to_id()) == *object && &*prop.sym == name)
        };
        // `module.exports = other()` hands the question to another module.
        let forwarded: Vec<&Expr> = assignments
            .iter()
            .filter(|(target, _)| on(target, &module, "exports"))
            .map(|(_, value)| *value)
            .collect();
        if let [value] = forwarded.as_slice() {
            return match self.lazy_call(*chunk, value) {
                Some(next) if depth < 8 => self.member(&next, name, depth + 1),
                _ => false,
            };
        }
        if !forwarded.is_empty() {
            return false;
        }
        let values: Vec<&Expr> = assignments
            .iter()
            .filter(|(target, value)| {
                on(target, &exports, name)
                    && !matches!(value, Expr::Unary(unary) if unary.op == UnaryOp::Void)
            })
            .map(|(_, value)| *value)
            .collect();
        let [value] = values.as_slice() else {
            return false;
        };
        let function = match value {
            Expr::Ident(ident) if constants.contains(&ident.to_id()) => {
                functions.get(&ident.to_id()).copied()
            }
            other => callable(other),
        };
        // What a member reads belongs to its module, which has run.
        let outer = std::mem::take(&mut self.current);
        let allocates =
            function.is_some_and(|function| self.allocator(*chunk, function, &constants));
        let facts = std::mem::replace(&mut self.current, outer);
        allocates
            && !facts.private
            && facts.literal_params.is_empty()
            && facts.intrinsics.is_empty()
    }

    /// Whether a lazy module's body names `__esModule`: its interop
    /// namespace's `default` is then not its exports object.
    fn es_module(&self, lazy: &LazyKey) -> bool {
        let Some(Def::Value(value)) = self.chunks[lazy.0].defs.get(&lazy.1).copied() else {
            return true;
        };
        let mut mentions = MentionsEsModule(false);
        value.visit_with(&mut mentions);
        mentions.0
    }
}

fn callable_arrow(def: Def<'_>) -> Option<Callable<'_>> {
    match def {
        Def::Value(value) => callable(value),
        Def::Function(_) | Def::Class(_) => None,
    }
}

/// A default value that is the same data on every call.
fn constant_default(expr: &Expr) -> bool {
    match expr {
        Expr::Paren(paren) => constant_default(&paren.expr),
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_)) => true,
        Expr::Unary(unary) => {
            matches!(
                unary.op,
                UnaryOp::Void | UnaryOp::Minus | UnaryOp::Plus | UnaryOp::Bang
            ) && matches!(&*unary.arg, Expr::Lit(Lit::Num(_)))
        }
        Expr::Array(array) => array.elems.iter().all(|element| {
            matches!(element, Some(ExprOrSpread { spread: None, expr }) if constant_default(expr))
        }),
        Expr::Object(object) => object.props.iter().all(|prop| match prop {
            PropOrSpread::Prop(prop) => matches!(&**prop, Prop::KeyValue(pair)
                if !matches!(pair.key, PropName::Computed(_)) && constant_default(&pair.value)),
            PropOrSpread::Spread(_) => false,
        }),
        _ => false,
    }
}

/// Parameters that are identifiers, each with at most a constant default.
fn plain_params(params: Vec<&Pat>) -> Option<Vec<Id>> {
    params
        .into_iter()
        .map(|param| match param {
            Pat::Ident(name) => Some(name.id.to_id()),
            Pat::Assign(AssignPat { left, right, .. }) => match &**left {
                Pat::Ident(name) if constant_default(right) => Some(name.id.to_id()),
                _ => None,
            },
            _ => None,
        })
        .collect()
}

/// Member paths used on a binding, as `a` or `a.b`.
#[derive(Default)]
struct MemberUses {
    paths: FxHashSet<(Id, String)>,
}

impl Visit for MemberUses {
    fn visit_member_expr(&mut self, member: &MemberExpr) {
        if let MemberProp::Ident(last) = &member.prop {
            match &*member.obj {
                Expr::Ident(base) => {
                    self.paths.insert((base.to_id(), last.sym.to_string()));
                }
                Expr::Member(MemberExpr {
                    obj,
                    prop: MemberProp::Ident(first),
                    ..
                }) => {
                    if let Expr::Ident(base) = &**obj {
                        self.paths
                            .insert((base.to_id(), format!("{}.{}", first.sym, last.sym)));
                    }
                }
                _ => {}
            }
        }
        member.visit_children_with(self);
    }
}

pub struct Allocator {
    pub binding: Id,
    /// The bindings of this chunk a call reads.
    pub reads: Vec<Id>,
    /// It also touches state private to its chunk: a call is contained.
    pub private: bool,
    /// Argument positions that must be fresh object literals.
    pub literal_params: Vec<usize>,
    /// Intrinsic collections a call constructs empty.
    pub intrinsics: Vec<&'static str>,
}

/// What derivation found for the bindings of one chunk.
#[derive(Default)]
pub struct Derived {
    /// Bindings that are allocators.
    pub allocators: Vec<Allocator>,
    /// Classes whose construction only stores into the new object, with
    /// the bindings of this chunk a construction reads and the intrinsic
    /// collections it constructs.
    pub classes: Vec<(Id, Vec<Id>, Vec<&'static str>)>,
    /// Bindings holding the function-naming helper.
    pub namers: Vec<Id>,
    /// `binding.path` that is an allocator.
    pub members: Vec<(Id, String)>,
    /// Bindings holding a lazy module's thunk, and whether its body is in
    /// this chunk.
    pub lazy: Vec<(Id, LazyKey, bool)>,
    /// Bindings holding the interop namespace helper.
    pub interop: Vec<Id>,
    /// Bindings holding the lazy-module wrapper.
    pub wrappers: Vec<Id>,
}

pub fn derive(
    module: &Module,
    unresolved: SyntaxContext,
    program: Option<&ProgramFacts>,
) -> Derived {
    let mut chunks = vec![Chunk::new(module, unresolved)];
    for (_, parsed) in program.iter().flat_map(|program| &program.modules) {
        let unresolved = SyntaxContext::empty().apply_mark(parsed.unresolved_mark);
        chunks.push(Chunk::new(&parsed.module, unresolved));
    }
    let mut resolver = Resolver {
        chunks,
        program,
        roles: FxHashMap::default(),
        facts: FxHashMap::default(),
        current: Facts::default(),
        namer: canonical(NAMER),
        common_js: canonical(COMMON_JS),
        copy_props: canonical(COPY_PROPS),
        to_esm: canonical(TO_ESM),
    };
    let mut bindings: Vec<Id> = resolver.chunks[0]
        .defs
        .keys()
        .chain(resolver.chunks[0].imports.keys())
        .cloned()
        .collect();
    bindings.sort();
    let mut uses = MemberUses::default();
    resolver.chunks[0].module.visit_with(&mut uses);
    let mut derived = Derived::default();
    for id in bindings {
        match resolver.role(0, &id) {
            Role::Allocator | Role::AllocatorClass => {
                let class = resolver.role(0, &id) == Role::AllocatorClass;
                // The facts are those of the defining chunk; what an
                // imported allocator reads is its provider's.
                let mut home = (0, id.clone());
                while let Some((specifier, export)) =
                    resolver.chunks[home.0].imports.get(&home.1).cloned()
                {
                    let Some(provider) = resolver.provider(home.0, &specifier) else {
                        break;
                    };
                    match resolver.chunks[provider].exports.get(&export) {
                        Some(Export::Local(local)) => home = (provider, local.clone()),
                        _ => break,
                    }
                }
                let facts = resolver.facts.get(&home).cloned().unwrap_or_default();
                let reads = if home.0 == 0 { facts.reads } else { Vec::new() };
                if class {
                    derived.classes.push((id, reads, facts.intrinsics));
                } else {
                    derived.allocators.push(Allocator {
                        binding: id,
                        reads,
                        private: facts.private,
                        literal_params: facts.literal_params,
                        intrinsics: facts.intrinsics,
                    });
                }
            }
            Role::Namer => derived.namers.push(id),
            Role::CommonJs => derived.wrappers.push(id),
            Role::ToEsm => derived.interop.push(id),
            Role::Lazy(lazy) => {
                let local = lazy.0 == 0;
                derived.lazy.push((id, lazy, local));
            }
            Role::Namespace { lazy, interop } => {
                let mut paths: Vec<&String> = uses
                    .paths
                    .iter()
                    .filter(|(base, _)| *base == id)
                    .map(|(_, path)| path)
                    .collect();
                paths.sort();
                for path in paths {
                    let name = match path.split_once('.') {
                        None => path.as_str(),
                        // `ns.default` is the exports object of a module
                        // that is not an ES module.
                        Some(("default", name)) if interop && !resolver.es_module(&lazy) => name,
                        Some(_) => continue,
                    };
                    if resolver.member(&lazy, name, 0) {
                        derived.members.push((id.clone(), path.clone()));
                    }
                }
            }
            Role::CopyProps | Role::Unknown => {}
        }
    }
    derived
}
