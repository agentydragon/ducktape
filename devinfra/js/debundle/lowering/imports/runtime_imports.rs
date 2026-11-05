//! Reconstruction helpers for emitting runtime import specifiers in moved modules.

use frontend_elaboration::source_ast::{RuntimeImportInfo, RuntimeImportKind};
use swc_common::DUMMY_SP;
use swc_ecma_ast::*;

fn ident_from_id(id: &Id) -> Ident {
    Ident::new(id.0.clone(), DUMMY_SP, id.1)
}

pub(crate) fn runtime_reimport_specifier(local: &Id, info: &RuntimeImportInfo) -> ImportSpecifier {
    match &info.kind {
        RuntimeImportKind::Named { imported } => {
            js_ast::named_import_specifier(ident_from_id(local), imported)
        }
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
pub(crate) fn runtime_reimport_named_specifier(local: &Id, imported: &str) -> ImportSpecifier {
    js_ast::named_import_specifier(ident_from_id(local), imported)
}

/// Build a single Named specifier (`{ <imported> as <local> }`, or just
/// `{ <local> }` when local == imported) for an ImportSpecifier-bound
/// reexport. Callers group same-source specifiers and wrap the list in
/// one `ImportDecl` via [`js_ast::import_decl_module_item`].
pub(crate) fn imported_binding_named_specifier(
    local: &str,
    imported: &str,
    top_level_mark: swc_common::Mark,
) -> ImportSpecifier {
    js_ast::named_import_specifier(
        Ident::new(
            local.into(),
            DUMMY_SP,
            swc_common::SyntaxContext::empty().apply_mark(top_level_mark),
        ),
        imported,
    )
}
