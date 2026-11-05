//! Source-level import facts shared by selector elaboration and lowering.

use js_ast::str_value;
use std::collections::HashMap;
use swc_ecma_ast::*;

#[derive(Debug)]
pub enum RuntimeImportKind {
    Named { imported: String },
    Default,
    Namespace,
}

#[derive(Debug)]
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

pub fn record_runtime_imports(item: &ModuleItem, imports: &mut HashMap<Id, RuntimeImportInfo>) {
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
