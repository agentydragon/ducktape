use std::collections::BTreeSet;

use analysis::facts::statement_facts::{EffectCell, StatementFacts};
use analyze::analyze_vendor_prune_chunk;
use anyhow::{Result, bail};
use binding_targets::{declaration_name_strings, public_export_name};
use js_ast::item_var_decl;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use super::shareable::{
    is_empty_object_var, is_shareable_function_var, is_shareable_global_object_fallback_var,
    is_shareable_intrinsic_alias_var, is_shareable_literal_expr, is_shareable_literal_var,
    is_shareable_vite_map_deps_var, is_static_member_call,
};

pub(super) struct ItemAnalysis {
    pub(super) declared: BTreeSet<Id>,
    pub(super) reads: BTreeSet<Id>,
    pub(super) local_effects: BTreeSet<Id>,
    pub(super) export_aliases: BTreeSet<String>,
    pub(super) side_effect: SideEffectKind,
    pub(super) shareable_helper: bool,
    /// Outer-observable storage cells this statement writes at-init
    /// (binding rebinds and static-key `globalThis.<prop>` writes).
    /// Empty/incomplete unless `effects_summarizable` is true.
    /// Carries the WRITE-cell view (`cell_writes_summarizable`):
    /// the strip's call side effects are covered by its own
    /// island-reachability analysis, so the S-chain's stronger
    /// opaque-call bail does not apply here.
    pub(super) observable_writes: BTreeSet<EffectCell>,
    /// False when the statement contains a shape the analyzer cannot
    /// statically summarize (dynamic `globalThis[expr]`, `with`,
    /// direct `eval`, `Function(...)`, etc.); in that case the write
    /// set is unreliable and the statement must be treated as touching
    /// every observable cell.
    pub(super) effects_summarizable: bool,
    /// Rebound bindings, eager or lazy.
    pub(super) rebinds: BTreeSet<Id>,
    /// Bindings the statement reads or rebinds at init, outside closures.
    pub(super) eager_bindings: BTreeSet<Id>,
    /// The statement's own at-init calls.
    pub(super) at_init: CallSites,
    /// Identifier callees anywhere in the statement, closures included.
    pub(super) lazy_calls: BTreeSet<Id>,
    /// Declares a function value directly (`function f`, `const f = () => …`).
    pub(super) direct_function: bool,
    /// What one call of the declared function or class runs first-order.
    pub(super) first_order: CallSites,
    /// Bindings that first-order body reads or rebinds.
    pub(super) first_order_bindings: BTreeSet<Id>,
}

/// Calls one code position makes: identifier callees, plus the bindings and
/// inline closures handed to calls that do not resolve to an identifier.
pub(super) struct CallSites {
    pub(super) calls: BTreeSet<Id>,
    pub(super) unresolved_sources: BTreeSet<Id>,
    pub(super) unresolved_inline_fn: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum SideEffectKind {
    None,
    LocalMutation,
    Hard,
}

pub(super) fn analyze_prune_items(module: &Module, chunk_path: &str) -> Result<Vec<ItemAnalysis>> {
    let facts = analyze_vendor_prune_chunk(module).facts;
    if facts.len() != module.body.len() {
        bail!(
            "strip_swapped_vendor_exports vendor entry {chunk_path}: analysis produced {} top-level facts for {} module items",
            facts.len(),
            module.body.len(),
        );
    }

    Ok(module
        .body
        .iter()
        .zip(facts.iter())
        .map(|(item, fact)| item_analysis_from_fact(item, fact))
        .collect())
}

fn item_analysis_from_fact(item: &ModuleItem, fact: &StatementFacts) -> ItemAnalysis {
    let mut reads = fact.reads.eager.clone();
    reads.extend(fact.reads.lazy.iter().cloned());
    reads.extend(fact.rebinds.eager.iter().cloned());
    reads.extend(fact.rebinds.lazy.iter().cloned());
    // Package ownership follows lexical dependencies. The source read set
    // already includes call expressions, their arguments, and nested bodies.
    // Inferred call targets describe possible runtime behavior; a callback
    // passed to another invocation of the same helper must not make this
    // package own that unrelated callback's implementation.
    for id in &fact.declared {
        reads.remove(id);
    }

    let side_effect = if module_linkage_item(item) || !fact.purity.is_pure() {
        SideEffectKind::Hard
    } else if !fact.local_effects.is_empty() {
        SideEffectKind::LocalMutation
    } else {
        SideEffectKind::None
    };

    ItemAnalysis {
        declared: fact.declared.clone(),
        reads,
        local_effects: fact.local_effects.clone(),
        export_aliases: export_aliases_for_item(item),
        side_effect,
        shareable_helper: item_is_shareable_helper(item),
        observable_writes: fact.effects().writes,
        effects_summarizable: fact.cell_writes_summarizable(),
        rebinds: fact
            .rebinds
            .eager
            .iter()
            .chain(&fact.rebinds.lazy)
            .cloned()
            .collect(),
        eager_bindings: fact
            .reads
            .eager
            .iter()
            .chain(&fact.rebinds.eager)
            .cloned()
            .collect(),
        at_init: CallSites {
            calls: fact.calls.eager.clone(),
            unresolved_sources: fact.at_init_unresolved_sources.clone(),
            unresolved_inline_fn: fact.at_init_unresolved_inline_fn,
        },
        lazy_calls: fact.calls.lazy.clone(),
        direct_function: fact.declares_direct_function,
        first_order: CallSites {
            calls: fact.first_order_calls().clone(),
            unresolved_sources: fact.first_order_unresolved_sources().clone(),
            unresolved_inline_fn: fact.first_order_unresolved_inline_fn(),
        },
        first_order_bindings: fact
            .first_order_reads()
            .iter()
            .chain(fact.first_order_rebinds())
            .cloned()
            .collect(),
    }
}

fn module_linkage_item(item: &ModuleItem) -> bool {
    matches!(
        item,
        ModuleItem::ModuleDecl(ModuleDecl::Import(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(NamedExport { src: Some(_), .. }))
    )
}

fn export_aliases_for_item(item: &ModuleItem) -> BTreeSet<String> {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            declaration_name_strings(&export_decl.decl)
                .into_iter()
                .collect()
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none() => {
            export_aliases_from_named(named)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => {
            BTreeSet::from(["default".to_string()])
        }
        _ => BTreeSet::new(),
    }
}

pub(super) fn item_is_shareable_helper(item: &ModuleItem) -> bool {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_)))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Fn(_), ..
        })) => true,
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Var(var),
            ..
        })) => {
            is_shareable_literal_var(var)
                || is_shareable_function_var(var)
                || is_shareable_intrinsic_alias_var(var)
                || is_shareable_vite_map_deps_var(var)
                || is_shareable_global_object_fallback_var(var)
        }
        _ => false,
    }
}

/// One synchronous run of `body`'s declared function, class constructor or
/// at-init closure: its first-order bindings, callees and unresolved-call
/// arguments. Everything lexically inside it counts when it hands a closure to
/// an unresolved call or calls a function declared inside itself, whose body
/// the first-order view does not cover.
pub(super) fn run_first_order_body<'a>(
    body: &'a ItemAnalysis,
    item: &ModuleItem,
    reached: &mut Vec<&'a Id>,
    lexical: &mut Vec<&'a Id>,
    callees: &mut Vec<&'a Id>,
) {
    reached.extend(&body.first_order_bindings);
    lexical.extend(&body.first_order.unresolved_sources);
    callees.extend(&body.first_order.calls);
    if body.first_order.unresolved_inline_fn || {
        let mut nested = NestedFunctionNames::default();
        item.visit_children_with(&mut nested);
        body.first_order
            .calls
            .iter()
            .any(|callee| nested.0.contains(callee))
    } {
        reached.extend(&body.reads);
        callees.extend(&body.lazy_calls);
    }
}

/// The at-init calls of one statement whose receiver is host code: a member
/// call rooted at a name neither the chunk nor the statement declares
/// (`__REACT_DEVTOOLS_GLOBAL_HOOK__.inject(internals)`), directly or through a
/// variable the statement initializes from such a name. Calls inside nested
/// functions are not at init and are not inspected.
#[derive(Default)]
pub(super) struct HostCalls {
    /// Names declared inside the statement: parameters and nested bindings.
    pub(super) locals: BTreeSet<Id>,
    /// Statement variables initialized from a host name or a property path on
    /// one (`var hook = globalThis.__HOOK__`).
    pub(super) host_aliases: BTreeSet<Id>,
    /// Names appearing in arguments of host calls.
    pub(super) to_host: BTreeSet<Id>,
    /// Names appearing anywhere else in an at-init call.
    pub(super) elsewhere: BTreeSet<Id>,
    pub(super) host_closures: usize,
    pub(super) other_closures: usize,
}

impl HostCalls {
    pub(super) fn of(item: &ModuleItem, chunk_declares: impl Fn(&Id) -> bool) -> Self {
        let mut locals = LocalNames::default();
        item.visit_with(&mut locals);
        let mut calls = HostCallScan {
            calls: HostCalls {
                locals: locals.0,
                ..HostCalls::default()
            },
            chunk_declares: &chunk_declares,
            depth: 0,
        };
        item.visit_with(&mut calls);
        calls.calls
    }

    pub(super) fn handed_only_to_host(&self, id: &Id) -> bool {
        self.to_host.contains(id) && !self.elsewhere.contains(id)
    }

    pub(super) fn only_host_closures(&self) -> bool {
        self.host_closures > 0 && self.other_closures == 0
    }
}

#[derive(Default)]
struct LocalNames(BTreeSet<Id>);

impl Visit for LocalNames {
    fn visit_function(&mut self, function: &Function) {
        let mut inner = BindingNames(&mut self.0);
        function.visit_children_with(&mut inner);
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        let mut inner = BindingNames(&mut self.0);
        arrow.visit_children_with(&mut inner);
    }

    fn visit_catch_clause(&mut self, clause: &CatchClause) {
        let mut inner = BindingNames(&mut self.0);
        clause.visit_children_with(&mut inner);
    }
}

struct BindingNames<'a>(&'a mut BTreeSet<Id>);

impl Visit for BindingNames<'_> {
    fn visit_binding_ident(&mut self, ident: &BindingIdent) {
        self.0.insert(ident.id.to_id());
    }

    fn visit_fn_decl(&mut self, decl: &FnDecl) {
        self.0.insert(decl.ident.to_id());
        decl.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, decl: &ClassDecl) {
        self.0.insert(decl.ident.to_id());
        decl.class.visit_with(self);
    }
}

struct HostCallScan<'a, F> {
    calls: HostCalls,
    chunk_declares: &'a F,
    depth: u32,
}

impl<F: Fn(&Id) -> bool> HostCallScan<'_, F> {
    fn is_host(&self, ident: &Ident) -> bool {
        let id = ident.to_id();
        self.calls.host_aliases.contains(&id)
            || (!(self.chunk_declares)(&id) && !self.calls.locals.contains(&id))
    }

    fn record_arguments(&mut self, args: &[ExprOrSpread], host: bool) {
        for arg in args {
            let mut names = AllNames::default();
            arg.expr.visit_with(&mut names);
            let closure = matches!(arg.expr.unwrap_parens(), Expr::Fn(_) | Expr::Arrow(_));
            if host {
                self.calls.to_host.extend(names.0);
                self.calls.host_closures += usize::from(closure);
            } else {
                self.calls.elsewhere.extend(names.0);
                self.calls.other_closures += usize::from(closure);
            }
        }
    }
}

impl<F: Fn(&Id) -> bool> Visit for HostCallScan<'_, F> {
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

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if self.depth == 0
            && let Pat::Ident(name) = &declarator.name
            && let Some(init) = declarator.init.as_deref().and_then(member_root)
            && self.is_host(init)
        {
            self.calls.host_aliases.insert(name.id.to_id());
        }
        declarator.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if self.depth == 0 {
            let host = match &call.callee {
                Callee::Expr(callee) => match callee.unwrap_parens() {
                    Expr::Member(member) => {
                        member_root(&member.obj).is_some_and(|root| self.is_host(root))
                    }
                    _ => false,
                },
                _ => false,
            };
            if !host && let Callee::Expr(callee) = &call.callee {
                let mut names = AllNames::default();
                callee.visit_with(&mut names);
                self.calls.elsewhere.extend(names.0);
            }
            self.record_arguments(&call.args, host);
        }
        call.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, new: &NewExpr) {
        if self.depth == 0
            && let Some(args) = &new.args
        {
            self.record_arguments(args, false);
        }
        new.visit_children_with(self);
    }

    fn visit_tagged_tpl(&mut self, tpl: &TaggedTpl) {
        if self.depth == 0 {
            let mut names = AllNames::default();
            tpl.visit_with(&mut names);
            self.calls.elsewhere.extend(names.0);
            self.calls.other_closures += 1;
        }
        tpl.visit_children_with(self);
    }
}

#[derive(Default)]
struct AllNames(BTreeSet<Id>);

impl Visit for AllNames {
    fn visit_ident(&mut self, ident: &Ident) {
        self.0.insert(ident.to_id());
    }
}

/// An item followed into the swap-privacy region, and whether its code may run
/// (`false`: only the values it holds are followed).
pub(super) type RegionStep = (usize, bool);

/// How code uses bindings' values: every occurrence except a plain property
/// read (`x.a`, `x.a.b`), `typeof`, or an identity check (`===`, `!==`,
/// `instanceof`) exposes the binding — a member write
/// or `delete` rooted at it, a method call on it (the callee gets it as
/// `this`), or any use that hands the value on. Names are compared without
/// scope resolution, so a shadowing local only adds exposures.
#[derive(Default)]
pub(super) struct ExposedBindings {
    /// Every exposing use.
    pub(super) uses: BTreeSet<Id>,
    /// Roots of in-place mutations: member writes, `delete`, method calls.
    pub(super) mutated: BTreeSet<Id>,
    /// Leave function and arrow bodies out.
    pub(super) skip_closures: bool,
}

impl ExposedBindings {
    fn mutate(&mut self, root: &Ident) {
        self.uses.insert(root.to_id());
        self.mutated.insert(root.to_id());
    }
}

fn member_root(expr: &Expr) -> Option<&Ident> {
    match expr {
        Expr::Ident(ident) => Some(ident),
        Expr::Member(member) => member_root(&member.obj),
        Expr::Paren(paren) => member_root(&paren.expr),
        _ => None,
    }
}

impl Visit for ExposedBindings {
    fn visit_ident(&mut self, ident: &Ident) {
        self.uses.insert(ident.to_id());
    }

    fn visit_function(&mut self, function: &Function) {
        if !self.skip_closures {
            function.visit_children_with(self);
        }
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        if !self.skip_closures {
            arrow.visit_children_with(self);
        }
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        self.visit_read_chain(&member.obj);
        member.prop.visit_with(self);
    }

    // Declared and rebound names are not values handed on; rebinds are
    // counted separately (`rebound`).
    fn visit_binding_ident(&mut self, _: &BindingIdent) {}

    fn visit_member_prop(&mut self, prop: &MemberProp) {
        if let MemberProp::Computed(computed) = prop {
            computed.visit_with(self);
        }
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let Some(root) = member_root(&member.obj)
        {
            self.mutate(root);
        }
        call.visit_children_with(self);
    }

    fn visit_simple_assign_target(&mut self, target: &SimpleAssignTarget) {
        if let SimpleAssignTarget::Member(member) = target
            && let Some(root) = member_root(&member.obj)
        {
            self.mutate(root);
        }
        target.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        if let Expr::Member(member) = &*update.arg
            && let Some(root) = member_root(&member.obj)
        {
            self.mutate(root);
        }
        update.visit_children_with(self);
    }

    // Identity checks read a value without handing it on.
    fn visit_bin_expr(&mut self, bin: &BinExpr) {
        if matches!(
            bin.op,
            BinaryOp::EqEqEq | BinaryOp::NotEqEq | BinaryOp::InstanceOf
        ) {
            self.visit_read_chain(&bin.left);
            self.visit_read_chain(&bin.right);
        } else {
            bin.visit_children_with(self);
        }
    }

    fn visit_unary_expr(&mut self, unary: &UnaryExpr) {
        if unary.op == UnaryOp::TypeOf {
            self.visit_read_chain(&unary.arg);
            return;
        }
        if unary.op == UnaryOp::Delete
            && let Expr::Member(member) = &*unary.arg
            && let Some(root) = member_root(&member.obj)
        {
            self.mutate(root);
        }
        unary.visit_children_with(self);
    }
}

impl ExposedBindings {
    /// The object of a property read: its root binding stays unexposed,
    /// computed keys along the chain are ordinary uses.
    fn visit_read_chain(&mut self, expr: &Expr) {
        match expr {
            Expr::Ident(_) => {}
            Expr::Member(member) => {
                self.visit_read_chain(&member.obj);
                member.prop.visit_with(self);
            }
            Expr::Paren(paren) => self.visit_read_chain(&paren.expr),
            _ => expr.visit_with(self),
        }
    }
}

/// Function names declared inside an item's own bodies.
#[derive(Default)]
struct NestedFunctionNames(BTreeSet<Id>);

impl Visit for NestedFunctionNames {
    fn visit_function(&mut self, function: &Function) {
        let mut inner = NestedFunctionDecls(&mut self.0);
        function.body.visit_with(&mut inner);
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        let mut inner = NestedFunctionDecls(&mut self.0);
        arrow.body.visit_with(&mut inner);
    }
}

struct NestedFunctionDecls<'a>(&'a mut BTreeSet<Id>);

impl Visit for NestedFunctionDecls<'_> {
    fn visit_fn_decl(&mut self, decl: &FnDecl) {
        self.0.insert(decl.ident.to_id());
        decl.function.visit_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let Pat::Ident(name) = &declarator.name
            && matches!(
                declarator.init.as_deref(),
                Some(Expr::Fn(_) | Expr::Arrow(_))
            )
        {
            self.0.insert(name.id.to_id());
        }
        declarator.visit_children_with(self);
    }
}

/// A declaration whose value is a function or a primitive: nothing a callee
/// can mutate in place (a function's body effects are followed separately).
pub(super) fn item_declares_code_or_primitive(item: &ModuleItem) -> bool {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_)))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Fn(_), ..
        })) => true,
        _ => item_var_decl(item).is_some_and(|var| {
            is_shareable_literal_var(var)
                || is_shareable_function_var(var)
                || is_shareable_intrinsic_alias_var(var)
                || is_symbol_var(var)
                || var.decls.iter().all(|decl| {
                    decl.init
                        .as_deref()
                        .is_some_and(js_ast::has_definite_primitive_result)
                })
        }),
    }
}

/// `var seen = new Set()` (or `Map`, `WeakMap`, `WeakSet`, `[]`): a fresh
/// native container. Its methods run no chunk code beyond the callbacks a
/// call hands them, which the calling statement's own facts cover. Assumes
/// the standard globals are not shadowed or patched (the `predefined_globals`
/// host claim).
pub(super) fn item_declares_native_container(item: &ModuleItem) -> bool {
    item_var_decl(item).is_some_and(|var| {
        var.decls
            .iter()
            .all(|decl| match decl.init.as_deref().map(Expr::unwrap_parens) {
                Some(Expr::New(new)) => {
                    matches!(&*new.callee, Expr::Ident(ident)
                    if matches!(ident.sym.as_ref(), "Set" | "Map" | "WeakMap" | "WeakSet"))
                        && new.args.as_ref().is_none_or(|args| args.is_empty())
                }
                Some(Expr::Array(array)) => array.elems.is_empty(),
                _ => false,
            })
    })
}

/// `var key = Symbol("description")` (or `Symbol.for(...)`): a primitive.
fn is_symbol_var(var: &VarDecl) -> bool {
    var.decls.iter().all(|decl| {
        let Some(Expr::Call(call)) = decl.init.as_deref().map(Expr::unwrap_parens) else {
            return false;
        };
        let Callee::Expr(callee) = &call.callee else {
            return false;
        };
        let is_symbol = |expr: &Expr| matches!(expr, Expr::Ident(ident) if ident.sym == *"Symbol");
        let symbol_callee = match callee.unwrap_parens() {
            Expr::Member(member) => {
                is_symbol(&member.obj)
                    && matches!(&member.prop, MemberProp::Ident(prop) if prop.sym == *"for")
            }
            expr => is_symbol(expr),
        };
        symbol_callee
            && call
                .args
                .iter()
                .all(|arg| arg.spread.is_none() && is_shareable_literal_expr(&arg.expr))
    })
}

pub(super) fn item_is_vite_preload_dependency_cell(item: &ModuleItem) -> bool {
    let Some(var) = item_var_decl(item) else {
        return false;
    };
    is_shareable_literal_var(var) || is_shareable_function_var(var) || is_empty_object_var(var)
}

pub(super) fn item_is_vite_preload_helper(item: &ModuleItem) -> bool {
    let Some(var) = item_var_decl(item) else {
        return false;
    };
    let [decl] = var.decls.as_slice() else {
        return false;
    };
    let Some(init) = decl.init.as_deref() else {
        return false;
    };
    let Some(body) = function_like_body(init) else {
        return false;
    };
    let mut probe = VitePreloadProbe::default();
    body.visit_with(&mut probe);
    probe.promise_all_settled && probe.document_create_element && probe.window_dispatch_event
}

fn function_like_body(expr: &Expr) -> Option<&FunctionBody> {
    match expr {
        Expr::Fn(function) => function.function.body.as_ref(),
        Expr::Arrow(arrow) => match arrow.body.as_ref() {
            ArrowFunctionBody::FunctionBody(body) => Some(body),
            ArrowFunctionBody::Expr(_) => None,
        },
        _ => None,
    }
}

#[derive(Default)]
struct VitePreloadProbe {
    promise_all_settled: bool,
    document_create_element: bool,
    window_dispatch_event: bool,
}

impl Visit for VitePreloadProbe {
    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee {
            self.promise_all_settled |= is_static_member_call(callee, "Promise", "allSettled");
            self.document_create_element |=
                is_static_member_call(callee, "document", "createElement");
            self.window_dispatch_event |= is_static_member_call(callee, "window", "dispatchEvent");
        }
        call.visit_children_with(self);
    }
}

pub(super) fn static_member_name(prop: &MemberProp) -> Option<String> {
    match prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::PrivateName(name) => Some(name.name.to_string()),
        MemberProp::Computed(computed) => match &*computed.expr {
            Expr::Lit(Lit::Str(value)) => Some(value.value.to_string_lossy().into_owned()),
            _ => None,
        },
    }
}

fn export_aliases_from_named(named: &NamedExport) -> BTreeSet<String> {
    named
        .specifiers
        .iter()
        .filter_map(|spec| match spec {
            ExportSpecifier::Named(named_spec) => Some(public_export_name(named_spec)),
            _ => None,
        })
        .collect()
}

pub(super) fn id_name(id: &Id) -> String {
    id.0.to_string()
}
