use std::collections::{BTreeMap, HashMap, HashSet};

use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use artifact::{
    ChunkCounts, ChunkFileRecord, ChunkManifest, ExportAliasRecord, ImportRecord,
    ImportSpecifierRecord, KeptTopLevelDeclarationRecord, ParserOptionsRecord,
    TopLevelDeclarationKind,
};
use js_ast::{ParsedJsModule, line_for_span, str_value};

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
            let import_record = describe_import(parsed, decl, imports.len());
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
                        line: line_for_span(parsed, named.span),
                        local: Some(module_export_name(&named_specifier.orig)),
                    });
                }
            }
            continue;
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) = item {
            export_aliases.push(ExportAliasRecord {
                exported: "default".to_string(),
                line: line_for_span(parsed, default_decl.span),
                local: export_default_decl_name(default_decl),
            });
            continue;
        }

        if let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) = item {
            export_aliases.push(ExportAliasRecord {
                exported: "default".to_string(),
                line: line_for_span(parsed, default_expr.span),
                local: None,
            });
            continue;
        }

        if let Some((kind, names)) = classify_top_level_decl(item) {
            let accesses = identifier_accesses_in_module_item(item, kind.into());
            owners.push(OwnerRecord {
                id: format!("owner_{:05}", owners.len()),
                line: item_line(parsed, item),
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
) -> ChunkManifest {
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

    ChunkManifest {
        schema_version: 1,
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
            role: "entry",
        }],
        imports: analysis.imports.clone(),
        export_aliases: analysis.export_aliases.clone(),
        unresolved_exports,
        kept_top_level_declarations,
        logical_modules: None,
        selected_module_lowerings: None,
        extra: BTreeMap::new(),
    }
}

fn describe_import(parsed: &ParsedJsModule, decl: &ImportDecl, index: usize) -> ImportRecord {
    let source = str_value(&decl.src);
    ImportRecord {
        id: format!("import_{index:05}"),
        line: line_for_span(parsed, decl.span),
        source,
        specifiers: decl
            .specifiers
            .iter()
            .map(|specifier| match specifier {
                ImportSpecifier::Default(default) => ImportSpecifierRecord {
                    kind: "default",
                    imported: None,
                    local: default.local.sym.to_string(),
                    source: None,
                },
                ImportSpecifier::Namespace(namespace) => ImportSpecifierRecord {
                    kind: "namespace",
                    imported: None,
                    local: namespace.local.sym.to_string(),
                    source: None,
                },
                ImportSpecifier::Named(named) => ImportSpecifierRecord {
                    kind: "named",
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

fn binding_names(pattern: &Pat) -> Vec<String> {
    match pattern {
        Pat::Ident(ident) => vec![ident.id.sym.to_string()],
        Pat::Rest(rest) => binding_names(&rest.arg),
        Pat::Assign(assign) => binding_names(&assign.left),
        Pat::Array(array) => array
            .elems
            .iter()
            .flatten()
            .flat_map(binding_names)
            .collect(),
        Pat::Object(object) => object
            .props
            .iter()
            .flat_map(|prop| match prop {
                ObjectPatProp::KeyValue(key_value) => binding_names(&key_value.value),
                ObjectPatProp::Assign(assign) => vec![assign.key.id.sym.to_string()],
                ObjectPatProp::Rest(rest) => binding_names(&rest.arg),
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn item_line(parsed: &ParsedJsModule, item: &ModuleItem) -> Option<usize> {
    line_for_span(parsed, item.span())
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
        if member_root_ident(&node.obj).is_some_and(|name| name == "window" || name == "document") {
            self.observed = true;
        }
        node.visit_children_with(self);
    }
}

fn member_root_ident(expr: &Expr) -> Option<&str> {
    match expr {
        Expr::Ident(ident) => Some(ident.sym.as_ref()),
        Expr::Member(member) => member_root_ident(&member.obj),
        Expr::OptChain(opt_chain) => match &*opt_chain.base {
            OptChainBase::Member(member) => member_root_ident(&member.obj),
            OptChainBase::Call(call) => member_root_ident(&call.callee),
        },
        Expr::Paren(paren) => member_root_ident(&paren.expr),
        _ => None,
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
        self.record_assign_target(&node.left, false);
        node.right.visit_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        match &node.left {
            ForHead::VarDecl(_) => {}
            ForHead::Pat(pattern) => self.record_pat_write(pattern),
            ForHead::UsingDecl(_) => {}
        }
        node.right.visit_with(self);
        node.body.visit_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        match &node.left {
            ForHead::VarDecl(_) => {}
            ForHead::Pat(pattern) => self.record_pat_write(pattern),
            ForHead::UsingDecl(_) => {}
        }
        node.right.visit_with(self);
        node.body.visit_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete
            && let Expr::Member(member) = &*node.arg
        {
            self.record_member_target(member);
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        self.record_update_target(&node.arg);
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

    fn record_assign_target(&mut self, target: &AssignTarget, force_member_write: bool) {
        match target {
            AssignTarget::Simple(simple) => {
                self.record_simple_assign_target(simple, force_member_write)
            }
            AssignTarget::Pat(pattern) => self.record_assign_target_pat(pattern),
        }
    }

    fn record_simple_assign_target(
        &mut self,
        target: &SimpleAssignTarget,
        force_member_write: bool,
    ) {
        match target {
            SimpleAssignTarget::Ident(ident) => {
                if force_member_write {
                    self.record_member_write(ident.id.sym.as_ref());
                } else {
                    self.record_write(ident.id.sym.as_ref());
                }
            }
            SimpleAssignTarget::Member(member) => {
                self.record_member_target(member);
            }
            SimpleAssignTarget::Paren(paren) => {
                self.record_assign_expr_target(&paren.expr, force_member_write);
            }
            SimpleAssignTarget::OptChain(opt_chain) => {
                if let Some(name) = opt_chain_base_name(opt_chain) {
                    self.record_member_write(name);
                }
            }
            _ => {}
        }
    }

    fn record_assign_expr_target(&mut self, target: &Expr, force_member_write: bool) {
        match target {
            Expr::Ident(ident) => {
                if force_member_write {
                    self.record_member_write(ident.sym.as_ref());
                } else {
                    self.record_write(ident.sym.as_ref());
                }
            }
            Expr::Member(member) => self.record_member_target(member),
            Expr::Paren(paren) => self.record_assign_expr_target(&paren.expr, force_member_write),
            Expr::OptChain(opt_chain) => {
                if let Some(name) = opt_chain_base_name(opt_chain) {
                    self.record_member_write(name);
                }
            }
            _ => {}
        }
    }

    fn record_assign_target_pat(&mut self, target: &AssignTargetPat) {
        match target {
            AssignTargetPat::Array(array) => {
                for element in array.elems.iter().flatten() {
                    self.record_pat_write(element);
                }
            }
            AssignTargetPat::Object(object) => {
                for prop in &object.props {
                    match prop {
                        ObjectPatProp::KeyValue(key_value) => {
                            self.record_pat_write(&key_value.value);
                        }
                        ObjectPatProp::Assign(assign) => {
                            self.record_write(assign.key.id.sym.as_ref());
                        }
                        ObjectPatProp::Rest(rest) => {
                            self.record_pat_write(&rest.arg);
                        }
                    }
                }
            }
            AssignTargetPat::Invalid(_) => {}
        }
    }

    fn record_pat_write(&mut self, pattern: &Pat) {
        for name in binding_names(pattern) {
            self.record_write(&name);
        }
    }

    fn record_member_target(&mut self, member: &MemberExpr) {
        if let Some(name) = member_root_ident(&member.obj) {
            self.record_member_write(name);
        }
    }

    fn record_update_target(&mut self, target: &Expr) {
        match target {
            Expr::Ident(ident) => {
                self.record_read(ident.sym.as_ref());
                self.record_write(ident.sym.as_ref());
            }
            Expr::Member(member) => {
                if let Some(name) = member_root_ident(&member.obj) {
                    self.record_read(name);
                    self.record_member_write(name);
                }
            }
            Expr::Paren(paren) => self.record_update_target(&paren.expr),
            Expr::OptChain(opt_chain) => {
                if let Some(name) = opt_chain_base_name(opt_chain) {
                    self.record_read(name);
                    self.record_member_write(name);
                }
            }
            _ => {}
        }
    }
}

fn opt_chain_base_name(opt_chain: &OptChainExpr) -> Option<&str> {
    match &*opt_chain.base {
        OptChainBase::Member(member) => member_root_ident(&member.obj),
        OptChainBase::Call(call) => member_root_ident(&call.callee),
    }
}
