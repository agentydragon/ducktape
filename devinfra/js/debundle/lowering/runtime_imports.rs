//! Per-chunk runtime-imports table built from the source chunk
//! plus the helpers that emit re-import specifiers in moved modules.

use super::*;

pub(super) struct RuntimeImportFacts {
    /// Maps a source-chunk import binding's `Id` (the local name at the
    /// import site) to the `RuntimeImportInfo` describing where it came
    /// from. Keyed by the **pre-rename** `Id` because the map is built
    /// before any naturalizer pass runs; `plan_module_reference_needs`
    /// bridges the rename via the sealed rename map's inverse projection
    /// (`RuntimeImportLookup::original_by_renamed`) when the body has
    /// been renamed.
    pub(super) imports: HashMap<Id, RuntimeImportInfo>,
}

impl RuntimeImportFacts {
    /// Sym-only lookup for callers that have a `String`/`&str` binding
    /// name without hygiene context (typically spec-derived names that
    /// pre-date the `Id` migration). Returns the first matching entry,
    /// which is unambiguous within a chunk's top-level scope (resolver
    /// gives all top-level bindings the same `top_level_mark`).
    pub(super) fn lookup_by_sym(&self, sym: &str) -> Option<&RuntimeImportInfo> {
        self.imports
            .iter()
            .find(|(id, _)| id.0.as_ref() == sym)
            .map(|(_, info)| info)
    }

    /// `(local import binding name, import source specifier)` for every import
    /// specifier in the chunk — the local→module-source map the `member_of_module`
    /// use-site primitive joins member accesses against (`mod.X` ⟹ the source of
    /// `mod`). Uses the **pre-rename** local sym (the key the map is built on),
    /// which is what the `member_of_module` resolution runs against (it operates on
    /// the chunk's owner graph + AST member-reads, before any naturalizer rename).
    pub(super) fn iter_local_sources(&self) -> impl Iterator<Item = (&str, &str)> + '_ {
        self.imports
            .iter()
            .map(|(id, info)| (id.0.as_ref(), info.src.as_str()))
    }
}

pub(super) fn record_runtime_imports(
    item: &ModuleItem,
    imports: &mut HashMap<Id, RuntimeImportInfo>,
) {
    let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
        return;
    };
    let src = str_value(&import.src);
    for specifier in &import.specifiers {
        match specifier {
            ImportSpecifier::Named(named) => {
                let imported = match &named.imported {
                    Some(ModuleExportName::Ident(ident)) => ident.sym.to_string(),
                    Some(ModuleExportName::Str(s)) => str_value(s),
                    None => named.local.sym.to_string(),
                };
                imports.insert(
                    named.local.to_id(),
                    RuntimeImportInfo {
                        kind: RuntimeImportKind::Named { imported },
                        src: src.clone(),
                    },
                );
            }
            ImportSpecifier::Default(default) => {
                imports.insert(
                    default.local.to_id(),
                    RuntimeImportInfo {
                        kind: RuntimeImportKind::Default,
                        src: src.clone(),
                    },
                );
            }
            ImportSpecifier::Namespace(namespace) => {
                imports.insert(
                    namespace.local.to_id(),
                    RuntimeImportInfo {
                        kind: RuntimeImportKind::Namespace,
                        src: src.clone(),
                    },
                );
            }
        }
    }
}

#[derive(Debug)]
pub(super) struct RuntimeImportInfo {
    pub(super) kind: RuntimeImportKind,
    pub(super) src: String,
}

#[derive(Debug)]
pub(super) enum RuntimeImportKind {
    Named { imported: String },
    Default,
    Namespace,
}

fn ident_from_id(id: &Id) -> Ident {
    Ident::new(id.0.clone(), DUMMY_SP, id.1)
}

pub(super) fn runtime_reimport_specifier(local: &Id, info: &RuntimeImportInfo) -> ImportSpecifier {
    match &info.kind {
        RuntimeImportKind::Named { imported } => ImportSpecifier::Named(ImportNamedSpecifier {
            span: DUMMY_SP,
            local: ident_from_id(local),
            imported: if imported == local.0.as_ref() {
                None
            } else {
                Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                    imported.clone().into(),
                    DUMMY_SP,
                )))
            },
            is_type_only: false,
        }),
        RuntimeImportKind::Default => ImportSpecifier::Default(ImportDefaultSpecifier {
            span: DUMMY_SP,
            local: ident_from_id(local),
        }),
        RuntimeImportKind::Namespace => ImportSpecifier::Namespace(ImportStarAsSpecifier {
            span: DUMMY_SP,
            local: ident_from_id(local),
        }),
    }
}

/// Named re-import specifier with an explicit imported name, used when
/// the vendor plan's boundary mapping overrides the recorded source
/// name.
pub(super) fn runtime_reimport_named_specifier(local: &Id, imported: &str) -> ImportSpecifier {
    ImportSpecifier::Named(ImportNamedSpecifier {
        span: DUMMY_SP,
        local: ident_from_id(local),
        imported: (imported != local.0.as_ref())
            .then(|| ModuleExportName::Ident(Ident::new_no_ctxt(imported.into(), DUMMY_SP))),
        is_type_only: false,
    })
}

/// Build a single Named specifier (`{ <imported> as <local> }`, or just
/// `{ <local> }` when local == imported) for an ImportSpecifier-bound
/// reexport. Callers group same-source specifiers and wrap the list in
/// one `ImportDecl` via [`import_decl_module_item`].
pub(super) fn imported_binding_named_specifier(local: &str, imported: &str) -> ImportSpecifier {
    ImportSpecifier::Named(ImportNamedSpecifier {
        span: DUMMY_SP,
        local: Ident::new_no_ctxt(local.into(), DUMMY_SP),
        imported: if local == imported {
            None
        } else {
            Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                imported.into(),
                DUMMY_SP,
            )))
        },
        is_type_only: false,
    })
}
