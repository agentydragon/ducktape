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
    // Build a sym-only set of binding names up front so the
    // per-specifier "is this name claimed by any binding?" probe is
    // O(1) instead of an O(N) scan over `bindings`. The previous
    // `bindings.iter().any(...)` made the loop O(specifiers × bindings) —
    // ~2s on gaffer's main chunk where `bindings` is in the thousands.
    // Mirrors the same shape used by `build_module_plans` in commit
    // `6ac23db1f`. The set is scoped to this function and dropped
    // when we return.
    let claimed_syms: HashSet<&str> = bindings.keys().map(|id| id.0.as_ref()).collect();
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
                let claimed = claimed_syms.contains(local);
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
    reject_duplicate_field(operation, id, "exported logical names", members, |m| {
        &m.export_name
    })
}

pub(super) fn reject_duplicate_member_bindings(
    operation: &str,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    reject_duplicate_field(operation, id, "source bindings", members, |m| &m.binding)
}

fn reject_duplicate_field(
    operation: &str,
    id: &str,
    label: &str,
    members: &[MemberRequest],
    extract: impl Fn(&MemberRequest) -> &str,
) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for member in members {
        let value = extract(member);
        if !seen.insert(value.to_string()) {
            duplicates.insert(value.to_string());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "{operation} {id} has duplicate {label}: {}",
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
    binding_assignment: &HashMap<Id, usize>,
    entry_renames: &BTreeMap<String, String>,
) -> Vec<ModuleItem> {
    let mut exports = BTreeMap::<String, String>::new();
    for decl in declarations.iter().filter(|decl| decl.exported) {
        for (name, id) in &decl.bindings {
            if binding_assignment.contains_key(id) {
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
    declaration_by_name: &HashMap<Id, usize>,
    binding_assignment: &HashMap<Id, usize>,
    pre_existing_entry_exports: &HashSet<Id>,
    pre_existing_public_export_names: &HashSet<String>,
    entry_renames: &BTreeMap<String, String>,
) -> BTreeMap<String, String> {
    let mut needed = BTreeSet::<String>::new();
    for body in selected_by_module {
        let facts = collect_module_body_facts(body);
        for id in &facts.referenced_idents {
            if facts.provided_locals.contains(id) {
                continue;
            }
            if binding_assignment.contains_key(id) {
                continue;
            }
            if !declaration_by_name.contains_key(id) {
                continue;
            }
            if pre_existing_entry_exports.contains(id) {
                continue;
            }
            needed.insert(id.0.as_ref().to_string());
        }
    }
    // `taken_public_names` accumulates every public name already
    // committed to entry's export list — the source-level set we
    // were handed, plus each new grown public name as we mint it.
    // When a candidate's natural public name collides, suffix-mint
    // a fresh `<name>$<n>` instead of skipping: skipping forces the
    // peeled module's body reference to resolve as an unexported
    // residual binding and `residual_entry_imports_for_moved_body`
    // would bail with "moved module references residual entry
    // binding(s) not exported by entry". The peeled module's
    // importer side renames the import back to the original local
    // sym via `EntryExport.{local_name, exported_name}`, so the
    // mint is invisible to the moved body.
    let mut taken_public_names = pre_existing_public_export_names.clone();
    needed
        .into_iter()
        .map(|name| {
            let final_local = entry_renames
                .get(&name)
                .cloned()
                .unwrap_or_else(|| name.clone());
            let public_name =
                import_emit::mint_unique_name(&name, |n| taken_public_names.insert(n.to_string()));
            (final_local, public_name)
        })
        .collect()
}
