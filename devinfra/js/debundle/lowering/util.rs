use super::*;

/// True iff `s` is a usable JavaScript identifier for the emitted ESM:
/// the start char is `[A-Za-z_$]`, the rest is `[A-Za-z0-9_$]`, and `s`
/// is not a reserved word in any ECMAScript context. Reserved words are
/// rejected because emitted modules are ESM (always strict mode) and a
/// base like `default` / `class` / `await` used directly in an
/// `import {...}` / `export {...}` clause would produce un-parseable JS
/// with no diagnostic. Reserved-word detection uses SWC's
/// `EsReserved::is_reserved_in_any` (from `swc_ecma_ast`), which covers
/// the union of sloppy-mode, strict-mode, and ES3 reserved sets. The
/// intent is also to filter typos (`with-dash`, `0digit`, empty string)
/// from spec authors.
pub(super) fn is_valid_js_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    let first = match chars.next() {
        Some(c) => c,
        None => return false,
    };
    if !(first.is_ascii_alphabetic() || first == '_' || first == '$') {
        return false;
    }
    if !chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '$') {
        return false;
    }
    !s.is_reserved_in_any()
}

pub(super) fn target_file_for_request(target_dir: &str, target_path: &str) -> Result<String> {
    let normalized = normalize_module_path(target_path)?;
    let with_ext = if normalized.ends_with(".js") {
        normalized
    } else {
        format!("{normalized}.js")
    };
    Ok(join_module_path(&[target_dir, &with_ext]))
}

pub(super) fn normalize_optional_relative_dir(value: &str) -> Result<String> {
    if value.is_empty() {
        return Ok(String::new());
    }
    normalize_module_path(value)
}

pub(super) fn remaining_item_after_selection(
    item: &ModuleItem,
    binding_assignment: &HashMap<Id, usize>,
    selected_by_module: &mut [Vec<ModuleItem>],
) -> Result<Vec<ModuleItem>> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => {
            split_var_decl(var, false, binding_assignment, selected_by_module)
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => match &export_decl.decl {
            Decl::Var(var) => split_var_decl(var, true, binding_assignment, selected_by_module),
            decl => {
                let ids = declaration_ids(decl);
                if let Some(module_index) = assigned_module_for_ids(&ids, binding_assignment) {
                    selected_by_module[module_index]
                        .push(ModuleItem::Stmt(Stmt::Decl(decl.clone())));
                    Ok(Vec::new())
                } else {
                    Ok(vec![item.clone()])
                }
            }
        },
        ModuleItem::Stmt(Stmt::Decl(decl)) => {
            let ids = declaration_ids(decl);
            if let Some(module_index) = assigned_module_for_ids(&ids, binding_assignment) {
                selected_by_module[module_index].push(item.clone());
                Ok(Vec::new())
            } else {
                Ok(vec![item.clone()])
            }
        }
        _ => Ok(vec![item.clone()]),
    }
}

pub(super) fn split_var_decl(
    var: &VarDecl,
    was_exported: bool,
    binding_assignment: &HashMap<Id, usize>,
    selected_by_module: &mut [Vec<ModuleItem>],
) -> Result<Vec<ModuleItem>> {
    let mut residual_decls = Vec::new();
    for declarator in &var.decls {
        let ids = binding_ids(&declarator.name);
        if let Some(module_index) = assigned_module_for_ids(&ids, binding_assignment) {
            let selected_var = VarDecl {
                span: var.span,
                ctxt: var.ctxt,
                kind: var.kind,
                declare: var.declare,
                decls: vec![declarator.clone()],
            };
            selected_by_module[module_index].push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(
                Box::new(selected_var),
            ))));
        } else {
            residual_decls.push(declarator.clone());
        }
    }
    if residual_decls.is_empty() {
        return Ok(Vec::new());
    }
    let residual_var = VarDecl {
        span: var.span,
        ctxt: var.ctxt,
        kind: var.kind,
        declare: var.declare,
        decls: residual_decls,
    };
    if was_exported {
        Ok(vec![ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(
            ExportDecl {
                span: DUMMY_SP,
                decl: Decl::Var(Box::new(residual_var)),
            },
        ))])
    } else {
        Ok(vec![ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(
            residual_var,
        ))))])
    }
}

pub(super) fn assigned_module_for_ids(
    ids: &[Id],
    binding_assignment: &HashMap<Id, usize>,
) -> Option<usize> {
    ids.iter()
        .filter_map(|id| binding_assignment.get(id).copied())
        .next()
}

/// Per-cause guidance for the atomic-unit-conflict bail message —
/// gives the spec author vocabulary to search for (`cycle`,
/// `side-effect`, `mutable`, `assignment`, `cross-destination`).
pub(super) fn render_atomic_unit_cause_guidance(conflicts: &[AtomicUnitConflict]) -> String {
    // `AtomicUnit::causes` is already a `BTreeSet<DepKind>` — gather
    // per-conflict causes into one `BTreeSet` so iteration stays
    // `DepKind`-`Ord`-stable without a post-collection sort.
    let causes: BTreeSet<DepKind> = conflicts
        .iter()
        .flat_map(|c| c.causes.iter().copied())
        .collect();
    let mut out = String::new();
    for cause in &causes {
        out.push_str(match cause {
            DepKind::EagerUse => {
                "EagerUse cycle: a top-level statement reads a binding at-init; \
                 splitting reader and declarer across modules forms an evaluation-order cycle. "
            }
            DepKind::EagerRebind | DepKind::LazyRebind => {
                "Rebind: a function or top-level statement performs an assignment \
                 to a mutable binding owned by a different module — the resulting ESM \
                 import would be read-only, so this cross-destination assignment is invalid. \
                 The assigner and the binding declarer must materialize together. "
            }
            DepKind::Sequenced => {
                "Sequenced side-effect chain: two top-level side-effect statements are \
                 forced into a fixed source order; splitting them across modules \
                 inverts the run order. "
            }
            DepKind::LocalEffect => {
                "Local effect: a trusted helper call mutates a target binding \
                 (for example a TypeScript decorator application on a class prototype); \
                 the mutating statement and target binding must materialize together. "
            }
            DepKind::LazyUse => continue,
        });
    }
    out
}
