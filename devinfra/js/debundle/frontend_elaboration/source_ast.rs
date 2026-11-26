//! Source-level import facts shared by selector elaboration and lowering.

use binding_targets::imported_export_name;
use js_ast::str_value;
use std::collections::HashMap;
use swc_ecma_ast::*;

#[derive(Debug, PartialEq, Eq)]
pub enum RuntimeImportKind {
    Named { imported: String },
    Default,
    Namespace,
}

#[derive(Debug, PartialEq, Eq)]
pub struct RuntimeImportInfo {
    pub kind: RuntimeImportKind,
    pub src: String,
}

pub struct RuntimeImportFacts {
    /// Import bindings retain their source AST identity before naturalization.
    pub imports: HashMap<Id, RuntimeImportInfo>,
}

impl RuntimeImportFacts {
    pub fn lookup_by_sym(&self, sym: &str) -> Option<&RuntimeImportInfo> {
        self.imports
            .iter()
            .find(|(id, _)| id.0.as_ref() == sym)
            .map(|(_, info)| info)
    }
}

/// Pair each local binding with the module and export it imports.
pub fn runtime_import_bindings(
    import: &ImportDecl,
) -> impl Iterator<Item = (&Ident, RuntimeImportInfo)> {
    let src = str_value(&import.src);
    import.specifiers.iter().map(move |specifier| {
        let kind = match specifier {
            ImportSpecifier::Named(named) => RuntimeImportKind::Named {
                imported: imported_export_name(named),
            },
            ImportSpecifier::Default(_) => RuntimeImportKind::Default,
            ImportSpecifier::Namespace(_) => RuntimeImportKind::Namespace,
        };
        (
            specifier.local(),
            RuntimeImportInfo {
                src: src.clone(),
                kind,
            },
        )
    })
}
