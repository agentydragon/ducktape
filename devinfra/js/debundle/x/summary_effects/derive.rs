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

/// A lazy module: the chunk and binding holding its thunk.
pub type LazyKey = (usize, Id);

#[derive(Clone, PartialEq, Eq)]
enum Role {
    Unknown,
    CommonJs,
    CopyProps,
    ToEsm,
    Allocator,
    Lazy(LazyKey),
    Namespace { lazy: LazyKey, interop: bool },
}

#[derive(Clone, Copy)]
enum Def<'a> {
    Function(&'a Function),
    Value(&'a Expr),
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
                Decl::Var(var) => {
                    for declarator in &var.decls {
                        if let Pat::Ident(name) = &declarator.name {
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
        Self {
            module,
            unresolved,
            defs,
            imports,
            exports,
            assigned,
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
                let Prop::KeyValue(pair) = &**prop else {
                    return None;
                };
                let key = match &pair.key {
                    PropName::Ident(name) => name.sym.to_string(),
                    PropName::Str(value) => js_ast::str_value(value),
                    _ => return None,
                };
                out += &format!("{key}:{},", shape(&pair.value, names, outer)?);
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
    /// Per allocator binding: the top-level bindings of its chunk a call
    /// reads, its own callees included.
    reads: FxHashMap<(usize, Id), Vec<Id>>,
    /// Those of the allocator body being classified.
    reading: Vec<Id>,
    common_js: String,
    copy_props: String,
    to_esm: String,
}

/// Bindings an allocator body may use as values.
struct Scope<'s> {
    chunk: usize,
    params: FxHashSet<Id>,
    /// Parameters and locals holding an object the body allocated.
    fresh: FxHashSet<Id>,
    /// Bindings of an enclosing lazy module body, declared once.
    enclosing: &'s FxHashSet<Id>,
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
                }
            }
            let none = FxHashSet::default();
            let outer = std::mem::take(&mut self.reading);
            let allocates = self.allocator(chunk, function, &none);
            let reads = std::mem::replace(&mut self.reading, outer);
            return if allocates {
                self.reads.insert((chunk, id.clone()), reads);
                Role::Allocator
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
                        let mut scope = Scope {
                            chunk,
                            params,
                            fresh: FxHashSet::default(),
                            enclosing,
                        };
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
        let mut scope = Scope {
            chunk,
            params,
            fresh: FxHashSet::default(),
            enclosing,
        };
        let Some((last, rest)) = body.split_last() else {
            return false;
        };
        for statement in rest {
            let ok = match statement {
                Stmt::Expr(statement) => self.value(&statement.expr, &mut scope).is_some(),
                Stmt::Decl(Decl::Var(var)) => var.decls.iter().all(|declarator| {
                    let Pat::Ident(name) = &declarator.name else {
                        return false;
                    };
                    let fresh = match &declarator.init {
                        Some(init) => match self.value(init, &mut scope) {
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
            };
            if !ok {
                return false;
            }
        }
        matches!(last, Stmt::Return(returned)
            if returned.arg.as_deref().is_some_and(|value| self.value(value, &mut scope).is_some()))
    }

    /// Whether evaluating `expr` only allocates; `Some(true)` when its value
    /// is an object the body allocated.
    fn value(&mut self, expr: &Expr, scope: &mut Scope<'_>) -> Option<bool> {
        Some(match expr {
            Expr::Paren(paren) => return self.value(&paren.expr, scope),
            Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_)) => false,
            Expr::Unary(unary) if unary.op == UnaryOp::Void => {
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
                    self.reading.push(id);
                    false
                }
            }
            Expr::Arrow(_) | Expr::Fn(_) => true,
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
                        return None;
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
                let callee = callee_ident(callee)?;
                if scope.params.contains(&callee.to_id())
                    || self.role(scope.chunk, &callee.to_id()) != Role::Allocator
                {
                    return None;
                }
                let key = (scope.chunk, callee.to_id());
                self.reading.push(callee.to_id());
                self.reading
                    .extend(self.reads.get(&key).into_iter().flatten().cloned());
                for arg in &call.args {
                    if arg.spread.is_some() {
                        return None;
                    }
                    self.value(&arg.expr, scope)?;
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
        let outer = std::mem::take(&mut self.reading);
        let allocates =
            function.is_some_and(|function| self.allocator(*chunk, function, &constants));
        self.reading = outer;
        allocates
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
        Def::Function(_) => None,
    }
}

fn plain_params(params: Vec<&Pat>) -> Option<FxHashSet<Id>> {
    params
        .into_iter()
        .map(|param| match param {
            Pat::Ident(name) => Some(name.id.to_id()),
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

/// What derivation found for the bindings of one chunk.
#[derive(Default)]
pub struct Derived {
    /// Bindings that are allocators, with the bindings of this chunk a
    /// call reads.
    pub allocators: Vec<(Id, Vec<Id>)>,
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
        reads: FxHashMap::default(),
        reading: Vec::new(),
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
            Role::Allocator => {
                // An imported allocator's reads are its provider's.
                let reads = resolver.reads.get(&(0, id.clone())).cloned();
                derived.allocators.push((id, reads.unwrap_or_default()));
            }
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
