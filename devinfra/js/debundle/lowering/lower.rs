//! Lower a single chunk: produce the per-chunk set of JS files (entry + extracted
//! logical modules) by running every module plan through the rename pipeline,
//! emitting cross-module imports, and naturalizing object shorthand. The chunk-
//! level planning and gate checks that wrap this live in `materialize/`.

use js_ast::import_decl_module_item;
use source_identity::ChunkId;
use source_identity::StatementId;
use std::sync::Mutex;

use super::body_facts::{
    IndexedStaticModuleRequest, PreparedImportBinding, PreparedImportTarget, PreparedStaticRequest,
    source_import_bindings, source_request_targets,
};
use super::module_output::{
    LoweredModuleOutput, ModuleEmissionInputs, PreparedModuleEmission, prepare_module_emission,
};
use swc_common::{DUMMY_SP, GLOBALS, Span, Spanned};

use super::chunk_renames::CHUNK_RENAMES_CONTRIBUTOR;
use super::imports::PreparedImportDecl;
use super::imports::import_emit::{
    disambiguate_import_locals, import_decl_for_plan, preserve_export_specifier_names,
    relative_source,
};
use super::imports::static_request_for_item;

use super::scope_names::{
    collect_local_binding_names, collect_nested_binding_names, collect_occupied_local_names,
};
use super::util::remaining_item_after_selection;
use crate::exports::{
    ExportGrowthFacts, auto_grown_residual_exports, entry_exports_for_moved_bindings,
    export_named_for_bindings, trim_dead_named_specifiers,
};
use crate::imports::{
    ArtifactSourceImportResolutionCache, VendorReimportOracle,
    collect_entry_exports_by_original_local, collect_imported_reexports_by_module,
};
use crate::naturalize::NaturalizedModuleBody;
use crate::placement::EmissionPlacement;
use crate::plans::ModulePlan;
use crate::rename_ledger::{
    RenameIntent, RenameLedger, RenameOrigin, RenameScope, ScopeOccupancy, SealValidation,
    SealedRenames,
};
use crate::visitors::{IdentifierRenamer, RenameCaptureProbe};
use analysis::{ModuleId, top_level_id};
use anyhow::Result;
use artifact::reports::SelectedModuleLowering;
use artifact::reports::{
    CandidateImportTarget, CandidatePreparedRequestBinding, CandidatePreparedRequestRoute,
    CandidateSourceBindingRoute, CandidateSourceUseLocation, OutputFileAddress,
    SourceCorrespondenceStatus,
};
use artifact::{ArtifactIndexes, ChunkBundle, FileMetadata, FileRole, JsFile, JsFileBody};
use frontend_elaboration::chunk_ast::{
    TopLevelDecl, top_level_declaration_ids, top_level_declaration_names,
};
use frontend_elaboration::source_ast::RuntimeImportFacts;
use js_ast::ParsedJsModule;
use rayon::prelude::*;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use swc_common::SyntaxContext;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMutWith, VisitWith};

pub(super) const ENTRY_IMPORT_LOCAL_CONTRIBUTOR: &str = "entry import-local disambiguation";

pub(super) struct LoweredChunk {
    pub(super) files: Vec<JsFile>,
    pub(super) file_records: Vec<(String, FileRole)>,
    pub(super) applied: Vec<SelectedModuleLowering>,
    /// Per-symbol vendor-swap rewrite counts applied at construction
    /// time across this chunk's module bodies (see
    /// `imports::vendor_imports::plan_vendor_reimports`).
    pub(super) vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
}

/// The exact entry and logical-module ASTs after import planning and body
/// rewrites, ready for final file/report construction.
pub(super) struct PreparedCandidateEmissionPlan<'a> {
    files: Vec<JsFile>,
    file_records: Vec<(String, FileRole)>,
    modules: Vec<PreparedModuleEmission<'a>>,
    cross_chunk_import_aliases: Vec<(String, String)>,
    entry_static_requests: Vec<PreparedStaticRequest>,
    entry_binding_renames: BTreeMap<Id, Id>,
    entry_exports_by_original_local: HashMap<Id, String>,
}

impl PreparedCandidateEmissionPlan<'_> {
    pub(super) fn output_file_for_destination(
        &self,
        destination: ModuleId,
        entry_destination: ModuleId,
    ) -> Option<&str> {
        if destination == entry_destination {
            return self.files.first().map(|file| file.path.as_str());
        }
        self.modules
            .iter()
            .find(|module| module.destination_id() == destination)
            .map(|module| module.output_file_layout().0)
    }

    pub(super) fn source_binding_route(
        &self,
        source_chunk: ChunkId,
        source_binding: &Id,
        destination: ModuleId,
        entry_destination: ModuleId,
    ) -> Option<CandidateSourceBindingRoute> {
        let file = self.output_file_for_destination(destination, entry_destination)?;
        if destination == entry_destination {
            let candidate_local = self
                .entry_binding_renames
                .get(source_binding)
                .cloned()
                .unwrap_or_else(|| source_binding.clone());
            if !self
                .files
                .first()?
                .ast()?
                .module
                .body
                .iter()
                .any(|item| item_declares_id(item, &candidate_local))
            {
                return None;
            }
            let (declaration_span, binding_span) = self
                .files
                .first()?
                .ast()?
                .module
                .body
                .iter()
                .find(|item| item_declares_id(item, &candidate_local))
                .map(|item| {
                    (
                        Some(item.span()),
                        binding_identifier_span(item, &candidate_local),
                    )
                })
                .unwrap_or((None, None));
            return Some(CandidateSourceBindingRoute {
                output: OutputFileAddress {
                    chunk: source_chunk,
                    file: file.to_owned(),
                },
                candidate_local: Some(candidate_local),
                declaration_span,
                binding_span,
                exported_specifier_span: None,
                exported_name: self
                    .entry_exports_by_original_local
                    .get(source_binding)
                    .cloned(),
            });
        }
        let module = self
            .modules
            .iter()
            .find(|module| module.destination_id() == destination)?;
        let name = module.plan.bindings.keys().find(|name| {
            top_level_id(name, module.output_context.chunk_top_level_mark) == *source_binding
        })?;
        let emitted_name = module
            .explicit_renames
            .get(name)
            .cloned()
            .unwrap_or_else(|| name.clone());
        let candidate_local =
            top_level_id(&emitted_name, module.output_context.chunk_top_level_mark);
        if !module
            .body
            .iter()
            .any(|item| item_declares_id(item, &candidate_local))
        {
            return None;
        }
        let (declaration_span, binding_span) = module
            .body
            .iter()
            .find(|item| item_declares_id(item, &candidate_local))
            .map(|item| {
                (
                    Some(item.span()),
                    binding_identifier_span(item, &candidate_local),
                )
            })
            .unwrap_or((None, None));
        Some(CandidateSourceBindingRoute {
            output: OutputFileAddress {
                chunk: source_chunk,
                file: file.to_owned(),
            },
            candidate_local: Some(candidate_local),
            declaration_span,
            binding_span,
            exported_specifier_span: None,
            exported_name: module.plan.bindings.get(name).cloned(),
        })
    }

    pub(super) fn candidate_source_unit_positions(
        &self,
        source_chunk: ChunkId,
        source_item: &ModuleItem,
        declarator_span: Option<Span>,
        destination: ModuleId,
        entry_destination: ModuleId,
    ) -> Vec<(OutputFileAddress, usize, Span, Option<Span>, Option<usize>)> {
        let (file, body): (&str, &[ModuleItem]) = if destination == entry_destination {
            let Some(entry) = self.files.first().and_then(JsFile::ast) else {
                return Vec::new();
            };
            (self.files[0].path.as_str(), &entry.module.body)
        } else {
            let Some(module) = self
                .modules
                .iter()
                .find(|m| m.destination_id() == destination)
            else {
                return Vec::new();
            };
            (module.plan.target_file.as_str(), &module.body)
        };
        let mut positions = Vec::new();
        for (item_index, candidate_item) in body.iter().enumerate() {
            if let Some(expected_declarator) = declarator_span {
                let Some(declaration) = var_declaration(candidate_item) else {
                    continue;
                };
                for (candidate_offset, declarator) in declaration.decls.iter().enumerate() {
                    if same_source_span(declarator.span, expected_declarator) {
                        positions.push((
                            OutputFileAddress {
                                chunk: source_chunk,
                                file: file.to_owned(),
                            },
                            item_index,
                            candidate_item.span(),
                            Some(declarator.span),
                            Some(candidate_offset),
                        ));
                    }
                }
            } else if same_source_span(candidate_item.span(), source_item.span()) {
                positions.push((
                    OutputFileAddress {
                        chunk: source_chunk,
                        file: file.to_owned(),
                    },
                    item_index,
                    candidate_item.span(),
                    None,
                    None,
                ));
            }
        }
        positions
    }

    pub(super) fn candidate_identifier_routes(
        &self,
        source_chunk: ChunkId,
        source_span: Span,
    ) -> Vec<CandidateSourceUseLocation> {
        let mut routes = Vec::new();
        for file in &self.files {
            let Some(parsed) = file.ast() else { continue };
            for (item_index, item) in parsed.module.body.iter().enumerate() {
                let mut visitor = MatchingIdentVisitor {
                    source_span,
                    candidate_locals: Vec::new(),
                };
                item.visit_with(&mut visitor);
                for (candidate_local, candidate_span) in visitor.candidate_locals {
                    routes.push(CandidateSourceUseLocation {
                        output: OutputFileAddress {
                            chunk: source_chunk,
                            file: file.path.clone(),
                        },
                        item_index,
                        candidate_span,
                        candidate_local,
                    });
                }
            }
        }
        for module in &self.modules {
            for (item_index, item) in module.body.iter().enumerate() {
                let mut visitor = MatchingIdentVisitor {
                    source_span,
                    candidate_locals: Vec::new(),
                };
                item.visit_with(&mut visitor);
                for (candidate_local, candidate_span) in visitor.candidate_locals {
                    routes.push(CandidateSourceUseLocation {
                        output: OutputFileAddress {
                            chunk: source_chunk,
                            file: module.plan.target_file.clone(),
                        },
                        item_index,
                        candidate_span,
                        candidate_local,
                    });
                }
            }
        }
        routes
    }

    pub(super) fn materialized_vendor_outputs(
        &self,
    ) -> impl Iterator<
        Item = (
            &super::module_output::MaterializedVendorOutput,
            &[ModuleItem],
        ),
    > {
        self.modules
            .iter()
            .map(PreparedModuleEmission::vendor_output)
    }

    pub(super) fn prepared_request_routes(
        &self,
        source_chunk: ChunkId,
        source_body: &[ModuleItem],
        source_cells: &BTreeMap<Id, source_identity::ModuleCellId>,
    ) -> Vec<CandidatePreparedRequestRoute> {
        let mut prepared = Vec::new();
        for (file, _source_file, requests, is_entry) in self.prepared_static_request_inventories() {
            let body = if is_entry {
                self.files
                    .first()
                    .and_then(JsFile::ast)
                    .map(|ast| &ast.module.body)
            } else {
                self.modules
                    .iter()
                    .find(|module| module.plan.target_file == file)
                    .map(|module| &module.body)
            };
            let Some(body) = body else {
                continue;
            };
            let output = OutputFileAddress {
                chunk: source_chunk,
                file: file.to_owned(),
            };
            let mut request_cursor = 0usize;
            for (item_index, item) in body.iter().enumerate() {
                let Some(actual_request) = static_request_for_item(item) else {
                    continue;
                };
                let source_statement = source_body
                    .iter()
                    .position(|source_item| same_source_span(source_item.span(), item.span()))
                    .map(StatementId);
                let Some(request) = requests.get(request_cursor) else {
                    prepared.push(CandidatePreparedRequestRoute {
                        output: output.clone(),
                        item_index,
                        item_span: item.span(),
                        request_kind: candidate_request_kind(item),
                        source_statement,
                        emitted_request: actual_request.specifier,
                        mode: actual_request.mode,
                        targets: vec![CandidateImportTarget::Unsupported],
                        bindings: Vec::new(),
                        status: SourceCorrespondenceStatus::MissingCandidateRoute,
                    });
                    continue;
                };
                request_cursor += 1;
                if request.request.specifier != actual_request.specifier
                    || request.request.mode != actual_request.mode
                {
                    prepared.push(CandidatePreparedRequestRoute {
                        output: output.clone(),
                        item_index,
                        item_span: item.span(),
                        request_kind: candidate_request_kind(item),
                        source_statement,
                        emitted_request: actual_request.specifier,
                        mode: actual_request.mode,
                        targets: vec![CandidateImportTarget::Unsupported],
                        bindings: Vec::new(),
                        status: SourceCorrespondenceStatus::Ambiguous,
                    });
                    continue;
                }
                let targets = request
                    .targets
                    .iter()
                    .map(candidate_target)
                    .collect::<Vec<_>>();
                let bindings = request
                    .bindings
                    .iter()
                    .enumerate()
                    .map(
                        |(specifier_index, binding)| CandidatePreparedRequestBinding {
                            source_binding: candidate_import_local(item, specifier_index)
                                .filter(|local| *local == binding.candidate_binding)
                                .filter(|_| binding.exported_name.is_none())
                                .map(|_| binding.source_binding.clone()),
                            candidate_local: candidate_import_local(item, specifier_index)
                                .filter(|local| *local == binding.candidate_binding),
                            specifier_index,
                            specifier_span: candidate_specifier_span(item, specifier_index)
                                .unwrap_or(DUMMY_SP),
                            imported_name: candidate_import_local(item, specifier_index)
                                .map(|_| binding.imported_name.clone()),
                            kind: binding.kind,
                            exported_name: binding.exported_name.clone(),
                            source_cell: source_cells.get(&binding.source_binding).copied(),
                            target: candidate_target(&binding.target),
                        },
                    )
                    .collect::<Vec<_>>();
                let supported = bindings.iter().all(|binding| {
                    binding.specifier_span != DUMMY_SP && binding.candidate_local.is_some()
                }) && targets
                    .iter()
                    .chain(bindings.iter().map(|binding| &binding.target))
                    .all(|target| {
                        !matches!(
                            target,
                            CandidateImportTarget::Unsupported
                                | CandidateImportTarget::OriginalSourceUnknownExports { .. }
                        )
                    });
                prepared.push(CandidatePreparedRequestRoute {
                    output: output.clone(),
                    item_index,
                    item_span: item.span(),
                    request_kind: candidate_request_kind(item),
                    source_statement,
                    emitted_request: request.request.specifier.clone(),
                    mode: request.request.mode,
                    targets,
                    bindings,
                    status: if supported {
                        SourceCorrespondenceStatus::Complete
                    } else {
                        SourceCorrespondenceStatus::UnsupportedRewrite
                    },
                });
            }
            for request in requests.iter().skip(request_cursor) {
                prepared.push(CandidatePreparedRequestRoute {
                    output: output.clone(),
                    item_index: usize::MAX,
                    item_span: DUMMY_SP,
                    request_kind: match request.request.mode {
                        artifact::reports::StaticModuleRequestMode::OrdinaryEsm => {
                            artifact::reports::CandidateOutputRequestKind::Import
                        }
                        artifact::reports::StaticModuleRequestMode::Unsupported => {
                            artifact::reports::CandidateOutputRequestKind::Import
                        }
                    },
                    source_statement: None,
                    emitted_request: request.request.specifier.clone(),
                    mode: request.request.mode,
                    targets: request.targets.iter().map(candidate_target).collect(),
                    bindings: request
                        .bindings
                        .iter()
                        .enumerate()
                        .map(
                            |(specifier_index, binding)| CandidatePreparedRequestBinding {
                                source_binding: None,
                                candidate_local: None,
                                specifier_index,
                                specifier_span: DUMMY_SP,
                                imported_name: None,
                                kind: binding.kind,
                                exported_name: binding.exported_name.clone(),
                                source_cell: source_cells.get(&binding.source_binding).copied(),
                                target: candidate_target(&binding.target),
                            },
                        )
                        .collect(),
                    status: SourceCorrespondenceStatus::MissingCandidateRoute,
                });
            }
        }
        prepared
    }

    /// The exact file paths and source coordinates consumed by final emission.
    pub(super) fn output_file_layout(&self) -> impl Iterator<Item = (&str, &str)> {
        self.files
            .iter()
            .map(|file| (file.path.as_str(), file.metadata.source_path.as_str()))
            .chain(
                self.modules
                    .iter()
                    .map(|module| module.output_file_layout()),
            )
    }

    /// Ordered static requests from the same construction passes that prepare
    /// each output body. The entry comes first, followed by logical-module
    /// order; later whole-artifact postpasses are outside this inventory.
    pub(super) fn prepared_static_request_inventories<'a>(
        &'a self,
    ) -> impl Iterator<Item = (&'a str, &'a str, &'a [PreparedStaticRequest], bool)> + 'a {
        let entry = self
            .files
            .first()
            .expect("a candidate emission plan always contains its entry file");
        std::iter::once((
            entry.path.as_str(),
            entry.metadata.source_path.as_str(),
            self.entry_static_requests.as_slice(),
            true,
        ))
        .chain(self.modules.iter().map(|module| {
            let (output_file, source_file, requests) = module.prepared_static_request_inventory();
            (output_file, source_file, requests, false)
        }))
    }

    /// The one prepared body that owns a source-unit destination. The entry
    /// inventory is addressed by the assignment's explicit entry destination;
    /// logical bodies retain their canonical ModuleId from preparation.
    pub(super) fn prepared_static_request_inventory_for_destination(
        &self,
        destination: ModuleId,
        entry_destination: ModuleId,
    ) -> Option<(&str, &str, &[PreparedStaticRequest], bool)> {
        if destination == entry_destination {
            let entry = self
                .files
                .first()
                .expect("a candidate emission plan always contains its entry file");
            return Some((
                entry.path.as_str(),
                entry.metadata.source_path.as_str(),
                self.entry_static_requests.as_slice(),
                true,
            ));
        }
        self.modules
            .iter()
            .find(|module| module.destination_id() == destination)
            .map(|module| {
                let (output_file, source_file) = module.output_file_layout();
                let (_, _, requests) = module.prepared_static_request_inventory();
                (output_file, source_file, requests, false)
            })
    }

    pub(super) fn prepend_entry_header_lines(&mut self, prefix: &[String]) {
        let entry = self
            .files
            .first_mut()
            .expect("a candidate emission plan always contains its entry file");
        let mut header_lines = prefix.to_vec();
        header_lines.append(&mut entry.header_lines);
        entry.header_lines = header_lines;
    }

    pub(super) fn emit(self) -> LoweredChunk {
        let Self {
            mut files,
            mut file_records,
            modules,
            cross_chunk_import_aliases,
            ..
        } = self;
        let module_outputs: Vec<LoweredModuleOutput> = GLOBALS.with(|globals| {
            modules
                .into_par_iter()
                .map(|module| GLOBALS.set(globals, || module.emit()))
                .collect()
        });
        let mut applied = Vec::new();
        let mut vendor_reference_rewrites = BTreeMap::<(ChunkId, String), usize>::new();
        for output in module_outputs {
            files.push(output.file);
            file_records.push(output.record);
            let mut lowering = output.lowering;
            lowering.cross_chunk_import_aliases = cross_chunk_import_aliases.clone();
            applied.push(lowering);
            for (key, count) in output.vendor_reference_rewrites {
                *vendor_reference_rewrites.entry(key).or_insert(0) += count;
            }
        }

        LoweredChunk {
            files,
            file_records,
            applied,
            vendor_reference_rewrites,
        }
    }
}

fn candidate_target(target: &PreparedImportTarget) -> CandidateImportTarget {
    match target {
        PreparedImportTarget::OriginalSource { specifier } => {
            CandidateImportTarget::OriginalSource {
                request: specifier.clone(),
            }
        }
        PreparedImportTarget::UnsupportedOriginalSource { specifier }
            if !specifier.starts_with('.') && !specifier.starts_with('/') =>
        {
            CandidateImportTarget::OriginalExternal {
                request: specifier.clone(),
            }
        }
        PreparedImportTarget::UnsupportedOriginalSource { specifier } => {
            CandidateImportTarget::OriginalSourceUnknownExports {
                request: specifier.clone(),
            }
        }
        PreparedImportTarget::CandidateOutput { chunk, file } => {
            CandidateImportTarget::CandidateOutput {
                chunk: *chunk,
                file: file.clone(),
            }
        }
        PreparedImportTarget::Unsupported => CandidateImportTarget::Unsupported,
    }
}

fn candidate_request_kind(item: &ModuleItem) -> artifact::reports::CandidateOutputRequestKind {
    use artifact::reports::CandidateOutputRequestKind as Kind;
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => Kind::Import,
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_some() => {
            Kind::NamedReExport
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => Kind::ExportAll,
        _ => Kind::LocalExport,
    }
}

fn candidate_specifier_span(item: &ModuleItem, index: usize) -> Option<Span> {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
            .specifiers
            .get(index)
            .map(|specifier| specifier.span()),
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) => export
            .specifiers
            .get(index)
            .map(|specifier| specifier.span()),
        _ => None,
    }
}

fn candidate_import_local(item: &ModuleItem, index: usize) -> Option<Id> {
    let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
        return None;
    };
    match import.specifiers.get(index)? {
        ImportSpecifier::Named(specifier) => Some(specifier.local.to_id()),
        ImportSpecifier::Default(specifier) => Some(specifier.local.to_id()),
        ImportSpecifier::Namespace(specifier) => Some(specifier.local.to_id()),
    }
}

fn binding_identifier_span(item: &ModuleItem, wanted: &Id) -> Option<Span> {
    struct Finder<'a> {
        wanted: &'a Id,
        span: Option<Span>,
    }
    impl Visit for Finder<'_> {
        fn visit_binding_ident(&mut self, binding: &BindingIdent) {
            if binding.id.to_id() == *self.wanted {
                self.span = Some(binding.id.span);
            }
            binding.visit_children_with(self);
        }
    }
    let mut finder = Finder { wanted, span: None };
    item.visit_with(&mut finder);
    finder.span
}

fn item_declares_id(item: &ModuleItem, wanted: &Id) -> bool {
    if top_level_declaration_ids(item)
        .iter()
        .any(|id| id == wanted)
    {
        return true;
    }
    let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
        return false;
    };
    import.specifiers.iter().any(|specifier| {
        let local = match specifier {
            ImportSpecifier::Named(named) => named.local.to_id(),
            ImportSpecifier::Default(default) => default.local.to_id(),
            ImportSpecifier::Namespace(namespace) => namespace.local.to_id(),
        };
        &local == wanted
    })
}

fn var_declaration(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) => Some(declaration),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(declaration) => Some(declaration),
            _ => None,
        },
        _ => None,
    }
}

fn same_source_span(left: Span, right: Span) -> bool {
    left != DUMMY_SP && right != DUMMY_SP && left.lo == right.lo && left.hi == right.hi
}

struct MatchingIdentVisitor {
    source_span: Span,
    candidate_locals: Vec<(Id, Span)>,
}

impl Visit for MatchingIdentVisitor {
    fn visit_ident(&mut self, ident: &Ident) {
        if same_source_span(ident.span, self.source_span) {
            self.candidate_locals.push((ident.to_id(), ident.span));
        }
    }
}

/// Chunk-identity + file-path inputs to `prepare_candidate_emission_plan`. Held in
/// `LowerChunkInputs::context`.
pub(super) struct LowerChunkContext<'a> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) chunk_id: &'a str,
    pub(super) chunk_id_interned: ChunkId,
    pub(super) source_path: &'a str,
    pub(super) entry_file: &'a str,
    pub(super) header_lines: &'a [String],
    pub(super) vendor_import_oracle: Option<&'a VendorReimportOracle<'a>>,
}

/// AST-side inputs: the parsed runtime module plus the top-level
/// declaration index. Held in `LowerChunkInputs::ast`.
pub(super) struct LowerChunkAst<'a> {
    pub(super) runtime_ast: &'a ParsedJsModule,
    pub(super) declarations: &'a [TopLevelDecl],
    pub(super) declaration_by_name: &'a HashMap<Id, Vec<StatementId>>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
}

/// Plan-side inputs: the per-chunk module plans and preflight-derived emission
/// placement.
/// Held in `LowerChunkInputs::plan`.
pub(super) struct LowerChunkPlan<'output, 'placement> {
    pub(super) module_plans: &'output [ModulePlan],
    pub(super) placement: &'placement EmissionPlacement<'output>,
}

/// Spec-derived + chunk-AST-derived facts the lowerer consults at
/// emission time. Held in `LowerChunkInputs::spec_facts`.
pub(super) struct LowerChunkSpecFacts<'a> {
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
    /// The chunk's sealed rename ledger (explicit contributors collected
    /// and conflict-validated in `materialize_logical_chunk`). The
    /// per-plan naturalize pass below queries its Module-scope maps;
    /// `chunk_renames` is its Chunk-scope projection.
    pub(super) sealed_renames: &'a SealedRenames,
    /// In-place renames from `TransformSpec::chunk_renames` — the sealed
    /// ledger's Chunk-scope projection. Applied
    /// to bindings staying in entry's body — i.e. those *not* in
    /// `binding_assignment`. Bindings claimed by a logical module
    /// take their rename from the module plan; entries here for
    /// those bindings are silently dropped. Iteration order is
    /// undefined; the validation pass sorts by binding name before
    /// iterating so any spec errors are deterministic.
    pub(super) chunk_renames: &'a HashMap<String, String>,
    /// Bindings the source chunk's entry exports verbatim
    /// (`record_pre_existing_named_exports`). Consulted by
    /// `auto_grown_residual_exports` so the auto-grow pass doesn't
    /// emit a `Duplicate export of 'name'` clash with an existing
    /// source export.
    pub(super) pre_existing_entry_exports: &'a HashSet<Id>,
    /// **Public** names entry already uses — the `exported` side of
    /// the same `export { … }` block plus the declared name of any
    /// `export const/function/class` declaration. Consulted by
    /// `auto_grown_residual_exports` so the grown public name
    /// suffix-mints (`<base>$1`) past any source-level alias
    /// collision instead of emitting a duplicate.
    pub(super) pre_existing_public_export_names: &'a HashSet<String>,
}

pub(super) struct LowerChunkInputs<'output, 'placement> {
    pub(super) context: LowerChunkContext<'output>,
    pub(super) ast: LowerChunkAst<'output>,
    pub(super) plan: LowerChunkPlan<'output, 'placement>,
    pub(super) spec_facts: LowerChunkSpecFacts<'output>,
}

pub(super) fn prepare_candidate_emission_plan<'output, 'placement>(
    inputs: LowerChunkInputs<'output, 'placement>,
) -> Result<PreparedCandidateEmissionPlan<'output>> {
    let LowerChunkInputs {
        context,
        ast,
        plan,
        spec_facts,
    } = inputs;
    let LowerChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        chunk_id_interned,
        source_path,
        entry_file,
        header_lines,
        vendor_import_oracle,
    } = context;
    let LowerChunkAst {
        runtime_ast,
        declarations,
        declaration_by_name,
        chunk_top_level_mark,
    } = ast;
    let LowerChunkPlan {
        module_plans,
        placement,
    } = plan;
    let assignment_analysis = placement.assignment_analysis();
    let binding_assignment = placement.bindings();
    let whole_statement_assignment = placement.statements();
    let LowerChunkSpecFacts {
        runtime_import_facts,
        sealed_renames,
        chunk_renames,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
    } = spec_facts;
    let unresolved_context = SyntaxContext::empty().apply_mark(runtime_ast.unresolved_mark);
    let is_module_owned = |name: &str| -> bool {
        binding_assignment.contains_key(&top_level_id(name, chunk_top_level_mark))
    };
    let selected_statements =
        compute_selected_statements(declarations, binding_assignment, whole_statement_assignment);

    let mut selected_by_module = vec![Vec::<ModuleItem>::new(); module_plans.len()];
    let mut selected_exports_by_module =
        vec![Option::<BTreeMap<String, String>>::None; module_plans.len()];
    plan_selected_exports(
        module_plans,
        &is_module_owned,
        &mut selected_exports_by_module,
    );

    let mut entry_body = Vec::new();
    let mut entry_requests_by_item = Vec::<IndexedStaticModuleRequest>::new();
    split_entry_body(
        &runtime_ast.module.body,
        &selected_statements,
        whole_statement_assignment,
        binding_assignment,
        &mut entry_body,
        &mut entry_requests_by_item,
        &mut selected_by_module,
    );
    // Two passes: build entry imports in plan order (so the
    // first plan to claim a binding wins disambiguation), then
    // order them through the shared `EsmImportOrder` so ECMA-262's
    // depth-first link traversal evaluates dependencies first.
    // Plan-order disambiguation + shared-order placement keeps
    // the import-collision contract while satisfying Lemma 2's
    // emit-side constraint. See docs/design.md "Module dep graphs"
    // and "Lemma 2".
    let mut entry_imports: Vec<(ModuleId, PreparedImportDecl)> = Vec::new();
    // Entry-side renames flow through ONE per-chunk ledger sealed once:
    // the chunk_renames entries staying in entry (Explicit, Chunk scope),
    // the entry import-local mints (ImportInduced, Chunk scope), and the
    // auto-grown residual public exports (ImportInduced,
    // EntryPublicExports scope — a separate namespace from local-binding
    // renames). The Chunk-scope contributors' source sets are disjoint by
    // construction — chunk_renames seeding skips module-owned bindings,
    // mints fire only for module-owned bindings — so a seal conflict can
    // only come from one contributor disagreeing with itself.
    //
    // Occupancy facts for seal validation: the post-split entry body's
    // top-level names (root) and everything bound below them (nested).
    // Seal rejects chunk_renames targets colliding at the root level —
    // collecting every violation in one error — and asserts mints stayed
    // clear of both levels; nested-capture facts come from the read-only
    // capture probe below.
    let entry_root_names = collect_occupied_local_names(&entry_body);
    let entry_nested_names = collect_nested_binding_names(&entry_body);
    let mut entry_ledger = RenameLedger::default();
    entry_ledger.seed_taken(RenameScope::Chunk, entry_root_names.iter().cloned());
    // Submit `chunk_renames` intents for bindings staying in entry's
    // body (not claimed by any logical module). Bindings owned by a
    // logical module take their rename from the module plan via the
    // disambiguate-imports pass below; chunk_renames entries for those
    // bindings are silently dropped here (the logical-module rename
    // wins).
    //
    // Each target name is claimed before the import-disambiguation pass
    // runs, so a later cross-module import doesn't mint a fresh local
    // that collides with one of the chunk_renames' targets. Iterate
    // `chunk_renames` (a `HashMap`) in sorted order so the ledger's
    // intent order is stable.
    let mut sorted_renames: Vec<(&String, &String)> = chunk_renames.iter().collect();
    sorted_renames.sort_by(|a, b| a.0.cmp(b.0));
    for (binding, export_name) in sorted_renames {
        if is_module_owned(binding) {
            continue;
        }
        entry_ledger.claim(RenameScope::Chunk, export_name);
        entry_ledger.submit(RenameIntent {
            scope: RenameScope::Chunk,
            from: top_level_id(binding, chunk_top_level_mark),
            to: export_name.as_str().into(),
            origin: RenameOrigin::Explicit {
                contributor: CHUNK_RENAMES_CONTRIBUTOR,
            },
        });
    }
    // Mints must additionally avoid every nested binding name, or the
    // follow-up body rewrite could capture references meant to resolve
    // to the import.
    entry_ledger.seed_taken(RenameScope::Chunk, collect_local_binding_names(&entry_body));
    for (module_index, plan) in module_plans.iter().enumerate() {
        // Drop bindings that don't exist anywhere (no entry in
        // `binding_assignment`). Bindings owned by another plan stay
        // in the import — they're a separate "two plans claim the
        // same binding" disambiguation case handled by
        // `disambiguate_import_locals`.
        let live_bindings: BTreeMap<String, String> = plan
            .bindings
            .iter()
            .filter(|(name, _)| is_module_owned(name))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if live_bindings.is_empty() {
            // A plan with no importable bindings can still carry
            // emitted content (anonymous claimed statements). The
            // entry must still load it — a module nothing imports
            // never evaluates, silently dropping its side effects —
            // so emit a side-effect-only `import "./<plan>.js";`.
            // Included in the shared entry ordering below, exactly
            // like the gate simulator's universal residual edges.
            if !selected_by_module[module_index].is_empty() {
                entry_imports.push((
                    ModuleId(module_index),
                    PreparedImportDecl {
                        item: import_decl_module_item(
                            Vec::new(),
                            &relative_source(entry_file, &plan.target_file),
                        ),
                        targets: vec![PreparedImportTarget::CandidateOutput {
                            chunk: chunk_id_interned,
                            file: plan.target_file.clone(),
                        }],
                        bindings: Vec::new(),
                    },
                ));
            }
            continue;
        }
        let mut emit_renames = BTreeMap::<String, String>::new();
        let resolved = disambiguate_import_locals(
            &live_bindings,
            &mut entry_ledger,
            RenameScope::Chunk,
            &mut emit_renames,
        );
        // A rename only propagates to consumer-body references when the
        // moved decl actually belongs to this plan. Plans that listed a
        // binding without owning the decl emit a dangling import; the
        // body refs continue to resolve to whichever binding owned the
        // original local name.
        for (local, fresh) in emit_renames {
            let local_id = top_level_id(&local, chunk_top_level_mark);
            if binding_assignment.get(&local_id).copied() == Some(module_index) {
                entry_ledger.submit(RenameIntent {
                    scope: RenameScope::Chunk,
                    from: local_id,
                    to: fresh.as_str().into(),
                    origin: RenameOrigin::ImportInduced {
                        contributor: ENTRY_IMPORT_LOCAL_CONTRIBUTOR,
                    },
                });
            }
        }
        let targets = vec![PreparedImportTarget::CandidateOutput {
            chunk: chunk_id_interned,
            file: plan.target_file.clone(),
        }];
        let bindings = live_bindings
            .iter()
            .map(|(source_local, imported_name)| PreparedImportBinding {
                source_binding: top_level_id(source_local, chunk_top_level_mark),
                candidate_binding: top_level_id(
                    minted.get(source_local).unwrap_or(source_local),
                    chunk_top_level_mark,
                ),
                imported_name: imported_name.clone(),
                kind: artifact::reports::ImportSpecifierKind::Named,
                exported_name: None,
                target: targets[0].clone(),
            })
            .collect();
        entry_imports.push((
            ModuleId(module_index),
            PreparedImportDecl {
                item: import_decl_for_plan(
                    entry_file,
                    &plan.target_file,
                    &resolved,
                    chunk_top_level_mark,
                ),
                targets,
                bindings,
            },
        ));
    }
    // Order entry imports through the shared `EsmImportOrder`
    // (Lemma 2, docs/design.md "The realizability theorem"): for
    // acyclic imports graphs the order matches linker_order
    // (dependency-first source), but for cyclic-I shapes accepted
    // by the relaxed clause-3 rule the SCC members are reverse-
    // sorted so DFS unwinds the dependency first in post-order.
    // The gate's evaluation simulator sorts residual's DFS
    // neighbors with the same call, so the emitted entry order and
    // the simulated order are structurally identical.
    assignment_analysis
        .import_order()
        .sort_entry_imports(&mut entry_imports);
    let entry_imports: Vec<PreparedImportDecl> =
        entry_imports.into_iter().map(|(_, it)| it).collect();
    // Capture probe: a read-only walk of the un-renamed entry body with
    // the pending Chunk-scope map, reporting the reference-resolution
    // conflicts seal validates. It uses parser-owned unresolved identity
    // for reverse capture and scope tracking for target-shadow capture. The
    // probe replaces the pre-seal trial application; the body is mutated
    // only by the post-seal executor below.
    let entry_candidate_renames = entry_ledger.pending_renames(&RenameScope::Chunk);
    let mut entry_captured = BTreeSet::new();
    if !entry_candidate_renames.is_empty() {
        let mut probe = RenameCaptureProbe::new(&entry_candidate_renames, unresolved_context);
        for item in entry_body.iter() {
            item.visit_with(&mut probe);
        }
        entry_captured = probe.captured;
    }
    // Naturalize before collecting facts: both entry export growth and module
    // emission must see the same post-rename references and their rename maps.
    let naturalized_modules = selected_by_module
        .into_iter()
        .zip(module_plans)
        .enumerate()
        .map(|(index, (body, plan))| {
            let module = ModuleId(index);
            NaturalizedModuleBody::prepare(
                body,
                plan,
                module,
                sealed_renames.module_renames_by_name(module),
                chunk_top_level_mark,
                unresolved_context,
            )
        })
        .collect::<Result<Vec<_>>>()?;
    // Auto-grow entry's export list for any residual binding a
    // moved module body references. Without this, the per-module
    // emit path below would surface a "moved module references
    // residual entry binding(s) … not exported by entry"
    // rejection — i.e. would refuse to emit valid JS — for any
    // peel whose body happens to read a top-level binding that
    // the upstream source didn't already `export {...}`.
    // Emitting the export makes the assignment importable
    // by construction (see docs/design.md "Valid peels and atomic
    // modules", importability clause). The grow set excludes
    // names already in entry's source-level exports.
    //
    // The mints land in the chunk ledger's `EntryPublicExports` scope —
    // a separate namespace from the Chunk-scope local renames — seeded
    // with the source-level public names so the grown names suffix past
    // them. Intents are keyed by the residual binding's ORIGINAL name;
    // the emitted clause remaps to entry's post-rename local below.
    let entry_declared_names: HashSet<String> = entry_body
        .iter()
        .flat_map(|item| top_level_declaration_names(item).0)
        .collect();
    entry_ledger.seed_taken(
        RenameScope::EntryPublicExports,
        pre_existing_public_export_names.iter().cloned(),
    );
    auto_grown_residual_exports(
        naturalized_modules.iter(),
        &ExportGrowthFacts {
            declaration_by_name,
            binding_assignment,
            pre_existing_entry_exports,
            entry_declared_names: &entry_declared_names,
        },
        chunk_top_level_mark,
        &mut entry_ledger,
    );
    // Seal the chunk ledger: the single validation point for every
    // entry-side rename. Chunk scope: conflicting targets (target name
    // already taken by a body local that isn't being renamed away, or
    // chosen by another chunk_renames entry, invalid as an identifier,
    // or would change reference resolution per the probe above) bail here
    // with every violation listed, rather than producing invalid JS
    // silently. EntryPublicExports scope: a single contributor over a
    // set-deduped source set cannot conflict; the occupancy check
    // asserts the mints stayed clear of pre-existing public names.
    let sealed_entry_renames = entry_ledger.seal(&SealValidation {
        occupancy: BTreeMap::from([
            (
                RenameScope::Chunk,
                ScopeOccupancy::Body {
                    label: chunk_id.to_string(),
                    root: entry_root_names,
                    nested: entry_nested_names,
                    captured: entry_captured,
                },
            ),
            (
                RenameScope::EntryPublicExports,
                ScopeOccupancy::Body {
                    label: chunk_id.to_string(),
                    root: pre_existing_public_export_names.iter().cloned().collect(),
                    nested: BTreeSet::new(),
                    captured: BTreeSet::new(),
                },
            ),
        ]),
        reserved: BTreeSet::new(),
    })?;
    let entry_binding_renames_by_id = sealed_entry_renames
        .scope_renames(&RenameScope::Chunk)
        .unwrap_or_default();
    debug_assert_eq!(
        entry_binding_renames_by_id, entry_candidate_renames,
        "sealed entry renames diverged from the pending map the capture probe walked",
    );
    let entry_binding_renames = sealed_entry_renames.scope_renames_by_name(&RenameScope::Chunk);
    // Execute once: the single rename pass over the entry body, applying
    // the sealed Chunk-scope map. Re-exports `export { local }` (without
    // `from`) collapse `local` and the public exported name into a single
    // ident; renaming the orig would also rename the public name, so
    // rewrite them to `export { fresh as local }` before the generic
    // renamer visits the rest. The probe above already fed seal the walk's
    // capture verdict, so a capture here means probe and executor diverged.
    if !entry_binding_renames_by_id.is_empty() {
        for item in entry_body.iter_mut() {
            preserve_export_specifier_names(item, &entry_binding_renames_by_id);
        }
        let mut renamer = IdentifierRenamer::new(&entry_binding_renames_by_id, unresolved_context);
        for item in entry_body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
        debug_assert!(
            renamer.captured.is_empty(),
            "entry rename executor captured despite seal validation: {:?}",
            renamer.captured,
        );
        for request in &mut entry_requests_by_item {
            for binding in &mut request.bindings {
                if let Some(fresh) = entry_binding_renames_by_id.get(&binding.candidate_binding) {
                    binding.candidate_binding = (fresh.clone(), binding.candidate_binding.1);
                }
                if entry_binding_renames_by_id.contains_key(&binding.source_binding) {
                    binding.target = PreparedImportTarget::Unsupported;
                }
            }
        }
    }
    if !entry_imports.is_empty() {
        let import_insert_index = entry_body
            .iter()
            .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
            .count();
        let request_insert_index = entry_requests_by_item
            .iter()
            .take_while(|request| request.item_index < import_insert_index)
            .count();
        let inserted_requests: Vec<IndexedStaticModuleRequest> = entry_imports
            .iter()
            .enumerate()
            .filter_map(|(item_offset, prepared)| {
                static_request_for_item(&prepared.item).map(|request| IndexedStaticModuleRequest {
                    item_index: import_insert_index + item_offset,
                    request,
                    targets: prepared.targets.clone(),
                    bindings: prepared.bindings.clone(),
                })
            })
            .collect();
        for request in entry_requests_by_item.iter_mut().skip(request_insert_index) {
            request.item_index += entry_imports.len();
        }
        let tail = entry_body.split_off(import_insert_index);
        entry_body.extend(entry_imports.into_iter().map(|prepared| prepared.item));
        entry_body.extend(tail);
        entry_requests_by_item.splice(
            request_insert_index..request_insert_index,
            inserted_requests,
        );
    }
    for export in entry_exports_for_moved_bindings(
        declarations,
        binding_assignment,
        &entry_binding_renames,
        chunk_top_level_mark,
    ) {
        entry_body.push(export);
    }
    // The sealed EntryPublicExports map is keyed by the residual
    // binding's original name; the emitted export clause is keyed by
    // entry's post-rename local, so remap through
    // `entry_binding_renames` at application.
    let auto_grow: BTreeMap<String, String> = sealed_entry_renames
        .scope_renames_by_name(&RenameScope::EntryPublicExports)
        .into_iter()
        .map(|(original, public_name)| {
            (
                entry_binding_renames
                    .get(&original)
                    .cloned()
                    .unwrap_or(original),
                public_name,
            )
        })
        .collect();
    if !auto_grow.is_empty() {
        entry_body.push(export_named_for_bindings(&auto_grow, chunk_top_level_mark));
    }
    // A readable name assigned to a chunk-exported binding is additive:
    // retain the source chunk's original public export (needed by chunks
    // outside the pipeline, dynamic imports, and namespace consumers) and
    // expose the same live binding under its readable name as well.
    // Candidates are ordered by name then source binding so collisions are
    // deterministic; a collision simply leaves that alias unavailable.
    let mut readable_export_candidates = Vec::<(String, String)>::new();
    for plan in module_plans {
        for (binding, readable) in &plan.bindings {
            if plan.spec_named_bindings.contains(binding)
                && pre_existing_entry_exports.contains(&top_level_id(binding, chunk_top_level_mark))
            {
                readable_export_candidates.push((readable.clone(), binding.clone()));
            }
        }
    }
    readable_export_candidates.sort();
    let mut taken_public_names = pre_existing_public_export_names.clone();
    taken_public_names.extend(auto_grow.values().cloned());
    let mut readable_exports = BTreeMap::new();
    for (readable, binding) in readable_export_candidates {
        if !taken_public_names.insert(readable.clone()) {
            continue;
        }
        let local = entry_binding_renames
            .get(&binding)
            .cloned()
            .unwrap_or(binding);
        readable_exports.insert(local, readable);
    }
    let mut cross_chunk_import_aliases = Vec::<(String, String)>::new();
    if !readable_exports.is_empty() {
        // Record only aliases that were actually emitted, associated with
        // the pre-existing public names of the same local binding. A name
        // that merely happens to equal another binding's spec name is not
        // enough to authorize rewriting its imports.
        for item in &entry_body {
            let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) = item else {
                continue;
            };
            if export.src.is_some() {
                continue;
            }
            for specifier in &export.specifiers {
                let ExportSpecifier::Named(named) = specifier else {
                    continue;
                };
                let ModuleExportName::Ident(local) = &named.orig else {
                    continue;
                };
                let Some(readable) = readable_exports.get(local.sym.as_ref()) else {
                    continue;
                };
                let public = named
                    .exported
                    .as_ref()
                    .map(binding_targets::module_export_name)
                    .unwrap_or_else(|| local.sym.to_string());
                cross_chunk_import_aliases.push((public, readable.clone()));
            }
        }
        cross_chunk_import_aliases.sort();
        cross_chunk_import_aliases.dedup();
        entry_body.push(export_named_for_bindings(
            &readable_exports,
            chunk_top_level_mark,
        ));
    }
    let local_bindings = assignment_analysis
        .inputs()
        .local_bindings(chunk_id_interned)?;
    // Dead-import trimming removes only specifiers and retains the directive
    // as a side-effect import, so its request inventory is unchanged.
    trim_dead_named_specifiers(&mut entry_body, local_bindings.as_map());
    let entry_exports_by_original_local = collect_entry_exports_by_original_local(
        &entry_body,
        &entry_binding_renames,
        chunk_top_level_mark,
    );
    let imported_reexports_by_module = collect_imported_reexports_by_module(
        assignment_analysis,
        chunk_id_interned,
        module_plans.len(),
        runtime_import_facts,
    )?;
    let source_import_cache = Mutex::new(ArtifactSourceImportResolutionCache::new(
        artifact,
        artifact_indexes,
    ));

    let files = vec![JsFile {
        path: entry_file.to_string(),
        body: JsFileBody::Ast(ParsedJsModule {
            cm: runtime_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body: entry_body,
                shebang: None,
            },
            unresolved_mark: runtime_ast.unresolved_mark,
            top_level_mark: runtime_ast.top_level_mark,
        }),
        header_lines: header_lines.to_vec(),
        binding_comments: BTreeMap::new(),
        leading_item_comments: BTreeMap::new(),
        metadata: FileMetadata {
            chunk_id: chunk_id.to_string(),
            chunk_file: entry_file.to_string(),
            role: FileRole::Entry,
            source_path: source_path.to_string(),
        },
    }];
    let file_records = vec![(entry_file.to_string(), FileRole::Entry)];
    let entry_static_requests = entry_requests_by_item
        .into_iter()
        .map(|indexed_request| PreparedStaticRequest {
            request: indexed_request.request,
            targets: indexed_request.targets,
            bindings: indexed_request.bindings,
        })
        .collect();

    // Filter chunk_renames down to entries the per-module emit path
    // should apply: bindings *not* claimed by any logical module.
    // Claimed bindings get their rename from the module plan
    // (handled via `disambiguate_import_locals` for cross-module
    // imports of the binding); the chunk_renames entry is dropped
    // for those. Mirrors the residual-side rule on body_renames
    // seeding above. The map is empty for chunks with no
    // chunk_renames; the per-module renamer is then a no-op.
    let cross_module_chunk_renames: BTreeMap<String, String> = chunk_renames
        .iter()
        .filter(|(binding, _)| !is_module_owned(binding))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();

    // Per-plan preparation is independent — the only shared mutable state
    // the loop body touches is `source_import_cache` (memoization for
    // source-chunk import resolution), wrapped in a `Mutex` above. Each
    // worker consumes one naturalized body with its matching rename maps
    // and facts and constructs the exact import/body AST used by emission.
    //
    // `swc_common::GLOBALS` is a `scoped_tls` thread-local; it does
    // NOT carry into rayon worker threads, so we capture the parent
    // thread's `Globals` and re-set it inside each worker closure.
    // Mirrors the chunk-level `par_iter` in `lowering/mod.rs`.
    let modules: Vec<PreparedModuleEmission<'output>> = GLOBALS.with(|globals| -> Result<_> {
        naturalized_modules
            .into_par_iter()
            .zip(module_plans.par_iter())
            .enumerate()
            .map(|(index, (module, plan))| {
                GLOBALS.set(globals, || {
                    prepare_module_emission(ModuleEmissionInputs {
                        index,
                        plan,
                        naturalized: module,
                        assignment_analysis,
                        declaration_by_name,
                        binding_assignment,
                        runtime_import_facts,
                        entry_exports_by_original_local: &entry_exports_by_original_local,
                        imported_reexports: &imported_reexports_by_module[index],
                        selected_exports: selected_exports_by_module[index].as_ref(),
                        cross_module_chunk_renames: &cross_module_chunk_renames,
                        source_import_cache: &source_import_cache,
                        chunk_id,
                        chunk_id_interned,
                        entry_file,
                        source_path,
                        chunk_top_level_mark,
                        runtime_ast,
                        vendor_import_oracle,
                    })
                })
            })
            .collect()
    })?;

    Ok(PreparedCandidateEmissionPlan {
        files,
        file_records,
        modules,
        cross_chunk_import_aliases,
        entry_static_requests,
        entry_binding_renames: entry_binding_renames_by_id
            .into_iter()
            .map(|(source, fresh)| {
                let candidate = (fresh, source.1);
                (source, candidate)
            })
            .collect(),
        entry_exports_by_original_local: entry_exports_by_original_local
            .into_iter()
            .map(|(source, export)| (source, export.exported_name))
            .collect(),
    })
}

fn compute_selected_statements(
    declarations: &[TopLevelDecl],
    binding_assignment: &HashMap<Id, usize>,
    whole_statement_assignment: &BTreeMap<StatementId, usize>,
) -> BTreeSet<StatementId> {
    let mut selected_statements = BTreeSet::new();
    for decl in declarations {
        if decl
            .bindings
            .iter()
            .any(|(_, id)| binding_assignment.contains_key(id))
        {
            selected_statements.insert(decl.statement);
        }
    }
    for ordinal in whole_statement_assignment.keys() {
        selected_statements.insert(*ordinal);
    }
    selected_statements
}

fn plan_selected_exports(
    module_plans: &[ModulePlan],
    is_module_owned: &impl Fn(&str) -> bool,
    selected_exports_by_module: &mut [Option<BTreeMap<String, String>>],
) {
    for (module_index, plan) in module_plans.iter().enumerate() {
        if plan.bindings.is_empty() {
            continue;
        }
        let exports: BTreeMap<String, String> = plan
            .bindings
            .iter()
            .filter(|(name, _)| is_module_owned(name))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if !exports.is_empty() {
            selected_exports_by_module[module_index] = Some(exports);
        }
    }
}

fn split_entry_body(
    body: &[ModuleItem],
    selected_statements: &BTreeSet<StatementId>,
    whole_statement_assignment: &BTreeMap<StatementId, usize>,
    binding_assignment: &HashMap<Id, usize>,
    entry_body: &mut Vec<ModuleItem>,
    entry_requests_by_item: &mut Vec<IndexedStaticModuleRequest>,
    selected_by_module: &mut [Vec<ModuleItem>],
) {
    for (index, item) in body.iter().enumerate() {
        let ordinal = StatementId(index);
        if !selected_statements.contains(&ordinal) {
            push_entry_item(entry_body, entry_requests_by_item, item.clone());
            continue;
        }
        if let Some(module_index) = whole_statement_assignment.get(&ordinal).copied() {
            selected_by_module[module_index].push(item.clone());
            // Local named exports define the source chunk's public surface.
            // An anonymous claim may move their source directive alongside
            // the binding, but entry still needs a facade for outside chunks
            // and dynamic importers. Its planned import of the moved binding
            // supplies the local name used by this unchanged export clause.
            if matches!(
                item,
                ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none()
            ) {
                push_entry_item(entry_body, entry_requests_by_item, item.clone());
            }
            continue;
        }
        if let Some(entry_item) =
            remaining_item_after_selection(item, binding_assignment, selected_by_module)
        {
            push_entry_item(entry_body, entry_requests_by_item, entry_item);
        }
    }
}

fn push_entry_item(
    body: &mut Vec<ModuleItem>,
    static_requests: &mut Vec<IndexedStaticModuleRequest>,
    item: ModuleItem,
) {
    if let Some(request) = static_request_for_item(&item) {
        static_requests.push(IndexedStaticModuleRequest {
            item_index: body.len(),
            request,
            targets: source_request_targets(&item),
            bindings: source_import_bindings(&item),
        });
    }
    body.push(item);
}
