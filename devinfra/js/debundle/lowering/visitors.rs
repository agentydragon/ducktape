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
//!
//! ## Target capture
//!
//! Shadow tracking covers rename TARGETS as well as sources: renaming
//! `a` -> `b` at a point where an enclosing scope binds `b` would make
//! the rewritten reference resolve to the inner `b` (capture). The
//! visitors do not apply such a rename; they record the offending
//! `(source, target)` pair in `captured` instead — a partial
//! application (decl renamed at top level, captured reference left
//! as-is) is itself a miscompile. The recorded set has three consumer
//! classes: the read-only `RenameCaptureProbe` walks the un-renamed
//! body to feed the ledger seal the capture facts it hard-errors on;
//! the post-seal executors `debug_assert!` it is empty (seal already
//! rejected captures — a non-empty set means probe and executor
//! diverged); and the moved-module-body `chunk_renames` application
//! bails on it, the one application whose body occupancy seal cannot
//! see (see `rename_ledger.rs` "Boundaries that validate at
//! application").
//!
//! ## Shorthand expansion
//!
//! `{ a }` (object literal shorthand) and `const { a } = o` (object
//! pattern shorthand) couple a property KEY and a binding reference in
//! one ident. Renaming the binding must preserve the key, so the
//! visitors expand shorthand to the key-value form (`{ a: b }` /
//! `const { a: b } = o`) instead of rewriting the ident in place.
//!
//! Labels are a separate namespace from bindings; label idents and
//! their `break`/`continue` references are never renamed.

use super::*;

/// Stack of per-scope shadow sets. A name is "shadowed at this point in
/// the traversal" iff it appears in any active stack entry; while shadowed
/// its rename is suppressed (and renames INTO it are captures). Shared by
/// both renaming visitors.
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

/// The rename-relevant name universe: a bound name enters a shadow scope iff
/// it is a rename SOURCE (its rename must be suppressed in the subtree) or a
/// rename TARGET (renames into it inside the subtree would be captures).
struct RenameNames<'a> {
    sources: &'a BTreeMap<String, String>,
    targets: &'a BTreeSet<String>,
}

impl RenameNames<'_> {
    fn is_relevant(&self, name: &str) -> bool {
        self.sources.contains_key(name) || self.targets.contains(name)
    }
}

fn rename_targets(renames: &BTreeMap<String, String>) -> BTreeSet<String> {
    renames.values().cloned().collect()
}

/// Names a single parameter pattern binds that are rename sources or targets.
fn collect_shadowed_by_pat(pat: &Pat, names: &RenameNames<'_>, out: &mut BTreeSet<String>) {
    match pat {
        Pat::Ident(ident) => {
            let name = ident.id.sym.as_ref();
            if names.is_relevant(name) {
                out.insert(name.to_string());
            }
        }
        Pat::Rest(rest) => collect_shadowed_by_pat(&rest.arg, names, out),
        Pat::Assign(assign) => collect_shadowed_by_pat(&assign.left, names, out),
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                collect_shadowed_by_pat(elem, names, out);
            }
        }
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(kv) => collect_shadowed_by_pat(&kv.value, names, out),
                    ObjectPatProp::Assign(assign) => {
                        let name = assign.key.id.sym.as_ref();
                        if names.is_relevant(name) {
                            out.insert(name.to_string());
                        }
                    }
                    ObjectPatProp::Rest(rest) => collect_shadowed_by_pat(&rest.arg, names, out),
                }
            }
        }
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

fn shadowed_by_params<'a, I>(params: I, names: &RenameNames<'_>) -> BTreeSet<String>
where
    I: IntoIterator<Item = &'a Pat>,
{
    let mut out = BTreeSet::new();
    for param in params {
        collect_shadowed_by_pat(param, names, &mut out);
    }
    out
}

/// Names that lexical declarations (`let`/`const`/`var`/`function`/`class`)
/// directly inside `stmts` bind and that are rename sources or targets. Only
/// the statement list's own scope is inspected (no descent into nested
/// function/arrow bodies — those push their own scopes when visited). `var`
/// hoists to the enclosing function rather than the block, but treating it
/// as block-shadowing here only over-suppresses, which is sound.
fn shadowed_by_block_decls(stmts: &[Stmt], names: &RenameNames<'_>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for stmt in stmts {
        match stmt {
            Stmt::Decl(Decl::Var(var)) => {
                for declarator in &var.decls {
                    collect_shadowed_by_pat(&declarator.name, names, &mut out);
                }
            }
            Stmt::Decl(Decl::Fn(function)) => {
                let name = function.ident.sym.as_ref();
                if names.is_relevant(name) {
                    out.insert(name.to_string());
                }
            }
            Stmt::Decl(Decl::Class(class)) => {
                let name = class.ident.sym.as_ref();
                if names.is_relevant(name) {
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
/// the same name and recording target captures (see module docs). Used by
/// both `IdentifierRenamer` (rename-only) and
/// `RenameAndShorthandNaturalizer` (rename + shorthand collapse).
///
/// Expects `self.renames: &BTreeMap<String, String>`,
/// `self.targets: BTreeSet<String>`, `self.scopes: RenameScopeStack`, and
/// `self.captured: BTreeSet<(String, String)>`.
macro_rules! impl_rename_visit_mut {
    () => {
        fn visit_mut_ident(&mut self, ident: &mut Ident) {
            if self.scopes.is_shadowed(ident.sym.as_ref()) {
                return;
            }
            if let Some(to) = self.rename_target_for(ident.sym.as_ref()) {
                ident.sym = to.into();
            }
        }

        fn visit_mut_import_named_specifier(&mut self, spec: &mut ImportNamedSpecifier) {
            let original_local = spec.local.sym.clone();
            if self.scopes.is_shadowed(original_local.as_ref()) {
                return;
            }
            let Some(to) = self.rename_target_for(original_local.as_ref()) else {
                return;
            };
            if spec.imported.is_none() {
                spec.imported = Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                    original_local,
                    DUMMY_SP,
                )));
            }
            spec.local.sym = to.into();
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

        // `{ a }` reads binding `a` under property key `a`; renaming the
        // binding must keep the key, so expand to `{ a: <renamed> }`
        // instead of letting `visit_mut_ident` rewrite the coupled ident.
        fn visit_mut_prop(&mut self, prop: &mut Prop) {
            let Prop::Shorthand(ident) = prop else {
                prop.visit_mut_children_with(self);
                return;
            };
            if self.scopes.is_shadowed(ident.sym.as_ref()) {
                return;
            }
            let Some(to) = self.rename_target_for(ident.sym.as_ref()) else {
                return;
            };
            let key = PropName::Ident(IdentName::new(ident.sym.clone(), ident.span));
            let mut value = ident.clone();
            value.sym = to.into();
            *prop = Prop::KeyValue(KeyValueProp {
                key,
                value: Box::new(Expr::Ident(value)),
            });
        }

        // `const { a } = o` / `const { a = d } = o` declare binding `a`
        // from property `a`; renaming the binding must keep the key, so
        // expand to `{ a: <renamed> }` / `{ a: <renamed> = d }`.
        fn visit_mut_object_pat_prop(&mut self, prop: &mut ObjectPatProp) {
            let ObjectPatProp::Assign(assign) = prop else {
                prop.visit_mut_children_with(self);
                return;
            };
            // The default expr holds ordinary references; rename them.
            if let Some(value) = &mut assign.value {
                value.visit_mut_with(self);
            }
            if self.scopes.is_shadowed(assign.key.id.sym.as_ref()) {
                return;
            }
            let Some(to) = self.rename_target_for(assign.key.id.sym.as_ref()) else {
                return;
            };
            let key = PropName::Ident(IdentName::new(
                assign.key.id.sym.clone(),
                assign.key.id.span,
            ));
            let mut binding = assign.key.clone();
            binding.id.sym = to.into();
            let value = match assign.value.take() {
                Some(default) => Pat::Assign(AssignPat {
                    span: assign.span,
                    left: Box::new(Pat::Ident(binding)),
                    right: default,
                }),
                None => Pat::Ident(binding),
            };
            *prop = ObjectPatProp::KeyValue(KeyValuePatProp {
                key,
                value: Box::new(value),
            });
        }

        // Labels live in a separate namespace from bindings: never rename
        // a label declaration or its `break`/`continue` references.
        fn visit_mut_labeled_stmt(&mut self, labeled: &mut LabeledStmt) {
            labeled.body.visit_mut_with(self);
        }

        fn visit_mut_break_stmt(&mut self, _break_stmt: &mut BreakStmt) {}

        fn visit_mut_continue_stmt(&mut self, _continue_stmt: &mut ContinueStmt) {}

        fn visit_mut_named_export(&mut self, named: &mut NamedExport) {
            if named.src.is_none() {
                named.specifiers.visit_mut_with(self);
            }
        }

        fn visit_mut_export_named_specifier(&mut self, spec: &mut ExportNamedSpecifier) {
            spec.orig.visit_mut_with(self);
        }

        fn visit_mut_function(&mut self, function: &mut Function) {
            let scope =
                shadowed_by_params(function.params.iter().map(|p| &p.pat), &self.rename_names());
            self.with_rename_scope(scope, |s| function.visit_mut_children_with(s));
        }

        fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
            let scope = shadowed_by_params(arrow.params.iter(), &self.rename_names());
            self.with_rename_scope(scope, |s| arrow.visit_mut_children_with(s));
        }

        fn visit_mut_constructor(&mut self, constructor: &mut Constructor) {
            let params = constructor.params.iter().filter_map(|p| match p {
                ParamOrTsParamProp::Param(param) => Some(&param.pat),
                ParamOrTsParamProp::TsParamProp(_) => None,
            });
            let scope = shadowed_by_params(params, &self.rename_names());
            self.with_rename_scope(scope, |s| constructor.visit_mut_children_with(s));
        }

        fn visit_mut_catch_clause(&mut self, clause: &mut CatchClause) {
            let scope = match &clause.param {
                Some(pat) => {
                    let mut out = BTreeSet::new();
                    collect_shadowed_by_pat(pat, &self.rename_names(), &mut out);
                    out
                }
                None => BTreeSet::new(),
            };
            self.with_rename_scope(scope, |s| clause.visit_mut_children_with(s));
        }

        fn visit_mut_block_stmt(&mut self, block: &mut BlockStmt) {
            let scope = shadowed_by_block_decls(&block.stmts, &self.rename_names());
            self.with_rename_scope(scope, |s| block.visit_mut_children_with(s));
        }
    };
}

/// Generates the shared non-`VisitMut` helpers both renaming visitors use:
/// scope push/pop, the source/target name universe, and the
/// capture-checked rename lookup. The lookup returns the rename target for
/// `name` only when applying it is safe at the current traversal point;
/// when the target is shadowed it records the `(source, target)` pair in
/// `self.captured` and returns `None` (see the module docs' "Target
/// capture" for who consumes the set).
macro_rules! impl_rename_helpers {
    () => {
        fn with_rename_scope<F: FnOnce(&mut Self)>(&mut self, scope: BTreeSet<String>, f: F) {
            self.scopes.push(scope);
            f(self);
            self.scopes.pop();
        }

        fn rename_names(&self) -> RenameNames<'_> {
            RenameNames {
                sources: self.renames,
                targets: &self.targets,
            }
        }

        fn rename_target_for(&mut self, name: &str) -> Option<String> {
            let to = self.renames.get(name)?;
            if to != name && self.scopes.is_shadowed(to) {
                self.captured.insert((name.to_string(), to.clone()));
                return None;
            }
            Some(to.clone())
        }
    };
}

/// Read-only mirror of the renaming visitors: walks the same reference
/// sites with the same shadow tracking, but instead of mutating it only
/// records the `(source, target)` pairs the rename map would withhold
/// (target shadowed at a reference of the un-shadowed source). This is
/// how seal receives reference-precise capture facts from the
/// **un-renamed** tree — the executor (the single post-seal rename pass)
/// then `debug_assert!`s that its own `captured` set is empty, pinning
/// the probe and the executor to the same verdict.
///
/// MUST stay traversal-identical to `impl_rename_visit_mut!`: every site
/// the mutating visitors consult the rename map at, the probe consults
/// it at too, with the same scope pushes.
pub(super) struct RenameCaptureProbe<'a> {
    renames: &'a BTreeMap<String, String>,
    targets: BTreeSet<String>,
    scopes: RenameScopeStack,
    /// See [`IdentifierRenamer::captured`].
    pub(super) captured: BTreeSet<(String, String)>,
}

impl<'a> RenameCaptureProbe<'a> {
    pub(super) fn new(renames: &'a BTreeMap<String, String>) -> Self {
        Self {
            renames,
            targets: rename_targets(renames),
            scopes: RenameScopeStack::default(),
            captured: BTreeSet::new(),
        }
    }

    impl_rename_helpers!();

    /// Consult the rename map for `name` exactly like the mutating
    /// visitors do (recording a capture when the target is shadowed),
    /// discarding the would-be target.
    fn probe(&mut self, name: &str) {
        if self.scopes.is_shadowed(name) {
            return;
        }
        let _ = self.rename_target_for(name);
    }
}

impl Visit for RenameCaptureProbe<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        self.probe(ident.sym.as_ref());
    }

    fn visit_import_named_specifier(&mut self, spec: &ImportNamedSpecifier) {
        // Mirror: the mutating visitor only consults the map for the
        // local; `imported` is a public name, never renamed.
        self.probe(spec.local.sym.as_ref());
    }

    fn visit_prop_name(&mut self, prop_name: &PropName) {
        if let PropName::Computed(computed) = prop_name {
            computed.visit_children_with(self);
        }
    }

    fn visit_member_prop(&mut self, member_prop: &MemberProp) {
        if let MemberProp::Computed(computed) = member_prop {
            computed.visit_children_with(self);
        }
    }

    fn visit_prop(&mut self, prop: &Prop) {
        let Prop::Shorthand(ident) = prop else {
            prop.visit_children_with(self);
            return;
        };
        self.probe(ident.sym.as_ref());
    }

    fn visit_object_pat_prop(&mut self, prop: &ObjectPatProp) {
        let ObjectPatProp::Assign(assign) = prop else {
            prop.visit_children_with(self);
            return;
        };
        if let Some(value) = &assign.value {
            value.visit_with(self);
        }
        self.probe(assign.key.id.sym.as_ref());
    }

    fn visit_labeled_stmt(&mut self, labeled: &LabeledStmt) {
        labeled.body.visit_with(self);
    }

    fn visit_break_stmt(&mut self, _break_stmt: &BreakStmt) {}

    fn visit_continue_stmt(&mut self, _continue_stmt: &ContinueStmt) {}

    fn visit_named_export(&mut self, named: &NamedExport) {
        if named.src.is_none() {
            named.specifiers.visit_with(self);
        }
    }

    fn visit_export_named_specifier(&mut self, spec: &ExportNamedSpecifier) {
        spec.orig.visit_with(self);
    }

    fn visit_function(&mut self, function: &Function) {
        let scope =
            shadowed_by_params(function.params.iter().map(|p| &p.pat), &self.rename_names());
        self.with_rename_scope(scope, |s| function.visit_children_with(s));
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        let scope = shadowed_by_params(arrow.params.iter(), &self.rename_names());
        self.with_rename_scope(scope, |s| arrow.visit_children_with(s));
    }

    fn visit_constructor(&mut self, constructor: &Constructor) {
        let params = constructor.params.iter().filter_map(|p| match p {
            ParamOrTsParamProp::Param(param) => Some(&param.pat),
            ParamOrTsParamProp::TsParamProp(_) => None,
        });
        let scope = shadowed_by_params(params, &self.rename_names());
        self.with_rename_scope(scope, |s| constructor.visit_children_with(s));
    }

    fn visit_catch_clause(&mut self, clause: &CatchClause) {
        let scope = match &clause.param {
            Some(pat) => {
                let mut out = BTreeSet::new();
                collect_shadowed_by_pat(pat, &self.rename_names(), &mut out);
                out
            }
            None => BTreeSet::new(),
        };
        self.with_rename_scope(scope, |s| clause.visit_children_with(s));
    }

    fn visit_block_stmt(&mut self, block: &BlockStmt) {
        let scope = shadowed_by_block_decls(&block.stmts, &self.rename_names());
        self.with_rename_scope(scope, |s| block.visit_children_with(s));
    }
}

pub(super) struct IdentifierRenamer<'a> {
    pub(super) renames: &'a BTreeMap<String, String>,
    targets: BTreeSet<String>,
    scopes: RenameScopeStack,
    /// `(source, target)` pairs whose rename was withheld because the
    /// target was shadowed at the reference. Non-empty after a pass means
    /// the mutated body is partially renamed (see the module docs'
    /// "Target capture" for the consumer classes).
    pub(super) captured: BTreeSet<(String, String)>,
}

impl<'a> IdentifierRenamer<'a> {
    pub(super) fn new(renames: &'a BTreeMap<String, String>) -> Self {
        Self {
            renames,
            targets: rename_targets(renames),
            scopes: RenameScopeStack::default(),
            captured: BTreeSet::new(),
        }
    }

    impl_rename_helpers!();
}

impl VisitMut for IdentifierRenamer<'_> {
    impl_rename_visit_mut!();
}

pub(super) struct RenameAndShorthandNaturalizer<'a> {
    pub(super) renames: &'a BTreeMap<String, String>,
    targets: BTreeSet<String>,
    scopes: RenameScopeStack,
    /// See [`IdentifierRenamer::captured`].
    pub(super) captured: BTreeSet<(String, String)>,
}

impl<'a> RenameAndShorthandNaturalizer<'a> {
    pub(super) fn new(renames: &'a BTreeMap<String, String>) -> Self {
        Self {
            renames,
            targets: rename_targets(renames),
            scopes: RenameScopeStack::default(),
            captured: BTreeSet::new(),
        }
    }

    impl_rename_helpers!();
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
