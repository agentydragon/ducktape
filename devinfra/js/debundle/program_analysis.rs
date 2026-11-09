use artifact::runtime_resources::{RuntimeResourceKind, RuntimeResourceUse};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use analysis::facts::abstract_domain::primitive::{NoBindings, evaluate};
use analysis::facts::module_has_top_level_await;
use analysis_model::PrimKinds;
use analysis_model::StableExportValue;
use source_identity::StatementId;
use swc_common::{Span, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::FileRole;
use artifact::reports::{
    ChunkAnalysisReport, ChunkCounts, ChunkFileRecord, ChunkModuleKind, ChunkRuntimeFacts,
    ExportAliasRecord, ImportRecord, ImportSpecifierKind, ImportSpecifierRecord, InteropUse,
    InteropUseKind, KeptTopLevelDeclarationRecord, LoaderBinding, LoaderBindingUse,
    LoaderBindingUses, NamespaceRequest, ParsedSourceExport, ParsedSourceImportBinding,
    ParsedSourceImportUseSite, ParsedSourceImports, ParsedSourceReExport, ParsedSourceRequest,
    SitePhase, SourceReExportKind, SourceStaticRequestKind, StaticModuleRequest,
    StaticModuleRequestMode, TopLevelDeclarationKind,
};
use binding_targets::{
    DeclShape, TargetAccessRecorder, binding_names, decl_shape, declaration_name_strings,
    member_root_sym, module_export_name, record_assign_target, record_pat_write,
    record_update_target,
};
use js_ast::{ParsedGlobalUses, ParsedJsModule, SourceLineIndex, str_value};

mod loader_phase;
mod loader_reach;

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

/// A direct call of the global `eval` with one argument whose value folds to
/// exactly `"require"`.
fn is_eval_of_require(call: &CallExpr, unresolved_context: SyntaxContext) -> bool {
    matches!(&call.callee, Callee::Expr(callee)
        if matches!(callee.unwrap_parens(), Expr::Ident(id)
            if id.sym == *"eval" && id.ctxt == unresolved_context))
        && matches!(call.args.as_slice(), [arg]
            if arg.spread.is_none() && fold_string(&arg.expr).as_deref() == Some("require"))
}

/// The value of a string expression built only from literals, `+` and
/// `String.prototype.replace` with a literal pattern (a string, or the empty
/// `/^/` regex, which prepends) and a replacement without `$` patterns.
fn fold_string(expr: &Expr) -> Option<String> {
    match expr.unwrap_parens() {
        Expr::Lit(Lit::Str(value)) => Some(str_value(value)),
        Expr::Tpl(tpl) if tpl.exprs.is_empty() => tpl
            .quasis
            .first()?
            .cooked
            .as_ref()
            .map(|cooked| cooked.to_string_lossy().into_owned()),
        Expr::Bin(bin) if bin.op == BinaryOp::Add => {
            Some(fold_string(&bin.left)? + &fold_string(&bin.right)?)
        }
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            let Expr::Member(member) = callee.unwrap_parens() else {
                return None;
            };
            if !member_is_named(member, "replace") {
                return None;
            }
            let receiver = fold_string(&member.obj)?;
            let [pattern, replacement] = call.args.as_slice() else {
                return None;
            };
            if pattern.spread.is_some() || replacement.spread.is_some() {
                return None;
            }
            let replacement = fold_string(&replacement.expr)?;
            if replacement.contains('$') {
                return None;
            }
            match pattern.expr.unwrap_parens() {
                Expr::Lit(Lit::Regex(regex)) if &*regex.exp == "^" && regex.flags.is_empty() => {
                    Some(replacement + &receiver)
                }
                pattern => {
                    let pattern = fold_string(pattern)?;
                    Some(receiver.replacen(&pattern, &replacement, 1))
                }
            }
        }
        _ => None,
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

/// Classify one static ESM module declaration for the ordered runtime request
/// inventory shared by source analysis and candidate emission.
pub fn static_module_request_for_decl(decl: &ModuleDecl) -> Option<StaticModuleRequest> {
    let (source, mode) = match decl {
        ModuleDecl::Import(import) if import_decl_has_runtime_request(import) => (
            &import.src,
            if matches!(&import.phase, ImportPhase::Evaluation) && import.with.is_none() {
                StaticModuleRequestMode::OrdinaryEsm
            } else {
                StaticModuleRequestMode::Unsupported
            },
        ),
        ModuleDecl::ExportNamed(export) if named_export_has_runtime_request(export) => {
            let source = export.src.as_ref()?;
            (
                source,
                if export.with.is_none() {
                    StaticModuleRequestMode::OrdinaryEsm
                } else {
                    StaticModuleRequestMode::Unsupported
                },
            )
        }
        ModuleDecl::ExportAll(export) if !export.type_only => (
            &export.src,
            if export.with.is_none() {
                StaticModuleRequestMode::OrdinaryEsm
            } else {
                StaticModuleRequestMode::Unsupported
            },
        ),
        _ => return None,
    };
    Some(StaticModuleRequest {
        specifier: str_value(source),
        mode,
    })
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
    Value(PrimitiveValue),
    ReExport { source: String, export: String },
}

/// The primitive types an expression computes itself, and the bindings whose
/// values it copies.
struct PrimitiveValue {
    kinds: PrimKinds,
    copies: BTreeSet<Id>,
}

impl PrimitiveValue {
    fn join(mut self, other: Self) -> Self {
        self.kinds = self.kinds.union(other.kinds);
        self.copies.extend(other.copies);
        self
    }
}

/// A normally-completing expression's value, when it is primitive given the
/// bindings it copies. This is deliberately separate from purity: templates
/// and operators can coerce objects and run user code while still producing a
/// primitive result when they complete.
fn primitive_value(expression: &Expr) -> Option<PrimitiveValue> {
    match expression.unwrap_parens() {
        Expr::Ident(id) => Some(PrimitiveValue {
            kinds: PrimKinds::NONE,
            copies: BTreeSet::from([id.to_id()]),
        }),
        Expr::Bin(binary)
            if matches!(
                binary.op,
                BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
            ) =>
        {
            Some(primitive_value(&binary.left)?.join(primitive_value(&binary.right)?))
        }
        Expr::Cond(cond) => Some(primitive_value(&cond.cons)?.join(primitive_value(&cond.alt)?)),
        Expr::Seq(sequence) => primitive_value(sequence.exprs.last()?),
        computed => evaluate(computed, &NoBindings).map(|kinds| PrimitiveValue {
            kinds,
            copies: BTreeSet::new(),
        }),
    }
}

/// Resolves stable local bindings to their types and the imports they copy.
/// A binding copying an unstable binding, or reached again through a cycle,
/// stays unknown.
struct StableValueResolver<'a> {
    bindings: &'a BTreeMap<Id, PrimitiveValue>,
    imported_bindings: &'a BTreeMap<Id, String>,
    resolved: BTreeMap<Id, Option<(PrimKinds, BTreeSet<String>)>>,
}

impl StableValueResolver<'_> {
    fn resolve(&mut self, value: &PrimitiveValue) -> Option<(PrimKinds, BTreeSet<String>)> {
        let mut kinds = value.kinds;
        let mut imports = BTreeSet::new();
        for copy in &value.copies {
            if let Some(local) = self.imported_bindings.get(copy) {
                imports.insert(local.clone());
                continue;
            }
            let (copied, copied_imports) = self.resolve_binding(copy)?;
            kinds = kinds.union(copied);
            imports.extend(copied_imports);
        }
        Some((kinds, imports))
    }

    fn resolve_binding(&mut self, id: &Id) -> Option<(PrimKinds, BTreeSet<String>)> {
        if let Some(resolved) = self.resolved.get(id) {
            return resolved.clone();
        }
        // Visiting marks the binding unknown, so a cycle resolves to unknown.
        self.resolved.insert(id.clone(), None);
        let value = self.bindings.get(id)?;
        let resolved = self.resolve(value);
        self.resolved.insert(id.clone(), resolved.clone());
        resolved
    }
}

fn collect_primitive_export_candidates(
    item: &ModuleItem,
    bindings: &mut BTreeMap<Id, PrimitiveValue>,
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
                && let Some(mut value) = primitive_value(initializer)
            {
                // A `var` read before its initializer runs, including by an
                // importer in a module cycle, is `undefined`.
                if var.kind == VarDeclKind::Var {
                    value.kinds = value.kinds.union(PrimKinds::UNDEFINED);
                }
                bindings.insert(id.id.to_id(), value);
            }
        }
    }
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => {
            for id in binding_targets::declaration_ids(&decl.decl) {
                exports.entry(id.0.to_string()).or_default().push(Some(
                    PrimitiveExportSource::Value(PrimitiveValue {
                        kinds: PrimKinds::NONE,
                        copies: BTreeSet::from([id]),
                    }),
                ));
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
                                    Some(PrimitiveExportSource::Value(PrimitiveValue {
                                        kinds: PrimKinds::NONE,
                                        copies: BTreeSet::from([id.to_id()]),
                                    }))
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
            let source = primitive_value(&default.expr).map(PrimitiveExportSource::Value);
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
        resources,
        binding_declarations,
        binding_writes,
        mut unresolved_uses,
        dynamic_scope,
        ..
    } = module_scan;
    for spans in unresolved_uses.values_mut() {
        spans.sort_unstable_by_key(|span| (span.lo.0, span.hi.0));
    }
    let global_uses = Arc::new(ParsedGlobalUses {
        source_file: parsed.source_file_name(),
        uses: unresolved_uses,
        dynamic_scope,
    });
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

    let census_sites = interop_uses
        .iter()
        .map(|(_, span, _, _)| loader_phase::site_key(*span))
        .collect();
    let reach = loader_reach::loader_reach(&parsed.module, &census_sites, dynamic_scope);
    let suspended = loader_phase::mandatorily_suspended_sites(
        &parsed.module,
        &census_sites
            .union(&loader_reach::tracked_use_keys(&reach))
            .copied()
            .collect(),
    );
    let phase = |key: loader_phase::SiteKey| {
        if reach.unreachable.contains(&key) {
            SitePhase::Unreachable
        } else if suspended.contains(&key) {
            SitePhase::MandatorilySuspended
        } else if reach.top_level.contains(&key) {
            SitePhase::ModuleBody
        } else {
            SitePhase::Synchronous
        }
    };
    let key_line = |(lo, hi): loader_phase::SiteKey| {
        line_index.line_for_span(Span::new(swc_common::BytePos(lo), swc_common::BytePos(hi)))
    };
    let loader_bindings = reach
        .tracked
        .into_iter()
        .map(|tracked| LoaderBindingUses {
            binding: match tracked.binding {
                loader_reach::TrackedBinding::Import { source, imported } => {
                    LoaderBinding::Import { source, imported }
                }
                loader_reach::TrackedBinding::LoaderAlias {
                    definition: (lo, hi),
                } => LoaderBinding::LoaderAlias {
                    definition: Span::new(swc_common::BytePos(lo), swc_common::BytePos(hi)),
                    exported_as: tracked.exported_as,
                },
                loader_reach::TrackedBinding::CreatedLoader { .. } => LoaderBinding::CreatedLoader,
            },
            uses: tracked
                .uses
                .into_iter()
                .map(|tracked_use| LoaderBindingUse {
                    item_index: tracked_use.item_index,
                    line: key_line(tracked_use.key),
                    literal: tracked_use.literal,
                    call: tracked_use.call,
                    consumer: tracked_use.consumer,
                    phase: phase(tracked_use.key),
                })
                .collect(),
        })
        .collect();
    let namespace_requests = reach
        .namespace_sources
        .into_iter()
        .map(|(item_index, specifier)| NamespaceRequest {
            item_index,
            specifier,
        })
        .collect();
    let mut runtime_facts = ChunkRuntimeFacts {
        global_uses: Some(global_uses),
        parsed_source_imports: Some(parsed_source_imports(parsed)),
        resources,
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
                span,
                kind,
                specifier,
                phase: phase(loader_phase::site_key(span)),
            })
            .collect(),
        loader_bindings,
        namespace_requests,
        ..ChunkRuntimeFacts::default()
    };

    let mut primitive_bindings = BTreeMap::new();
    let mut primitive_imported_bindings = BTreeMap::new();
    let mut primitive_exports = BTreeMap::<String, Vec<Option<PrimitiveExportSource>>>::new();
    for item in &parsed.module.body {
        if let ModuleItem::ModuleDecl(decl) = item
            && let Some(request) = static_module_request_for_decl(decl)
        {
            runtime_facts.static_requests.push(request);
        }
        collect_primitive_export_candidates(item, &mut primitive_bindings, &mut primitive_exports);
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(decl)) = item {
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
            && !named.specifiers.is_empty()
        {
            for specifier in &named.specifiers {
                if let ExportSpecifier::Named(named_specifier) = specifier
                    && !named.type_only
                    && !named_specifier.is_type_only
                {
                    export_aliases.push(ExportAliasRecord {
                        exported: module_export_name(
                            named_specifier
                                .exported
                                .as_ref()
                                .unwrap_or(&named_specifier.orig),
                        ),
                        line: line_index.line_for_span(named.span),
                        local: Some(module_export_name(&named_specifier.orig)),
                        source: named.src.as_ref().map(|source| str_value(source)),
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
                source: None,
            });
            continue;
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) = item {
            export_aliases.push(ExportAliasRecord {
                exported: "default".to_string(),
                line: line_index.line_for_span(default_expr.span),
                local: None,
                source: None,
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
        let stable_bindings = primitive_bindings
            .into_iter()
            .filter(|(id, _)| {
                binding_declarations.get(id) == Some(&1) && !binding_writes.contains(id)
            })
            .collect::<BTreeMap<_, _>>();
        let mut resolver = StableValueResolver {
            bindings: &stable_bindings,
            imported_bindings: &primitive_imported_bindings,
            resolved: BTreeMap::new(),
        };
        for (name, sources) in primitive_exports {
            let [Some(source)] = sources.as_slice() else {
                continue;
            };
            let value = match source {
                PrimitiveExportSource::Value(value) => {
                    let Some((kinds, imports)) = resolver.resolve(value) else {
                        continue;
                    };
                    StableExportValue::Local { kinds, imports }
                }
                PrimitiveExportSource::ReExport { source, export } => StableExportValue::ReExport {
                    source: source.clone(),
                    export: export.clone(),
                },
            };
            runtime_facts.stable_export_values.insert(name, value);
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

fn parsed_source_imports(parsed: &ParsedJsModule) -> ParsedSourceImports {
    let mut requests = Vec::new();
    let mut bindings = Vec::new();
    let mut reexports = Vec::new();
    let mut exports = Vec::new();
    for (statement_index, item) in parsed.module.body.iter().enumerate() {
        let statement = StatementId(statement_index);
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
                let request = str_value(&import.src);
                let mode = static_module_request_for_decl(&ModuleDecl::Import(import.clone()))
                    .map(|request| request.mode)
                    .unwrap_or(StaticModuleRequestMode::Unsupported);
                requests.push(ParsedSourceRequest {
                    statement,
                    declaration_span: import.span,
                    request: request.clone(),
                    mode,
                    kind: SourceStaticRequestKind::Import,
                    specifier_spans: import
                        .specifiers
                        .iter()
                        .map(|specifier| specifier.span())
                        .collect(),
                    type_only: import.type_only
                        || (!import.specifiers.is_empty()
                            && import.specifiers.iter().all(|specifier| match specifier {
                                ImportSpecifier::Named(named) => named.is_type_only,
                                ImportSpecifier::Default(_) | ImportSpecifier::Namespace(_) => {
                                    false
                                }
                            })),
                });
                for (specifier_index, specifier) in import.specifiers.iter().enumerate() {
                    let (local, local_span, imported_name, kind, specifier_type_only) =
                        match specifier {
                            ImportSpecifier::Named(named) => {
                                let imported = match &named.imported {
                                    Some(ModuleExportName::Ident(ident)) => ident.sym.to_string(),
                                    Some(ModuleExportName::Str(string)) => str_value(string),
                                    None => named.local.sym.to_string(),
                                };
                                (
                                    named.local.to_id(),
                                    named.local.span,
                                    imported,
                                    ImportSpecifierKind::Named,
                                    named.is_type_only,
                                )
                            }
                            ImportSpecifier::Default(default) => (
                                default.local.to_id(),
                                default.local.span,
                                "default".to_owned(),
                                ImportSpecifierKind::Default,
                                false,
                            ),
                            ImportSpecifier::Namespace(namespace) => (
                                namespace.local.to_id(),
                                namespace.local.span,
                                "*".to_owned(),
                                ImportSpecifierKind::Namespace,
                                false,
                            ),
                        };
                    bindings.push(ParsedSourceImportBinding {
                        statement,
                        specifier_index,
                        local,
                        local_span,
                        declaration_span: import.span,
                        request: request.clone(),
                        imported_name,
                        kind,
                        type_only: import.type_only || specifier_type_only,
                        use_spans: Vec::new(),
                        use_sites: Vec::new(),
                    });
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) => {
                if let Some(source) = &export.src {
                    let mode =
                        static_module_request_for_decl(&ModuleDecl::ExportNamed(export.clone()))
                            .map(|request| request.mode)
                            .unwrap_or(StaticModuleRequestMode::Unsupported);
                    requests.push(ParsedSourceRequest {
                        statement,
                        declaration_span: export.span,
                        request: str_value(source),
                        mode,
                        kind: SourceStaticRequestKind::NamedReExport,
                        specifier_spans: export
                            .specifiers
                            .iter()
                            .map(|specifier| specifier.span())
                            .collect(),
                        type_only: export.type_only
                            || (!export.specifiers.is_empty()
                                && export.specifiers.iter().all(|specifier| match specifier {
                                    ExportSpecifier::Named(named) => named.is_type_only,
                                    ExportSpecifier::Namespace(_) | ExportSpecifier::Default(_) => {
                                        false
                                    }
                                })),
                    });
                    for (specifier_index, specifier) in export.specifiers.iter().enumerate() {
                        let (imported_name, exported_name, kind, specifier_type_only) =
                            match specifier {
                                ExportSpecifier::Named(named) => (
                                    Some(binding_targets::module_export_name(&named.orig)),
                                    Some(binding_targets::module_export_name(
                                        named.exported.as_ref().unwrap_or(&named.orig),
                                    )),
                                    SourceReExportKind::Named,
                                    named.is_type_only,
                                ),
                                ExportSpecifier::Namespace(namespace) => (
                                    None,
                                    Some(binding_targets::module_export_name(&namespace.name)),
                                    SourceReExportKind::Namespace,
                                    false,
                                ),
                                ExportSpecifier::Default(default) => (
                                    Some("default".to_owned()),
                                    Some(default.exported.sym.to_string()),
                                    SourceReExportKind::Default,
                                    false,
                                ),
                            };
                        reexports.push(ParsedSourceReExport {
                            statement,
                            specifier: Some(specifier_index),
                            specifier_span: Some(specifier.span()),
                            declaration_span: export.span,
                            request: str_value(source),
                            imported_name,
                            exported_name,
                            kind,
                            type_only: export.type_only || specifier_type_only,
                        });
                    }
                } else {
                    for specifier in &export.specifiers {
                        if let ExportSpecifier::Named(named) = specifier {
                            let ModuleExportName::Ident(local) = &named.orig else {
                                continue;
                            };
                            exports.push(ParsedSourceExport {
                                statement,
                                specifier_span: Some(named.span),
                                declaration_span: export.span,
                                local: Some(local.to_id()),
                                public_name: binding_targets::module_export_name(
                                    named.exported.as_ref().unwrap_or(&named.orig),
                                ),
                                type_only: export.type_only || named.is_type_only,
                            });
                        }
                    }
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportAll(export)) => {
                requests.push(ParsedSourceRequest {
                    statement,
                    declaration_span: export.span,
                    request: str_value(&export.src),
                    mode: static_module_request_for_decl(&ModuleDecl::ExportAll(export.clone()))
                        .map(|request| request.mode)
                        .unwrap_or(StaticModuleRequestMode::Unsupported),
                    kind: SourceStaticRequestKind::ExportAll,
                    specifier_spans: Vec::new(),
                    type_only: export.type_only,
                });
                reexports.push(ParsedSourceReExport {
                    statement,
                    specifier: None,
                    specifier_span: None,
                    declaration_span: export.span,
                    request: str_value(&export.src),
                    imported_name: None,
                    exported_name: None,
                    kind: SourceReExportKind::All,
                    type_only: export.type_only,
                });
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                for local in binding_targets::declaration_ids(&export.decl) {
                    exports.push(ParsedSourceExport {
                        statement,
                        specifier_span: None,
                        declaration_span: export.span,
                        public_name: local.0.to_string(),
                        local: Some(local),
                        type_only: false,
                    });
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(export)) => {
                let local = match &export.decl {
                    DefaultDecl::Fn(function) => function.ident.as_ref().map(|ident| ident.to_id()),
                    DefaultDecl::Class(class) => class.ident.as_ref().map(|ident| ident.to_id()),
                    DefaultDecl::TsInterfaceDecl(_) => None,
                };
                exports.push(ParsedSourceExport {
                    statement,
                    specifier_span: None,
                    declaration_span: export.span,
                    local,
                    public_name: "default".to_owned(),
                    type_only: matches!(&export.decl, DefaultDecl::TsInterfaceDecl(_)),
                });
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(export)) => {
                exports.push(ParsedSourceExport {
                    statement,
                    specifier_span: None,
                    declaration_span: export.span,
                    local: None,
                    public_name: "default".to_owned(),
                    type_only: false,
                });
            }
            _ => {}
        }
    }
    let imported_ids: HashSet<Id> = bindings
        .iter()
        .map(|binding| binding.local.clone())
        .collect();
    let mut occurrences = SourceImportOccurrenceCollector {
        imported_ids,
        statement: StatementId(0),
        sites_by_id: HashMap::new(),
    };
    for (statement_index, item) in parsed.module.body.iter().enumerate() {
        occurrences.statement = StatementId(statement_index);
        item.visit_with(&mut occurrences);
    }
    for binding in &mut bindings {
        let mut sites = occurrences
            .sites_by_id
            .remove(&binding.local)
            .unwrap_or_default();
        sites.retain(|site| {
            site.span.lo != binding.local_span.lo || site.span.hi != binding.local_span.hi
        });
        sites.sort_by_key(|site| (site.span.lo, site.span.hi));
        sites
            .dedup_by(|left, right| left.span.lo == right.span.lo && left.span.hi == right.span.hi);
        binding.use_spans = sites.iter().map(|site| site.span).collect();
        binding.use_sites = sites
            .into_iter()
            .map(|site| ParsedSourceImportUseSite {
                statement: site.statement,
                span: site.span,
            })
            .collect();
    }
    ParsedSourceImports {
        source_file: parsed.source_file_name(),
        requests,
        bindings,
        reexports,
        exports,
    }
}

struct SourceImportOccurrenceCollector {
    imported_ids: HashSet<Id>,
    statement: StatementId,
    sites_by_id: HashMap<Id, Vec<ParsedSourceImportUseSite>>,
}

impl Visit for SourceImportOccurrenceCollector {
    fn visit_ident(&mut self, ident: &Ident) {
        let id = ident.to_id();
        if self.imported_ids.contains(&id) {
            self.sites_by_id
                .entry(id)
                .or_default()
                .push(ParsedSourceImportUseSite {
                    statement: self.statement,
                    span: ident.span,
                });
        }
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
    resources: Vec<RuntimeResourceUse>,
    dynamic_import_count: usize,
    observable_module_effect: bool,
    has_rewritable_specifier: bool,
    has_cjs_interop: bool,
    has_own_cjs_export_surface: bool,
    item_index: usize,
    interop_uses: Vec<(usize, Span, InteropUseKind, Option<String>)>,
    direct_call_members: BTreeSet<u32>,
    /// Direct `eval` calls already classified as the callee of an
    /// `EvaluatedRequire*Call`.
    evaluated_require_callees: BTreeSet<u32>,
    unresolved_context: SyntaxContext,
    binding_declarations: BTreeMap<Id, usize>,
    binding_writes: BTreeSet<Id>,
    unresolved_uses: BTreeMap<Id, Vec<Span>>,
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

    fn visit_labeled_stmt(&mut self, labeled: &LabeledStmt) {
        // Labels have their own namespace; keep walking the statement body.
        labeled.body.visit_with(self);
    }

    fn visit_break_stmt(&mut self, _break_stmt: &BreakStmt) {}

    fn visit_continue_stmt(&mut self, _continue_stmt: &ContinueStmt) {}

    fn visit_ident(&mut self, node: &Ident) {
        if node.ctxt == self.unresolved_context {
            self.unresolved_uses
                .entry(node.to_id())
                .or_default()
                .push(node.span);
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
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Call(inner) = callee.unwrap_parens()
            && is_eval_of_require(inner, self.unresolved_context)
        {
            let literal = match node.args.as_slice() {
                [arg] if arg.spread.is_none() => match arg.expr.unwrap_parens() {
                    Expr::Lit(Lit::Str(value)) => Some(str_value(value)),
                    _ => None,
                },
                _ => None,
            };
            let kind = if literal.is_some() {
                InteropUseKind::EvaluatedRequireLiteralCall
            } else {
                InteropUseKind::EvaluatedRequireDynamicCall
            };
            self.evaluated_require_callees.insert(inner.span.lo.0);
            self.has_cjs_interop = true;
            self.interop_uses
                .push((self.item_index, node.span, kind, literal));
        }
        if matches!(&node.callee, Callee::Expr(expr) if matches!(expr.unwrap_parens(), Expr::Ident(id) if id.sym.as_ref() == "eval"))
        {
            // Direct eval of code that is exactly `require` reads that binding
            // and nothing else; any other code may read or write any binding.
            if is_eval_of_require(node, self.unresolved_context) {
                self.has_cjs_interop = true;
                if !self.evaluated_require_callees.contains(&node.span.lo.0) {
                    self.interop_uses.push((
                        self.item_index,
                        node.span,
                        InteropUseKind::UnboundRequire,
                        None,
                    ));
                }
            } else {
                self.dynamic_scope = true;
            }
        }
        if matches!(node.callee, Callee::Import(_)) {
            self.dynamic_import_count += 1;
            let literal = match node.args.first() {
                Some(arg) if arg.spread.is_none() => match arg.expr.unwrap_parens() {
                    Expr::Lit(Lit::Str(value)) => Some(str_value(value)),
                    _ => None,
                },
                _ => None,
            };
            self.resources.push(RuntimeResourceUse {
                kind: RuntimeResourceKind::DynamicImport,
                specifier: literal.clone(),
            });
            self.interop_uses.push((
                self.item_index,
                node.span,
                InteropUseKind::DynamicImport,
                literal,
            ));
            if !self.has_rewritable_specifier && dynamic_import_relative_specifier(node).is_some() {
                self.has_rewritable_specifier = true;
            }
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        self.observable_module_effect = true;
        if let Expr::Ident(callee) = &*node.callee {
            let args = node.args.as_deref().unwrap_or_default();
            let kind = if callee.sym == *"Worker" || callee.sym == *"SharedWorker" {
                Some(RuntimeResourceKind::Worker)
            } else if callee.sym == *"URL" && args.get(1).is_some_and(|arg| {
                matches!(&*arg.expr, Expr::Member(member)
                    if matches!(&*member.obj, Expr::MetaProp(meta) if meta.kind == MetaPropKind::ImportMeta)
                    && matches!(&member.prop, MemberProp::Ident(property) if property.sym == *"url"))
            }) {
                Some(RuntimeResourceKind::Url)
            } else { None };
            if let Some(kind) = kind {
                let specifier = args.first().and_then(|arg| match &*arg.expr {
                    Expr::Lit(Lit::Str(value)) if arg.spread.is_none() => Some(str_value(value)),
                    _ => None,
                });
                self.resources.push(RuntimeResourceUse { kind, specifier });
            }
        }
        if !self.has_rewritable_specifier && worker_relative_specifier(node).is_some() {
            self.has_rewritable_specifier = true;
        }
        node.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        // `module.exports` names the export object, not the loader-capable
        // `module` record (`module.require`, `module.parent`).
        if let Expr::Ident(ident) = &*node.obj
            && ident.sym == *"module"
            && ident.ctxt == self.unresolved_context
            && matches!(&node.prop, MemberProp::Ident(prop) if prop.sym == *"exports")
        {
            self.has_cjs_interop = true;
            self.has_own_cjs_export_surface = true;
            self.interop_uses.push((
                self.item_index,
                node.span,
                InteropUseKind::UnboundModuleExports,
                None,
            ));
            return;
        }
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
        // Import specifiers only name the provider interface and establish
        // local bindings; neither is a lexical read in this module. Static
        // request/path facts are handled above and by the request inventory.
    }

    fn visit_named_export(&mut self, node: &NamedExport) {
        if !self.has_rewritable_specifier
            && let Some(src) = &node.src
            && is_relative_specifier(&str_value(src))
        {
            self.has_rewritable_specifier = true;
        }
        // A local named export reads its original local binding. Public
        // aliases and every name in a re-export are interface syntax.
        if node.src.is_none() && !node.type_only {
            for specifier in &node.specifiers {
                if let ExportSpecifier::Named(named) = specifier
                    && !named.is_type_only
                {
                    named.orig.visit_with(self);
                }
            }
        }
    }

    fn visit_export_all(&mut self, node: &ExportAll) {
        if !self.has_rewritable_specifier && is_relative_specifier(&str_value(&node.src)) {
            self.has_rewritable_specifier = true;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::GLOBALS;

    #[test]
    fn records_all_and_only_resolver_unbound_identifier_sites() {
        GLOBALS.set(&Default::default(), || {
            let parsed = js_ast::parse_js_module(
                "fixture/chunk.js",
                r#"
import { imported } from './dep.js';
const local = 1;
const undefined = 2;
const beforeInitialization = later;
let later = 3;
function f(parameter) {
    return missing + missing + local + imported + parameter + undefined + later;
}
eval('dynamicRead');
"#,
            )
            .expect("parse");
            let analysis = analyze_program_shallow(&parsed);
            let global_uses = analysis
                .runtime_facts
                .global_uses
                .expect("parsed shallow analysis records an inventory");
            let names = global_uses
                .uses
                .keys()
                .map(|(name, context)| {
                    assert_eq!(
                        *context,
                        SyntaxContext::empty().apply_mark(parsed.unresolved_mark)
                    );
                    name.to_string()
                })
                .collect::<BTreeSet<_>>();

            assert_eq!(global_uses.source_file, "fixture/chunk.js");
            assert_eq!(
                names,
                BTreeSet::from(["eval".to_string(), "missing".to_string()])
            );
            assert_eq!(
                global_uses
                    .uses
                    .iter()
                    .find(|((name, _), _)| name.as_ref() == "missing")
                    .map(|(_, spans)| spans.len()),
                Some(2)
            );
            assert!(global_uses.dynamic_scope);

            let empty = js_ast::parse_js_module("empty.js", "const local = 1;").expect("parse");
            let empty_analysis = analyze_program_shallow(&empty);
            let complete_empty = empty_analysis
                .runtime_facts
                .global_uses
                .expect("parsed empty inventory is present");
            assert!(complete_empty.uses.is_empty());
            assert!(ChunkRuntimeFacts::default().global_uses.is_none());
        });
    }

    #[test]
    fn global_use_inventory_tracks_lexical_refs_not_interface_names_or_labels() {
        GLOBALS.set(&Default::default(), || {
            let parsed = js_ast::parse_js_module(
                "fixture/module-interfaces.js",
                r#"
import { remoteImport as localImport } from './dep.js';
const local = actualRead;
const shorthand = { shorthandRead };
const imported = localImport;
export { local as publicLocal };
export { plainMissing };
export { missingLocal as publicUnresolved };
export { remoteName as publicReexport } from './reexport.js';
export * as publicNamespace from './namespace.js';
export default defaultRead;
scopeLabel: for (;;) {
    if (condition) continue scopeLabel;
    if (shouldBreak) break scopeLabel;
}
"#,
            )
            .expect("parse");
            let analysis = analyze_program_shallow(&parsed);
            let uses = analysis
                .runtime_facts
                .global_uses
                .expect("parsed shallow analysis records an inventory");
            let names = uses
                .uses
                .keys()
                .map(|(name, _)| name.to_string())
                .collect::<BTreeSet<_>>();

            assert_eq!(
                names,
                BTreeSet::from([
                    "actualRead".to_string(),
                    "condition".to_string(),
                    "defaultRead".to_string(),
                    "missingLocal".to_string(),
                    "plainMissing".to_string(),
                    "shouldBreak".to_string(),
                    "shorthandRead".to_string(),
                ])
            );
            assert!(uses.uses.values().all(|spans| !spans.is_empty()));
        });
    }

    #[test]
    fn folds_constant_string_expressions() {
        for (source, expected) in [
            (r#""require""#, Some("require")),
            (r#""quire".replace(/^/, "re")"#, Some("require")),
            (r#""re" + "quire""#, Some("require")),
            (r#""rexquire".replace("x", "")"#, Some("require")),
            ("`require`", Some("require")),
            (r#""quire".replace(/^/g, "re")"#, None),
            (r#""quire".replace(/q/, "re")"#, None),
            (r#""quire".replace(/^/, "$&")"#, None),
            ("name", None),
        ] {
            GLOBALS.set(&Default::default(), || {
                let parsed =
                    js_ast::parse_js_module("fixture.js", &format!("({source});")).expect("parse");
                let ModuleItem::Stmt(Stmt::Expr(statement)) = &parsed.module.body[0] else {
                    panic!("expression statement");
                };
                assert_eq!(
                    fold_string(&statement.expr).as_deref(),
                    expected,
                    "{source}"
                );
            });
        }
    }
}
