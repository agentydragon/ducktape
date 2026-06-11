use std::collections::HashMap;

use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{
    ChunkAnalysisReport, ChunkCounts, ChunkFileRecord, ExportAliasRecord, FileRole, ImportRecord,
    ImportSpecifierKind, ImportSpecifierRecord, KeptTopLevelDeclarationRecord, ParserOptionsRecord,
    TopLevelDeclarationKind,
};
use binding_targets::{binding_names, member_root_sym, module_export_name};
use js_ast::{ParsedJsModule, SourceLineIndex, str_value};

/// True when a specifier string is a relative module path that
/// `rewrite_chunk_entry_specifiers` could rewrite (any `.` / `/`
/// prefix). Kept in sync with the predicate in
/// `rewrite_specifiers::is_relative_specifier`.
fn is_relative_specifier(source: &str) -> bool {
    source.starts_with('.') || source.starts_with('/')
}

/// If `node` is `import("literal")` and the literal is a relative
/// module path, return it; otherwise `None`.
fn dynamic_import_relative_specifier(node: &CallExpr) -> Option<String> {
    if !matches!(node.callee, Callee::Import(_)) {
        return None;
    }
    let first = node.args.first()?;
    if first.spread.is_some() {
        return None;
    }
    let Expr::Lit(Lit::Str(s)) = &*first.expr else {
        return None;
    };
    let v = str_value(s);
    is_relative_specifier(&v).then_some(v)
}

/// If `node` is `new Worker("literal")` / `new SharedWorker("literal")`
/// and the literal is a relative module path, return it; otherwise `None`.
fn worker_relative_specifier(node: &NewExpr) -> Option<String> {
    let callee_is_worker = matches!(
        &*node.callee,
        Expr::Ident(ident) if ident.sym == *"Worker" || ident.sym == *"SharedWorker"
    );
    if !callee_is_worker {
        return None;
    }
    let args = node.args.as_ref()?;
    let first = args.first()?;
    if first.spread.is_some() {
        return None;
    }
    let Expr::Lit(Lit::Str(s)) = &*first.expr else {
        return None;
    };
    let v = str_value(s);
    is_relative_specifier(&v).then_some(v)
}

pub struct ProgramAnalysis {
    pub imports: Vec<ImportRecord>,
    pub import_by_local_name: HashMap<String, ImportSpecifierRecord>,
    pub export_aliases: Vec<ExportAliasRecord>,
    pub owners: Vec<OwnerRecord>,
    pub side_effects: Vec<SideEffectRecord>,
    pub dynamic_import_count: usize,
    pub observable_module_effect: bool,
    /// True when the module body contains any specifier that
    /// `rewrite_chunk_entry_specifiers` could rewrite (any `import`,
    /// `export … from`, `import(...)`, or `new Worker(...)` /
    /// `new SharedWorker(...)` whose source is a relative path
    /// starting with `.` or `/`). Collected during the same module
    /// walk as `dynamic_import_count` / `observable_module_effect` so
    /// `prepare_js_chunks` can decide AST retention without a second
    /// full-tree visit.
    pub has_rewritable_specifier: bool,
}

pub struct OwnerRecord {
    pub id: String,
    pub line: Option<usize>,
    pub names: Vec<String>,
    pub ordinal: usize,
    pub kind: TopLevelDeclarationKind,
}

/// If `item` is a top-level declaration of a kind we anchor extraction on,
/// classify it and pull its bound names. Otherwise return `None` and the
/// caller treats the item as a side effect.
fn classify_top_level_decl(item: &ModuleItem) -> Option<(TopLevelDeclarationKind, Vec<String>)> {
    let ModuleItem::Stmt(Stmt::Decl(decl)) = item else {
        return None;
    };
    match decl {
        Decl::Fn(function) => Some((
            TopLevelDeclarationKind::Function,
            vec![function.ident.sym.to_string()],
        )),
        Decl::Class(class) => Some((
            TopLevelDeclarationKind::Class,
            vec![class.ident.sym.to_string()],
        )),
        Decl::Var(var) => Some((
            TopLevelDeclarationKind::Variable,
            var.decls
                .iter()
                .flat_map(|decl| binding_names(&decl.name))
                .map(|id| id.0.to_string())
                .collect(),
        )),
        _ => None,
    }
}

pub struct SideEffectRecord {
    pub id: String,
    pub ordinal: usize,
}

pub fn analyze_program_shallow(parsed: &ParsedJsModule) -> ProgramAnalysis {
    let line_index = parsed.line_index();
    let mut imports = Vec::new();
    let mut import_by_local_name = HashMap::new();
    let mut export_aliases = Vec::new();
    let mut owners = Vec::new();
    let mut side_effects = Vec::new();
    // Fused module-level walk: collects everything that previously
    // required a separate `visit_with` over the whole module
    // (dynamic-import count, observable top-level effects,
    // rewritable-specifier presence). Each concern's `visit_*`
    // hooks live on the same struct and write into independent
    // fields, so adding/removing concerns is local.
    let mut module_scan = ModuleScanVisitor::default();
    parsed.module.visit_with(&mut module_scan);
    let ModuleScanVisitor {
        dynamic_import_count,
        observable_module_effect,
        has_rewritable_specifier,
    } = module_scan;

    for (ordinal, item) in parsed.module.body.iter().enumerate() {
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(decl)) = item {
            let import_record = describe_import(&line_index, decl, imports.len());
            for specifier in &import_record.specifiers {
                let mut specifier = specifier.clone();
                specifier.source = Some(import_record.source.clone());
                import_by_local_name.insert(specifier.local.clone(), specifier);
            }
            imports.push(import_record);
            continue;
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item
            && !named.specifiers.is_empty()
        {
            for specifier in &named.specifiers {
                if let ExportSpecifier::Named(named_specifier) = specifier {
                    export_aliases.push(ExportAliasRecord {
                        exported: module_export_name(
                            named_specifier
                                .exported
                                .as_ref()
                                .unwrap_or(&named_specifier.orig),
                        ),
                        line: line_index.line_for_span(named.span),
                        local: Some(module_export_name(&named_specifier.orig)),
                    });
                }
            }
            continue;
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) = item {
            export_aliases.push(ExportAliasRecord {
                exported: "default".to_string(),
                line: line_index.line_for_span(default_decl.span),
                local: export_default_decl_name(default_decl),
            });
            continue;
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) = item {
            export_aliases.push(ExportAliasRecord {
                exported: "default".to_string(),
                line: line_index.line_for_span(default_expr.span),
                local: None,
            });
            continue;
        }

        if let Some((kind, names)) = classify_top_level_decl(item) {
            owners.push(OwnerRecord {
                id: format!("owner_{:05}", owners.len()),
                line: item_line(&line_index, item),
                names,
                ordinal,
                kind,
            });
            continue;
        }

        side_effects.push(SideEffectRecord {
            id: format!("side_effect_{:05}", side_effects.len()),
            ordinal,
        });
    }

    ProgramAnalysis {
        imports,
        import_by_local_name,
        export_aliases,
        owners,
        side_effects,
        dynamic_import_count,
        observable_module_effect,
        has_rewritable_specifier,
    }
}

pub fn build_chunk_manifest_from_analysis(
    chunk_id: &str,
    entry_file: &str,
    source_path: &str,
    analysis: &ProgramAnalysis,
) -> ChunkAnalysisReport {
    let unresolved_exports = analysis
        .export_aliases
        .iter()
        .filter(|alias| {
            alias.local.as_ref().is_some_and(|local| {
                !analysis
                    .owners
                    .iter()
                    .any(|owner| owner.names.contains(local))
                    && !analysis.import_by_local_name.contains_key(local)
            })
        })
        .cloned()
        .collect::<Vec<_>>();
    let kept_top_level_declarations = analysis
        .owners
        .iter()
        .map(|owner| KeptTopLevelDeclarationRecord {
            id: owner.id.clone(),
            line: owner.line,
            names: owner.names.clone(),
            kind: owner.kind,
            unsafe_reason: "not_split",
        })
        .collect::<Vec<_>>();

    ChunkAnalysisReport {
        chunk_id: chunk_id.to_string(),
        source_path: source_path.to_string(),
        parser: ParserOptionsRecord::default(),
        entry_file: entry_file.to_string(),
        counts: ChunkCounts {
            dynamic_imports: analysis.dynamic_import_count,
            export_aliases: analysis.export_aliases.len(),
            import_declarations: analysis.imports.len(),
            kept_top_level_declaration_owners: analysis.owners.len(),
            top_level_bindings: analysis.owners.iter().map(|owner| owner.names.len()).sum(),
            top_level_declaration_owners: analysis.owners.len(),
            top_level_side_effects: analysis.side_effects.len(),
            unresolved_exports: unresolved_exports.len(),
        },
        files: vec![ChunkFileRecord {
            file: entry_file.to_string(),
            role: FileRole::Entry,
        }],
        imports: analysis.imports.clone(),
        export_aliases: analysis.export_aliases.clone(),
        unresolved_exports,
        kept_top_level_declarations,
    }
}

fn describe_import(line_index: &SourceLineIndex, decl: &ImportDecl, index: usize) -> ImportRecord {
    let source = str_value(&decl.src);
    ImportRecord {
        id: format!("import_{index:05}"),
        line: line_index.line_for_span(decl.span),
        source,
        specifiers: decl
            .specifiers
            .iter()
            .map(|specifier| match specifier {
                ImportSpecifier::Default(default) => ImportSpecifierRecord {
                    kind: ImportSpecifierKind::Default,
                    imported: None,
                    local: default.local.sym.to_string(),
                    source: None,
                },
                ImportSpecifier::Namespace(namespace) => ImportSpecifierRecord {
                    kind: ImportSpecifierKind::Namespace,
                    imported: None,
                    local: namespace.local.sym.to_string(),
                    source: None,
                },
                ImportSpecifier::Named(named) => ImportSpecifierRecord {
                    kind: ImportSpecifierKind::Named,
                    imported: Some(
                        named
                            .imported
                            .as_ref()
                            .map(module_export_name)
                            .unwrap_or_else(|| named.local.sym.to_string()),
                    ),
                    local: named.local.sym.to_string(),
                    source: None,
                },
            })
            .collect(),
    }
}

fn export_default_decl_name(decl: &ExportDefaultDecl) -> Option<String> {
    match &decl.decl {
        DefaultDecl::Class(class) => class.ident.as_ref().map(|ident| ident.sym.to_string()),
        DefaultDecl::Fn(function) => function.ident.as_ref().map(|ident| ident.sym.to_string()),
        DefaultDecl::TsInterfaceDecl(interface) => Some(interface.id.sym.to_string()),
    }
}

fn item_line(line_index: &SourceLineIndex, item: &ModuleItem) -> Option<usize> {
    line_index.line_for_span(item.span())
}

/// Single-pass module-level walk producing every fact set the
/// stage-one analyzer needs that does not depend on per-statement
/// phase tracking:
///
/// - `dynamic_import_count`: number of `import(...)` call expressions
///   anywhere in the module.
/// - `observable_module_effect`: any `new …(…)` or any `window.*` /
///   `document.*` member access; matches the previous
///   `ObservableTopLevelEffectCollector` exactly.
/// - `has_rewritable_specifier`: any `import`, `export … from`,
///   `import(…literal)`, or `new Worker(…literal)` /
///   `new SharedWorker(…literal)` whose source literal is a relative
///   module path (`.` / `/` prefix). Mirrors `rewrite_source`'s gate
///   so `prepare_js_chunks` can drop the AST when no rewrite is
///   possible.
///
/// Replaces three independent module-level `Visit` impls that each
/// walked the full module — one `visit_with` per concern. Concerns
/// here are independent: their hooks set distinct fields and never
/// short-circuit each other.
#[derive(Default)]
struct ModuleScanVisitor {
    dynamic_import_count: usize,
    observable_module_effect: bool,
    has_rewritable_specifier: bool,
}

impl Visit for ModuleScanVisitor {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if matches!(node.callee, Callee::Import(_)) {
            self.dynamic_import_count += 1;
            if !self.has_rewritable_specifier && dynamic_import_relative_specifier(node).is_some() {
                self.has_rewritable_specifier = true;
            }
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        self.observable_module_effect = true;
        if !self.has_rewritable_specifier && worker_relative_specifier(node).is_some() {
            self.has_rewritable_specifier = true;
        }
        node.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if !self.observable_module_effect
            && member_root_sym(&node.obj).is_some_and(|sym| sym == "window" || sym == "document")
        {
            self.observable_module_effect = true;
        }
        node.visit_children_with(self);
    }

    fn visit_import_decl(&mut self, node: &ImportDecl) {
        if !self.has_rewritable_specifier && is_relative_specifier(&str_value(&node.src)) {
            self.has_rewritable_specifier = true;
        }
        node.visit_children_with(self);
    }

    fn visit_named_export(&mut self, node: &NamedExport) {
        if !self.has_rewritable_specifier
            && let Some(src) = &node.src
            && is_relative_specifier(&str_value(src))
        {
            self.has_rewritable_specifier = true;
        }
        node.visit_children_with(self);
    }

    fn visit_export_all(&mut self, node: &ExportAll) {
        if !self.has_rewritable_specifier && is_relative_specifier(&str_value(&node.src)) {
            self.has_rewritable_specifier = true;
        }
        node.visit_children_with(self);
    }
}
