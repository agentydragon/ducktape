use std::collections::{BTreeMap, BTreeSet, HashSet};

use analysis::cross_module_values::resolve_primitive_binding_import_dependencies;
use analysis::facts::module_has_top_level_await;
use swc_common::{Span, Spanned, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::FileRole;
use artifact::reports::{
    ChunkAnalysisReport, ChunkCounts, ChunkFileRecord, ChunkModuleKind, ChunkRuntimeFacts,
    ExportAliasRecord, ImportRecord, ImportSpecifierKind, ImportSpecifierRecord, InteropUse,
    InteropUseKind, KeptTopLevelDeclarationRecord, StaticModuleRequest, StaticModuleRequestMode,
    TopLevelDeclarationKind,
};
use binding_targets::{
    DeclShape, TargetAccessRecorder, binding_names, decl_shape, declaration_name_strings,
    member_root_sym, module_export_name, record_assign_target, record_pat_write,
    record_update_target,
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

fn is_import_meta_require_member(member: &MemberExpr) -> bool {
    member_is_named(member, "require")
        && matches!(&*member.obj, Expr::MetaProp(meta) if meta.kind == MetaPropKind::ImportMeta)
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

enum PrimitiveExportSource {
    Bindings(BTreeSet<Id>),
    ReExport { source: String, export: String },
}

/// Bindings whose value is needed to prove that a normally-completing
/// expression returns a primitive. This is deliberately separate from purity:
/// templates and operators can coerce objects and run user code while still
/// producing a primitive result when they complete.
fn primitive_value_requirements(expression: &Expr) -> Option<BTreeSet<Id>> {
    let requirements = match expression.unwrap_parens() {
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
        | Expr::Tpl(_)
        | Expr::Unary(_)
        | Expr::Update(_) => return Some(BTreeSet::new()),
        Expr::Ident(id) => return Some(BTreeSet::from([id.to_id()])),
        Expr::Bin(binary) => match binary.op {
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing => {
                let mut requirements = primitive_value_requirements(&binary.left)?;
                requirements.extend(primitive_value_requirements(&binary.right)?);
                requirements
            }
            _ => return Some(BTreeSet::new()),
        },
        Expr::Cond(cond) => {
            let mut requirements = primitive_value_requirements(&cond.cons)?;
            requirements.extend(primitive_value_requirements(&cond.alt)?);
            requirements
        }
        Expr::Seq(sequence) => {
            return sequence
                .exprs
                .last()
                .and_then(|last| primitive_value_requirements(last));
        }
        _ => return None,
    };
    Some(requirements)
}

fn collect_primitive_export_candidates(
    item: &ModuleItem,
    bindings: &mut BTreeMap<Id, BTreeSet<Id>>,
    exports: &mut BTreeMap<String, Vec<Option<PrimitiveExportSource>>>,
) {
    let declaration = match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => Some(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => Some(&decl.decl),
        _ => None,
    };
    if let Some(Decl::Var(var)) = declaration
        && matches!(var.kind, VarDeclKind::Const | VarDeclKind::Var)
    {
        for decl in &var.decls {
            if let Pat::Ident(id) = &decl.name
                && let Some(initializer) = decl.init.as_deref()
                && let Some(requirements) = primitive_value_requirements(initializer)
            {
                bindings.insert(id.id.to_id(), requirements);
            }
        }
    }
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => {
            for id in binding_targets::declaration_ids(&decl.decl) {
                exports
                    .entry(id.0.to_string())
                    .or_default()
                    .push(Some(PrimitiveExportSource::Bindings(BTreeSet::from([id]))));
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
            if named.type_only {
                return;
            }
            for specifier in &named.specifiers {
                match specifier {
                    ExportSpecifier::Named(specifier) => {
                        if specifier.is_type_only {
                            continue;
                        }
                        let name = module_export_name(
                            specifier.exported.as_ref().unwrap_or(&specifier.orig),
                        );
                        let source = if let Some(source) = &named.src {
                            named
                                .with
                                .is_none()
                                .then(|| PrimitiveExportSource::ReExport {
                                    source: str_value(source),
                                    export: module_export_name(&specifier.orig),
                                })
                        } else {
                            match &specifier.orig {
                                ModuleExportName::Ident(id) => {
                                    Some(PrimitiveExportSource::Bindings(BTreeSet::from([
                                        id.to_id()
                                    ])))
                                }
                                _ => None,
                            }
                        };
                        exports.entry(name).or_default().push(source);
                    }
                    ExportSpecifier::Namespace(specifier) => {
                        exports
                            .entry(module_export_name(&specifier.name))
                            .or_default()
                            .push(None);
                    }
                    ExportSpecifier::Default(specifier) => {
                        exports
                            .entry(specifier.exported.sym.to_string())
                            .or_default()
                            .push(named.src.as_ref().and_then(|source| {
                                named
                                    .with
                                    .is_none()
                                    .then(|| PrimitiveExportSource::ReExport {
                                        source: str_value(source),
                                        export: "default".to_string(),
                                    })
                            }));
                    }
                }
            }
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default)) => {
            let source =
                primitive_value_requirements(&default.expr).map(PrimitiveExportSource::Bindings);
            exports
                .entry("default".to_string())
                .or_default()
                .push(source);
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_)) => {
            exports.entry("default".to_string()).or_default().push(None);
        }
        _ => {}
    }
}

fn resolve_primitive_binding_requirements(
    requirements: &BTreeSet<Id>,
    imported_bindings: &BTreeMap<Id, String>,
    stable_bindings: &BTreeMap<Id, BTreeSet<String>>,
) -> Option<BTreeSet<String>> {
    let mut imports = BTreeSet::new();
    for requirement in requirements {
        if let Some(local) = imported_bindings.get(requirement) {
            imports.insert(local.clone());
        } else {
            let local_imports = stable_bindings.get(requirement)?;
            imports.extend(local_imports.iter().cloned());
        }
    }
    Some(imports)
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
    // Assign each nested site its original top-level item while doing the
    // same single SWC walk used for the other shallow facts.
    for (index, item) in parsed.module.body.iter().enumerate() {
        module_scan.item_index = index;
        item.visit_with(&mut module_scan);
    }
    let ModuleScanVisitor {
        dynamic_import_count,
        observable_module_effect,
        has_rewritable_specifier,
        has_cjs_interop: visitor_has_cjs_interop,
        has_own_cjs_export_surface: visitor_has_own_cjs_export_surface,
        mut interop_uses,
        binding_declarations,
        binding_writes,
        dynamic_scope,
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
    let has_own_cjs_export_surface = visitor_has_own_cjs_export_surface
        || parsed.module.body.iter().any(|item| {
            matches!(
                item,
                ModuleItem::ModuleDecl(ModuleDecl::TsExportAssignment(_))
            )
        });
    for (item_index, item) in parsed.module.body.iter().enumerate() {
        let kind = match item {
            ModuleItem::ModuleDecl(ModuleDecl::TsImportEquals(_)) => {
                Some(InteropUseKind::TsImportEquals)
            }
            ModuleItem::ModuleDecl(ModuleDecl::TsExportAssignment(_)) => {
                Some(InteropUseKind::TsExportAssignment)
            }
            _ => None,
        };
        if let Some(kind) = kind {
            interop_uses.push((item_index, item.span(), kind, None));
        }
    }

    let mut runtime_facts = ChunkRuntimeFacts {
        module_kind: if parsed.module.body.is_empty() {
            ChunkModuleKind::EmptySource
        } else if parsed.module.body.iter().any(
            |item| matches!(item, ModuleItem::ModuleDecl(decl) if module_decl_is_runtime_esm(decl)),
        ) {
            ChunkModuleKind::Esm
        } else {
            ChunkModuleKind::Unknown
        },
        has_top_level_await: module_has_top_level_await(&parsed.module),
        has_cjs_interop,
        has_own_cjs_export_surface,
        interop_uses: interop_uses
            .into_iter()
            .map(|(item_index, span, kind, specifier)| InteropUse {
                item_index,
                line: line_index.line_for_span(span),
                kind,
                specifier,
            })
            .collect(),
        ..ChunkRuntimeFacts::default()
    };

    let mut primitive_binding_requirements = BTreeMap::new();
    let mut primitive_imported_bindings = BTreeMap::new();
    let mut primitive_exports = BTreeMap::<String, Vec<Option<PrimitiveExportSource>>>::new();
    for item in &parsed.module.body {
        collect_primitive_export_candidates(
            item,
            &mut primitive_binding_requirements,
            &mut primitive_exports,
        );
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
            if !decl.type_only
                && matches!(&decl.phase, ImportPhase::Evaluation)
                && decl.with.is_none()
            {
                for specifier in &decl.specifiers {
                    match specifier {
                        ImportSpecifier::Named(named) if !named.is_type_only => {
                            primitive_imported_bindings
                                .insert(named.local.to_id(), named.local.sym.to_string());
                        }
                        ImportSpecifier::Default(default) => {
                            primitive_imported_bindings
                                .insert(default.local.to_id(), default.local.sym.to_string());
                        }
                        ImportSpecifier::Namespace(_) | ImportSpecifier::Named(_) => {}
                    }
                }
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

    if runtime_facts.module_kind == ChunkModuleKind::Esm
        && !runtime_facts.has_own_cjs_export_surface
        && !dynamic_scope
    {
        let stable_primitive_bindings = primitive_binding_requirements
            .into_iter()
            .filter(|(id, _)| {
                binding_declarations.get(id) == Some(&1) && !binding_writes.contains(id)
            })
            .collect::<BTreeMap<_, _>>();
        let stable_binding_import_dependencies = resolve_primitive_binding_import_dependencies(
            stable_primitive_bindings,
            &primitive_imported_bindings,
        );

        for (name, sources) in primitive_exports {
            let [Some(source)] = sources.as_slice() else {
                continue;
            };
            match source {
                PrimitiveExportSource::Bindings(requirements) => {
                    let Some(imports) = resolve_primitive_binding_requirements(
                        requirements,
                        &primitive_imported_bindings,
                        &stable_binding_import_dependencies,
                    ) else {
                        continue;
                    };
                    if imports.is_empty() {
                        runtime_facts.stable_primitive_exports.insert(name);
                    } else {
                        runtime_facts
                            .stable_primitive_export_import_dependencies
                            .insert(name, imports);
                    }
                }
                PrimitiveExportSource::ReExport { source, export } => {
                    runtime_facts
                        .stable_primitive_export_reexports
                        .insert(name, (source.clone(), export.clone()));
                }
            }
        }
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
    has_own_cjs_export_surface: bool,
    item_index: usize,
    interop_uses: Vec<(usize, Span, InteropUseKind, Option<String>)>,
    direct_call_members: BTreeSet<u32>,
    unresolved_context: SyntaxContext,
    binding_declarations: BTreeMap<Id, usize>,
    binding_writes: BTreeSet<Id>,
    dynamic_scope: bool,
}

impl TargetAccessRecorder for ModuleScanVisitor {
    fn record_binding_write(&mut self, id: &Id) {
        self.binding_writes.insert(id.clone());
    }
}

impl Visit for ModuleScanVisitor {
    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        for id in binding_names(&node.name) {
            *self.binding_declarations.entry(id).or_default() += 1;
        }
        node.visit_children_with(self);
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        *self
            .binding_declarations
            .entry(node.ident.to_id())
            .or_default() += 1;
        node.visit_children_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        *self
            .binding_declarations
            .entry(node.ident.to_id())
            .or_default() += 1;
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        record_assign_target(&node.left, self);
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        record_update_target(&node.arg, self);
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        if let ForHead::Pat(pattern) = &node.left {
            record_pat_write(pattern, self);
        }
        node.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        if let ForHead::Pat(pattern) = &node.left {
            record_pat_write(pattern, self);
        }
        node.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, node: &WithStmt) {
        self.dynamic_scope = true;
        node.visit_children_with(self);
    }

    fn visit_ident(&mut self, node: &Ident) {
        if node.ctxt == self.unresolved_context {
            let kind = match node.sym.as_ref() {
                "require" => Some(InteropUseKind::UnboundRequire),
                "module" | "exports" => {
                    self.has_own_cjs_export_surface = true;
                    Some(if node.sym == *"module" {
                        InteropUseKind::UnboundModule
                    } else {
                        InteropUseKind::UnboundExports
                    })
                }
                _ => None,
            };
            if let Some(kind) = kind {
                self.has_cjs_interop = true;
                self.interop_uses
                    .push((self.item_index, node.span, kind, None));
            }
        }
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && is_import_meta_require_member(member)
        {
            self.direct_call_members.insert(member.span.lo.0);
            let literal = match node.args.as_slice() {
                [arg] if arg.spread.is_none() => match arg.expr.unwrap_parens() {
                    Expr::Lit(Lit::Str(value)) => Some(str_value(value)),
                    _ => None,
                },
                _ => None,
            };
            let kind = if literal.is_some() {
                InteropUseKind::ImportMetaRequireLiteralCall
            } else {
                InteropUseKind::ImportMetaRequireDynamicCall
            };
            self.interop_uses
                .push((self.item_index, node.span, kind, literal));
        }
        if matches!(&node.callee, Callee::Expr(expr) if matches!(expr.unwrap_parens(), Expr::Ident(id) if id.sym.as_ref() == "eval"))
        {
            self.dynamic_scope = true;
        }
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
            if !self.direct_call_members.contains(&node.span.lo.0) {
                let kind = if is_import_meta_require_member(node) {
                    InteropUseKind::ImportMetaRequireReference
                } else {
                    InteropUseKind::GlobalThisRequire
                };
                self.interop_uses
                    .push((self.item_index, node.span, kind, None));
            }
        }
        if !self.observable_module_effect
            && member_root_sym(&node.obj).is_some_and(|sym| sym == "window" || sym == "document")
        {
            self.observable_module_effect = true;
        }
        if matches!(&*node.obj, Expr::MetaProp(meta) if meta.kind == MetaPropKind::ImportMeta) {
            if !member_is_named(node, "require") {
                // Keep unknown computed access distinct from a fixed other
                // property such as `import.meta.url`.
                let kind = match &node.prop {
                    MemberProp::Computed(computed)
                        if !matches!(computed.expr.unwrap_parens(), Expr::Lit(Lit::Str(_))) =>
                    {
                        InteropUseKind::ImportMetaDynamicProperty
                    }
                    _ => InteropUseKind::ImportMetaOtherProperty,
                };
                self.interop_uses
                    .push((self.item_index, node.span, kind, None));
            }
            node.prop.visit_with(self);
        } else {
            node.visit_children_with(self);
        }
    }

    fn visit_meta_prop_expr(&mut self, node: &MetaPropExpr) {
        if node.kind == MetaPropKind::ImportMeta {
            self.interop_uses.push((
                self.item_index,
                node.span,
                InteropUseKind::ImportMetaValue,
                None,
            ));
        }
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
