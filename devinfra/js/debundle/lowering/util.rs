//! Pure helpers used across the lowering pipeline: identifier
//! validation, body/ordinal arithmetic, local-binding collection,
//! disambiguation, and small I/O wrappers.

use super::*;

/// True iff `s` is a valid JavaScript identifier — start char is
/// `[A-Za-z_$]` and rest is `[A-Za-z0-9_$]`. Reserved words are not
/// rejected (a target named e.g. `class` or `let` would still trip
/// at parse time downstream, but that's a louder failure than this
/// shallow check would catch). The intent is to filter typos
/// (`with-dash`, `0digit`, empty string) from spec authors.
pub(super) fn is_valid_js_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    let first = match chars.next() {
        Some(c) => c,
        None => return false,
    };
    if !(first.is_ascii_alphabetic() || first == '_' || first == '$') {
        return false;
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '$')
}

/// Number of post-comma-list-split positions a top-level body
/// item produces. `var x = …, y = …;` is one body item but two
/// post-split owners (and therefore two `StatementOrdinal`s in
/// the owner graph). All other top-level items count as one.
/// Mirrors the splitting in `facts::top_level_item_views`.
pub(super) fn post_split_top_level_count(item: &ModuleItem) -> usize {
    fn decl_count(decl: &Decl) -> usize {
        match decl {
            Decl::Var(var) if var.decls.len() > 1 => var.decls.len(),
            _ => 1,
        }
    }
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl_count(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            decl_count(&export_decl.decl)
        }
        _ => 1,
    }
}

/// Convert a pre-split body index to the first post-split
/// `StatementOrdinal` value for that body item. For anonymous
/// statements (which never split), this is the only ordinal in
/// the resulting range.
pub(super) fn statement_ordinal_for_body_index(body: &[ModuleItem], body_idx: usize) -> usize {
    body[..body_idx]
        .iter()
        .map(post_split_top_level_count)
        .sum()
}

/// Inverse of [`statement_ordinal_for_body_index`]: given a post-split
/// statement ordinal, return the pre-split body index of the body item
/// that produced it. Returns `None` if the ordinal is past the body.
pub(super) fn body_index_for_statement_ordinal(
    body: &[ModuleItem],
    stmt_ordinal: usize,
) -> Option<usize> {
    let mut running = 0usize;
    for (idx, item) in body.iter().enumerate() {
        let count = post_split_top_level_count(item);
        if stmt_ordinal < running + count {
            return Some(idx);
        }
        running += count;
    }
    None
}

/// `unassigned_mode == MiniFactors`: for each atomic factor
/// unit whose members are entirely unclaimed by the YAML spec (i.e.
/// either currently sitting in the residual catch-all or never
/// assigned to any plan), synthesize a stand-alone [`ModulePlan`]
/// containing exactly those members. Bindings and anonymous
/// statements that were temporarily routed through the residual
/// plan are moved into the synthesized plan; the residual plan then
/// only holds whatever truly couldn't be peeled (typically nothing
/// for clean chunks).
///
pub(super) fn is_identifier_like(name: &str) -> bool {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !(first == '_' || first == '$' || first.is_ascii_alphabetic()) {
        return false;
    }
    chars.all(|ch| ch == '_' || ch == '$' || ch.is_ascii_alphanumeric())
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

/// Names occupying the file-scope binding namespace of `body`.
///
/// Used to disambiguate consumer-side `import { exportedName as localName }`
/// emissions whose `localName` would collide with another binding in the
/// same scope (e.g. a surviving import or top-level declaration that
/// already uses the input-bundle name). `export { name }` re-exports without
/// `from` are references, not bindings, so they aren't tracked here; the
/// IdentifierRenamer pass that follows the disambiguation rewrites their
/// `orig` ident along with every other body reference.
pub(super) fn collect_occupied_local_names(body: &[ModuleItem]) -> BTreeSet<String> {
    let mut occupied = BTreeSet::new();
    for item in body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
                for specifier in &import.specifiers {
                    match specifier {
                        ImportSpecifier::Named(named) => {
                            occupied.insert(named.local.sym.to_string());
                        }
                        ImportSpecifier::Default(default) => {
                            occupied.insert(default.local.sym.to_string());
                        }
                        ImportSpecifier::Namespace(namespace) => {
                            occupied.insert(namespace.local.sym.to_string());
                        }
                    }
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declaration_names(&export_decl.decl) {
                    occupied.insert(name);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                if let DefaultDecl::Class(class) = &default_decl.decl
                    && let Some(ident) = &class.ident
                {
                    occupied.insert(ident.sym.to_string());
                }
                if let DefaultDecl::Fn(function) = &default_decl.decl
                    && let Some(ident) = &function.ident
                {
                    occupied.insert(ident.sym.to_string());
                }
            }
            ModuleItem::Stmt(Stmt::Decl(decl)) => {
                for name in declaration_names(decl) {
                    occupied.insert(name);
                }
            }
            _ => {}
        }
    }
    occupied
}

/// Names bound anywhere under `body`. This is stricter than file-scope
/// occupancy: readable import locals must avoid nested bindings too, or the
/// follow-up body rewrite can accidentally capture references that were
/// supposed to resolve to the import.
pub(super) fn collect_local_binding_names(body: &[ModuleItem]) -> BTreeSet<String> {
    struct Collector {
        names: BTreeSet<String>,
    }

    impl Visit for Collector {
        fn visit_binding_ident(&mut self, ident: &BindingIdent) {
            self.names.insert(ident.id.sym.to_string());
        }

        fn visit_class_decl(&mut self, decl: &ClassDecl) {
            self.names.insert(decl.ident.sym.to_string());
            decl.class.visit_with(self);
        }

        fn visit_class_expr(&mut self, expr: &ClassExpr) {
            if let Some(ident) = &expr.ident {
                self.names.insert(ident.sym.to_string());
            }
            expr.class.visit_with(self);
        }

        fn visit_fn_decl(&mut self, decl: &FnDecl) {
            self.names.insert(decl.ident.sym.to_string());
            decl.function.visit_with(self);
        }

        fn visit_fn_expr(&mut self, expr: &FnExpr) {
            if let Some(ident) = &expr.ident {
                self.names.insert(ident.sym.to_string());
            }
            expr.function.visit_with(self);
        }

        fn visit_import_default_specifier(&mut self, specifier: &ImportDefaultSpecifier) {
            self.names.insert(specifier.local.sym.to_string());
        }

        fn visit_import_named_specifier(&mut self, specifier: &ImportNamedSpecifier) {
            self.names.insert(specifier.local.sym.to_string());
        }

        fn visit_import_star_as_specifier(&mut self, specifier: &ImportStarAsSpecifier) {
            self.names.insert(specifier.local.sym.to_string());
        }
    }

    let mut collector = Collector {
        names: BTreeSet::new(),
    };
    for item in body {
        item.visit_with(&mut collector);
    }
    collector.names
}

/// Map plan-side `original -> exported` to `actual_local -> exported`.
///
/// When a spec gives a binding a readable exported name, prefer that
/// readable name as the consumer-side local too. That keeps the final
/// emitted tree from retaining the input-bundle name merely as an import
/// alias. Collisions still mint a fresh local and get recorded in
/// `renames` so the entry body can be rewritten after emission.
pub(super) fn disambiguate_import_locals(
    bindings: &BTreeMap<String, String>,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    bindings
        .iter()
        .map(|(original, exported)| {
            let preferred = if exported != original {
                exported.as_str()
            } else {
                original.as_str()
            };
            let actual = if occupied.contains(preferred) {
                mint_fresh_local_name(preferred, occupied)
            } else {
                preferred.to_string()
            };
            occupied.insert(actual.clone());
            if actual != *original {
                renames.insert(original.clone(), actual.clone());
            }
            (actual, exported.clone())
        })
        .collect()
}

/// Map residual-entry imports from `original -> entry export` to
/// `actual_local -> exported`.
///
/// Unlike logical-module imports, the readable local is not the entry's
/// public export name. Entry exports can be minified aliases that collide with
/// unrelated source locals (`export { DialogButtonRow as B }` while source
/// local `B` is a vendor import). Prefer the entry's actual local name so the
/// moved body keeps referring to the same residual binding it referenced in
/// the original chunk.
pub(super) fn disambiguate_residual_entry_import_locals(
    bindings: &BTreeMap<String, EntryExport>,
    occupied: &mut BTreeSet<String>,
    renames: &mut BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    bindings
        .iter()
        .map(|(original, entry_export)| {
            let preferred = entry_export.local_name.as_str();
            let actual = if occupied.contains(preferred) {
                mint_fresh_local_name(preferred, occupied)
            } else {
                preferred.to_string()
            };
            occupied.insert(actual.clone());
            if actual != *original {
                renames.insert(original.clone(), actual.clone());
            }
            (actual, entry_export.exported_name.clone())
        })
        .collect()
}

/// Pre-fill `exported` on `export { local }` re-export specifiers whose
/// `local` is about to be renamed, so the public export name survives.
pub(super) fn preserve_export_specifier_names(
    item: &mut ModuleItem,
    renames: &BTreeMap<String, String>,
) {
    let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
        return;
    };
    for specifier in &mut named.specifiers {
        let ExportSpecifier::Named(spec) = specifier else {
            continue;
        };
        if spec.exported.is_some() {
            continue;
        }
        let ModuleExportName::Ident(orig) = &spec.orig else {
            continue;
        };
        if !renames.contains_key(&orig.sym.to_string()) {
            continue;
        }
        spec.exported = Some(spec.orig.clone());
    }
}

pub(super) fn mint_fresh_local_name(base: &str, occupied: &BTreeSet<String>) -> String {
    let mut suffix = 1usize;
    loop {
        let candidate = format!("{base}${suffix}");
        if !occupied.contains(&candidate) {
            return candidate;
        }
        suffix += 1;
    }
}

pub(super) fn import_decl_for_plan(
    entry_file: &str,
    target_file: &str,
    bindings: &BTreeMap<String, String>,
) -> ModuleItem {
    let source = relative_source(entry_file, target_file);
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span: DUMMY_SP,
        specifiers: bindings
            .iter()
            .map(|(local, exported)| {
                ImportSpecifier::Named(ImportNamedSpecifier {
                    span: DUMMY_SP,
                    local: Ident::new_no_ctxt(local.clone().into(), DUMMY_SP),
                    imported: if local == exported {
                        None
                    } else {
                        Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                            exported.clone().into(),
                            DUMMY_SP,
                        )))
                    },
                    is_type_only: false,
                })
            })
            .collect(),
        src: Box::new(Str {
            span: DUMMY_SP,
            value: source.into(),
            raw: None,
        }),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}

pub(super) fn relative_source(from_file: &str, target_file: &str) -> String {
    let from_dir = std::path::Path::new(from_file)
        .parent()
        .and_then(|parent| parent.to_str())
        .unwrap_or("")
        .replace('\\', "/");
    let mut rel = relative_module_path(&from_dir, target_file);
    if !rel.starts_with('.') {
        rel = format!("./{rel}");
    }
    rel
}

pub(super) fn prune_artifact_to_chunk_ids(artifact: &mut ChunkBundle, selected: &[String]) {
    let selected_ids: std::collections::HashSet<ChunkId> = selected
        .iter()
        .filter_map(|name| artifact.chunk_table.get(name))
        .collect();
    artifact.retain_chunks(|chunk_id| selected_ids.contains(&chunk_id));
}

pub(super) fn write_chunk_report_json<T: Serialize>(
    report_out_dir: &Path,
    chunk_id: &str,
    filename: &str,
    value: &T,
) -> Result<()> {
    let path = report_out_dir
        .join(chunk_id.split('/').collect::<PathBuf>())
        .join(filename);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if filename == "owner_graph.json" {
        // This side output is large enough on real app chunks that pretty
        // printing meaningfully affects local and remote test artifact size.
        // Keep small human-first reports pretty; keep the graph jq-first.
        let mut output = BufWriter::new(fs::File::create(path)?);
        serde_json::to_writer(&mut output, value)?;
        writeln!(output)?;
    } else {
        let body = serde_json::to_string_pretty(value)?;
        fs::write(path, body + "\n")?;
    }
    Ok(())
}

pub(super) fn prepare_output_dir(out_dir: &Path, force: bool) -> Result<()> {
    if out_dir.exists() {
        if !out_dir.is_dir() {
            bail!(
                "Output path exists and is not a directory: {}",
                out_dir.display()
            );
        }
        if fs::read_dir(out_dir)?.next().is_some() && !force {
            bail!(
                "Output directory is not empty: {}. Pass --force to replace it.",
                out_dir.display()
            );
        }
        if force {
            fs::remove_dir_all(out_dir)?;
        }
    }
    fs::create_dir_all(out_dir)?;
    Ok(())
}

/// Per-cause guidance for the atomic-unit-conflict bail message —
/// gives the spec author vocabulary to search for (`cycle`,
/// `side-effect`, `mutable`, `assignment`, `cross-destination`).
pub(super) fn render_atomic_unit_cause_guidance(conflicts: &[AtomicUnitConflict]) -> String {
    let mut causes: Vec<DepKind> = conflicts
        .iter()
        .flat_map(|c| c.causes.iter().copied())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    causes.sort();
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
