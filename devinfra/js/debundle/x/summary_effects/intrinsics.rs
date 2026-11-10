//! Which intrinsics a chunk itself modifies, from syntax.
//!
//! Tier 0 accepts some uses of intrinsics as pure (`new Map()`, `class X
//! extends Error`, `Object.prototype.toString`). Each depends on the global
//! binding still naming the intrinsic and on specific properties being
//! untouched. This census records, per global name, the properties the chunk
//! writes; a use the census cannot prove untouched stays impure.
//!
//! Using an intrinsic as a value does not modify it; a store does. The
//! census follows an intrinsic, its `prototype` and the global object
//! through local bindings and the parameters of local functions called by
//! name, and attributes each store through such an alias. Where the value
//! goes anywhere else (a literal, a member, a return, an argument of code
//! the chunk does not define) it has *escaped*: the only stores then charged
//! to it are those through `<anything>.prototype`. That a store through any
//! other expression does not land on an escaped intrinsic is a premise.
//!
//! It sees this chunk only. Writes by other chunks are a per-program fact
//! the caller must supply.

use rustc_hash::{FxHashMap, FxHashSet};
use swc_common::{Span, Spanned, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Writes {
    /// The global is replaced, or is written under a key or at a depth the
    /// census cannot name.
    pub any: bool,
    pub props: FxHashSet<String>,
}

#[derive(Default, Clone)]
pub struct Census {
    pub written: FxHashMap<String, Writes>,
    /// A store on the global object under an unknown key.
    pub all: bool,
    /// Globals whose object or prototype flows where the census does not
    /// follow.
    escaped: FxHashSet<String>,
    /// The global object itself does.
    pub escaped_all: bool,
    /// Stores through `<base>.prototype` for a base the census does not
    /// follow: they may land on any escaped global.
    pub unfollowed_prototype_stores: Writes,
}

impl Census {
    /// Adds what another chunk writes.
    pub fn merge(&mut self, other: Census) {
        self.all |= other.all;
        self.escaped_all |= other.escaped_all;
        self.escaped.extend(other.escaped);
        self.unfollowed_prototype_stores.any |= other.unfollowed_prototype_stores.any;
        self.unfollowed_prototype_stores
            .props
            .extend(other.unfollowed_prototype_stores.props);
        for (global, writes) in other.written {
            let mine = self.written.entry(global).or_default();
            mine.any |= writes.any;
            mine.props.extend(writes.props);
        }
    }

    pub fn escaped(&self, global: &str) -> bool {
        self.escaped_all || self.escaped.contains(global)
    }

    /// Whether the chunk leaves the global binding and these properties of
    /// it (or of its `prototype`) untouched.
    pub fn untouched(&self, global: &str, props: &[&str]) -> bool {
        if self.all {
            return false;
        }
        let clear =
            |writes: &Writes| !writes.any && props.iter().all(|prop| !writes.props.contains(*prop));
        self.written.get(global).is_none_or(clear)
            && (!self.escaped(global) || clear(&self.unfollowed_prototype_stores))
    }
}

const GLOBAL_OBJECTS: [&str; 4] = ["globalThis", "window", "self", "global"];

/// Functions that modify their first argument.
const MUTATORS: [(&str, &str); 10] = [
    ("Object", "defineProperty"),
    ("Object", "defineProperties"),
    ("Object", "assign"),
    ("Object", "setPrototypeOf"),
    ("Object", "freeze"),
    ("Object", "seal"),
    ("Object", "preventExtensions"),
    ("Reflect", "defineProperty"),
    ("Reflect", "set"),
    ("Reflect", "setPrototypeOf"),
];

/// What a value may be.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
enum Intrinsic {
    GlobalObject,
    /// The value the global of this name holds.
    Object(String),
    /// Its `prototype`.
    Prototype(String),
}

type Taint = Vec<Intrinsic>;

fn join(into: &mut Taint, from: Taint) -> bool {
    let before = into.len();
    for intrinsic in from {
        if !into.contains(&intrinsic) {
            into.push(intrinsic);
        }
    }
    into.len() != before
}

fn strip(mut expr: &Expr) -> &Expr {
    while let Expr::Paren(paren) = expr {
        expr = &paren.expr;
    }
    expr
}

/// A key the census can name: an identifier, a string literal, or a
/// well-known symbol.
fn key(prop: &MemberProp, unresolved: SyntaxContext) -> Option<String> {
    match prop {
        MemberProp::Ident(name) => Some(name.sym.to_string()),
        MemberProp::PrivateName(_) => None,
        MemberProp::Computed(computed) => match strip(&computed.expr) {
            Expr::Lit(Lit::Str(value)) => Some(js_ast::str_value(value)),
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(name),
                ..
            }) if matches!(&**obj, Expr::Ident(symbol)
                if symbol.ctxt == unresolved && &*symbol.sym == "Symbol") =>
            {
                Some(format!("@@{}", name.sym))
            }
            // A registry or fresh symbol is no binding's name and no
            // well-known symbol.
            Expr::Call(CallExpr {
                callee: Callee::Expr(callee),
                ..
            }) if matches!(strip(callee), Expr::Ident(symbol)
                if symbol.ctxt == unresolved && &*symbol.sym == "Symbol")
                || matches!(strip(callee), Expr::Member(MemberExpr {
                    obj,
                    prop: MemberProp::Ident(method),
                    ..
                }) if &*method.sym == "for" && matches!(&**obj, Expr::Ident(symbol)
                    if symbol.ctxt == unresolved && &*symbol.sym == "Symbol")) =>
            {
                Some("@@other".to_string())
            }
            _ => None,
        },
    }
}

/// The expressions whose value `expr` may take: itself, or the operands a
/// conditional, a logical operator or a sequence passes through.
fn passed_through<'e>(expr: &'e Expr, out: &mut Vec<&'e Expr>) {
    match expr {
        Expr::Paren(paren) => passed_through(&paren.expr, out),
        Expr::Cond(cond) => {
            passed_through(&cond.cons, out);
            passed_through(&cond.alt, out);
        }
        Expr::Bin(binary)
            if matches!(
                binary.op,
                BinaryOp::LogicalOr | BinaryOp::LogicalAnd | BinaryOp::NullishCoalescing
            ) =>
        {
            passed_through(&binary.left, out);
            passed_through(&binary.right, out);
        }
        Expr::Seq(seq) => {
            if let Some(last) = seq.exprs.last() {
                passed_through(last, out);
            }
        }
        other => out.push(other),
    }
}

/// Local functions called by name, with the parameter each argument
/// position binds, where it is a plain identifier.
#[derive(Default)]
struct Functions {
    params: FxHashMap<Id, Vec<Option<Id>>>,
    declared: FxHashMap<Id, usize>,
    assigned: FxHashSet<Id>,
}

struct UsesArguments(bool);

impl Visit for UsesArguments {
    fn visit_ident(&mut self, ident: &Ident) {
        self.0 |= &*ident.sym == "arguments";
    }
}

/// The binding each argument position reaches, where the parameter is a
/// plain identifier.
fn pattern_positions<'p>(params: impl Iterator<Item = &'p Pat>) -> Vec<Option<Id>> {
    let mut positions = Vec::new();
    for param in params {
        match param {
            Pat::Ident(binding) => positions.push(Some(binding.id.to_id())),
            Pat::Assign(AssignPat { left, .. }) => positions.push(match &**left {
                Pat::Ident(binding) => Some(binding.id.to_id()),
                _ => None,
            }),
            Pat::Rest(_) => break,
            _ => positions.push(None),
        }
    }
    positions
}

/// `None` for a function that reads `arguments`.
fn function_positions(function: &Function) -> Option<Vec<Option<Id>>> {
    let mut uses = UsesArguments(false);
    function.visit_with(&mut uses);
    (!uses.0).then(|| pattern_positions(function.params.iter().map(|param| &param.pat)))
}

fn positions(function: &Expr) -> Option<Vec<Option<Id>>> {
    match function {
        Expr::Fn(function) => function_positions(&function.function),
        // An arrow has no `arguments` of its own.
        Expr::Arrow(arrow) => Some(pattern_positions(arrow.params.iter())),
        _ => None,
    }
}

impl Visit for Functions {
    fn visit_fn_decl(&mut self, function: &FnDecl) {
        let id = function.ident.to_id();
        *self.declared.entry(id.clone()).or_default() += 1;
        if let Some(positions) = function_positions(&function.function) {
            self.params.insert(id, positions);
        }
        function.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let Pat::Ident(name) = &declarator.name {
            *self.declared.entry(name.id.to_id()).or_default() += 1;
            if let Some(positions) = declarator
                .init
                .as_deref()
                .and_then(|init| positions(strip(init)))
            {
                self.params.insert(name.id.to_id(), positions);
            }
        }
        declarator.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        if let AssignTarget::Simple(SimpleAssignTarget::Ident(target)) = &assign.left {
            self.assigned.insert(target.id.to_id());
        }
        assign.visit_children_with(self);
    }
}

struct Flow {
    unresolved: SyntaxContext,
    taints: FxHashMap<Id, Taint>,
    params: FxHashMap<Id, Vec<Option<Id>>>,
}

impl Flow {
    fn taint(&self, expr: &Expr) -> Taint {
        let mut sources = Vec::new();
        passed_through(expr, &mut sources);
        let mut taint = Taint::new();
        for source in sources {
            match source {
                Expr::Ident(ident) if ident.ctxt == self.unresolved => {
                    let intrinsic = if GLOBAL_OBJECTS.contains(&&*ident.sym) {
                        Intrinsic::GlobalObject
                    } else {
                        Intrinsic::Object(ident.sym.to_string())
                    };
                    join(&mut taint, vec![intrinsic]);
                }
                Expr::Ident(ident) => {
                    if let Some(known) = self.taints.get(&ident.to_id()) {
                        join(&mut taint, known.clone());
                    }
                }
                Expr::Member(member) => {
                    let name = key(&member.prop, self.unresolved);
                    for base in self.taint(&member.obj) {
                        match (base, &name) {
                            (Intrinsic::GlobalObject, Some(name)) => {
                                join(&mut taint, vec![Intrinsic::Object(name.clone())]);
                            }
                            (Intrinsic::Object(global), Some(name)) if name == "prototype" => {
                                join(&mut taint, vec![Intrinsic::Prototype(global)]);
                            }
                            _ => {}
                        }
                    }
                }
                _ => {}
            }
        }
        taint
    }

    /// The parameter and the value flowing into it, for each argument the
    /// census follows into a local function: one called by name, or a
    /// function expression called where it is written.
    fn arguments<'e>(&self, call: &'e CallExpr) -> Vec<(Id, &'e Expr)> {
        let Callee::Expr(callee) = &call.callee else {
            return Vec::new();
        };
        let (params, immediate) = match strip(callee) {
            Expr::Ident(callee) => (self.params.get(&callee.to_id()).cloned(), false),
            function => (positions(function), true),
        };
        call.args
            .iter()
            .take_while(|arg| arg.spread.is_none())
            .zip(params.unwrap_or_default())
            .filter_map(|(arg, param)| Some((param?, &*arg.expr)))
            // The global object is followed only into a function called
            // once: through shared parameters it would reach every store.
            .filter(|(_, value)| immediate || !self.taint(value).contains(&Intrinsic::GlobalObject))
            .collect()
    }
}

struct FlowPass<'a> {
    flow: &'a mut Flow,
    changed: bool,
}

impl FlowPass<'_> {
    fn bind(&mut self, id: Id, value: &Expr) {
        let taint = self.flow.taint(value);
        if !taint.is_empty() {
            self.changed |= join(self.flow.taints.entry(id).or_default(), taint);
        }
    }
}

impl Visit for FlowPass<'_> {
    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let (Pat::Ident(name), Some(init)) = (&declarator.name, &declarator.init) {
            self.bind(name.id.to_id(), init);
        }
        declarator.visit_children_with(self);
    }

    fn visit_assign_pat(&mut self, pattern: &AssignPat) {
        if let Pat::Ident(name) = &*pattern.left {
            self.bind(name.id.to_id(), &pattern.right);
        }
        pattern.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        if let AssignTarget::Simple(SimpleAssignTarget::Ident(target)) = &assign.left
            && target.id.ctxt != self.flow.unresolved
        {
            self.bind(target.id.to_id(), &assign.right);
        }
        assign.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        for (param, value) in self.flow.arguments(call) {
            self.bind(param, value);
        }
        call.visit_children_with(self);
    }
}

struct Visitor<'a> {
    flow: &'a Flow,
    object_aliases: &'a FxHashMap<Id, String>,
    census: Census,
    /// Every expression that may be an intrinsic, by span.
    occurrences: Vec<((u32, u32), Taint)>,
    /// Those in a position that reads the value or hands it to a flow the
    /// census follows.
    used: FxHashSet<(u32, u32)>,
    exported: Vec<Id>,
}

fn span_key(span: Span) -> (u32, u32) {
    (span.lo.0, span.hi.0)
}

impl Visitor<'_> {
    /// Marks the value of `expr` as read here, or followed.
    fn consume(&mut self, expr: &Expr) {
        let mut sources = Vec::new();
        passed_through(expr, &mut sources);
        for source in sources {
            self.used.insert(span_key(source.span()));
        }
    }

    /// `Map`, `globalThis.Map`: an expression that can only be that global.
    fn named_by_global(&self, expr: &Expr) -> bool {
        let global = |expr: &Expr| match strip(expr) {
            Expr::Ident(ident) if ident.ctxt == self.flow.unresolved => Some(ident.sym.clone()),
            _ => None,
        };
        match strip(expr) {
            Expr::Member(member) => {
                global(&member.obj).is_some_and(|name| GLOBAL_OBJECTS.contains(&&*name))
                    && key(&member.prop, self.flow.unresolved).is_some()
            }
            other => global(other).is_some(),
        }
    }

    fn writes(&mut self, global: String) -> &mut Writes {
        self.census.written.entry(global).or_default()
    }

    /// Records a write to the object `target` names, under `prop`.
    fn write(&mut self, target: &Expr, prop: Option<String>) {
        self.consume(target);
        let taint = self.flow.taint(target);
        for intrinsic in &taint {
            match (intrinsic, &prop) {
                // `globalThis.Map = …` replaces the binding.
                (Intrinsic::GlobalObject, Some(name)) => self.writes(name.clone()).any = true,
                (Intrinsic::GlobalObject, None) => self.census.all = true,
                (Intrinsic::Object(global) | Intrinsic::Prototype(global), Some(name)) => {
                    self.writes(global.clone()).props.insert(name.clone());
                }
                (Intrinsic::Object(global) | Intrinsic::Prototype(global), None) => {
                    self.writes(global.clone()).any = true;
                }
            }
        }
        let Expr::Member(member) = strip(target) else {
            return;
        };
        // A local may also hold an escaped intrinsic the census lost.
        let on_prototype = key(&member.prop, self.flow.unresolved).as_deref() == Some("prototype");
        if on_prototype && !self.named_by_global(&member.obj) {
            let stores = &mut self.census.unfollowed_prototype_stores;
            match &prop {
                Some(name) => {
                    stores.props.insert(name.clone());
                }
                None => stores.any = true,
            }
        }
        if !taint.is_empty() {
            return;
        }
        // A store deeper inside an intrinsic the census follows.
        let mut base = &member.obj;
        let rooted = loop {
            let taint = self.flow.taint(base);
            if !taint.is_empty() {
                break taint;
            }
            match strip(base) {
                Expr::Member(inner) => base = &inner.obj,
                _ => break Taint::new(),
            }
        };
        for intrinsic in &rooted {
            match intrinsic {
                Intrinsic::GlobalObject => self.census.all = true,
                Intrinsic::Object(global) | Intrinsic::Prototype(global) => {
                    self.writes(global.clone()).any = true;
                }
            }
        }
    }

    fn mutator(&self, callee: &Expr) -> Option<&'static str> {
        let (object, method) = match strip(callee) {
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(method),
                ..
            }) => match strip(obj) {
                Expr::Ident(object) if object.ctxt == self.flow.unresolved => {
                    (object.sym.to_string(), method.sym.to_string())
                }
                _ => return None,
            },
            Expr::Ident(alias) => (
                "Object".to_string(),
                self.object_aliases.get(&alias.to_id())?.clone(),
            ),
            _ => return None,
        };
        MUTATORS
            .iter()
            .find(|(owner, name)| *owner == object && *name == method)
            .map(|(_, name)| *name)
    }
}

impl Visit for Visitor<'_> {
    fn visit_expr(&mut self, expr: &Expr) {
        if matches!(expr, Expr::Ident(_) | Expr::Member(_)) {
            let taint = self.flow.taint(expr);
            if !taint.is_empty() {
                self.occurrences.push((span_key(expr.span()), taint));
            }
        }
        expr.visit_children_with(self);
    }

    fn visit_prop(&mut self, prop: &Prop) {
        // `{ Map }` stores the value in a literal.
        if let Prop::Shorthand(ident) = prop {
            let taint = self.flow.taint(&Expr::Ident(ident.clone()));
            if !taint.is_empty() {
                self.occurrences.push((span_key(ident.span), taint));
            }
        }
        prop.visit_children_with(self);
    }

    fn visit_export_named_specifier(&mut self, specifier: &ExportNamedSpecifier) {
        // An exported alias is another chunk's to use.
        if let ModuleExportName::Ident(ident) = &specifier.orig {
            let taint = self.flow.taint(&Expr::Ident(ident.clone()));
            if !taint.is_empty() {
                self.occurrences.push((span_key(ident.span), taint));
            }
        }
    }

    fn visit_export_decl(&mut self, export: &ExportDecl) {
        // An exported alias is another chunk's to use.
        if let Decl::Var(var) = &export.decl {
            for declarator in &var.decls {
                if let Pat::Ident(name) = &declarator.name {
                    self.exported.push(name.id.to_id());
                }
            }
        }
        export.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        self.consume(&member.obj);
        member.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let (Pat::Ident(_), Some(init)) = (&declarator.name, &declarator.init) {
            self.consume(init);
        }
        declarator.visit_children_with(self);
    }

    fn visit_assign_pat(&mut self, pattern: &AssignPat) {
        if matches!(&*pattern.left, Pat::Ident(_)) {
            self.consume(&pattern.right);
        }
        pattern.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        match &assign.left {
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                self.write(&member.obj, key(&member.prop, self.flow.unresolved));
            }
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                if binding.id.ctxt == self.flow.unresolved {
                    self.writes(binding.id.sym.to_string()).any = true;
                } else {
                    self.consume(&assign.right);
                }
            }
            _ => {}
        }
        assign.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        if let Expr::Member(member) = strip(&update.arg) {
            self.write(&member.obj, key(&member.prop, self.flow.unresolved));
        }
        update.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, unary: &UnaryExpr) {
        match unary.op {
            UnaryOp::Delete => {
                if let Expr::Member(member) = strip(&unary.arg) {
                    self.write(&member.obj, key(&member.prop, self.flow.unresolved));
                }
            }
            UnaryOp::TypeOf | UnaryOp::Bang | UnaryOp::Void => self.consume(&unary.arg),
            _ => {}
        }
        unary.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee {
            self.consume(callee);
            if let (Some(mutator), Some(target)) = (self.mutator(callee), call.args.first()) {
                let prop = match (mutator, call.args.get(1)) {
                    ("defineProperty" | "set", Some(name)) => match strip(&name.expr) {
                        Expr::Lit(Lit::Str(value)) => Some(js_ast::str_value(value)),
                        _ => None,
                    },
                    _ => None,
                };
                self.write(&target.expr, prop);
            }
        }
        for (_, value) in self.flow.arguments(call) {
            self.consume(value);
        }
        call.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, new: &NewExpr) {
        self.consume(&new.callee);
        new.visit_children_with(self);
    }

    fn visit_bin_expr(&mut self, binary: &BinExpr) {
        match binary.op {
            BinaryOp::InstanceOf => self.consume(&binary.right),
            // An identity comparison keeps no reference.
            BinaryOp::EqEqEq | BinaryOp::NotEqEq | BinaryOp::EqEq | BinaryOp::NotEq => {
                self.consume(&binary.left);
                self.consume(&binary.right);
            }
            _ => {}
        }
        binary.visit_children_with(self);
    }

    fn visit_cond_expr(&mut self, cond: &CondExpr) {
        self.consume(&cond.test);
        cond.visit_children_with(self);
    }

    fn visit_if_stmt(&mut self, statement: &IfStmt) {
        self.consume(&statement.test);
        statement.visit_children_with(self);
    }

    fn visit_expr_stmt(&mut self, statement: &ExprStmt) {
        self.consume(&statement.expr);
        statement.visit_children_with(self);
    }

    fn visit_class(&mut self, class: &Class) {
        if let Some(heritage) = &class.super_class {
            self.consume(heritage);
        }
        class.visit_children_with(self);
    }
}

pub fn census(
    module: &Module,
    unresolved: SyntaxContext,
    object_aliases: &FxHashMap<Id, String>,
) -> Census {
    let mut functions = Functions::default();
    module.visit_with(&mut functions);
    let Functions {
        mut params,
        declared,
        assigned,
    } = functions;
    // A binding that may hold another function receives arguments the
    // census cannot place.
    params.retain(|id, _| declared[id] == 1 && !assigned.contains(id));
    let mut flow = Flow {
        unresolved,
        taints: FxHashMap::default(),
        params,
    };
    loop {
        let mut pass = FlowPass {
            flow: &mut flow,
            changed: false,
        };
        module.visit_with(&mut pass);
        if !pass.changed {
            break;
        }
    }
    let mut visitor = Visitor {
        flow: &flow,
        object_aliases,
        census: Census::default(),
        occurrences: Vec::new(),
        used: FxHashSet::default(),
        exported: Vec::new(),
    };
    module.visit_with(&mut visitor);
    let unused = visitor
        .occurrences
        .iter()
        .filter(|(span, _)| !visitor.used.contains(span))
        .map(|(_, taint)| taint);
    let exported = visitor.exported.iter().filter_map(|id| flow.taints.get(id));
    for taint in unused.chain(exported) {
        for intrinsic in taint {
            match intrinsic {
                Intrinsic::GlobalObject => visitor.census.escaped_all = true,
                Intrinsic::Object(global) | Intrinsic::Prototype(global) => {
                    visitor.census.escaped.insert(global.clone());
                }
            }
        }
    }
    visitor.census
}
