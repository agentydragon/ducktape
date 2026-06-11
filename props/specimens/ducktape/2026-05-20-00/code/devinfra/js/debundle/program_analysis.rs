use std::collections::{HashMap, HashSet};

use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{
    ChunkAnalysis, ChunkCounts, ChunkFileRecord, ExportAliasRecord, FileRole, ImportRecord,
    ImportSpecifierKind, ImportSpecifierRecord, KeptTopLevelDeclarationRecord, ParserOptionsRecord,
    TopLevelDeclarationKind,
};
use binding_targets::{
    TargetAccessRecorder, binding_names, member_root_sym, record_assign_target,
    record_member_target, record_pat_write, record_update_target,
};
use js_ast::{ParsedJsModule, SourceLineIndex, str_value};

pub struct ProgramAnalysis {
    pub imports: Vec<ImportRecord>,
    pub import_by_local_name: HashMap<String, ImportSpecifierRecord>,
    pub export_aliases: Vec<ExportAliasRecord>,
    pub owners: Vec<OwnerRecord>,
    pub side_effects: Vec<SideEffectRecord>,
    pub dynamic_import_count: usize,
    pub observable_module_effect: bool,
}

pub struct OwnerRecord {
    pub id: String,
    pub line: Option<usize>,
    pub names: Vec<String>,
    pub ordinal: usize,
    pub kind: TopLevelDeclarationKind,
    pub accesses: IdentifierAccesses,
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

impl From<TopLevelDeclarationKind> for AccessSourceKind {
    fn from(kind: TopLevelDeclarationKind) -> Self {
        match kind {
            TopLevelDeclarationKind::Function => AccessSourceKind::Function,
            TopLevelDeclarationKind::Class => AccessSourceKind::Class,
            TopLevelDeclarationKind::Variable => AccessSourceKind::Variable,
        }
    }
}

pub struct SideEffectRecord {
    pub id: String,
    pub ordinal: usize,
    pub accesses: IdentifierAccesses,
    pub runtime_sensitive: bool,
    pub replayable: bool,
}

#[derive(Default, Clone)]
pub struct IdentifierAccesses {
    eager_reads: HashSet<String>,
    lazy_reads: HashSet<String>,
    eager_writes: HashSet<String>,
    lazy_writes: HashSet<String>,
    eager_member_writes: HashSet<String>,
    lazy_member_writes: HashSet<String>,
}

pub fn analyze_program_shallow(parsed: &ParsedJsModule) -> ProgramAnalysis {
    let line_index = parsed.line_index();
    let mut imports = Vec::new();
    let mut import_by_local_name = HashMap::new();
    let mut export_aliases = Vec::new();
    let mut owners = Vec::new();
    let mut side_effects = Vec::new();
    let mut dynamic_imports = DynamicImportCounter::default();
    parsed.module.visit_with(&mut dynamic_imports);
    let observable_module_effect = observable_effect_module(&parsed.module);

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
            let accesses = identifier_accesses_in_module_item(item, kind.into());
            owners.push(OwnerRecord {
                id: format!("owner_{:05}", owners.len()),
                line: item_line(&line_index, item),
                names,
                ordinal,
                kind,
                accesses,
            });
            continue;
        }

        let accesses = identifier_accesses_in_module_item(item, AccessSourceKind::SideEffect);
        side_effects.push(SideEffectRecord {
            id: format!("side_effect_{:05}", side_effects.len()),
            ordinal,
            runtime_sensitive: runtime_sensitive_module_item(item),
            replayable: matches!(item, ModuleItem::Stmt(Stmt::Expr(_))),
            accesses,
        });
    }

    ProgramAnalysis {
        imports,
        import_by_local_name,
        export_aliases,
        owners,
        side_effects,
        dynamic_import_count: dynamic_imports.count,
        observable_module_effect,
    }
}

pub fn build_chunk_manifest_from_analysis(
    chunk_id: &str,
    entry_file: &str,
    source_path: &str,
    analysis: &ProgramAnalysis,
) -> ChunkAnalysis {
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

    ChunkAnalysis {
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

fn module_export_name(name: &ModuleExportName) -> String {
    name.atom().to_string()
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

#[derive(Clone, Copy, PartialEq, Eq)]
enum AccessPhase {
    Eager,
    Lazy,
}

#[derive(Clone, Copy)]
enum AccessSourceKind {
    Variable,
    Function,
    Class,
    SideEffect,
}

fn identifier_accesses_in_module_item(
    item: &ModuleItem,
    source_kind: AccessSourceKind,
) -> IdentifierAccesses {
    let mut collector = IdentifierAccessCollector::new(source_kind);
    item.visit_with(&mut collector);
    collector.accesses
}

fn runtime_sensitive_module_item(item: &ModuleItem) -> bool {
    let mut collector = RuntimeSensitiveCollector::default();
    item.visit_with(&mut collector);
    collector.sensitive
}

fn observable_effect_module(module: &Module) -> bool {
    let mut collector = ObservableTopLevelEffectCollector::default();
    module.visit_with(&mut collector);
    collector.observed
}

#[derive(Default)]
struct DynamicImportCounter {
    count: usize,
}

impl Visit for DynamicImportCounter {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if matches!(node.callee, Callee::Import(_)) {
            self.count += 1;
        }
        node.visit_children_with(self);
    }
}

#[derive(Default)]
struct RuntimeSensitiveCollector {
    sensitive: bool,
}

impl Visit for RuntimeSensitiveCollector {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if matches!(node.callee, Callee::Import(_))
            || matches!(&node.callee, Callee::Expr(expr) if matches!(&**expr, Expr::Ident(ident) if ident.sym == *"eval"))
        {
            self.sensitive = true;
        }
        node.visit_children_with(self);
    }

    fn visit_meta_prop_expr(&mut self, _node: &MetaPropExpr) {
        self.sensitive = true;
    }

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        self.sensitive = true;
        node.visit_children_with(self);
    }
}

#[derive(Default)]
struct ObservableTopLevelEffectCollector {
    observed: bool,
}

impl Visit for ObservableTopLevelEffectCollector {
    fn visit_new_expr(&mut self, node: &NewExpr) {
        self.observed = true;
        node.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if member_root_sym(&node.obj).is_some_and(|sym| sym == "window" || sym == "document") {
            self.observed = true;
        }
        node.visit_children_with(self);
    }
}

struct IdentifierAccessCollector {
    accesses: IdentifierAccesses,
    phase: AccessPhase,
}

impl Visit for IdentifierAccessCollector {
    fn visit_ident(&mut self, node: &Ident) {
        self.record_read(node.sym.as_ref());
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    fn visit_named_export(&mut self, _node: &NamedExport) {}

    fn visit_export_default_decl(&mut self, _node: &ExportDefaultDecl) {}

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        self.with_phase(AccessPhase::Lazy, |collector| {
            node.function.visit_with(collector);
        });
    }

    fn visit_fn_expr(&mut self, node: &FnExpr) {
        self.with_phase(AccessPhase::Lazy, |collector| {
            node.function.visit_with(collector);
        });
    }

    fn visit_function(&mut self, node: &Function) {
        self.with_phase(AccessPhase::Lazy, |collector| {
            node.visit_children_with(collector);
        });
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.with_phase(AccessPhase::Lazy, |collector| {
            node.visit_children_with(collector);
        });
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        record_assign_target(&node.left, self);
        node.right.visit_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        match &node.left {
            ForHead::VarDecl(_) => {}
            ForHead::Pat(pattern) => record_pat_write(pattern, self),
            ForHead::UsingDecl(_) => {}
        }
        node.right.visit_with(self);
        node.body.visit_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        match &node.left {
            ForHead::VarDecl(_) => {}
            ForHead::Pat(pattern) => record_pat_write(pattern, self),
            ForHead::UsingDecl(_) => {}
        }
        node.right.visit_with(self);
        node.body.visit_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete
            && let Expr::Member(member) = &*node.arg
        {
            record_member_target(member, self);
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        record_update_target(&node.arg, self);
    }
}

impl IdentifierAccessCollector {
    fn new(source_kind: AccessSourceKind) -> Self {
        Self {
            accesses: IdentifierAccesses::default(),
            phase: match source_kind {
                AccessSourceKind::Function => AccessPhase::Lazy,
                _ => AccessPhase::Eager,
            },
        }
    }

    fn with_phase(&mut self, phase: AccessPhase, visit: impl FnOnce(&mut Self)) {
        let previous = self.phase;
        self.phase = phase;
        visit(self);
        self.phase = previous;
    }

    fn record_read(&mut self, name: &str) {
        match self.phase {
            AccessPhase::Eager => self.accesses.eager_reads.insert(name.to_string()),
            AccessPhase::Lazy => self.accesses.lazy_reads.insert(name.to_string()),
        };
    }

    fn record_write(&mut self, name: &str) {
        match self.phase {
            AccessPhase::Eager => self.accesses.eager_writes.insert(name.to_string()),
            AccessPhase::Lazy => self.accesses.lazy_writes.insert(name.to_string()),
        };
    }

    fn record_member_write(&mut self, name: &str) {
        match self.phase {
            AccessPhase::Eager => self.accesses.eager_member_writes.insert(name.to_string()),
            AccessPhase::Lazy => self.accesses.lazy_member_writes.insert(name.to_string()),
        };
    }
}

impl TargetAccessRecorder for IdentifierAccessCollector {
    fn record_binding_read(&mut self, id: &Id) {
        self.record_read(id.0.as_ref());
    }

    fn record_binding_write(&mut self, id: &Id) {
        self.record_write(id.0.as_ref());
    }

    fn record_member_write(&mut self, id: &Id) {
        IdentifierAccessCollector::record_member_write(self, id.0.as_ref());
    }
}
