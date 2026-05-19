//! Naturalization: rewrite body identifiers to readable names + collapse
//! `{ x: x }` shorthand. Combines plan-driven renames (from spec) with
//! heuristic renames derived from the AST (return-object aliases,
//! destructure unpacks, constructor `this.x = param` mappings).
//!
//! `submit_naturalize_renames` is the public entry; every contributor
//! emits `(Id, Atom)` pairs (hygiene-preserving) so the plan's
//! `is_name_taken` check and the plan-aware execute visitor can match
//! function-local bindings as precisely as chunk-top-level ones.

use swc_atoms::Atom;
use swc_common::Mark;

use super::lowering_plan::{
    LoweringOp, LoweringPlan, NamePolicy, Priority, Scope, SubmitOutcome, SubmitPolicy,
};
use super::util::is_identifier_like;
use super::*;

/// Phase 6: submit spec-driven (Explicit) + heuristic (Heuristic /
/// SkipIfClaimed) naturalize renames to the per-module
/// `LoweringPlan`. Does NOT mutate `body`; the caller applies via
/// `apply_plan_renames_and_naturalize` after every per-module
/// rename contributor has submitted.
///
/// Returns the accepted rename map (atom → atom, for callers that
/// still need a `local → exported` view — notably
/// `plan_module_reference_needs`'s reverse-lookup, Phase 9
/// retires it).
pub(super) fn submit_naturalize_renames(
    body: &[ModuleItem],
    module_spec: &ModulePlan,
    plan: &mut LoweringPlan,
    chunk_top_level_mark: Mark,
) -> Result<BTreeMap<String, String>> {
    let mut renames = BTreeMap::<String, String>::new();
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
            Err(_) => continue,
        }
    }
    // Heuristic renames: collect (Id, Atom) pairs and submit each
    // with `SkipIfClaimed` so they defer to spec-driven claims.
    let mut heuristic = BTreeMap::<Id, Atom>::new();
    for item in body {
        collect_naturalization_renames_from_item(item, &mut heuristic);
    }
    let mut sorted_heuristic: Vec<(&Id, &Atom)> = heuristic.iter().collect();
    sorted_heuristic.sort_by(|a, b| (&a.0.0, a.0.1).cmp(&(&b.0.0, b.0.1)));
    for (original_id, target) in sorted_heuristic {
        if renames.contains_key(original_id.0.as_str()) {
            continue;
        }
        match plan.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original: original_id.clone(),
                name: NamePolicy::Required(target.clone()),
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
                renames.insert(original_id.0.to_string(), atom.to_string());
            }
            Ok(SubmitOutcome::Skipped { .. }) => {}
            Ok(other) => bail!(
                "unexpected submit outcome for heuristic naturalize rename \
                 ({:?} → {target}): {other:?}",
                original_id.0
            ),
            Err(_) => continue,
        }
    }
    Ok(renames)
}

pub(super) fn collect_naturalization_renames_from_item(
    item: &ModuleItem,
    renames: &mut BTreeMap<Id, Atom>,
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
    renames: &mut BTreeMap<Id, Atom>,
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
    renames: &mut BTreeMap<Id, Atom>,
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
    renames: &mut BTreeMap<Id, Atom>,
) {
    for member in &class.body {
        let ClassMember::Constructor(constructor) = member else {
            continue;
        };
        let mut param_ids = BTreeMap::<String, Id>::new();
        for param in &constructor.params {
            if let ParamOrTsParamProp::Param(param) = param
                && let Pat::Ident(ident) = &param.pat
            {
                param_ids.insert(ident.id.sym.to_string(), ident.id.to_id());
            }
        }
        let Some(body) = constructor.body.as_ref() else {
            continue;
        };
        for statement in &body.stmts {
            collect_constructor_assignment_renames(statement, &param_ids, renames);
        }
    }
}

pub(super) fn collect_naturalization_renames_from_pattern(
    pat: &Pat,
    renames: &mut BTreeMap<Id, Atom>,
) {
    match pat {
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        if let PropName::Ident(key) = &key_value.key
                            && let Pat::Ident(value) = &*key_value.value
                        {
                            let from = value.id.to_id();
                            let to = key.sym.clone();
                            if from.0 != to && is_identifier_like(&to) {
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
    renames: &mut BTreeMap<Id, Atom>,
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
                            let from = value.to_id();
                            let to = key.sym.clone();
                            if from.0 != to && is_identifier_like(&to) {
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
    param_ids: &BTreeMap<String, Id>,
    renames: &mut BTreeMap<Id, Atom>,
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
    let value_id = value.to_id();
    let Some(param_id) = param_ids.get(value_id.0.as_str()) else {
        return;
    };
    // Require the assignment's RHS Id to match the param's Id (hygiene)
    // — guards against shadowing where a different binding with the
    // same atom appears in the constructor body.
    if param_id != &value_id {
        return;
    }
    let target_atom = Atom::from(target_name.as_str());
    if value_id.0 != target_atom && is_identifier_like(&target_atom) {
        renames.insert(value_id, target_atom);
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
        MemberProp::Ident(name) => Some(name.sym.to_string()),
        _ => None,
    }
}
