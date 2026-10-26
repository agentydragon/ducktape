use std::collections::HashSet;

use analysis::facts::module_has_top_level_await;
use swc_common::{Spanned, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{
    ChunkAnalysisReport, ChunkCounts, ChunkFileRecord, ChunkModuleKind, ChunkRuntimeFacts,
    ExportAliasRecord, FileRole, ImportRecord, ImportSpecifierKind, ImportSpecifierRecord,
    KeptTopLevelDeclarationRecord, StaticModuleRequest, StaticModuleRequestMode,
    TopLevelDeclarationKind,
};
use binding_targets::{
    DeclShape, decl_shape, declaration_name_strings, member_root_sym, module_export_name,
};
use js_ast::{ParsedJsModule, SourceLineIndex, str_value};

/// True when a specifier string is a relative module path that the
/// pass-through emission rewriter could canonicalize (any `.` / `/`
/// prefix). Kept in sync with the gate in
/// `vendor::passthrough`'s `SourceCanonicalizer::rewrite_source`.
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

fn member_is_named(member: &MemberExpr, name: &str) -> bool {
    match &member.prop {
        MemberProp::Ident(ident) => ident.sym == name,
        MemberProp::Computed(computed) => match &*computed.expr {
            Expr::Lit(Lit::Str(string)) => str_value(string) == name,
            _ => false,
        },
        MemberProp::PrivateName(_) => false,
    }
}

fn is_host_require_member(member: &MemberExpr, unresolved_context: SyntaxContext) -> bool {
    member_is_named(member, "require")
        && match &*member.obj {
            Expr::MetaProp(meta) => meta.kind == MetaPropKind::ImportMeta,
            Expr::Ident(ident) => ident.sym == *"globalThis" && ident.ctxt == unresolved_context,
            _ => false,
        }
}

fn import_decl_has_runtime_request(import: &ImportDecl) -> bool {
    // The declaration-level `type` modifier is erased. Keep non-type-only
    // imports even when all bindings are type-marked: their emitted form may
    // still be an empty import whose dependency has evaluation side effects.
    !import.type_only
}

fn named_export_has_runtime_request(export: &NamedExport) -> bool {
    !export.type_only
        && (export.src.is_some()
            || export.specifiers.is_empty()
            || export.specifiers.iter().any(|specifier| match specifier {
                ExportSpecifier::Named(named) => !named.is_type_only,
                ExportSpecifier::Default(_) | ExportSpecifier::Namespace(_) => true,
            }))
}

fn module_decl_is_runtime_esm(decl: &ModuleDecl) -> bool {
    match decl {
        ModuleDecl::Import(import) => import_decl_has_runtime_request(import),
        ModuleDecl::ExportDecl(export) => match &export.decl {
            Decl::Fn(decl) => !decl.declare,
            Decl::Class(decl) => !decl.declare,
            Decl::Var(decl) => !decl.declare,
            Decl::TsInterface(_) | Decl::TsTypeAlias(_) | Decl::TsEnum(_) | Decl::TsModule(_) => {
                false
            }
            _ => true,
        },
        ModuleDecl::ExportNamed(export) => named_export_has_runtime_request(export),
        ModuleDecl::ExportDefaultDecl(export) => {
            !matches!(&export.decl, DefaultDecl::TsInterfaceDecl(_))
        }
        ModuleDecl::ExportDefaultExpr(_) => true,
        ModuleDecl::ExportAll(export) => !export.type_only,
        ModuleDecl::TsImportEquals(_)
        | ModuleDecl::TsExportAssignment(_)
        | ModuleDecl::TsNamespaceExport(_) => false,
    }
}

pub struct ProgramAnalysis {
    pub imports: Vec<ImportRecord>,
    pub import_local_names: HashSet<String>,
    pub export_aliases: Vec<ExportAliasRecord>,
    pub declarations: Vec<DeclarationRecord>,
    pub side_effect_count: usize,
    pub dynamic_import_count: usize,
    pub observable_module_effect: bool,
    /// True when the module body contains any specifier that the
    /// pass-through emission rewriter could canonicalize (any `import`,
    /// `export … from`, `import(...)`, or `new Worker(...)` /
    /// `new SharedWorker(...)` whose source is a relative path
    /// starting with `.` or `/`). Collected during the same module
    /// walk as `dynamic_import_count` / `observable_module_effect` so
    /// `prepare_js_chunks` can decide AST retention without a second
    /// full-tree visit.
    pub has_rewritable_specifier: bool,
    pub runtime_facts: ChunkRuntimeFacts,
}

pub struct DeclarationRecord {
    pub line: Option<usize>,
    pub names: Vec<String>,
    pub kind: TopLevelDeclarationKind,
}

/// If `item` is a top-level declaration of a kind we anchor extraction on,
/// classify it and pull its bound names. Otherwise return `None` and the
/// caller treats the item as a side effect.
fn classify_top_level_decl(item: &ModuleItem) -> Option<(TopLevelDeclarationKind, Vec<String>)> {
    let ModuleItem::Stmt(Stmt::Decl(decl)) = item else {
        return None;
    };
    let kind = match decl_shape(decl)? {
        DeclShape::Function => TopLevelDeclarationKind::Function,
        DeclShape::Class => TopLevelDeclarationKind::Class,
        DeclShape::Variable => TopLevelDeclarationKind::Variable,
    };
    Some((kind, declaration_name_strings(decl)))
}

pub fn analyze_program_shallow(parsed: &ParsedJsModule) -> ProgramAnalysis {
    let line_index = parsed.line_index();
    let mut imports = Vec::new();
    let mut import_local_names = HashSet::new();
    let mut export_aliases = Vec::new();
    let mut declarations = Vec::new();
    let mut side_effect_count = 0;
    // Fused module-level walk: collects everything that previously
    // required a separate `visit_with` over the whole module
    // (dynamic-import count, observable top-level effects,
    // rewritable-specifier presence). Each concern's `visit_*`
    // hooks live on the same struct and write into independent
    // fields, so adding/removing concerns is local.
    let mut module_scan = ModuleScanVisitor {
        unresolved_context: SyntaxContext::empty().apply_mark(parsed.unresolved_mark),
        ..ModuleScanVisitor::default()
    };
    parsed.module.visit_with(&mut module_scan);
    let ModuleScanVisitor {
        dynamic_import_count,
        observable_module_effect,
        has_rewritable_specifier,
        has_cjs_interop: visitor_has_cjs_interop,
        ..
    } = module_scan;
    let has_cjs_interop = visitor_has_cjs_interop
        || parsed.module.body.iter().any(|item| {
            matches!(
                item,
                ModuleItem::ModuleDecl(
                    ModuleDecl::TsImportEquals(_) | ModuleDecl::TsExportAssignment(_)
                )
            )
        });

    let mut runtime_facts = ChunkRuntimeFacts {
        module_kind: if parsed.module.body.iter().any(
            |item| matches!(item, ModuleItem::ModuleDecl(decl) if module_decl_is_runtime_esm(decl)),
        ) {
            ChunkModuleKind::Esm
        } else {
            ChunkModuleKind::Unknown
        },
        has_top_level_await: module_has_top_level_await(&parsed.module),
        has_cjs_interop,
        ..ChunkRuntimeFacts::default()
    };

    for item in &parsed.module.body {
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(decl)) = item {
            if import_decl_has_runtime_request(decl) {
                runtime_facts.static_requests.push(StaticModuleRequest {
                    specifier: str_value(&decl.src),
                    mode: if matches!(&decl.phase, ImportPhase::Evaluation) && decl.with.is_none() {
                        StaticModuleRequestMode::OrdinaryEsm
                    } else {
                        StaticModuleRequestMode::Unsupported
                    },
                });
            }
            let import_record = describe_import(&line_index, decl);
            import_local_names.extend(
                import_record
                    .specifiers
                    .iter()
                    .map(|specifier| specifier.local.clone()),
            );
            imports.push(import_record);
            continue;
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item
            && named_export_has_runtime_request(named)
            && let Some(source) = &named.src
        {
            runtime_facts.static_requests.push(StaticModuleRequest {
                specifier: str_value(source),
                mode: if named.with.is_none() {
                    StaticModuleRequestMode::OrdinaryEsm
                } else {
                    StaticModuleRequestMode::Unsupported
                },
            });
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportAll(export_all)) = item
            && !export_all.type_only
        {
            runtime_facts.static_requests.push(StaticModuleRequest {
                specifier: str_value(&export_all.src),
                mode: if export_all.with.is_none() {
                    StaticModuleRequestMode::OrdinaryEsm
                } else {
                    StaticModuleRequestMode::Unsupported
                },
            });
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
            declarations.push(DeclarationRecord {
                line: item_line(&line_index, item),
                names,
                kind,
            });
            continue;
        }

        side_effect_count += 1;
    }

    ProgramAnalysis {
        imports,
        import_local_names,
        export_aliases,
        declarations,
        side_effect_count,
        dynamic_import_count,
        observable_module_effect,
        has_rewritable_specifier,
        runtime_facts,
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
                    .declarations
                    .iter()
                    .any(|declaration| declaration.names.contains(local))
                    && !analysis.import_local_names.contains(local)
            })
        })
        .cloned()
        .collect::<Vec<_>>();
    let kept_top_level_declarations = analysis
        .declarations
        .iter()
        .map(|declaration| KeptTopLevelDeclarationRecord {
            line: declaration.line,
            names: declaration.names.clone(),
            kind: declaration.kind,
        })
        .collect::<Vec<_>>();

    ChunkAnalysisReport {
        chunk_id: chunk_id.to_string(),
        source_path: source_path.to_string(),
        entry_file: entry_file.to_string(),
        counts: ChunkCounts {
            dynamic_imports: analysis.dynamic_import_count,
            export_aliases: analysis.export_aliases.len(),
            import_declarations: analysis.imports.len(),
            kept_top_level_declarations: analysis.declarations.len(),
            top_level_bindings: analysis
                .declarations
                .iter()
                .map(|declaration| declaration.names.len())
                .sum(),
            top_level_declarations: analysis.declarations.len(),
            top_level_side_effects: analysis.side_effect_count,
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
        runtime_facts: analysis.runtime_facts.clone(),
    }
}

fn describe_import(line_index: &SourceLineIndex, decl: &ImportDecl) -> ImportRecord {
    let source = str_value(&decl.src);
    ImportRecord {
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
                },
                ImportSpecifier::Namespace(namespace) => ImportSpecifierRecord {
                    kind: ImportSpecifierKind::Namespace,
                    imported: None,
                    local: namespace.local.sym.to_string(),
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
    has_cjs_interop: bool,
    unresolved_context: SyntaxContext,
}

impl Visit for ModuleScanVisitor {
    fn visit_ident(&mut self, node: &Ident) {
        if node.ctxt == self.unresolved_context
            && matches!(node.sym.as_ref(), "require" | "module" | "exports")
        {
            self.has_cjs_interop = true;
        }
    }

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
        // Treat the loader reference itself as host interop even when it is
        // captured and called through an alias later.
        if is_host_require_member(node, self.unresolved_context) {
            self.has_cjs_interop = true;
        }
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
