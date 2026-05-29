//! Naturalization: rewrite body identifiers to readable names + collapse
//! `{ x: x }` shorthand. Combines plan-driven renames (from spec) with
//! heuristic renames derived from the AST (return-object aliases,
//! destructure unpacks, constructor `this.x = param` mappings).
//!
//! `naturalize_module_body` is the public entry; everything else is a
//! contributor of rename intents that get merged via `drop_target_collisions`.

use super::util::is_valid_js_identifier;
use super::*;

pub(super) fn naturalize_module_body(
    body: &mut [ModuleItem],
    plan: &ModulePlan,
) -> BTreeMap<String, String> {
    let mut plan_driven = BTreeMap::<String, String>::new();
    // Stable iteration over `plan.bindings` (a HashMap) so the order
    // renames land in `plan_driven` — and thus the rename-precedence the
    // visitor applies when two locals compete for the same target —
    // doesn't vary by hash seed.
    let mut sorted_bindings: Vec<(&String, &String)> = plan.bindings.iter().collect();
    sorted_bindings.sort_by(|a, b| a.0.cmp(b.0));
    for (local, exported) in sorted_bindings {
        if local != exported && is_valid_js_identifier(exported) {
            plan_driven.insert(local.clone(), exported.clone());
        }
    }
    let mut heuristic = BTreeMap::<String, String>::new();
    for item in body.iter() {
        collect_naturalization_renames_from_item(item, &mut heuristic);
    }
    // The merged map is what callers read back as the local-name → readable
    // lookup. Application below splits the two categories: plan-driven names
    // are top-level module bindings and rename module-wide (suppressed in any
    // subtree that re-binds them); heuristic names are derived from a single
    // function/constructor/arrow's own params, return-object aliases, or
    // `this.x = param` assignments and must rename only within that node's
    // own subtree — applying them module-wide would rewrite an unrelated
    // binding of the same name in a sibling scope.
    let merged = drop_target_collisions(plan_driven.clone(), heuristic);
    // Heuristic-only entries (those not also plan-driven) are the scope-local
    // ones; plan-driven entries that survived collision-dropping apply
    // module-wide.
    let plan_driven_effective: BTreeMap<String, String> = merged
        .iter()
        .filter(|(local, _)| plan_driven.contains_key(*local))
        .map(|(local, target)| (local.clone(), target.clone()))
        .collect();
    let heuristic_effective: BTreeMap<String, String> = merged
        .iter()
        .filter(|(local, _)| !plan_driven.contains_key(*local))
        .map(|(local, target)| (local.clone(), target.clone()))
        .collect();

    if plan_driven_effective.is_empty() && heuristic_effective.is_empty() {
        for item in body.iter_mut() {
            item.visit_mut_with(&mut ShorthandNaturalizer);
        }
        return merged;
    }

    // Apply plan-driven (module-scope) renames + shorthand collapse first,
    // module-wide but scope-aware. Shorthand collapse here covers the
    // top-level object literals/patterns.
    if !plan_driven_effective.is_empty() {
        let mut naturalizer = RenameAndShorthandNaturalizer::new(&plan_driven_effective);
        for item in body.iter_mut() {
            item.visit_mut_with(&mut naturalizer);
        }
    }

    // Apply heuristic (scope-local) renames per the function/constructor/arrow
    // they were derived from. `ScopedHeuristicNaturalizer` recurses into the
    // body, and at each function-like node computes that node's own heuristic
    // renames and rewrites just that node's subtree (its params + body),
    // suppressing the rename inside any further-nested subtree that re-binds
    // the same name.
    if !heuristic_effective.is_empty() {
        let mut scoped = ScopedHeuristicNaturalizer {
            allowed: &heuristic_effective,
        };
        for item in body.iter_mut() {
            item.visit_mut_with(&mut scoped);
        }
    } else if plan_driven_effective.is_empty() {
        // No heuristics survived and no plan-driven renames ran: still collapse
        // shorthand. (Reached only when `merged` is non-empty but every entry
        // was a heuristic dropped by collision handling — rare, but keep the
        // shorthand pass.)
        for item in body.iter_mut() {
            item.visit_mut_with(&mut ShorthandNaturalizer);
        }
    }

    merged
}

/// Walks the module body and applies heuristic naturalization renames
/// scope-locally: at each function/constructor/arrow it derives that node's
/// own heuristic renames (param destructure aliases, return-object aliases,
/// `this.x = param` constructor assignments) and rewrites only that node's
/// subtree. A heuristic rename derived from one scope must never leak into a
/// sibling/parent scope that binds the same name, so the rewrite is confined
/// to the deriving node — and `RenameAndShorthandNaturalizer`'s nested
/// shadow-suppression still guards any further-nested re-binding subtree.
struct ScopedHeuristicNaturalizer<'a> {
    /// The merged set of heuristic renames that survived collision-dropping;
    /// a node's locally-derived rename only fires if it appears here.
    allowed: &'a BTreeMap<String, String>,
}

impl ScopedHeuristicNaturalizer<'_> {
    /// Filter a node's locally-derived renames down to the globally-allowed
    /// set, returning the effective scope-local rename map (empty if nothing
    /// fires).
    fn effective(&self, derived: BTreeMap<String, String>) -> BTreeMap<String, String> {
        derived
            .into_iter()
            .filter(|(from, to)| self.allowed.get(from) == Some(to))
            .collect()
    }
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
        // Recurse first so nested functions apply their own heuristics.
        function.visit_mut_children_with(self);
        let mut derived = BTreeMap::new();
        collect_naturalization_renames_from_function(function, &mut derived);
        let local = self.effective(derived);
        if local.is_empty() {
            return;
        }
        // Params and the root body share this function's scope, where the
        // renamed name is bound exactly once and every reference resolves to
        // it. Drive past the root param/block scope (visiting params and the
        // body's statements directly) so the own-binding isn't treated as a
        // shadow; nested subtrees still suppress.
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        function.params.visit_mut_with(&mut renamer);
        rename_root_body(&mut renamer, function.body.as_mut());
    }

    fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
        arrow.visit_mut_children_with(self);
        let mut derived = BTreeMap::new();
        for param in &arrow.params {
            collect_naturalization_renames_from_pattern(param, &mut derived);
        }
        let local = self.effective(derived);
        if local.is_empty() {
            return;
        }
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        arrow.params.visit_mut_with(&mut renamer);
        match &mut *arrow.body {
            BlockStmtOrExpr::BlockStmt(block) => rename_root_body(&mut renamer, Some(block)),
            BlockStmtOrExpr::Expr(expr) => expr.visit_mut_with(&mut renamer),
        }
    }

    fn visit_mut_constructor(&mut self, constructor: &mut Constructor) {
        constructor.visit_mut_children_with(self);
        let mut derived = BTreeMap::new();
        collect_naturalization_renames_from_constructor(constructor, &mut derived);
        let local = self.effective(derived);
        if local.is_empty() {
            return;
        }
        let mut renamer = RenameAndShorthandNaturalizer::new(&local);
        for param in &mut constructor.params {
            param.visit_mut_with(&mut renamer);
        }
        rename_root_body(&mut renamer, constructor.body.as_mut());
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

pub(super) fn collect_naturalization_renames_from_item(
    item: &ModuleItem,
    renames: &mut BTreeMap<String, String>,
) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => collect_from_decl(decl, renames),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            collect_from_decl(&export_decl.decl, renames);
        }
        _ => {}
    }
}

fn collect_from_decl(decl: &Decl, renames: &mut BTreeMap<String, String>) {
    match decl {
        Decl::Fn(function) => {
            collect_naturalization_renames_from_function(&function.function, renames);
        }
        Decl::Class(class) => {
            collect_naturalization_renames_from_class(&class.class, renames);
        }
        Decl::Var(var) => {
            for declarator in &var.decls {
                if let Some(init) = declarator.init.as_ref() {
                    collect_naturalization_renames_from_expr(init, renames);
                }
            }
        }
        _ => {}
    }
}

pub(super) fn collect_naturalization_renames_from_expr(
    expr: &Expr,
    renames: &mut BTreeMap<String, String>,
) {
    match expr {
        Expr::Fn(function) => {
            collect_naturalization_renames_from_function(&function.function, renames)
        }
        Expr::Arrow(arrow) => {
            for param in &arrow.params {
                collect_naturalization_renames_from_pattern(param, renames);
            }
        }
        Expr::Class(class) => collect_naturalization_renames_from_class(&class.class, renames),
        _ => {}
    }
}

pub(super) fn collect_naturalization_renames_from_function(
    function: &Function,
    renames: &mut BTreeMap<String, String>,
) {
    for param in &function.params {
        collect_naturalization_renames_from_pattern(&param.pat, renames);
    }
    let Some(body) = function.body.as_ref() else {
        return;
    };
    collect_return_object_alias_renames(&body.stmts, renames);
}

pub(super) fn collect_naturalization_renames_from_class(
    class: &Class,
    renames: &mut BTreeMap<String, String>,
) {
    for member in &class.body {
        if let ClassMember::Constructor(constructor) = member {
            collect_naturalization_renames_from_constructor(constructor, renames);
        }
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
