//! A syntactic pre-pass: which top-level bindings are small, immutable
//! helper functions worth specializing per call site, and which are aliases
//! of `Object.<name>`.
//!
//! Bundler runtimes route every class field and re-export through a helper
//! of the form `(obj, key, value) => obj[key] = value`. Analyzed once, such
//! a helper merges every object and every key that reaches it; specialized
//! per call, its key is the call's string literal.

use rustc_hash::{FxHashMap, FxHashSet};
use swc_common::SyntaxContext;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

/// A helper is at most this many source bytes.
const HELPER_BYTES: u32 = 400;

#[derive(Clone, Copy)]
pub enum HelperFn<'a> {
    Arrow(&'a ArrowExpr),
    Function(&'a Function),
}

pub struct Helper<'a> {
    pub function: HelperFn<'a>,
    /// Every binding declared inside the helper.
    pub locals: FxHashSet<Id>,
    params: FxHashSet<Id>,
    keys: FxHashSet<Id>,
    calls: Vec<(Id, FxHashSet<Id>)>,
}

#[derive(Default)]
pub struct Prepass<'a> {
    pub helpers: FxHashMap<Id, Helper<'a>>,
    /// Bindings that only ever hold `Object.<name>`.
    pub object_aliases: FxHashMap<Id, String>,
    /// Bindings that only ever hold a member chain off a global identifier.
    pub global_aliases: FxHashSet<Id>,
}

/// Every identifier some assignment, update or loop head writes.
#[derive(Default)]
pub(crate) struct Assigned {
    ids: FxHashSet<Id>,
}

impl Assigned {
    fn pattern(&mut self, pat: &Pat) {
        match pat {
            Pat::Ident(binding) => {
                self.ids.insert(binding.id.to_id());
            }
            Pat::Array(array) => {
                for element in array.elems.iter().flatten() {
                    self.pattern(element);
                }
            }
            Pat::Rest(rest) => self.pattern(&rest.arg),
            Pat::Object(object) => {
                for prop in &object.props {
                    match prop {
                        ObjectPatProp::KeyValue(key_value) => self.pattern(&key_value.value),
                        ObjectPatProp::Assign(assign) => {
                            self.ids.insert(assign.key.id.to_id());
                        }
                        ObjectPatProp::Rest(rest) => self.pattern(&rest.arg),
                    }
                }
            }
            Pat::Assign(assign) => self.pattern(&assign.left),
            Pat::Expr(expr) => {
                if let Expr::Ident(ident) = &**expr {
                    self.ids.insert(ident.to_id());
                }
            }
            Pat::Invalid(_) => {}
        }
    }
}

impl Visit for Assigned {
    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        match &assign.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                self.ids.insert(binding.id.to_id());
            }
            AssignTarget::Pat(AssignTargetPat::Array(array)) => {
                self.pattern(&Pat::Array(array.clone()));
            }
            AssignTarget::Pat(AssignTargetPat::Object(object)) => {
                self.pattern(&Pat::Object(object.clone()));
            }
            _ => {}
        }
        assign.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        if let Expr::Ident(ident) = &*update.arg {
            self.ids.insert(ident.to_id());
        }
        update.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, looped: &ForInStmt) {
        if let ForHead::Pat(pat) = &looped.left {
            self.pattern(pat);
        }
        looped.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, looped: &ForOfStmt) {
        if let ForHead::Pat(pat) = &looped.left {
            self.pattern(pat);
        }
        looped.visit_children_with(self);
    }
}

/// Every identifier an assignment, update or loop head inside `node` writes.
pub(crate) fn assigned_ids(node: &impl VisitWith<Assigned>) -> FxHashSet<Id> {
    let mut assigned = Assigned::default();
    node.visit_with(&mut assigned);
    assigned.ids
}

/// What disqualifies a function from specialization, its bindings, and
/// where its identifiers are used as property keys.
struct Body {
    own: Id,
    unresolved: SyntaxContext,
    disqualified: bool,
    locals: FxHashSet<Id>,
    /// Identifiers inside a computed property key.
    keys: FxHashSet<Id>,
    /// Per call of an identifier: the callee and the identifiers in its
    /// arguments.
    calls: Vec<(Id, FxHashSet<Id>)>,
}

#[derive(Default)]
struct Idents {
    ids: FxHashSet<Id>,
}

impl Visit for Idents {
    fn visit_ident(&mut self, ident: &Ident) {
        self.ids.insert(ident.to_id());
    }
}

fn idents(node: &impl VisitWith<Idents>) -> FxHashSet<Id> {
    let mut collector = Idents::default();
    node.visit_with(&mut collector);
    collector.ids
}

impl Visit for Body {
    fn visit_ident(&mut self, ident: &Ident) {
        if ident.to_id() == self.own
            || (ident.ctxt == self.unresolved && &*ident.sym == "arguments")
        {
            self.disqualified = true;
        }
    }

    fn visit_binding_ident(&mut self, binding: &BindingIdent) {
        self.locals.insert(binding.id.to_id());
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        if let MemberProp::Computed(computed) = &member.prop {
            self.keys.extend(idents(&computed.expr));
        }
        member.visit_children_with(self);
    }

    fn visit_bin_expr(&mut self, binary: &BinExpr) {
        if binary.op == BinaryOp::In {
            self.keys.extend(idents(&binary.left));
        }
        binary.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Ident(callee) = &**callee
        {
            self.calls.push((callee.to_id(), idents(&call.args)));
        }
        call.visit_children_with(self);
    }

    fn visit_assign_pat_prop(&mut self, prop: &AssignPatProp) {
        self.locals.insert(prop.key.id.to_id());
        prop.value.visit_with(self);
    }

    fn visit_fn_decl(&mut self, function: &FnDecl) {
        self.locals.insert(function.ident.to_id());
        function.function.visit_with(self);
    }

    fn visit_fn_expr(&mut self, function: &FnExpr) {
        if let Some(ident) = &function.ident {
            self.locals.insert(ident.to_id());
        }
        function.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, class: &ClassDecl) {
        self.locals.insert(class.ident.to_id());
        class.class.visit_with(self);
    }

    fn visit_class_expr(&mut self, class: &ClassExpr) {
        if let Some(ident) = &class.ident {
            self.locals.insert(ident.to_id());
        }
        class.class.visit_with(self);
    }

    fn visit_this_expr(&mut self, _: &ThisExpr) {
        self.disqualified = true;
    }

    fn visit_super(&mut self, _: &Super) {
        self.disqualified = true;
    }

    fn visit_yield_expr(&mut self, _: &YieldExpr) {
        self.disqualified = true;
    }

    fn visit_await_expr(&mut self, _: &AwaitExpr) {
        self.disqualified = true;
    }

    fn visit_meta_prop_expr(&mut self, _: &MetaPropExpr) {
        self.disqualified = true;
    }
}

fn helper<'a>(
    own: &Ident,
    function: HelperFn<'a>,
    unresolved: SyntaxContext,
) -> Option<Helper<'a>> {
    let (span, suspendable) = match function {
        HelperFn::Arrow(arrow) => (arrow.span, arrow.is_async || arrow.is_generator),
        HelperFn::Function(function) => (function.span, function.is_async || function.is_generator),
    };
    if suspendable || span.hi.0 - span.lo.0 > HELPER_BYTES {
        return None;
    }
    let mut body = Body {
        own: own.to_id(),
        unresolved,
        disqualified: false,
        locals: FxHashSet::default(),
        keys: FxHashSet::default(),
        calls: Vec::new(),
    };
    let params: Vec<&Pat> = match function {
        HelperFn::Arrow(arrow) => arrow.params.iter().collect(),
        HelperFn::Function(function) => function.params.iter().map(|param| &param.pat).collect(),
    };
    let params = params
        .into_iter()
        .filter_map(|pat| match pat {
            Pat::Ident(binding) => Some(binding.id.to_id()),
            _ => None,
        })
        .collect();
    match function {
        HelperFn::Arrow(arrow) => arrow.visit_with(&mut body),
        HelperFn::Function(function) => function.visit_with(&mut body),
    }
    (!body.disqualified).then_some(Helper {
        function,
        locals: body.locals,
        params,
        keys: body.keys,
        calls: body.calls,
    })
}

fn global_chain(expr: &Expr, unresolved: SyntaxContext) -> bool {
    match expr {
        Expr::Paren(paren) => global_chain(&paren.expr, unresolved),
        Expr::Member(member) => global_chain(&member.obj, unresolved),
        Expr::Ident(ident) => ident.ctxt == unresolved,
        _ => false,
    }
}

pub fn prepass(module: &Module, unresolved: SyntaxContext) -> Prepass<'_> {
    let mut assigned = Assigned::default();
    module.visit_with(&mut assigned);
    let mut declarations: FxHashMap<Id, usize> = FxHashMap::default();
    let mut candidates: Vec<(&Ident, Option<&Expr>, Option<&Function>)> = Vec::new();
    for item in &module.body {
        let decl = match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => &export.decl,
            _ => continue,
        };
        match decl {
            Decl::Var(var) => {
                for declarator in &var.decls {
                    if let Pat::Ident(binding) = &declarator.name {
                        *declarations.entry(binding.id.to_id()).or_default() += 1;
                        candidates.push((&binding.id, declarator.init.as_deref(), None));
                    }
                }
            }
            Decl::Fn(function) => {
                *declarations.entry(function.ident.to_id()).or_default() += 1;
                candidates.push((&function.ident, None, Some(&function.function)));
            }
            _ => {}
        }
    }
    let mut out = Prepass::default();
    for (ident, init, function) in candidates {
        let id = ident.to_id();
        if declarations[&id] != 1 || assigned.ids.contains(&id) {
            continue;
        }
        if init.is_some_and(|init| global_chain(init, unresolved)) {
            out.global_aliases.insert(id.clone());
        }
        let function = match (init, function) {
            (_, Some(function)) => Some(HelperFn::Function(function)),
            (Some(Expr::Arrow(arrow)), _) => Some(HelperFn::Arrow(arrow)),
            (Some(Expr::Fn(function)), _) if function.ident.is_none() => {
                Some(HelperFn::Function(&function.function))
            }
            (
                Some(Expr::Member(MemberExpr {
                    obj,
                    prop: MemberProp::Ident(name),
                    ..
                })),
                _,
            ) => {
                if matches!(&**obj, Expr::Ident(object)
                    if object.ctxt == unresolved && &*object.sym == "Object")
                {
                    out.object_aliases.insert(id.clone(), name.sym.to_string());
                }
                None
            }
            _ => None,
        };
        if let Some(function) = function
            && let Some(helper) = helper(ident, function, unresolved)
        {
            out.helpers.insert(id, helper);
        }
    }
    // Only a helper that uses a parameter as a property key, directly or
    // through another such helper or `Object.defineProperty`, gains from
    // specialization.
    let mut keyed: FxHashSet<Id> = FxHashSet::default();
    loop {
        let before = keyed.len();
        for (id, helper) in &out.helpers {
            let uses_key = helper.params.iter().any(|param| {
                helper.keys.contains(param)
                    || helper.calls.iter().any(|(callee, args)| {
                        args.contains(param)
                            && (keyed.contains(callee)
                                || out
                                    .object_aliases
                                    .get(callee)
                                    .is_some_and(|name| name == "defineProperty"))
                    })
            });
            if uses_key {
                keyed.insert(id.clone());
            }
        }
        if keyed.len() == before {
            break;
        }
    }
    out.helpers.retain(|id, _| keyed.contains(id));
    out
}
