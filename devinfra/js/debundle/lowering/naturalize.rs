//! Naturalization: rewrite body identifiers to readable names + collapse
//! `{ x: x }` shorthand. Combines plan-driven renames (spec `export_name`s,
//! collected into the rename ledger by
//! [`collect_plan_export_rename_intents`] and arriving here as the sealed
//! Module-scope map) with heuristic renames derived from the AST
//! (return-object aliases, destructure unpacks, constructor
//! `this.x = param` mappings).
//!
//! `naturalize_module_body` is the public entry. Heuristic renames are
//! split by what their source name resolves to:
//!
//! - **Free sources** (return-object aliases of names the deriving
//!   function does not bind — typically source-chunk import aliases)
//!   rewrite references of top-level bindings. They flow into the
//!   returned merged map because `plan_module_reference_needs`
//!   reverse-resolves the post-rename name through it to emit the
//!   runtime reimport (`import { t as options }`), and they keep the
//!   module-global target-uniqueness rule (`drop_target_collisions`) —
//!   two of them onto one target would collide in module scope.
//!
//! - **Bound sources** (destructured params, constructor
//!   `this.x = param` assignments, return-object aliases of the
//!   function's own root bindings) rename a binding owned by a single
//!   scope. `ScopedHeuristicNaturalizer` derives and validates them per
//!   deriving scope, with no module-global uniqueness requirement: two
//!   sibling scopes reusing the same minified spelling (`e` → `value`
//!   here, `e` → `registry` there) are independent renames (#2045).
//!   They stay out of the returned map so a scope-local rename can
//!   never remap an unrelated top-level export or runtime reimport
//!   that happens to share the spelling.
//!
//! Both heuristic families submit intents into a per-module
//! [`RenameLedger`] sealed before any heuristic rename is applied
//! (free sources at `Module` scope, bound sources at `Function` scope);
//! `SealedScopeRenameApplier` replays the sealed per-scope maps onto the
//! real body, and the returned merged map is rebuilt from the sealed
//! Module-scope intents.

use swc_common::Span;

use super::scope_names::{
    BindingNameCollector, collect_binding_names_in, collect_occupied_local_names,
};
use super::util::is_valid_js_identifier;
use super::*;

/// The rename maps `naturalize_module_body` applied, split by scope.
pub(super) struct NaturalizedRenames {
    /// Plan-driven + heuristic, original → readable. The reverse lookup
    /// for bridging post-rename body syms back to pre-rename fact-table
    /// keys (`RuntimeImportLookup`).
    pub(super) merged: BTreeMap<String, String>,
    /// Plan-driven renames applied module-wide to top-level declarations.
    /// The ONLY map export locals and binding-comment keys may be remapped
    /// through: heuristic entries are scope-local and never rename a
    /// top-level declaration, so mapping a top-level name through `merged`
    /// can remap an export/comment whose declaration kept its name.
    pub(super) module_scope: BTreeMap<String, String>,
}

/// Collect each plan's spec-driven `export_name` renames into the ledger
/// (scope: that plan's [`ModuleId`], origin: `Explicit`). Applies the same
/// filter the pre-ledger `plan_driven` map applied: self-renames and
/// non-identifier targets never become intents. Iterates `plan.bindings`
/// (a `HashMap`) in sorted order so the ledger's intent order — and any
/// seal-time diagnostics — don't vary by hash seed.
pub(super) fn collect_plan_export_rename_intents(
    module_plans: &[ModulePlan],
    chunk_top_level_mark: swc_common::Mark,
    ledger: &mut RenameLedger,
) {
    for (index, plan) in module_plans.iter().enumerate() {
        let mut sorted_bindings: Vec<(&String, &String)> = plan.bindings.iter().collect();
        sorted_bindings.sort_by(|a, b| a.0.cmp(b.0));
        for (local, exported) in sorted_bindings {
            if local != exported && is_valid_js_identifier(exported) {
                ledger.submit(RenameIntent {
                    scope: RenameScope::Module(ModuleId::logical(index)),
                    from: top_level_id(local, chunk_top_level_mark),
                    to: exported.as_str().into(),
                    origin: RenameOrigin::Explicit {
                        contributor: PLAN_EXPORT_NAME_CONTRIBUTOR,
                    },
                });
            }
        }
    }
}

pub(super) const PLAN_EXPORT_NAME_CONTRIBUTOR: &str = "logical_module member export_name";
pub(super) const FREE_ALIAS_CONTRIBUTOR: &str = "return-object alias (free source)";
pub(super) const SCOPED_HEURISTIC_CONTRIBUTOR: &str = "scope-local heuristic naturalizer";

/// `plan_driven` is this plan's sealed Module-scope rename map
/// (`SealedRenames::module_renames_by_name`); sorted (`BTreeMap`)
/// iteration keeps the rename-precedence the visitor applies when two
/// locals compete for the same target independent of hash seed.
pub(super) fn naturalize_module_body(
    body: &mut [ModuleItem],
    plan: &ModulePlan,
    module: ModuleId,
    plan_driven: BTreeMap<String, String>,
    chunk_top_level_mark: swc_common::Mark,
) -> Result<NaturalizedRenames> {
    // Both heuristic contributors submit into this per-module ledger,
    // sealed below before any heuristic rename is applied.
    // `free_heuristic` is the derive-phase scratch view of the same
    // Module-scope intents: the bound-source derive pass needs the
    // module-global collision rules resolved before the ledger can seal
    // (sealing also waits for the derive pass's Function-scope intents).
    // Free aliases are submitted per deriving function, so two functions
    // aliasing one free source to different targets — previously a
    // silent last-write-wins on this map — are a seal-time conflict.
    let mut ledger = RenameLedger::default();
    let mut per_function_free = Vec::<BTreeMap<String, String>>::new();
    for item in body.iter() {
        collect_free_alias_renames_from_item(item, &mut per_function_free);
    }
    let mut free_heuristic = BTreeMap::<String, String>::new();
    for aliases in per_function_free {
        for (from, to) in aliases {
            ledger.submit(RenameIntent {
                scope: RenameScope::Module(module),
                from: top_level_id(&from, chunk_top_level_mark),
                to: to.as_str().into(),
                origin: RenameOrigin::Heuristic {
                    contributor: FREE_ALIAS_CONTRIBUTOR,
                },
            });
            free_heuristic.insert(from, to);
        }
    }
    // The merged map is what callers read back as the local-name → readable
    // lookup (runtime-reimport bridge, export remapping). Plan-driven names
    // are top-level module bindings and rename module-wide (suppressed in
    // any subtree that re-binds them); free heuristic names rename only
    // within the function that derived them, via the scoped pass below.
    let merged = drop_target_collisions(plan_driven.clone(), free_heuristic);
    let plan_driven_effective: BTreeMap<String, String> = merged
        .iter()
        .filter(|(local, _)| plan_driven.contains_key(*local))
        .map(|(local, target)| (local.clone(), target.clone()))
        .collect();
    let free_effective: BTreeMap<String, String> = merged
        .iter()
        .filter(|(local, _)| !plan_driven.contains_key(*local))
        .map(|(local, target)| (local.clone(), target.clone()))
        .collect();

    // Module-wide pass: plan-driven renames + shorthand collapse when the
    // plan renames anything, shorthand collapse alone otherwise.
    if plan_driven_effective.is_empty() {
        for item in body.iter_mut() {
            item.visit_mut_with(&mut ShorthandNaturalizer);
        }
    } else {
        // Validate plan-driven targets against the body's top-level scope
        // before any mutation: renaming a declaration onto a name another
        // top-level binding already uses produces a duplicate declaration
        // (a load-time SyntaxError). A top-level binding that is itself
        // renamed away vacates its slot and is allowed. Nested-scope target
        // collisions are NOT pre-checked here — a target bound in a nested
        // scope is harmless unless the un-shadowed source is referenced
        // inside that scope, which the renamer detects precisely at the
        // reference (see the `captured` check below).
        let root_names = collect_occupied_local_names(body);
        let mut errors = Vec::new();
        for (from, to) in &plan_driven_effective {
            if root_names.contains(to) && !plan_driven_effective.contains_key(to) {
                errors.push(format!(
                    "rename of binding {from} to {to} collides with another top-level binding in the module body",
                ));
            }
        }
        if !errors.is_empty() {
            bail!(
                "invalid renames for module {}:\n  - {}",
                plan.id,
                errors.join("\n  - "),
            );
        }

        let mut naturalizer = RenameAndShorthandNaturalizer::new(&plan_driven_effective);
        for item in body.iter_mut() {
            item.visit_mut_with(&mut naturalizer);
        }
        if !naturalizer.captured.is_empty() {
            // A rename target was bound in a scope where the un-shadowed
            // source is referenced — applying it would make the renamed
            // reference resolve to the inner binding. The body is
            // partially renamed at this point; the bail discards the
            // whole pipeline run before anything is emitted.
            bail!(
                "renames for module {} would be captured by a nested binding: {:?}",
                plan.id,
                naturalizer.captured,
            );
        }
    }

    // Names a scope-local rename must never target: anything the plan or a
    // free rename reads or writes module-wide. Renaming a scope's binding
    // onto one of these would capture module-level references inside the
    // scope (or collide with the free rename applied in the same scope).
    let mut reserved = BTreeSet::<String>::new();
    for (from, to) in plan_driven.iter().chain(merged.iter()) {
        reserved.insert(from.clone());
        reserved.insert(to.clone());
    }
    // Derive pass: run the scoped heuristic machinery over a scratch
    // clone of the body. It performs exactly the pre-ledger
    // derive-and-apply walk — outer scopes validate against subtrees
    // that already carry their nested scopes' renames — but the
    // mutations land on the clone; the real body is only renamed by the
    // sealed-output applier below. Each fired per-scope map is submitted
    // as Function-scope intents. The clone is deleted with PR 4's
    // execute-once pass.
    let mut derive_body = body.to_vec();
    let mut deriver = ScopedHeuristicNaturalizer {
        free_allowed: &free_effective,
        reserved: &reserved,
        ledger: &mut ledger,
    };
    for item in derive_body.iter_mut() {
        item.visit_mut_with(&mut deriver);
    }
    let sealed = ledger.seal()?;
    // Apply pass: replay the sealed per-scope maps onto the real body in
    // the same order the derive pass fired them (children first), so each
    // scope's map applies to the same intermediate tree state the
    // derivation validated against.
    let mut applier = SealedScopeRenameApplier { sealed: &sealed };
    for item in body.iter_mut() {
        item.visit_mut_with(&mut applier);
    }

    Ok(NaturalizedRenames {
        // The merged map consumers read is rebuilt from the sealed
        // Module-scope (free-source) intents — identical to the derive
        // scratch `merged` whenever seal succeeded.
        merged: drop_target_collisions(plan_driven, sealed.module_renames_by_name(module)),
        module_scope: plan_driven_effective,
    })
}

/// The derive pass of the per-scope heuristic pipeline: walks a scratch
/// clone of the module body and, at each function/constructor/arrow,
/// derives that node's own heuristic renames (param destructure aliases,
/// return-object aliases, `this.x = param` constructor assignments),
/// validates them, submits the fired map as Function-scope intents, and
/// rewrites the (clone's) subtree so enclosing scopes validate against
/// the post-rename nested state — exactly the pre-ledger
/// derive-and-apply order. Bound-source renames fire when their readable
/// target passes the per-scope safety checks in [`Self::validated_bound`];
/// free-source renames fire when they survived the module-global
/// collision rules (`free_allowed`). A rename derived from one scope
/// never leaks into a sibling/parent scope, and
/// `RenameAndShorthandNaturalizer`'s nested shadow-suppression still
/// guards any further-nested re-binding subtree. The real body is
/// renamed by [`SealedScopeRenameApplier`] from the sealed ledger.
struct ScopedHeuristicNaturalizer<'a> {
    /// Free-source renames that survived module-global collision-dropping;
    /// a node's locally-derived free alias only fires if it appears here.
    free_allowed: &'a BTreeMap<String, String>,
    /// Module-wide rename sources and targets (plan-driven + free); a
    /// scope-local rename must not target any of them.
    reserved: &'a BTreeSet<String>,
    /// Sink for the fired per-scope maps (Function-scope intents).
    ledger: &'a mut RenameLedger,
}

/// Replays the sealed Function-scope heuristic renames onto the real
/// module body, children-first — the same order
/// [`ScopedHeuristicNaturalizer`] fired them in on the derive clone, so
/// each scope's map applies to the same intermediate tree state the
/// derivation validated against.
struct SealedScopeRenameApplier<'a> {
    sealed: &'a SealedRenames,
}

impl SealedScopeRenameApplier<'_> {
    fn scope_map(&self, span: Span) -> BTreeMap<String, String> {
        self.sealed
            .scope_renames_by_name(&RenameScope::Function(span.into()))
    }
}

impl VisitMut for SealedScopeRenameApplier<'_> {
    fn visit_mut_function(&mut self, function: &mut Function) {
        function.visit_mut_children_with(self);
        let local = self.scope_map(function.span);
        if local.is_empty() {
            return;
        }
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        function.params.visit_mut_with(&mut renamer);
        rename_root_body(&mut renamer, function.body.as_mut());
        assert_no_heuristic_capture(&renamer);
    }

    fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
        arrow.visit_mut_children_with(self);
        let local = self.scope_map(arrow.span);
        if local.is_empty() {
            return;
        }
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        arrow.params.visit_mut_with(&mut renamer);
        match &mut *arrow.body {
            BlockStmtOrExpr::BlockStmt(block) => rename_root_body(&mut renamer, Some(block)),
            BlockStmtOrExpr::Expr(expr) => expr.visit_mut_with(&mut renamer),
        }
        assert_no_heuristic_capture(&renamer);
    }

    fn visit_mut_constructor(&mut self, constructor: &mut Constructor) {
        constructor.visit_mut_children_with(self);
        let local = self.scope_map(constructor.span);
        if local.is_empty() {
            return;
        }
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        for param in &mut constructor.params {
            param.visit_mut_with(&mut renamer);
        }
        rename_root_body(&mut renamer, constructor.body.as_mut());
        assert_no_heuristic_capture(&renamer);
    }
}

/// Name sets describing one function-like subtree, gathered in a single
/// walk and consumed by the per-scope rename validation.
#[derive(Default)]
struct SubtreeNameFacts {
    /// Every value/binding identifier sym in the subtree. Object-literal
    /// keys and static member props are `IdentName`s, not `Ident`s, so
    /// they are intentionally absent — a property key spelled like a
    /// rename target neither shadows nor captures it.
    mentions: BTreeSet<String>,
    /// Names bound by any declaration within the subtree (params, `var`/
    /// `let`/`const`, function/class declarations and named expressions).
    bound: BTreeSet<String>,
}

#[derive(Default)]
struct SubtreeNameFactsCollector {
    facts: SubtreeNameFacts,
}

impl Visit for SubtreeNameFactsCollector {
    fn visit_ident(&mut self, ident: &Ident) {
        self.facts.mentions.insert(ident.sym.to_string());
    }

    fn visit_binding_ident(&mut self, ident: &BindingIdent) {
        self.facts.bound.insert(ident.id.sym.to_string());
        ident.visit_children_with(self);
    }

    fn visit_fn_decl(&mut self, decl: &FnDecl) {
        self.facts.bound.insert(decl.ident.sym.to_string());
        decl.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, decl: &ClassDecl) {
        self.facts.bound.insert(decl.ident.sym.to_string());
        decl.visit_children_with(self);
    }

    fn visit_fn_expr(&mut self, expr: &FnExpr) {
        if let Some(ident) = &expr.ident {
            self.facts.bound.insert(ident.sym.to_string());
        }
        expr.visit_children_with(self);
    }

    fn visit_class_expr(&mut self, expr: &ClassExpr) {
        if let Some(ident) = &expr.ident {
            self.facts.bound.insert(ident.sym.to_string());
        }
        expr.visit_children_with(self);
    }
}

fn collect_subtree_name_facts<N>(node: &N) -> SubtreeNameFacts
where
    N: VisitWith<SubtreeNameFactsCollector>,
{
    let mut collector = SubtreeNameFactsCollector::default();
    node.visit_with(&mut collector);
    collector.facts
}

/// Names bound in a function's root scope: its params plus lexical
/// declarations directly in its body statements (no descent into nested
/// function/arrow bodies). Return-object aliases of these names are
/// scope-local renames; `rename_root_body` treats the root declarations
/// as rename targets, so the declaration and its references move together.
fn function_root_binding_names(function: &Function) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    for param in &function.params {
        collect_pat_binding_names(&param.pat, &mut names);
    }
    let Some(body) = function.body.as_ref() else {
        return names;
    };
    for stmt in &body.stmts {
        match stmt {
            Stmt::Decl(Decl::Var(var)) => {
                for declarator in &var.decls {
                    collect_pat_binding_names(&declarator.name, &mut names);
                }
            }
            Stmt::Decl(Decl::Fn(function)) => {
                names.insert(function.ident.sym.to_string());
            }
            Stmt::Decl(Decl::Class(class)) => {
                names.insert(class.ident.sym.to_string());
            }
            _ => {}
        }
    }
    names
}

fn collect_pat_binding_names(pat: &Pat, out: &mut BTreeSet<String>) {
    match pat {
        Pat::Ident(ident) => {
            out.insert(ident.id.sym.to_string());
        }
        Pat::Rest(rest) => collect_pat_binding_names(&rest.arg, out),
        Pat::Assign(assign) => collect_pat_binding_names(&assign.left, out),
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                collect_pat_binding_names(elem, out);
            }
        }
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(kv) => collect_pat_binding_names(&kv.value, out),
                    ObjectPatProp::Assign(assign) => {
                        out.insert(assign.key.id.sym.to_string());
                    }
                    ObjectPatProp::Rest(rest) => collect_pat_binding_names(&rest.arg, out),
                }
            }
        }
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

impl ScopedHeuristicNaturalizer<'_> {
    /// Record one deriving scope's fired rename map as Function-scope
    /// intents. The sources are function-local bindings whose hygiene
    /// context the string-keyed derivation never resolves; the
    /// string-era key is encoded as the empty `SyntaxContext` (the
    /// sealed by-name projection is what the apply pass consumes; see
    /// the ledger module doc's hygiene-boundary section).
    fn submit_scope_intents(&mut self, span: Span, local: &BTreeMap<String, String>) {
        for (from, to) in local {
            self.ledger.submit(RenameIntent {
                scope: RenameScope::Function(span.into()),
                from: (from.as_str().into(), SyntaxContext::empty()),
                to: to.as_str().into(),
                origin: RenameOrigin::Heuristic {
                    contributor: SCOPED_HEURISTIC_CONTRIBUTOR,
                },
            });
        }
    }

    /// Per-scope safety checks for bound-source renames. A rename fires
    /// only when its readable target is globally unreserved, absent from
    /// the scope's subtree (renaming onto a name the subtree already
    /// binds or references would capture or collide), unique among this
    /// scope's targets, and not itself another rename's source.
    /// Over-suppression is acceptable; silent capture is not.
    fn validated_bound(
        &self,
        bound: BTreeMap<String, String>,
        facts: &SubtreeNameFacts,
    ) -> BTreeMap<String, String> {
        let mut target_counts = BTreeMap::<String, usize>::new();
        for target in bound.values() {
            *target_counts.entry(target.clone()).or_default() += 1;
        }
        bound
            .iter()
            .filter(|(_, to)| {
                target_counts.get(to.as_str()).copied() == Some(1)
                    && !self.reserved.contains(to.as_str())
                    && !facts.mentions.contains(to.as_str())
                    && !bound.contains_key(to.as_str())
            })
            .map(|(from, to)| (from.clone(), to.clone()))
            .collect()
    }

    /// Split a function's return-object aliases into the applicable
    /// categories: aliases of root-scope bindings join `bound`; aliases of
    /// names the function doesn't bind anywhere apply when the module-global
    /// free map allows the exact pair; aliases of names bound only in a
    /// nested scope are dropped (renaming the root-level references would
    /// desync them from the shadow-suppressed nested declaration).
    fn classify_function_aliases(
        &self,
        function: &Function,
        facts: &SubtreeNameFacts,
        bound: &mut BTreeMap<String, String>,
        free: &mut BTreeMap<String, String>,
    ) {
        let Some(body) = function.body.as_ref() else {
            return;
        };
        let mut aliases = BTreeMap::new();
        collect_return_object_alias_renames(&body.stmts, &mut aliases);
        if aliases.is_empty() {
            return;
        }
        let root_bound = function_root_binding_names(function);
        for (from, to) in aliases {
            if root_bound.contains(&from) {
                bound.insert(from, to);
            } else if !facts.bound.contains(&from) && self.free_allowed.get(&from) == Some(&to) {
                free.insert(from, to);
            }
        }
    }
}

/// Drop heuristic renames whose target `node`'s subtree binds. Applying
/// such a rename would either collide with the binding or be suppressed
/// only inside the binding's scope — leaving the declaration renamed but
/// some references not (a silent capture/miscompile). Heuristic renames
/// are cosmetic, so whole-entry suppression is the sound fallback.
fn drop_subtree_captured_targets<N>(
    local: BTreeMap<String, String>,
    node: &N,
) -> BTreeMap<String, String>
where
    N: VisitWith<BindingNameCollector>,
{
    if local.is_empty() {
        return local;
    }
    let bound = collect_binding_names_in(node);
    local
        .into_iter()
        .filter(|(_, to)| !bound.contains(to))
        .collect()
}

/// Invariant check after a heuristic scope-local rename pass: the
/// subtree pre-filter must have made captures impossible. A non-empty
/// `captured` set means the body is partially renamed — crash loudly
/// rather than emit a miscompile.
fn assert_no_heuristic_capture(renamer: &RenameAndShorthandNaturalizer<'_>) {
    assert!(
        renamer.captured.is_empty(),
        "heuristic rename captured despite subtree pre-filter: {:?}",
        renamer.captured,
    );
}

/// Rewrites the statements of a function/constructor body with a heuristic
/// rename map, treating the body's own (root-scope) bindings as rename
/// targets rather than shadows. The renamer is applied to each statement
/// individually so `RenameAndShorthandNaturalizer::visit_mut_block_stmt`
/// never fires for this root block (its `let`/`const` declarations are the
/// targets); nested blocks/functions encountered inside the statements do
/// push their own shadow scopes, so a deeper re-binding of the same name is
/// still suppressed. Visiting statements one-by-one (rather than the enclosing
/// block node) is what skips `visit_mut_block_stmt`'s own-decl shadow push for
/// the root block.
fn rename_root_body(renamer: &mut RenameAndShorthandNaturalizer<'_>, body: Option<&mut BlockStmt>) {
    if let Some(body) = body {
        for stmt in &mut body.stmts {
            stmt.visit_mut_with(renamer);
        }
    }
}

impl VisitMut for ScopedHeuristicNaturalizer<'_> {
    fn visit_mut_function(&mut self, function: &mut Function) {
        // Recurse first so nested functions apply their own heuristics;
        // the facts collected below then see the post-rename subtree.
        function.visit_mut_children_with(self);
        let facts = collect_subtree_name_facts(&*function);
        let mut bound = BTreeMap::new();
        for param in &function.params {
            collect_naturalization_renames_from_pattern(&param.pat, &mut bound);
        }
        let mut free = BTreeMap::new();
        self.classify_function_aliases(function, &facts, &mut bound, &mut free);
        // Free-source aliases get no `mentions` pre-check from
        // `validated_bound`, so drop any whose target the subtree binds:
        // applying one would be suppressed only inside the re-binding
        // scope, tripping the capture assert below on a partially renamed
        // body. (Bound-source renames are already covered — `validated_bound`
        // rejects targets the subtree so much as mentions.)
        let mut local = drop_subtree_captured_targets(free, &*function);
        local.extend(self.validated_bound(bound, &facts));
        if local.is_empty() {
            return;
        }
        self.submit_scope_intents(function.span, &local);
        // Params and the root body share this function's scope, where the
        // renamed name is bound exactly once and every reference resolves to
        // it. Drive past the root param/block scope (visiting params and the
        // body's statements directly) so the own-binding isn't treated as a
        // shadow; nested subtrees still suppress.
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        function.params.visit_mut_with(&mut renamer);
        rename_root_body(&mut renamer, function.body.as_mut());
        assert_no_heuristic_capture(&renamer);
    }

    fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
        arrow.visit_mut_children_with(self);
        let facts = collect_subtree_name_facts(&*arrow);
        let mut bound = BTreeMap::new();
        for param in &arrow.params {
            collect_naturalization_renames_from_pattern(param, &mut bound);
        }
        let local = self.validated_bound(bound, &facts);
        if local.is_empty() {
            return;
        }
        self.submit_scope_intents(arrow.span, &local);
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        arrow.params.visit_mut_with(&mut renamer);
        match &mut *arrow.body {
            BlockStmtOrExpr::BlockStmt(block) => rename_root_body(&mut renamer, Some(block)),
            BlockStmtOrExpr::Expr(expr) => expr.visit_mut_with(&mut renamer),
        }
        assert_no_heuristic_capture(&renamer);
    }

    fn visit_mut_constructor(&mut self, constructor: &mut Constructor) {
        constructor.visit_mut_children_with(self);
        let facts = collect_subtree_name_facts(&*constructor);
        let mut bound = BTreeMap::new();
        collect_naturalization_renames_from_constructor(constructor, &mut bound);
        let local = self.validated_bound(bound, &facts);
        if local.is_empty() {
            return;
        }
        self.submit_scope_intents(constructor.span, &local);
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        for param in &mut constructor.params {
            param.visit_mut_with(&mut renamer);
        }
        rename_root_body(&mut renamer, constructor.body.as_mut());
        assert_no_heuristic_capture(&renamer);
    }
}

/// Merge `heuristic` into `plan_driven`, dropping any heuristic mapping
/// whose target is either already claimed by `plan_driven` or shared with
/// another heuristic source. Two sources renamed onto the same target
/// would collapse distinct bindings into a duplicate decl as soon as both
/// happen to live in the same scope.
pub(super) fn drop_target_collisions(
    mut plan_driven: BTreeMap<String, String>,
    heuristic: BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    // Only effective heuristic mappings (locals not already in plan_driven)
    // contribute to the collision count. Counting skipped entries inflates
    // counts[target] and can drop unrelated heuristic mappings that have only
    // one effective claimant.
    let mut counts = BTreeMap::<String, usize>::new();
    for target in plan_driven.values() {
        *counts.entry(target.clone()).or_default() += 1;
    }
    for (local, target) in &heuristic {
        if plan_driven.contains_key(local) {
            continue;
        }
        *counts.entry(target.clone()).or_default() += 1;
    }
    for (local, target) in heuristic {
        if plan_driven.contains_key(&local) {
            continue;
        }
        if counts.get(&target).copied().unwrap_or(0) > 1 {
            continue;
        }
        plan_driven.insert(local, target);
    }
    plan_driven
}

/// Collect a top-level item's free-source return-object aliases: renames
/// of names the deriving function does not bind anywhere (typically
/// source-chunk import aliases). These are the only heuristic renames
/// that rewrite references of top-level bindings and therefore the only
/// ones that join the module-global merged map. Each deriving function's
/// aliases are pushed as a separate map so the caller can submit them as
/// per-function ledger intents.
fn collect_free_alias_renames_from_item(
    item: &ModuleItem,
    per_function: &mut Vec<BTreeMap<String, String>>,
) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => {
            collect_free_alias_renames_from_decl(decl, per_function)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            collect_free_alias_renames_from_decl(&export_decl.decl, per_function);
        }
        _ => {}
    }
}

fn collect_free_alias_renames_from_decl(
    decl: &Decl,
    per_function: &mut Vec<BTreeMap<String, String>>,
) {
    match decl {
        Decl::Fn(function) => {
            collect_free_alias_renames_from_function(&function.function, per_function);
        }
        Decl::Var(var) => {
            for declarator in &var.decls {
                if let Some(Expr::Fn(function)) = declarator.init.as_deref() {
                    collect_free_alias_renames_from_function(&function.function, per_function);
                }
            }
        }
        _ => {}
    }
}

fn collect_free_alias_renames_from_function(
    function: &Function,
    per_function: &mut Vec<BTreeMap<String, String>>,
) {
    let Some(body) = function.body.as_ref() else {
        return;
    };
    let mut aliases = BTreeMap::new();
    collect_return_object_alias_renames(&body.stmts, &mut aliases);
    if aliases.is_empty() {
        return;
    }
    let facts = collect_subtree_name_facts(function);
    let renames: BTreeMap<String, String> = aliases
        .into_iter()
        .filter(|(from, _)| !facts.bound.contains(from))
        .collect();
    if !renames.is_empty() {
        per_function.push(renames);
    }
}

pub(super) fn collect_naturalization_renames_from_constructor(
    constructor: &Constructor,
    renames: &mut BTreeMap<String, String>,
) {
    let mut param_names = BTreeSet::new();
    for param in &constructor.params {
        if let ParamOrTsParamProp::Param(param) = param
            && let Pat::Ident(ident) = &param.pat
        {
            param_names.insert(ident.id.sym.to_string());
        }
    }
    let Some(body) = constructor.body.as_ref() else {
        return;
    };
    for statement in &body.stmts {
        collect_constructor_assignment_renames(statement, &param_names, renames);
    }
}

pub(super) fn collect_naturalization_renames_from_pattern(
    pat: &Pat,
    renames: &mut BTreeMap<String, String>,
) {
    match pat {
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        if let PropName::Ident(key) = &key_value.key
                            && let Pat::Ident(value) = &*key_value.value
                        {
                            let from = value.id.sym.to_string();
                            let to = key.sym.to_string();
                            if from != to && is_valid_js_identifier(&to) {
                                renames.insert(from, to);
                            }
                        }
                    }
                    ObjectPatProp::Assign(_) => {}
                    ObjectPatProp::Rest(rest) => {
                        collect_naturalization_renames_from_pattern(&rest.arg, renames);
                    }
                }
            }
        }
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                collect_naturalization_renames_from_pattern(elem, renames);
            }
        }
        Pat::Assign(assign) => collect_naturalization_renames_from_pattern(&assign.left, renames),
        Pat::Rest(rest) => collect_naturalization_renames_from_pattern(&rest.arg, renames),
        _ => {}
    }
}

pub(super) fn collect_return_object_alias_renames(
    stmts: &[Stmt],
    renames: &mut BTreeMap<String, String>,
) {
    for stmt in stmts {
        match stmt {
            Stmt::Return(return_stmt) => {
                if let Some(expr) = &return_stmt.arg
                    && let Expr::Object(object) = &**expr
                {
                    for prop in &object.props {
                        if let PropOrSpread::Prop(prop) = prop
                            && let Prop::KeyValue(key_value) = &**prop
                            && let PropName::Ident(key) = &key_value.key
                            && let Expr::Ident(value) = &*key_value.value
                        {
                            let from = value.sym.to_string();
                            let to = key.sym.to_string();
                            if from != to && is_valid_js_identifier(&to) {
                                renames.insert(from, to);
                            }
                        }
                    }
                }
            }
            Stmt::Block(block) => collect_return_object_alias_renames(&block.stmts, renames),
            _ => {}
        }
    }
}

pub(super) fn collect_constructor_assignment_renames(
    stmt: &Stmt,
    param_names: &BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) {
    let Stmt::Expr(expr_stmt) = stmt else {
        return;
    };
    let Expr::Assign(assign) = &*expr_stmt.expr else {
        return;
    };
    if assign.op != AssignOp::Assign {
        return;
    }
    let Some(target_name) = this_property_name(&assign.left) else {
        return;
    };
    let Expr::Ident(value) = &*assign.right else {
        return;
    };
    let from = value.sym.to_string();
    if param_names.contains(&from) && from != target_name && is_valid_js_identifier(&target_name) {
        renames.insert(from, target_name);
    }
}

pub(super) fn this_property_name(target: &AssignTarget) -> Option<String> {
    let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = target else {
        return None;
    };
    if !matches!(&*member.obj, Expr::This(_)) {
        return None;
    }
    match &member.prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::Computed(computed) => match &*computed.expr {
            Expr::Lit(Lit::Str(value)) if is_valid_js_identifier(&str_value(value)) => {
                Some(str_value(value))
            }
            _ => None,
        },
        _ => None,
    }
}
