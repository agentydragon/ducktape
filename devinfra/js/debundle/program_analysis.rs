use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use swc_common::Spanned;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use analysis_summary::{
    AnalysisSummary, ModuleAnalysis, OwnerAccessRecord, OwnerAnalysis, OwnerDependencyEdge,
    SideEffectAnalysis,
};
use artifact::{
    ChunkCounts, ChunkFileRecord, ChunkManifest, ExportAliasRecord, ImportRecord,
    ImportSpecifierRecord, KeptTopLevelDeclarationRecord, ParserOptionsRecord,
};
use js_ast::{ParsedJsModule, line_for_span, str_value};
use module_path::resolve_dep;

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
    pub node_type: String,
    pub accesses: IdentifierAccesses,
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

impl IdentifierAccesses {
    fn touched_names(&self) -> Vec<String> {
        let mut names = self
            .eager_reads
            .iter()
            .chain(self.lazy_reads.iter())
            .chain(self.eager_writes.iter())
            .chain(self.lazy_writes.iter())
            .chain(self.eager_member_writes.iter())
            .chain(self.lazy_member_writes.iter())
            .cloned()
            .collect::<Vec<_>>();
        names.sort();
        names.dedup();
        names
    }
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

        let names = top_level_declaration_names(item);
        if !names.is_empty() {
            let accesses = identifier_accesses_in_module_item(
                item,
                access_source_kind_for_module_item(module_item_type(item)),
            );
            owners.push(OwnerRecord {
                id: format!("owner_{:05}", owners.len()),
                line: item_line(parsed, item),
                names,
                ordinal,
                node_type: module_item_type(item).to_string(),
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

pub fn analyze_runtime_boundary_program(parsed: &ParsedJsModule) -> ProgramAnalysis {
    let mut imports = Vec::new();
    let mut import_by_local_name = HashMap::new();
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

        let names = runtime_boundary_declaration_names(item);
        if !names.is_empty() {
            let accesses = identifier_accesses_in_module_item(
                item,
                access_source_kind_for_module_item(runtime_boundary_module_item_type(item)),
            );
            owners.push(OwnerRecord {
                id: format!("owner_{:05}", owners.len()),
                line: item_line(parsed, item),
                names,
                ordinal,
                node_type: runtime_boundary_module_item_type(item).to_string(),
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
        export_aliases: Vec::new(),
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
            node_type: owner.node_type.clone(),
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
            parts: 0,
            split_function_declarations: 0,
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
        parts: Vec::new(),
        owner_to_part: BTreeMap::new(),
        logical_modules: None,
        selected_module_lowerings: None,
        extra: BTreeMap::new(),
    }
}

pub fn analyze_modules(
    modules: Vec<(String, String, &ParsedJsModule)>,
    export_counts: &HashMap<String, usize>,
) -> AnalysisSummary {
    let analyses = modules
        .iter()
        .map(|(_, _, parsed)| analyze_runtime_boundary_program(parsed))
        .collect::<Vec<_>>();
    let module_member_names = analyses
        .iter()
        .map(|analysis| {
            analysis
                .owners
                .iter()
                .flat_map(|owner| owner.names.iter().cloned())
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();
    let owner_ids_by_module = modules
        .iter()
        .zip(module_member_names.iter())
        .map(|((source_path, _, _), members)| {
            (
                source_path.clone(),
                members
                    .iter()
                    .map(|member| format!("{source_path}::{member}"))
                    .collect::<Vec<_>>(),
            )
        })
        .collect::<HashMap<_, _>>();
    let universe = modules
        .iter()
        .map(|(source_path, _, _)| source_path.clone())
        .collect::<HashSet<_>>();

    let mut module_summaries = Vec::new();
    for (((source_path, _, _), analysis), members) in modules
        .iter()
        .zip(analyses.iter())
        .zip(module_member_names.iter())
    {
        let import_specifiers = analysis
            .imports
            .iter()
            .map(|import| import.source.clone())
            .collect::<Vec<_>>();
        let resolved_deps = import_specifiers
            .iter()
            .filter_map(|spec| resolve_dep(source_path, spec))
            .filter(|dep| universe.contains(dep))
            .collect::<Vec<_>>();
        let owner_semantic_id_by_member_name = analysis
            .owners
            .iter()
            .flat_map(|owner| {
                owner
                    .names
                    .iter()
                    .map(|name| (name.clone(), owner.id.clone()))
                    .collect::<Vec<_>>()
            })
            .collect::<HashMap<_, _>>();
        let owner_ids = analysis
            .owners
            .iter()
            .map(|owner| owner.id.clone())
            .collect::<Vec<_>>();
        let mut program_item_ids = Vec::new();
        for item in analysis.imports.iter().map(|import| import.id.clone()) {
            program_item_ids.push(item);
        }
        let mut ordered_items = analysis
            .owners
            .iter()
            .map(|owner| (owner.ordinal, owner.id.clone()))
            .chain(
                analysis
                    .side_effects
                    .iter()
                    .map(|side_effect| (side_effect.ordinal, side_effect.id.clone())),
            )
            .collect::<Vec<_>>();
        ordered_items.sort_by_key(|(ordinal, _)| *ordinal);
        program_item_ids.extend(ordered_items.into_iter().map(|(_, id)| id));

        let mut side_effect_records = analysis
            .side_effects
            .iter()
            .map(|side_effect| SideEffectAnalysis {
                id: side_effect.id.clone(),
                replayable: side_effect.replayable,
                runtime_sensitive: side_effect.runtime_sensitive,
                touched_names: side_effect.accesses.touched_names(),
                touched_owner_ids: Vec::new(),
            })
            .collect::<Vec<_>>();
        let mut side_effect_touched_owner_ids = BTreeSet::new();
        for record in &mut side_effect_records {
            let mut touched = Vec::new();
            for owner_id in owner_ids_by_module
                .get(source_path)
                .cloned()
                .unwrap_or_default()
            {
                if let Some(owner_name) = owner_id.rsplit("::").next()
                    && record.touched_names.iter().any(|name| name == owner_name)
                {
                    touched.push(owner_id);
                }
            }
            for dep in &resolved_deps {
                for owner_id in owner_ids_by_module.get(dep).cloned().unwrap_or_default() {
                    if let Some(owner_name) = owner_id.rsplit("::").next()
                        && record.touched_names.iter().any(|name| name == owner_name)
                    {
                        touched.push(owner_id);
                    }
                }
            }
            touched.sort();
            touched.dedup();
            side_effect_touched_owner_ids.extend(touched.iter().cloned());
            record.touched_owner_ids = touched;
        }

        module_summaries.push(ModuleAnalysis {
            source_path: source_path.clone(),
            member_names: members.clone(),
            import_specifiers,
            resolved_deps,
            export_count: export_counts.get(source_path).copied().unwrap_or_default(),
            has_top_level_effects: analysis.observable_module_effect,
            owner_ids,
            owner_semantic_id_by_member_name,
            program_item_ids,
            side_effect_ids: analysis
                .side_effects
                .iter()
                .map(|side_effect| side_effect.id.clone())
                .collect(),
            replayable_side_effect_ids: analysis
                .side_effects
                .iter()
                .filter(|side_effect| side_effect.replayable)
                .map(|side_effect| side_effect.id.clone())
                .collect(),
            runtime_sensitive_effects: analysis
                .side_effects
                .iter()
                .any(|side_effect| side_effect.runtime_sensitive),
            side_effect_touched_owner_ids: side_effect_touched_owner_ids.into_iter().collect(),
            side_effect_records,
        });
    }

    let module_by_path = module_summaries
        .iter()
        .map(|module| (module.source_path.as_str(), module))
        .collect::<HashMap<_, _>>();
    let owners = modules
        .iter()
        .zip(analyses.iter())
        .flat_map(|((source_path, _, _), analysis)| {
            let module = module_by_path[source_path.as_str()];
            analysis
                .owners
                .iter()
                .flat_map(|owner| {
                    owner.names.iter().map(|member_name| {
                        let owner_id = format!("{source_path}::{member_name}");
                        let accesses = build_owner_access_records(
                            member_name,
                            &owner.accesses,
                            module,
                            &module_by_path,
                        );
                        let dep_edges = owner_dependency_edges_from_accesses(&accesses);
                        OwnerAnalysis {
                            id: owner_id,
                            module_id: source_path.clone(),
                            member_name: member_name.clone(),
                            line: owner.line.unwrap_or_default(),
                            dep_edges,
                            accesses,
                        }
                    })
                })
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();

    AnalysisSummary {
        modules: module_summaries,
        owners,
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

fn top_level_declaration_names(item: &ModuleItem) -> Vec<String> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_names(decl),
        _ => Vec::new(),
    }
}

fn runtime_boundary_declaration_names(item: &ModuleItem) -> Vec<String> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
            declaration_names(&export_decl.decl)
        }
        _ => Vec::new(),
    }
}

fn declaration_names(decl: &Decl) -> Vec<String> {
    match decl {
        Decl::Fn(function) => vec![function.ident.sym.to_string()],
        Decl::Class(class) => vec![class.ident.sym.to_string()],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|decl| binding_names(&decl.name))
            .collect(),
        _ => Vec::new(),
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

fn module_item_type(item: &ModuleItem) -> &'static str {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => "ImportDeclaration",
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(_)) => "ExportDeclaration",
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(_)) => "ExportNamedDeclaration",
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_)) => "ExportDefaultDeclaration",
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => "ExportDefaultExpression",
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => "ExportAllDeclaration",
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => "FunctionDeclaration",
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(_))) => "ClassDeclaration",
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(_))) => "VariableDeclaration",
        ModuleItem::Stmt(Stmt::Expr(_)) => "ExpressionStatement",
        ModuleItem::Stmt(_) => "Statement",
        _ => "ModuleDeclaration",
    }
}

fn runtime_boundary_module_item_type(item: &ModuleItem) -> &'static str {
    match item {
        // Babel represents `export const/function/class ...` as an
        // ExportNamedDeclaration with a declaration payload.
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(_)) => "ExportNamedDeclaration",
        _ => module_item_type(item),
    }
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
    TopLevelDeclaration,
}

fn access_source_kind_for_module_item(item_type: &str) -> AccessSourceKind {
    match item_type {
        "VariableDeclaration" => AccessSourceKind::Variable,
        "FunctionDeclaration" => AccessSourceKind::Function,
        "ClassDeclaration" => AccessSourceKind::Class,
        _ => AccessSourceKind::TopLevelDeclaration,
    }
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

fn build_owner_access_records(
    member_name: &str,
    access_buckets: &IdentifierAccesses,
    module: &ModuleAnalysis,
    module_by_path: &HashMap<&str, &ModuleAnalysis>,
) -> Vec<OwnerAccessRecord> {
    let mut accesses = Vec::new();
    for local_member in &module.member_names {
        if local_member == member_name {
            continue;
        }
        push_owner_access_records(
            &mut accesses,
            local_member,
            Some(format!("{}::{}", module.source_path, local_member)),
            "local_declaration",
            access_buckets,
        );
    }
    for dep in &module.resolved_deps {
        if let Some(dep_module) = module_by_path.get(dep.as_str()) {
            for dep_member in &dep_module.member_names {
                push_owner_access_records(
                    &mut accesses,
                    dep_member,
                    Some(format!("{dep}::{dep_member}")),
                    "local_declaration",
                    access_buckets,
                );
            }
        }
    }
    let known_names = accesses
        .iter()
        .map(|access| access.name.clone())
        .collect::<HashSet<_>>();
    for name in access_buckets.touched_names() {
        if known_names.contains(&name) || name == member_name {
            continue;
        }
        push_owner_access_records(&mut accesses, &name, None, "runtime_import", access_buckets);
    }
    accesses.sort_by(|left, right| {
        left.kind
            .cmp(&right.kind)
            .then_with(|| left.name.cmp(&right.name))
            .then_with(|| left.access_kind.cmp(&right.access_kind))
            .then_with(|| left.phase.cmp(&right.phase))
    });
    accesses
}

fn push_owner_access_records(
    accesses: &mut Vec<OwnerAccessRecord>,
    name: &str,
    owner_id: Option<String>,
    kind: &str,
    access_buckets: &IdentifierAccesses,
) {
    push_access_record_if_present(
        accesses,
        name,
        owner_id.as_deref(),
        kind,
        "read",
        "eager",
        &access_buckets.eager_reads,
    );
    push_access_record_if_present(
        accesses,
        name,
        owner_id.as_deref(),
        kind,
        "read",
        "lazy",
        &access_buckets.lazy_reads,
    );
    push_access_record_if_present(
        accesses,
        name,
        owner_id.as_deref(),
        kind,
        "write",
        "eager",
        &access_buckets.eager_writes,
    );
    push_access_record_if_present(
        accesses,
        name,
        owner_id.as_deref(),
        kind,
        "write",
        "lazy",
        &access_buckets.lazy_writes,
    );
    push_access_record_if_present(
        accesses,
        name,
        owner_id.as_deref(),
        kind,
        "member_write",
        "eager",
        &access_buckets.eager_member_writes,
    );
    push_access_record_if_present(
        accesses,
        name,
        owner_id.as_deref(),
        kind,
        "member_write",
        "lazy",
        &access_buckets.lazy_member_writes,
    );
}

fn push_access_record_if_present(
    accesses: &mut Vec<OwnerAccessRecord>,
    name: &str,
    owner_id: Option<&str>,
    kind: &str,
    access_kind: &str,
    phase: &str,
    bucket: &HashSet<String>,
) {
    if !bucket.contains(name) {
        return;
    }
    accesses.push(OwnerAccessRecord {
        name: name.to_string(),
        access_kind: access_kind.to_string(),
        phase: phase.to_string(),
        owner_id: owner_id.map(str::to_string),
        kind: kind.to_string(),
    });
}

fn owner_dependency_edges_from_accesses(
    accesses: &[OwnerAccessRecord],
) -> Vec<OwnerDependencyEdge> {
    let mut edges = accesses
        .iter()
        .filter_map(|access| {
            if !matches!(
                access.access_kind.as_str(),
                "read" | "write" | "member_write"
            ) {
                return None;
            }
            let owner_id = access.owner_id.as_ref()?;
            Some(OwnerDependencyEdge {
                to_owner_id: owner_id.clone(),
                phase: access.phase.clone(),
                access_kind: access.access_kind.clone(),
            })
        })
        .collect::<Vec<_>>();
    edges.sort_by(|left, right| {
        left.to_owner_id
            .cmp(&right.to_owner_id)
            .then_with(|| left.phase.cmp(&right.phase))
            .then_with(|| left.access_kind.cmp(&right.access_kind))
    });
    edges.dedup_by(|left, right| {
        left.to_owner_id == right.to_owner_id
            && left.phase == right.phase
            && left.access_kind == right.access_kind
    });
    edges
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
