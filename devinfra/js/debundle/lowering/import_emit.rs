use super::*;

/// Map plan-side `original -> exported` to `actual_local -> exported`.
///
/// When a spec gives a binding a readable exported name, prefer that
/// readable name as the consumer-side local too. That keeps the final
/// emitted tree from retaining the input-bundle name merely as an import
/// alias. Collisions still mint a fresh local through the ledger's
/// taken-name service ([`RenameLedger::mint`]) and get recorded in
/// `renames` so the consuming body can be rewritten after emission.
pub(super) fn disambiguate_import_locals(
    bindings: &BTreeMap<String, String>,
    ledger: &mut RenameLedger,
    scope: RenameScope,
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
            let actual = ledger.mint(scope, preferred);
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
    ledger: &mut RenameLedger,
    scope: RenameScope,
    renames: &mut BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    bindings
        .iter()
        .map(|(original, entry_export)| {
            let preferred = entry_export.local_name.as_str();
            let actual = ledger.mint(scope, preferred);
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

pub(super) fn import_decl_for_plan(
    entry_file: &str,
    target_file: &str,
    bindings: &BTreeMap<String, String>,
) -> ModuleItem {
    let source = relative_source(entry_file, target_file);
    let specifiers: Vec<ImportSpecifier> = bindings
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
        .collect();
    import_decl_module_item(specifiers, &source)
}
