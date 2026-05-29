//! AST visitors used by the lowering pipeline.
//!
//! - `IdentifierRenamer` rewrites local identifiers per a rename map.
//! - `RenameAndShorthandNaturalizer` combines that with shorthand collapse.
//! - `ShorthandNaturalizer` is the standalone shorthand-only pass.
//!
//! The `naturalize_object_*_shorthand` helpers are shared between them.
//!
//! ## Scope-aware renaming
//!
//! The rename map is keyed by bare symbol, but a rename must never leak
//! into a subtree that re-binds the same name. When a function/arrow/
//! method/catch/block introduces a binding whose name is a rename source,
//! that name inside the subtree refers to the inner binding, not the
//! top-level binding the rename targets — rewriting it (or its inner
//! declaration) would change runtime behavior. Both renaming visitors
//! maintain a scope stack (`RenameScopeStack`) and suppress a name's
//! rename for the duration of any subtree that shadows it. This mirrors
//! `purity::PlainDataWriteScanner`'s `shadowed_by_params` + `with_scope`
//! pattern; here it must additionally track block-scoped `let`/`const`/
//! `class`/`function` and `catch` bindings because rename targets reach
//! deeper than the purity scan's parameter-only tracking.
//!
//! Over-suppression (skipping a rename in a shadowed subtree where it
//! might technically have been safe) is acceptable; silent miscompilation
//! is not. The stack therefore errs toward suppressing whenever a name is
//! re-bound anywhere in the enclosing subtree.

use super::*;

/// Stack of per-scope shadow sets. A name is "shadowed at this point in
/// the traversal" iff it appears in any active stack entry; while shadowed
/// its rename is suppressed. Shared by both renaming visitors.
#[derive(Default)]
pub(super) struct RenameScopeStack {
    scopes: Vec<BTreeSet<String>>,
}

impl RenameScopeStack {
    fn is_shadowed(&self, name: &str) -> bool {
        self.scopes.iter().any(|scope| scope.contains(name))
    }

    fn push(&mut self, scope: BTreeSet<String>) {
        self.scopes.push(scope);
    }

    fn pop(&mut self) {
        self.scopes.pop();
    }
}

/// Names a single parameter pattern binds that are also rename sources.
fn collect_shadowed_by_pat(
    pat: &Pat,
    renames: &BTreeMap<String, String>,
    out: &mut BTreeSet<String>,
) {
    match pat {
        Pat::Ident(ident) => {
            let name = ident.id.sym.as_ref();
            if renames.contains_key(name) {
                out.insert(name.to_string());
            }
        }
        Pat::Rest(rest) => collect_shadowed_by_pat(&rest.arg, renames, out),
        Pat::Assign(assign) => collect_shadowed_by_pat(&assign.left, renames, out),
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                collect_shadowed_by_pat(elem, renames, out);
            }
        }
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(kv) => collect_shadowed_by_pat(&kv.value, renames, out),
                    ObjectPatProp::Assign(assign) => {
                        let name = assign.key.id.sym.as_ref();
                        if renames.contains_key(name) {
                            out.insert(name.to_string());
                        }
                    }
                    ObjectPatProp::Rest(rest) => collect_shadowed_by_pat(&rest.arg, renames, out),
                }
            }
        }
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

fn shadowed_by_params<'a, I>(params: I, renames: &BTreeMap<String, String>) -> BTreeSet<String>
where
    I: IntoIterator<Item = &'a Pat>,
{
    let mut out = BTreeSet::new();
    for param in params {
        collect_shadowed_by_pat(param, renames, &mut out);
    }
    out
}

/// Names that lexical declarations (`let`/`const`/`var`/`function`/`class`)
/// directly inside `stmts` bind and that are also rename sources. Only the
/// statement list's own scope is inspected (no descent into nested
/// function/arrow bodies — those push their own scopes when visited). `var`
/// hoists to the enclosing function rather than the block, but treating it
/// as block-shadowing here only over-suppresses, which is sound.
fn shadowed_by_block_decls(stmts: &[Stmt], renames: &BTreeMap<String, String>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for stmt in stmts {
        match stmt {
            Stmt::Decl(Decl::Var(var)) => {
                for declarator in &var.decls {
                    collect_shadowed_by_pat(&declarator.name, renames, &mut out);
                }
            }
            Stmt::Decl(Decl::Fn(function)) => {
                let name = function.ident.sym.as_ref();
                if renames.contains_key(name) {
                    out.insert(name.to_string());
                }
            }
            Stmt::Decl(Decl::Class(class)) => {
                let name = class.ident.sym.as_ref();
                if renames.contains_key(name) {
                    out.insert(name.to_string());
                }
            }
            _ => {}
        }
    }
    out
}

/// Generates the shared `VisitMut` methods that apply a string-keyed
/// rename map to identifiers, import specifiers, computed property/member
/// keys, and named exports, suppressing renames in subtrees that re-bind
/// the same name. Used by both `IdentifierRenamer` (rename-only) and
/// `RenameAndShorthandNaturalizer` (rename + shorthand collapse).
///
/// Expects `self.renames: &BTreeMap<String, String>` and
/// `self.scopes: RenameScopeStack`.
macro_rules! impl_rename_visit_mut {
    () => {
        fn visit_mut_ident(&mut self, ident: &mut Ident) {
            if self.scopes.is_shadowed(ident.sym.as_ref()) {
                return;
            }
            if let Some(to) = self.renames.get(ident.sym.as_ref()) {
                ident.sym = to.clone().into();
            }
        }

        fn visit_mut_import_named_specifier(&mut self, spec: &mut ImportNamedSpecifier) {
            let original_local = spec.local.sym.clone();
            if self.scopes.is_shadowed(original_local.as_ref()) {
                return;
            }
            let Some(to) = self.renames.get(original_local.as_ref()) else {
                return;
            };
            if spec.imported.is_none() {
                spec.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                    original_local,
                    DUMMY_SP,
                )));
            }
            spec.local.sym = to.clone().into();
        }

        fn visit_mut_prop_name(&mut self, prop_name: &mut PropName) {
            if let PropName::Computed(computed) = prop_name {
                computed.visit_mut_children_with(self);
            }
        }

        fn visit_mut_member_prop(&mut self, member_prop: &mut MemberProp) {
            if let MemberProp::Computed(computed) = member_prop {
                computed.visit_mut_children_with(self);
            }
        }

        fn visit_mut_named_export(&mut self, named: &mut NamedExport) {
            if named.src.is_none() {
                named.specifiers.visit_mut_with(self);
            }
        }

        fn visit_mut_export_named_specifier(&mut self, spec: &mut ExportNamedSpecifier) {
            spec.orig.visit_mut_with(self);
        }

        fn visit_mut_function(&mut self, function: &mut Function) {
            let scope = shadowed_by_params(function.params.iter().map(|p| &p.pat), self.renames);
            self.with_rename_scope(scope, |s| function.visit_mut_children_with(s));
        }

        fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
            let scope = shadowed_by_params(arrow.params.iter(), self.renames);
            self.with_rename_scope(scope, |s| arrow.visit_mut_children_with(s));
        }

        fn visit_mut_constructor(&mut self, constructor: &mut Constructor) {
            let params = constructor.params.iter().filter_map(|p| match p {
                ParamOrTsParamProp::Param(param) => Some(&param.pat),
                ParamOrTsParamProp::TsParamProp(_) => None,
            });
            let scope = shadowed_by_params(params, self.renames);
            self.with_rename_scope(scope, |s| constructor.visit_mut_children_with(s));
        }

        fn visit_mut_catch_clause(&mut self, clause: &mut CatchClause) {
            let scope = match &clause.param {
                Some(pat) => {
                    let mut out = BTreeSet::new();
                    collect_shadowed_by_pat(pat, self.renames, &mut out);
                    out
                }
                None => BTreeSet::new(),
            };
            self.with_rename_scope(scope, |s| clause.visit_mut_children_with(s));
        }

        fn visit_mut_block_stmt(&mut self, block: &mut BlockStmt) {
            let scope = shadowed_by_block_decls(&block.stmts, self.renames);
            self.with_rename_scope(scope, |s| block.visit_mut_children_with(s));
        }
    };
}

pub(super) struct IdentifierRenamer<'a> {
    pub(super) renames: &'a BTreeMap<String, String>,
    pub(super) scopes: RenameScopeStack,
}

impl<'a> IdentifierRenamer<'a> {
    pub(super) fn new(renames: &'a BTreeMap<String, String>) -> Self {
        Self {
            renames,
            scopes: RenameScopeStack::default(),
        }
    }

    fn with_rename_scope<F: FnOnce(&mut Self)>(&mut self, scope: BTreeSet<String>, f: F) {
        self.scopes.push(scope);
        f(self);
        self.scopes.pop();
    }
}

impl VisitMut for IdentifierRenamer<'_> {
    impl_rename_visit_mut!();
}

pub(super) struct RenameAndShorthandNaturalizer<'a> {
    pub(super) renames: &'a BTreeMap<String, String>,
    pub(super) scopes: RenameScopeStack,
}

impl<'a> RenameAndShorthandNaturalizer<'a> {
    pub(super) fn new(renames: &'a BTreeMap<String, String>) -> Self {
        Self {
            renames,
            scopes: RenameScopeStack::default(),
        }
    }

    fn with_rename_scope<F: FnOnce(&mut Self)>(&mut self, scope: BTreeSet<String>, f: F) {
        self.scopes.push(scope);
        f(self);
        self.scopes.pop();
    }
}

impl VisitMut for RenameAndShorthandNaturalizer<'_> {
    impl_rename_visit_mut!();

    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
        object.visit_mut_children_with(self);
        naturalize_object_pattern_shorthand(object);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        object.visit_mut_children_with(self);
        naturalize_object_literal_shorthand(object);
    }
}

pub(super) struct ShorthandNaturalizer;

impl VisitMut for ShorthandNaturalizer {
    fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
        object.visit_mut_children_with(self);
        naturalize_object_pattern_shorthand(object);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        object.visit_mut_children_with(self);
        naturalize_object_literal_shorthand(object);
    }
}

pub(super) fn naturalize_object_pattern_shorthand(object: &mut ObjectPat) {
    for prop in &mut object.props {
        if let ObjectPatProp::KeyValue(key_value) = prop
            && let PropName::Ident(key) = &key_value.key
            && let Pat::Ident(value) = &*key_value.value
            && key.sym == value.id.sym
        {
            *prop = ObjectPatProp::Assign(AssignPatProp {
                span: DUMMY_SP,
                key: value.clone(),
                value: None,
            });
        }
    }
}

pub(super) fn naturalize_object_literal_shorthand(object: &mut ObjectLit) {
    for prop in &mut object.props {
        if let PropOrSpread::Prop(prop_box) = prop
            && let Prop::KeyValue(key_value) = &**prop_box
            && let PropName::Ident(key) = &key_value.key
            && let Expr::Ident(value) = &*key_value.value
            && key.sym == value.sym
        {
            *prop = PropOrSpread::Prop(Box::new(Prop::Shorthand(value.clone())));
        }
    }
}
