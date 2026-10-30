//! Callable reachability of host-loader sites and uses of loader-valued
//! bindings (SPEC.md § Loader-closed derivation).
//!
//! A function can run only after the code that creates it runs. A function
//! expression, arrow, method, accessor, constructor or class initializer is
//! reachable when its enclosing code is. A function declaration, or a binding
//! initialized with a function expression or arrow, is reachable when its
//! enclosing code is and its binding escapes (any reference other than a
//! direct call, including an export) or a reachable owner calls it directly.
//! Top-level code is reachable. A site whose innermost function is not
//! reachable can never run. The analysis is per module: an imported function
//! is an export of its defining module and escapes there.
//!
//! The same walk records every use of an import binding and of a top-level
//! `X = import.meta.require` declaration, so a loader exported under a name
//! can be followed to each module that calls it.

use std::collections::{BTreeSet, HashMap, HashSet};

use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::loader_phase::{SiteKey, site_key};
use js_ast::str_value;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
enum Owner {
    TopLevel,
    Function(usize),
}

struct FunctionNode {
    parent: Owner,
    /// The binding of a declaration-like function; `None` for a function value
    /// created by an expression it cannot be separated from.
    binding: Option<Id>,
}

/// One use of a tracked binding.
#[derive(Clone, Debug)]
pub(crate) struct TrackedUse {
    pub item_index: usize,
    pub key: SiteKey,
    /// `Some` for a direct call whose only argument is a string literal.
    pub literal: Option<String>,
    /// A direct call, literal or not.
    pub call: bool,
}

/// A binding whose uses the walk records.
pub(crate) enum TrackedBinding {
    /// `import { imported as local } from source` (or `import local from`).
    Import { source: String, imported: String },
    /// Top-level `var`/`let`/`const local = import.meta.require`.
    LoaderAlias { definition: SiteKey },
}

pub(crate) struct TrackedBindingUses {
    pub binding: TrackedBinding,
    pub uses: Vec<TrackedUse>,
    /// Names a local `export { local as name }` publishes it under.
    pub exported_as: Vec<String>,
}

pub(crate) struct LoaderReach {
    /// Requested site keys that can never run.
    pub unreachable: BTreeSet<SiteKey>,
    pub tracked: Vec<TrackedBindingUses>,
    /// `import * as`, `export * from` and `export { … } from` sources: each
    /// exposes the source's whole export surface.
    pub namespace_sources: Vec<(usize, String)>,
}

/// Analyze one module. `sites` are the census sites; their reachability and
/// that of every tracked use is computed. Dynamic scope (direct `eval`,
/// `with`) makes every function reachable and every tracked binding escape.
pub(crate) fn loader_reach(
    module: &Module,
    sites: &BTreeSet<SiteKey>,
    dynamic_scope: bool,
) -> LoaderReach {
    let mut walk = ReachWalk {
        sites,
        item_index: 0,
        owners: vec![Owner::TopLevel],
        functions: Vec::new(),
        site_owner: HashMap::new(),
        use_owner: HashMap::new(),
        escaped: HashSet::new(),
        calls: Vec::new(),
        declared_functions: HashMap::new(),
        tracked_index: HashMap::new(),
        tracked: Vec::new(),
        namespace_sources: Vec::new(),
    };
    walk.register_tracked_bindings(module);
    for (index, item) in module.body.iter().enumerate() {
        walk.item_index = index;
        item.visit_with(&mut walk);
    }
    let reachable = if dynamic_scope {
        vec![true; walk.functions.len()]
    } else {
        walk.reachable_functions()
    };
    let is_reachable = |owner: Owner| match owner {
        Owner::TopLevel => true,
        Owner::Function(index) => reachable[index],
    };
    let unreachable = walk
        .site_owner
        .iter()
        .filter(|(_, owner)| !is_reachable(**owner))
        .map(|(key, _)| *key)
        .collect();
    let mut tracked = walk.tracked;
    for binding in &mut tracked {
        binding
            .uses
            .retain(|tracked_use| is_reachable(walk.use_owner[&tracked_use.key]));
        if dynamic_scope {
            // Dynamic scope can read the binding under any name.
            binding.uses.push(TrackedUse {
                item_index: 0,
                key: (0, 0),
                literal: None,
                call: false,
            });
        }
    }
    LoaderReach {
        unreachable,
        tracked,
        namespace_sources: walk.namespace_sources,
    }
}

struct ReachWalk<'a> {
    sites: &'a BTreeSet<SiteKey>,
    item_index: usize,
    owners: Vec<Owner>,
    functions: Vec<FunctionNode>,
    site_owner: HashMap<SiteKey, Owner>,
    use_owner: HashMap<SiteKey, Owner>,
    /// Bindings referenced other than as a direct callee.
    escaped: HashSet<Id>,
    calls: Vec<(Owner, Id)>,
    /// Declaration-like function bindings; `None` when declared more than
    /// once, which this analysis does not separate.
    declared_functions: HashMap<Id, Option<usize>>,
    tracked_index: HashMap<Id, usize>,
    tracked: Vec<TrackedBindingUses>,
    namespace_sources: Vec<(usize, String)>,
}

fn is_import_meta_require(expr: &Expr) -> bool {
    matches!(
        expr.unwrap_parens(),
        Expr::Member(member)
            if matches!(&*member.obj, Expr::MetaProp(meta) if meta.kind == MetaPropKind::ImportMeta)
                && matches!(&member.prop, MemberProp::Ident(prop) if prop.sym == *"require")
    )
}

impl ReachWalk<'_> {
    fn owner(&self) -> Owner {
        *self.owners.last().expect("top-level owner")
    }

    fn register_tracked_bindings(&mut self, module: &Module) {
        for (item_index, item) in module.body.iter().enumerate() {
            match item {
                ModuleItem::ModuleDecl(ModuleDecl::Import(import)) if !import.type_only => {
                    let source = str_value(&import.src);
                    for specifier in &import.specifiers {
                        let (local, imported) = match specifier {
                            ImportSpecifier::Named(named) if !named.is_type_only => (
                                &named.local,
                                named.imported.as_ref().map_or_else(
                                    || named.local.sym.to_string(),
                                    |name| match name {
                                        ModuleExportName::Ident(ident) => ident.sym.to_string(),
                                        ModuleExportName::Str(value) => str_value(value),
                                    },
                                ),
                            ),
                            ImportSpecifier::Default(default) => {
                                (&default.local, "default".to_string())
                            }
                            ImportSpecifier::Namespace(_) => {
                                self.namespace_sources.push((item_index, source.clone()));
                                continue;
                            }
                            ImportSpecifier::Named(_) => continue,
                        };
                        self.track(
                            local.to_id(),
                            TrackedBinding::Import {
                                source: source.clone(),
                                imported,
                            },
                        );
                    }
                }
                ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))
                | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                    decl: Decl::Var(var),
                    ..
                })) => {
                    for declarator in &var.decls {
                        if let Pat::Ident(name) = &declarator.name
                            && let Some(init) = &declarator.init
                            && is_import_meta_require(init)
                        {
                            self.track(
                                name.id.to_id(),
                                TrackedBinding::LoaderAlias {
                                    definition: site_key(init.unwrap_parens().span()),
                                },
                            );
                        }
                    }
                }
                _ => {}
            }
        }
    }

    fn track(&mut self, id: Id, binding: TrackedBinding) {
        self.tracked_index.insert(id, self.tracked.len());
        self.tracked.push(TrackedBindingUses {
            binding,
            uses: Vec::new(),
            exported_as: Vec::new(),
        });
    }

    fn record_use(&mut self, id: &Id, key: SiteKey, literal: Option<String>, call: bool) -> bool {
        let Some(&index) = self.tracked_index.get(id) else {
            return false;
        };
        self.use_owner.insert(key, self.owner());
        self.tracked[index].uses.push(TrackedUse {
            item_index: self.item_index,
            key,
            literal,
            call,
        });
        true
    }

    fn new_function(&mut self, binding: Option<Id>) -> usize {
        let index = self.functions.len();
        if let Some(id) = &binding {
            self.declared_functions
                .entry(id.clone())
                .and_modify(|existing| *existing = None)
                .or_insert(Some(index));
        }
        self.functions.push(FunctionNode {
            parent: self.owner(),
            binding,
        });
        index
    }

    fn in_function(&mut self, binding: Option<Id>, walk: impl FnOnce(&mut Self)) {
        let index = self.new_function(binding);
        self.owners.push(Owner::Function(index));
        walk(self);
        self.owners.pop();
    }

    fn reachable_functions(&self) -> Vec<bool> {
        let mut callers: HashMap<&Id, Vec<Owner>> = HashMap::new();
        for (owner, callee) in &self.calls {
            callers.entry(callee).or_default().push(*owner);
        }
        let mut reachable = vec![false; self.functions.len()];
        loop {
            let mut changed = false;
            for (index, function) in self.functions.iter().enumerate() {
                if reachable[index] {
                    continue;
                }
                let owner_reachable = |owner: Owner| match owner {
                    Owner::TopLevel => true,
                    Owner::Function(other) => reachable[other],
                };
                if !owner_reachable(function.parent) {
                    continue;
                }
                let entered = match &function.binding {
                    None => true,
                    Some(id) => {
                        self.declared_functions.get(id) != Some(&Some(index))
                            || self.escaped.contains(id)
                            || callers
                                .get(id)
                                .is_some_and(|owners| owners.iter().any(|o| owner_reachable(*o)))
                    }
                };
                if entered {
                    reachable[index] = true;
                    changed = true;
                }
            }
            if !changed {
                return reachable;
            }
        }
    }
}

impl Visit for ReachWalk<'_> {
    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    fn visit_export_all(&mut self, node: &ExportAll) {
        self.namespace_sources
            .push((self.item_index, str_value(&node.src)));
    }

    fn visit_named_export(&mut self, node: &NamedExport) {
        if let Some(src) = &node.src {
            self.namespace_sources
                .push((self.item_index, str_value(src)));
            return;
        }
        for specifier in &node.specifiers {
            if let ExportSpecifier::Named(named) = specifier
                && let ModuleExportName::Ident(orig) = &named.orig
                && let Some(&index) = self.tracked_index.get(&orig.to_id())
                && matches!(
                    self.tracked[index].binding,
                    TrackedBinding::LoaderAlias { .. }
                )
            {
                let exported = match named.exported.as_ref().unwrap_or(&named.orig) {
                    ModuleExportName::Ident(ident) => ident.sym.to_string(),
                    ModuleExportName::Str(value) => str_value(value),
                };
                self.tracked[index].exported_as.push(exported);
                continue;
            }
            specifier.visit_with(self);
        }
    }

    fn visit_export_decl(&mut self, node: &ExportDecl) {
        match &node.decl {
            Decl::Fn(function) => {
                self.escaped.insert(function.ident.to_id());
            }
            Decl::Var(var) => {
                for declarator in &var.decls {
                    if let Pat::Ident(name) = &declarator.name {
                        let id = name.id.to_id();
                        self.escaped.insert(id.clone());
                        if let Some(&index) = self.tracked_index.get(&id) {
                            self.tracked[index].exported_as.push(id.0.to_string());
                        }
                    }
                }
            }
            _ => {}
        }
        node.decl.visit_with(self);
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        self.in_function(Some(node.ident.to_id()), |walk| {
            node.function.visit_children_with(walk)
        });
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Pat::Ident(name) = &node.name {
            let id = name.id.to_id();
            match node.init.as_deref().map(Expr::unwrap_parens) {
                Some(Expr::Fn(function)) => {
                    self.in_function(Some(id), |walk| function.function.visit_children_with(walk));
                    return;
                }
                Some(Expr::Arrow(arrow)) => {
                    self.in_function(Some(id), |walk| arrow.visit_children_with(walk));
                    return;
                }
                Some(init)
                    if self.tracked_index.get(&id).is_some_and(|&index| {
                        matches!(
                            self.tracked[index].binding,
                            TrackedBinding::LoaderAlias { definition }
                                if definition == site_key(init.span())
                        )
                    }) =>
                {
                    // The alias definition itself; its census site is
                    // classified with the binding.
                    return;
                }
                // Any other declarator of a tracked name (a repeated `var`)
                // writes it.
                _ => {
                    self.record_use(&id, site_key(name.id.span), None, false);
                }
            }
            // The declared name is not a reference.
            node.init.visit_with(self);
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        self.in_function(None, |walk| node.visit_children_with(walk));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.in_function(None, |walk| node.visit_children_with(walk));
    }

    fn visit_constructor(&mut self, node: &Constructor) {
        node.key.visit_with(self);
        self.in_function(None, |walk| {
            node.params.visit_with(walk);
            node.body.visit_with(walk);
        });
    }

    fn visit_class_prop(&mut self, node: &ClassProp) {
        node.key.visit_with(self);
        self.in_function(None, |walk| node.value.visit_with(walk));
    }

    fn visit_private_prop(&mut self, node: &PrivateProp) {
        self.in_function(None, |walk| node.value.visit_with(walk));
    }

    fn visit_static_block(&mut self, node: &StaticBlock) {
        self.in_function(None, |walk| node.body.visit_with(walk));
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        // The class name is a declaration, not a reference.
        node.class.visit_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Ident(ident) = &**callee
        {
            let id = ident.to_id();
            let literal = match node.args.as_slice() {
                [arg] if arg.spread.is_none() => match arg.expr.unwrap_parens() {
                    Expr::Lit(Lit::Str(value)) => Some(str_value(value)),
                    _ => None,
                },
                _ => None,
            };
            if !self.record_use(&id, site_key(node.span), literal, true) {
                self.calls.push((self.owner(), id));
            }
            node.args.visit_with(self);
        } else {
            node.visit_children_with(self);
        }
        if self.sites.contains(&site_key(node.span)) {
            self.site_owner.insert(site_key(node.span), self.owner());
        }
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        node.visit_children_with(self);
        if self.sites.contains(&site_key(node.span)) {
            self.site_owner.insert(site_key(node.span), self.owner());
        }
    }

    fn visit_meta_prop_expr(&mut self, node: &MetaPropExpr) {
        if self.sites.contains(&site_key(node.span)) {
            self.site_owner.insert(site_key(node.span), self.owner());
        }
    }

    fn visit_ident(&mut self, node: &Ident) {
        let key = site_key(node.span);
        if self.sites.contains(&key) {
            self.site_owner.insert(key, self.owner());
        }
        let id = node.to_id();
        if !self.record_use(&id, key, None, false) {
            self.escaped.insert(id);
        }
    }
}

/// Reachability and suspension of a tracked use, keyed like a census site.
pub(crate) fn tracked_use_keys(reach: &LoaderReach) -> BTreeSet<SiteKey> {
    reach
        .tracked
        .iter()
        .flat_map(|binding| binding.uses.iter().map(|tracked_use| tracked_use.key))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::GLOBALS;

    /// Whether each `import.meta.require(...)` call can run, in source order.
    fn reachable(source: &str) -> Vec<bool> {
        GLOBALS.set(&Default::default(), || {
            let parsed = js_ast::parse_js_module("fixture.js", source).expect("parse");
            let mut calls = LoaderCalls(Vec::new());
            parsed.module.visit_with(&mut calls);
            let sites: BTreeSet<_> = calls.0.iter().copied().collect();
            let reach = loader_reach(&parsed.module, &sites, false);
            calls
                .0
                .iter()
                .map(|site| !reach.unreachable.contains(site))
                .collect()
        })
    }

    struct LoaderCalls(Vec<SiteKey>);

    impl Visit for LoaderCalls {
        fn visit_call_expr(&mut self, node: &CallExpr) {
            node.visit_children_with(self);
            if let Callee::Expr(callee) = &node.callee
                && is_import_meta_require(callee)
            {
                self.0.push(site_key(node.span));
            }
        }
    }

    #[test]
    fn a_site_runs_only_if_its_function_can_be_entered() {
        for (source, expected) in [
            ("import.meta.require('a');", vec![true]),
            ("function f() { import.meta.require('a'); }", vec![false]),
            (
                "function f() { import.meta.require('a'); } f();",
                vec![true],
            ),
            (
                "function f() { import.meta.require('a'); } function g() { f(); }",
                vec![false],
            ),
            (
                "function f() { import.meta.require('a'); } function g() { f(); } g();",
                vec![true],
            ),
            (
                "function f() { import.meta.require('a'); } globalThis.f = f;",
                vec![true],
            ),
            (
                "export function f() { import.meta.require('a'); }",
                vec![true],
            ),
            (
                "function f() { import.meta.require('a'); } export { f };",
                vec![true],
            ),
            (
                "const f = () => import.meta.require('a'); setTimeout(() => f());",
                vec![true],
            ),
            ("const f = () => import.meta.require('a');", vec![false]),
            (
                "function f() { import.meta.require('a'); } function f() {}",
                vec![true],
            ),
            (
                "function outer() { return () => import.meta.require('a'); }",
                vec![false],
            ),
            (
                "function outer() { return () => import.meta.require('a'); } outer();",
                vec![true],
            ),
            (
                "const o = { get g() { return import.meta.require('a'); } };",
                vec![true],
            ),
            (
                "function f() { import.meta.require('a'); } new f();",
                vec![true],
            ),
            (
                "function f() { import.meta.require('a'); } f.call();",
                vec![true],
            ),
            (
                "function f() { f(); import.meta.require('a'); }",
                vec![false],
            ),
        ] {
            assert_eq!(reachable(source), expected, "{source}");
        }
    }

    #[test]
    fn dynamic_scope_makes_every_function_reachable() {
        GLOBALS.set(&Default::default(), || {
            let parsed =
                js_ast::parse_js_module("fixture.js", "function f() { import.meta.require('a'); }")
                    .expect("parse");
            let mut calls = LoaderCalls(Vec::new());
            parsed.module.visit_with(&mut calls);
            let sites: BTreeSet<_> = calls.0.iter().copied().collect();
            assert!(
                loader_reach(&parsed.module, &sites, true)
                    .unreachable
                    .is_empty()
            );
        });
    }
}
