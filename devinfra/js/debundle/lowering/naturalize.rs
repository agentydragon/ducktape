//! Naturalization: rewrite body identifiers to readable names + collapse
//! `{ x: x }` shorthand. Combines plan-driven renames (from spec) with
//! heuristic renames derived from the AST (return-object aliases,
//! destructure unpacks, constructor `this.x = param` mappings).
//!
//! `naturalize_module_body` is the public entry; everything else is a
//! contributor of rename intents submitted to the per-module
//! `LoweringPlan` for conflict resolution.

use swc_atoms::Atom;
use swc_common::Mark;

use super::lowering_plan::{
    LoweringOp, LoweringPlan, NamePolicy, Priority, Scope, SubmitOutcome, SubmitPolicy,
};
use super::util::is_identifier_like;
use super::*;

/// Phase 6: naturalize a moved module's body via the per-module
/// `LoweringPlan`. Spec-driven renames from `module_spec.bindings`
/// flow in at `Priority::Explicit`; heuristic renames inferred
/// from AST shape flow in at `Priority::Heuristic` with
/// `SubmitPolicy::SkipIfClaimed` so they defer to spec-driven
/// claims and to chunk-wide `occupied` collisions. The plan's
/// `is_name_taken` check replaces the legacy
/// `drop_target_collisions` logic.
pub(super) fn naturalize_module_body(
    body: &mut [ModuleItem],
    module_spec: &ModulePlan,
    plan: &mut LoweringPlan,
    chunk_top_level_mark: Mark,
) -> Result<BTreeMap<String, String>> {
    let mut renames = BTreeMap::<String, String>::new();
    // Stable iteration over `module_spec.bindings` (a HashMap) so
    // the order doesn't vary by hash seed.
    let mut sorted_bindings: Vec<(&String, &String)> = module_spec.bindings.iter().collect();
    sorted_bindings.sort_by(|a, b| a.0.cmp(b.0));
    for (local, exported) in sorted_bindings {
        if local == exported || !is_identifier_like(exported) {
            continue;
        }
        let original = top_level_id(local, chunk_top_level_mark);
        match plan.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original,
                name: NamePolicy::Required(Atom::from(exported.as_str())),
                reason: "naturalize_module_spec",
                priority: Priority::Explicit,
            },
            SubmitPolicy::Fail,
        ) {
            Ok(SubmitOutcome::Accepted {
                final_op:
                    LoweringOp::Rename {
                        name: NamePolicy::Required(atom),
                        ..
                    },
            }) => {
                renames.insert(local.clone(), atom.to_string());
            }
            Ok(other) => bail!(
                "unexpected submit outcome for module-spec rename \
                 ({local} → {exported}): {other:?}"
            ),
            // Target-name collision (the spec asks for a name that
            // collides with an existing local). Legacy
            // `naturalize_module_body` silently dropped the
            // rename in this case via the `is_identifier_like`
            // filter combined with `drop_target_collisions`;
            // match that lenient behavior here too.
            Err(_) => continue,
        }
    }
    let mut heuristic = BTreeMap::<String, String>::new();
    for item in body.iter() {
        collect_naturalization_renames_from_item(item, &mut heuristic);
    }
    let mut sorted_heuristic: Vec<(&String, &String)> = heuristic.iter().collect();
    sorted_heuristic.sort_by(|a, b| a.0.cmp(b.0));
    for (local, target) in sorted_heuristic {
        if renames.contains_key(local) {
            continue;
        }
        let original = top_level_id(local, chunk_top_level_mark);
        match plan.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original,
                name: NamePolicy::Required(Atom::from(target.as_str())),
                reason: "naturalize_heuristic",
                priority: Priority::Heuristic,
            },
            SubmitPolicy::SkipIfClaimed,
        ) {
            Ok(SubmitOutcome::Accepted {
                final_op:
                    LoweringOp::Rename {
                        name: NamePolicy::Required(atom),
                        ..
                    },
            }) => {
                renames.insert(local.clone(), atom.to_string());
            }
            Ok(SubmitOutcome::Skipped { .. }) => {}
            Ok(other) => bail!(
                "unexpected submit outcome for heuristic naturalize rename \
                 ({local} → {target}): {other:?}"
            ),
            // Target-name collision: skip the heuristic (legacy
            // `drop_target_collisions` parity).
            Err(_) => continue,
        }
    }
    if renames.is_empty() {
        for item in body.iter_mut() {
            item.visit_mut_with(&mut ShorthandNaturalizer);
        }
    } else {
        let mut naturalizer = RenameAndShorthandNaturalizer { renames: &renames };
        for item in body.iter_mut() {
            item.visit_mut_with(&mut naturalizer);
        }
    }
    Ok(renames)
}

pub(super) fn collect_naturalization_renames_from_item(
    item: &ModuleItem,
    renames: &mut BTreeMap<String, String>,
) {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => {
            collect_naturalization_renames_from_function(&function.function, renames);
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => {
            collect_naturalization_renames_from_class(&class.class, renames);
        }
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            for declarator in &var.decls {
                if let Some(init) = declarator.init.as_ref() {
                    collect_naturalization_renames_from_expr(init, renames);
                }
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match &export_decl.decl {
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
        },
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
        let ClassMember::Constructor(constructor) = member else {
            continue;
        };
        let mut param_names = BTreeSet::new();
        for param in &constructor.params {
            if let ParamOrTsParamProp::Param(param) = param
                && let Pat::Ident(ident) = &param.pat
            {
                param_names.insert(ident.id.sym.to_string());
            }
        }
        let Some(body) = constructor.body.as_ref() else {
            continue;
        };
        for statement in &body.stmts {
            collect_constructor_assignment_renames(statement, &param_names, renames);
        }
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
                            if from != to && is_identifier_like(&to) {
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
                            if from != to && is_identifier_like(&to) {
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
    if param_names.contains(&from) && from != target_name && is_identifier_like(&target_name) {
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
            Expr::Lit(Lit::Str(value)) if is_identifier_like(&str_value(value)) => {
                Some(str_value(value))
            }
            _ => None,
        },
        _ => None,
    }
}
