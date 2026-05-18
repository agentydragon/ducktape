//! Export-list manipulation: trim dead specifiers, reject spec
//! duplicates, auto-grow residual exports for cross-module references,
//! and convert binding maps into `export { ... }` ModuleItems.

use super::*;

pub(super) fn trim_dead_named_specifiers(
    body: &mut [ModuleItem],
    bindings: &HashMap<Id, BindingKind>,
) {
    let mut collector = RefCollector::default();
    for item in body.iter() {
        item.visit_with(&mut collector);
    }
    // We only need by-sym membership here (matching the pre-hygiene
    // `claimed && unused` check); collapse Ids to their syms.
    // `id.0` is the `swc_atoms::Atom` (a.k.a. `JsWord`) carried in
    // `Id = (Atom, SyntaxContext)`; we collect refs by sym for the
    // claimed-and-unused filter below.
    let refs: HashSet<_> = collector.ids.iter().map(|id| &id.0).collect();
    for item in body.iter_mut() {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        // Side-effect-only imports never had specifiers; leave
        // them alone (they exist to evaluate the imported module).
        if import.specifiers.is_empty() {
            continue;
        }
        import.specifiers.retain(|spec| match spec {
            ImportSpecifier::Default(_) | ImportSpecifier::Namespace(_) => true,
            ImportSpecifier::Named(named) => {
                // bindings is Id-keyed; match by sym (top-level
                // names are unique within a chunk).
                let local = named.local.sym.as_ref();
                let claimed = bindings.iter().any(|(id, _)| id.0.as_ref() == local);
                let unused = !refs.contains(&named.local.sym);
                !(claimed && unused)
            }
        });
        // The directive's `specifiers: vec![]` shape is itself a
        // side-effect-only import — `import "./mod.js";`. Keeping
        // it preserves the source-module evaluation that the
        // original entry depended on, regardless of whether any
        // moved logical module is loaded by the residual.
    }
}

pub(super) fn reject_duplicate_export_names(
    operation: &str,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for member in members {
        if !seen.insert(member.export_name.clone()) {
            duplicates.insert(member.export_name.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "{operation} {id} has duplicate exported logical names: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    Ok(())
}

pub(super) fn reject_duplicate_member_bindings(
    operation: &str,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for member in members {
        if !seen.insert(member.binding.clone()) {
            duplicates.insert(member.binding.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "{operation} {id} has duplicate source bindings: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    Ok(())
}

pub(super) fn export_named_for_bindings(bindings: &BTreeMap<String, String>) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(NamedExport {
        span: DUMMY_SP,
        specifiers: bindings
            .iter()
            .map(|(local, exported)| {
                ExportSpecifier::Named(ExportNamedSpecifier {
                    span: DUMMY_SP,
                    orig: ModuleExportName::Ident(Ident::new_no_ctxt(
                        local.clone().into(),
                        DUMMY_SP,
                    )),
                    exported: if local == exported {
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
        src: None,
        type_only: false,
        with: None,
    }))
}

pub(super) fn entry_exports_for_moved_bindings(
    declarations: &[TopLevelDecl],
    binding_assignment: &BTreeMap<String, usize>,
    entry_renames: &BTreeMap<String, String>,
) -> Vec<ModuleItem> {
    let mut exports = BTreeMap::<String, String>::new();
    for decl in declarations.iter().filter(|decl| decl.exported) {
        for name in &decl.names {
            if binding_assignment.contains_key(name) {
                let final_local = entry_renames
                    .get(name)
                    .cloned()
                    .unwrap_or_else(|| name.clone());
                exports.insert(final_local, name.clone());
            }
        }
    }
    if exports.is_empty() {
        Vec::new()
    } else {
        vec![export_named_for_bindings(&exports)]
    }
}

/// Compute the residual entry bindings every moved module body
/// references but entry doesn't yet export. The per-module emit
/// path needs every such reference to import from entry, so the
/// materializer auto-grows entry's export list to cover them — that
/// way peeling a body whose lazy/eager reads target an
/// unexported residual binding emits valid JS without making the
/// peel proposer responsible for predicting the materializer's
/// export policy. See DESIGN.md "Valid peels and atomic modules"
/// (importability clause).
///
/// Returns a `local → exported` map (with `local == exported`
/// because we surface the residual binding's own name); the caller
/// feeds it to `export_named_for_bindings`.
///
/// Skips:
/// - bindings already in `existing_exports` (the upstream source
///   exports plus the moved-binding re-exports already emitted by
///   `entry_exports_for_moved_bindings`),
/// - names not declared anywhere in the chunk
///   (`declaration_by_name` covers every top-level decl, so this is
///   the "globals / runtime imports / unknown ident" case the
///   per-module emit path silently lets fall through to the implicit
///   runtime resolution),
/// - bindings owned by a logical module (`binding_assignment`), which
///   are imported directly module→module rather than mediated by
///   entry.
pub(super) fn auto_grown_residual_exports(
    selected_by_module: &[Vec<ModuleItem>],
    declaration_by_name: &BTreeMap<String, usize>,
    binding_assignment: &BTreeMap<String, usize>,
    pre_existing_entry_exports: &BTreeSet<String>,
    entry_renames: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    let mut needed = BTreeSet::<String>::new();
    for body in selected_by_module {
        let facts = collect_module_body_facts(body);
        for id in &facts.referenced_idents {
            // Spec-derived `*_by_name` maps are still keyed by sym;
            // `provided_locals` / `imported_locals` are Id-keyed.
            let name_str = id.0.as_ref();
            if facts.provided_locals.contains(id) {
                continue;
            }
            if binding_assignment.contains_key(name_str) {
                continue;
            }
            if !declaration_by_name.contains_key(name_str) {
                continue;
            }
            if pre_existing_entry_exports.contains(name_str) {
                continue;
            }
            needed.insert(name_str.to_string());
        }
    }
    needed
        .into_iter()
        .map(|name| {
            let final_local = entry_renames
                .get(&name)
                .cloned()
                .unwrap_or_else(|| name.clone());
            (final_local, name)
        })
        .collect()
}
