//! Exact request identities and correspondence updates for whole-output pruning.

use std::collections::{BTreeMap, BTreeSet};

use binding_targets::module_export_name;
use swc_common::Spanned;
use swc_ecma_ast::{
    ExportSpecifier, ImportPhase, ImportSpecifier, ModuleDecl, ModuleExportName, ModuleItem,
};

use crate::reports::{
    CandidateImportTarget, CandidateOutputRequestKind, CandidatePreparedRequestBinding,
    CandidateRewriteBindingIdentity, CandidateRewriteDisposition, CandidateRewriteElision,
    CandidateRewriteIssue, CandidateRewriteReceipt, CandidateRewriteStage,
    CandidateSourceCorrespondence, CandidateStaticRequestIdentity, ImportSpecifierKind,
    OutputFileAddress, SourceCorrespondenceStatus, StaticModuleRequestMode,
    prepared_request_identity,
};

/// Coordinates and proven elisions recorded by the actual pruning mutation.
#[derive(Default)]
pub struct PruningChanges {
    pub item_positions: Vec<Option<usize>>,
    pub retained_specifiers: BTreeMap<usize, Vec<usize>>,
    pub elisions: BTreeMap<usize, Vec<CandidateRewriteElision>>,
    pub earlier_loads: BTreeMap<usize, usize>,
}

impl PruningChanges {
    pub fn preserved(item_count: usize) -> Self {
        Self {
            item_positions: (0..item_count).map(Some).collect(),
            ..Self::default()
        }
    }
}

/// Observe an exact candidate item, then attach only its matching prepared intent.
/// An unavailable intent remains unavailable even when a name looks familiar.
pub fn request_identity(
    correspondence: Option<&CandidateSourceCorrespondence>,
    output: &OutputFileAddress,
    item_index: usize,
    item: &ModuleItem,
) -> Option<(
    CandidateStaticRequestIdentity,
    Option<CandidateRewriteIssue>,
)> {
    let unsupported_syntax = match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => {
            import.type_only || !matches!(&import.phase, ImportPhase::Evaluation) || import.with.is_some() || import.specifiers.iter().any(|specifier| {
                matches!(specifier, ImportSpecifier::Named(named) if named.is_type_only)
            })
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) => {
            export.type_only || export.with.is_some() || export.specifiers.iter().any(|specifier| {
                matches!(specifier, ExportSpecifier::Named(named) if named.is_type_only)
            })
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(export)) => export.type_only || export.with.is_some(),
        _ => false,
    };
    let (kind, request, mode, bindings) = match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => (
            CandidateOutputRequestKind::Import,
            import.src.value.as_str().map(str::to_owned),
            Some(
                if import.type_only
                    || !matches!(&import.phase, ImportPhase::Evaluation)
                    || import.with.is_some()
                {
                    StaticModuleRequestMode::Unsupported
                } else {
                    StaticModuleRequestMode::OrdinaryEsm
                },
            ),
            import
                .specifiers
                .iter()
                .enumerate()
                .map(|(index, specifier)| {
                    let (local, imported, kind) = match specifier {
                        ImportSpecifier::Named(named) => (
                            &named.local,
                            named
                                .imported
                                .as_ref()
                                .map_or_else(|| named.local.sym.to_string(), module_export_name),
                            ImportSpecifierKind::Named,
                        ),
                        ImportSpecifier::Default(default) => (
                            &default.local,
                            "default".to_owned(),
                            ImportSpecifierKind::Default,
                        ),
                        ImportSpecifier::Namespace(namespace) => (
                            &namespace.local,
                            "*".to_owned(),
                            ImportSpecifierKind::Namespace,
                        ),
                    };
                    CandidateRewriteBindingIdentity {
                        specifier_index: index,
                        specifier_span: specifier.span(),
                        local: Some(local.to_id()),
                        imported_name: Some(imported),
                        exported_name: None,
                        kind: Some(kind),
                        source_binding: None,
                        source_cell: None,
                        target: None,
                    }
                })
                .collect(),
        ),
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) => {
            let from = export.src.as_ref();
            (
                if from.is_some() {
                    CandidateOutputRequestKind::NamedReExport
                } else {
                    CandidateOutputRequestKind::LocalExport
                },
                from.and_then(|src| src.value.as_str().map(str::to_owned)),
                from.map(|_| {
                    if export.type_only || export.with.is_some() {
                        StaticModuleRequestMode::Unsupported
                    } else {
                        StaticModuleRequestMode::OrdinaryEsm
                    }
                }),
                export
                    .specifiers
                    .iter()
                    .enumerate()
                    .map(|(index, specifier)| {
                        let (local, imported, exported, kind) = match specifier {
                            ExportSpecifier::Named(named) => (
                                match &named.orig {
                                    ModuleExportName::Ident(local) if from.is_none() => {
                                        Some(local.to_id())
                                    }
                                    ModuleExportName::Ident(_) => None,
                                    ModuleExportName::Str(_) => None,
                                },
                                from.map(|_| module_export_name(&named.orig)),
                                Some(module_export_name(
                                    named.exported.as_ref().unwrap_or(&named.orig),
                                )),
                                ImportSpecifierKind::Named,
                            ),
                            ExportSpecifier::Namespace(namespace) => (
                                None,
                                None,
                                Some(module_export_name(&namespace.name)),
                                ImportSpecifierKind::Namespace,
                            ),
                            ExportSpecifier::Default(default) => (
                                from.is_none().then(|| default.exported.to_id()),
                                Some("default".to_owned()),
                                Some(default.exported.sym.to_string()),
                                ImportSpecifierKind::Default,
                            ),
                        };
                        CandidateRewriteBindingIdentity {
                            specifier_index: index,
                            specifier_span: specifier.span(),
                            local,
                            imported_name: imported,
                            exported_name: exported,
                            kind: Some(kind),
                            source_binding: None,
                            source_cell: None,
                            target: None,
                        }
                    })
                    .collect(),
            )
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(export)) => (
            CandidateOutputRequestKind::ExportAll,
            export.src.value.as_str().map(str::to_owned),
            Some(if export.type_only || export.with.is_some() {
                StaticModuleRequestMode::Unsupported
            } else {
                StaticModuleRequestMode::OrdinaryEsm
            }),
            Vec::new(),
        ),
        _ => return None,
    };
    let mut observed = CandidateStaticRequestIdentity {
        item_index,
        item_span: item.span(),
        kind,
        source_statement: None,
        request,
        mode,
        targets: Vec::new(),
        bindings,
    };
    let Some(correspondence) = correspondence else {
        return Some((
            observed,
            Some(CandidateRewriteIssue::MissingExactInputIdentity),
        ));
    };
    if kind == CandidateOutputRequestKind::LocalExport {
        let mut issue = unsupported_syntax.then_some(CandidateRewriteIssue::UnprovedTarget);
        for binding in &mut observed.bindings {
            let mut matches = Vec::new();
            for declaration in &correspondence.module_cell_declarations {
                if declaration.status != SourceCorrespondenceStatus::Complete {
                    continue;
                }
                for route in &declaration.routes {
                    if route.output == *output
                        && route.candidate_local == binding.local
                        && binding.local.is_some()
                        && route.declaration_span.is_some()
                        && route.exported_name == binding.exported_name
                        && route
                            .exported_specifier_span
                            .is_none_or(|span| span == binding.specifier_span)
                    {
                        let mut identity = binding.clone();
                        identity.source_binding = declaration.source_binding.clone();
                        identity.source_cell = Some(declaration.source_cell);
                        identity.target = Some(CandidateImportTarget::CandidateOutput {
                            chunk: output.chunk,
                            file: output.file.clone(),
                        });
                        if !matches.contains(&identity) {
                            matches.push(identity);
                        }
                    }
                }
            }
            for export in &correspondence.exports {
                if export.status != SourceCorrespondenceStatus::Complete {
                    continue;
                }
                for route in &export.routes {
                    if route.output == *output
                        && route.candidate_local == binding.local
                        && binding.local.is_some()
                        && route.declaration_span.is_some()
                        && route.exported_name == binding.exported_name
                        && route
                            .exported_specifier_span
                            .is_none_or(|span| span == binding.specifier_span)
                    {
                        let mut identity = binding.clone();
                        identity.source_binding = export.source.local.clone();
                        identity.source_cell = export.source.local.as_ref().and_then(|local| {
                            correspondence
                                .source_module_cells
                                .as_ref()?
                                .get(local)
                                .copied()
                        });
                        let imported = correspondence.imports.iter().any(|import| {
                            Some(&import.source.local) == export.source.local.as_ref()
                        });
                        let targets = correspondence
                            .prepared_requests
                            .iter()
                            .filter(|request| {
                                request.output == *output
                                    && request.request_kind == CandidateOutputRequestKind::Import
                            })
                            .flat_map(|request| &request.bindings)
                            .filter(|planned| {
                                planned.candidate_local == binding.local
                                    && planned.source_binding.as_ref()
                                        == export.source.local.as_ref()
                            })
                            .collect::<Vec<_>>();
                        if imported {
                            match targets.as_slice() {
                                [planned] => {
                                    identity.target = Some(planned.target.clone());
                                    identity.source_cell = planned.source_cell;
                                    if matches!(
                                        planned.target,
                                        CandidateImportTarget::Unsupported
                                            | CandidateImportTarget::OriginalSourceUnknownExports { .. }
                                    ) {
                                        issue = Some(CandidateRewriteIssue::UnprovedTarget);
                                    }
                                }
                                [] => issue = Some(CandidateRewriteIssue::UnprovedTarget),
                                _ => issue = Some(CandidateRewriteIssue::AmbiguousTransition),
                            }
                        } else {
                            identity.target = Some(CandidateImportTarget::CandidateOutput {
                                chunk: output.chunk,
                                file: output.file.clone(),
                            });
                        }
                        if !matches.contains(&identity) {
                            matches.push(identity);
                        }
                    }
                }
            }
            match matches.as_slice() {
                [identity] => {
                    if identity.source_binding.is_some() {
                        *binding = identity.clone();
                    } else {
                        issue = Some(CandidateRewriteIssue::MissingExactInputIdentity);
                    }
                }
                [] => issue = Some(CandidateRewriteIssue::MissingExactInputIdentity),
                _ => issue = Some(CandidateRewriteIssue::AmbiguousTransition),
            }
        }
        return Some((observed, issue));
    }
    let matches = correspondence
        .prepared_requests
        .iter()
        .filter(|route| {
            route.output == *output
                && route.item_index == item_index
                && route.item_span == item.span()
                && route.request_kind == kind
                && Some(route.emitted_request.as_str()) == observed.request.as_deref()
                && Some(route.mode) == observed.mode
        })
        .collect::<Vec<_>>();
    let [route] = matches.as_slice() else {
        let issue = if matches.is_empty() {
            CandidateRewriteIssue::MissingExactInputIdentity
        } else {
            CandidateRewriteIssue::AmbiguousTransition
        };
        return Some((observed, Some(issue)));
    };
    let prepared = prepared_request_identity(route);
    observed.source_statement = prepared.source_statement;
    observed.targets = prepared.targets.clone();
    let mut issue = (unsupported_syntax
        || route.status != SourceCorrespondenceStatus::Complete
        || prepared.targets.is_empty())
    .then_some(CandidateRewriteIssue::UnprovedTarget);
    for binding in &mut observed.bindings {
        let matches = prepared
            .bindings
            .iter()
            .filter(|planned| {
                planned.specifier_index == binding.specifier_index
                    && planned.specifier_span == binding.specifier_span
                    && planned.local == binding.local
                    && planned.imported_name == binding.imported_name
                    && planned.kind == binding.kind
                    && (kind == CandidateOutputRequestKind::Import
                        || planned.exported_name == binding.exported_name)
            })
            .collect::<Vec<_>>();
        match matches.as_slice() {
            [planned] => {
                if kind == CandidateOutputRequestKind::Import
                    && (planned.source_binding.is_none() || planned.local.is_none())
                {
                    issue = Some(CandidateRewriteIssue::MissingExactInputIdentity);
                }
                if planned.target.as_ref().is_none_or(|target| {
                    matches!(
                        target,
                        CandidateImportTarget::Unsupported
                            | CandidateImportTarget::OriginalSourceUnknownExports { .. }
                    )
                }) {
                    issue = Some(CandidateRewriteIssue::UnprovedTarget);
                }
                *binding = (*planned).clone();
            }
            [] => issue = Some(CandidateRewriteIssue::MissingExactInputIdentity),
            _ => issue = Some(CandidateRewriteIssue::AmbiguousTransition),
        }
    }
    if prepared.bindings.len() != observed.bindings.len() {
        issue = Some(CandidateRewriteIssue::MissingExactInputIdentity);
    }
    Some((observed, issue))
}

/// Finish an item transition using indices retained by the actual mutation.
pub fn pruning_receipt(
    output: &OutputFileAddress,
    stage: CandidateRewriteStage,
    before: CandidateStaticRequestIdentity,
    mut issue: Option<CandidateRewriteIssue>,
    after_index: Option<usize>,
    retained_specifiers: &[usize],
    elisions: Vec<CandidateRewriteElision>,
) -> CandidateRewriteReceipt {
    let after = after_index.map(|item_index| {
        let mut after = before.clone();
        after.item_index = item_index;
        after.bindings.clear();
        let mut seen = BTreeSet::new();
        for (new_index, old_index) in retained_specifiers.iter().enumerate() {
            let Some(binding) = before
                .bindings
                .iter()
                .find(|binding| binding.specifier_index == *old_index)
            else {
                issue = Some(CandidateRewriteIssue::MissingExactOutputIdentity);
                continue;
            };
            if !seen.insert(*old_index) {
                issue = Some(CandidateRewriteIssue::AmbiguousTransition);
            }
            let mut binding = binding.clone();
            binding.specifier_index = new_index;
            after.bindings.push(binding);
        }
        after
    });
    let disposition = if issue.is_some() {
        CandidateRewriteDisposition::Unsupported
    } else if after.is_none() {
        CandidateRewriteDisposition::Elided
    } else if after.as_ref() == Some(&before) {
        CandidateRewriteDisposition::Preserved
    } else {
        CandidateRewriteDisposition::Rewritten
    };
    CandidateRewriteReceipt {
        source_chunk: output.chunk,
        output: output.clone(),
        stage,
        before: Some(before),
        after,
        disposition,
        elisions,
        issue,
    }
}

fn retained_binding<'a>(
    after: &'a CandidateStaticRequestIdentity,
    binding: &CandidatePreparedRequestBinding,
) -> Option<&'a CandidateRewriteBindingIdentity> {
    after.bindings.iter().find(|retained| {
        retained.specifier_span == binding.specifier_span
            && retained.local == binding.candidate_local
            && retained.imported_name == binding.imported_name
            && retained.exported_name == binding.exported_name
            && retained.kind == Some(binding.kind)
            && retained.source_binding == binding.source_binding
            && retained.source_cell == binding.source_cell
            && retained.target.as_ref() == Some(&binding.target)
    })
}

impl CandidateSourceCorrespondence {
    /// Apply one pruning stage's simultaneous old-to-new coordinate map.
    /// All receipts refer to stage-input indices, never already-shifted indices.
    pub fn apply_pruning_receipts(&mut self, receipts: &mut [CandidateRewriteReceipt]) {
        for receipt in receipts
            .iter_mut()
            .filter(|receipt| receipt.source_chunk == self.source_chunk)
        {
            let Some(before) = receipt.before.as_ref() else {
                receipt.issue = Some(CandidateRewriteIssue::MissingExactInputIdentity);
                receipt.disposition = CandidateRewriteDisposition::Unsupported;
                continue;
            };
            if before.kind == CandidateOutputRequestKind::LocalExport {
                continue;
            }
            let matches = self
                .prepared_requests
                .iter()
                .filter(|route| {
                    route.output == receipt.output && prepared_request_identity(route) == *before
                })
                .count();
            if matches != 1 {
                receipt.issue = Some(if matches == 0 {
                    CandidateRewriteIssue::MissingExactInputIdentity
                } else {
                    CandidateRewriteIssue::AmbiguousTransition
                });
                receipt.disposition = CandidateRewriteDisposition::Unsupported;
            }
            let source_route_mismatch = self
                .source_requests
                .iter()
                .flat_map(|request| &request.routes)
                .chain(self.reexports.iter().flat_map(|reexport| &reexport.routes))
                .filter(|route| {
                    route.output == receipt.output
                        && route.item_index == before.item_index
                        && route.item_span == before.item_span
                })
                .any(|route| {
                    Some(route.emitted_request.as_str()) != before.request.as_deref()
                        || Some(route.mode) != before.mode
                        || route
                            .targets
                            .iter()
                            .any(|target| !before.targets.contains(target))
                        || route
                            .bindings
                            .iter()
                            .any(|binding| retained_binding(before, binding).is_none())
                });
            if source_route_mismatch {
                receipt.issue = Some(CandidateRewriteIssue::MissingExactInputIdentity);
                receipt.disposition = CandidateRewriteDisposition::Unsupported;
            }
        }
        let receipts = receipts
            .iter()
            .filter(|receipt| receipt.source_chunk == self.source_chunk)
            .cloned()
            .collect::<Vec<_>>();
        let mut removed_items = BTreeMap::<OutputFileAddress, BTreeSet<usize>>::new();
        for receipt in &receipts {
            let Some(before) = &receipt.before else {
                continue;
            };
            if receipt.after.is_none() {
                removed_items
                    .entry(receipt.output.clone())
                    .or_default()
                    .insert(before.item_index);
            }
            if receipt.disposition == CandidateRewriteDisposition::Unsupported
                || receipt.issue.is_some()
            {
                for request in &mut self.source_requests {
                    if request.routes.iter().any(|route| {
                        route.output == receipt.output
                            && route.item_index == before.item_index
                            && route.item_span == before.item_span
                    }) {
                        request.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                    }
                }
                for import in &mut self.imports {
                    if import.routes.iter().any(|route| {
                        route.output == receipt.output && route.item_span == before.item_span
                    }) {
                        import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                    }
                }
                for export in &mut self.exports {
                    if export.routes.iter().any(|route| {
                        route.output == receipt.output
                            && before.bindings.iter().any(|binding| {
                                binding.local == route.candidate_local
                                    && binding.exported_name == route.exported_name
                            })
                    }) {
                        export.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                    }
                }
                for reexport in &mut self.reexports {
                    if reexport.routes.iter().any(|route| {
                        route.output == receipt.output
                            && route.item_index == before.item_index
                            && route.item_span == before.item_span
                    }) {
                        reexport.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                    }
                }
                continue;
            }
            for import in &mut self.imports {
                let source_local = &import.source.local;
                import.routes.retain_mut(|route| {
                    if route.output != receipt.output
                        || route.item_span != before.item_span
                        || Some(route.emitted_request.as_str()) != before.request.as_deref()
                    {
                        return true;
                    }
                    let Some(binding) = before.bindings.iter().find(|binding| {
                        binding.specifier_index == route.specifier_index
                            && binding.specifier_span == route.specifier_span
                            && binding.local.as_ref() == Some(&route.candidate_local)
                            && binding.source_binding.as_ref() == Some(source_local)
                            && binding.imported_name.as_deref()
                                == Some(route.imported_name.as_str())
                            && binding.kind == Some(route.kind)
                            && binding.source_cell == route.source_cell
                            && binding.target.as_ref() == Some(&route.target)
                    }) else {
                        return true;
                    };
                    let Some(retained) = receipt.after.as_ref().and_then(|after| {
                        after.bindings.iter().find(|retained| {
                            retained.source_binding == binding.source_binding
                                && retained.local == binding.local
                                && retained.specifier_span == binding.specifier_span
                        })
                    }) else {
                        return false;
                    };
                    route.specifier_index = retained.specifier_index;
                    true
                });
            }
        }
        // Visit every route once against stage-input coordinates. In particular,
        // synthesized requests can share a span, so sequential receipt updates
        // must not make a shifted route match another receipt's old index.
        for routes in self
            .source_requests
            .iter_mut()
            .map(|request| &mut request.routes)
            .chain(
                self.reexports
                    .iter_mut()
                    .map(|reexport| &mut reexport.routes),
            )
        {
            routes.retain_mut(|route| {
                let matching = receipts
                    .iter()
                    .filter(|receipt| {
                        receipt.output == route.output
                            && receipt.before.as_ref().is_some_and(|before| {
                                before.kind != CandidateOutputRequestKind::LocalExport
                                    && before.item_index == route.item_index
                                    && before.item_span == route.item_span
                            })
                    })
                    .collect::<Vec<_>>();
                let [receipt] = matching.as_slice() else {
                    return true;
                };
                if receipt.issue.is_some()
                    || receipt.disposition == CandidateRewriteDisposition::Unsupported
                {
                    return true;
                }
                let Some(after) = &receipt.after else {
                    return false;
                };
                route.item_index = after.item_index;
                route.bindings.retain_mut(|binding| {
                    let Some(retained) = retained_binding(after, binding) else {
                        return false;
                    };
                    binding.specifier_index = retained.specifier_index;
                    true
                });
                true
            });
        }
        self.prepared_requests.retain_mut(|route| {
            let matches = receipts
                .iter()
                .filter(|receipt| {
                    receipt.output == route.output
                        && receipt.before.as_ref().is_some_and(|before| {
                            before.item_index == route.item_index
                                && before.item_span == route.item_span
                        })
                })
                .collect::<Vec<_>>();
            let [receipt] = matches.as_slice() else {
                return true;
            };
            if receipt.issue.is_some()
                || receipt.disposition == CandidateRewriteDisposition::Unsupported
                || receipt.before.as_ref() != Some(&prepared_request_identity(route))
            {
                route.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                return true;
            }
            let Some(after) = &receipt.after else {
                return false;
            };
            route.item_index = after.item_index;
            route.bindings.retain_mut(|binding| {
                let Some(retained) = retained_binding(after, binding) else {
                    return false;
                };
                binding.specifier_index = retained.specifier_index;
                true
            });
            true
        });
        for receipt in &receipts {
            if receipt.disposition == CandidateRewriteDisposition::Unsupported
                || receipt.issue.is_some()
            {
                continue;
            }
            let Some(before) = receipt
                .before
                .as_ref()
                .filter(|before| before.kind == CandidateOutputRequestKind::LocalExport)
            else {
                continue;
            };
            for binding in &before.bindings {
                if receipt.after.as_ref().is_some_and(|after| {
                    after.bindings.iter().any(|retained| {
                        retained.source_binding == binding.source_binding
                            && retained.local == binding.local
                            && retained.exported_name == binding.exported_name
                    })
                }) {
                    continue;
                }
                for export in &mut self.exports {
                    if export.source.local != binding.source_binding {
                        continue;
                    }
                    export.routes.retain(|route| {
                        route.output != receipt.output
                            || route.candidate_local != binding.local
                            || route.exported_name != binding.exported_name
                    });
                }
                for declaration in &mut self.module_cell_declarations {
                    if declaration.source_binding != binding.source_binding
                        || Some(declaration.source_cell) != binding.source_cell
                    {
                        continue;
                    }
                    for route in &mut declaration.routes {
                        if route.output == receipt.output
                            && route.candidate_local == binding.local
                            && route.exported_name == binding.exported_name
                        {
                            route.exported_name = None;
                            route.exported_specifier_span = None;
                        }
                    }
                }
            }
        }
        for (output, removed) in removed_items {
            for unit in self.source_units.values_mut() {
                if !unit
                    .positions
                    .iter()
                    .any(|position| position.output == output)
                {
                    continue;
                }
                unit.positions.retain_mut(|position| {
                    if position.output != output {
                        return true;
                    }
                    if removed.contains(&position.item_index) {
                        return false;
                    }
                    position.item_index -= removed.range(..position.item_index).count();
                    true
                });
                if !unit
                    .positions
                    .iter()
                    .any(|position| position.output == output)
                {
                    unit.outputs.retain(|candidate| candidate != &output);
                }
            }
            for import in &mut self.imports {
                for uses in &mut import.uses {
                    uses.routes.retain_mut(|location| {
                        if location.output != output {
                            return true;
                        }
                        if removed.contains(&location.item_index) {
                            return false;
                        }
                        location.item_index -= removed.range(..location.item_index).count();
                        true
                    });
                }
            }
        }
        self.record_rewrite_receipts(receipts);
    }
}

#[cfg(test)]
mod tests {
    use js_ast::parse_js_module;
    use source_identity::ChunkTable;

    use super::*;

    #[test]
    fn source_reexport_names_do_not_become_local_bindings() {
        js_ast::with_swc_globals(|| {
            let mut chunks = ChunkTable::default();
            let output = OutputFileAddress {
                chunk: chunks.intern("fixture".to_owned()),
                file: "entry.js".to_owned(),
            };
            let module = parse_js_module(
                "entry.js",
                "export { value as public } from './peer.js'; export * as peer from './peer.js';",
            )
            .unwrap()
            .module;
            let identities = module
                .body
                .iter()
                .enumerate()
                .map(|(index, item)| request_identity(None, &output, index, item).unwrap().0)
                .collect::<Vec<_>>();
            let named = &identities[0].bindings[0];
            assert_eq!(named.imported_name.as_deref(), Some("value"));
            assert_eq!(named.exported_name.as_deref(), Some("public"));
            let namespace = &identities[1].bindings[0];
            assert_eq!(namespace.imported_name, None);
            assert_eq!(namespace.exported_name.as_deref(), Some("peer"));
            for binding in [named, namespace] {
                assert_eq!(binding.local, None);
                assert_eq!(binding.source_binding, None);
            }
        });
    }

    #[test]
    fn output_names_cannot_supply_missing_source_intent() {
        js_ast::with_swc_globals(|| {
            let mut chunks = ChunkTable::default();
            let output = OutputFileAddress {
                chunk: chunks.intern("fixture".to_owned()),
                file: "entry.js".to_owned(),
            };
            let module = parse_js_module(
                "entry.js",
                "import { value as local } from './peer.js'; export { local as public };",
            )
            .unwrap()
            .module;
            for (index, item) in module.body.iter().enumerate() {
                let (identity, issue) = request_identity(None, &output, index, item).unwrap();
                assert_eq!(
                    issue,
                    Some(CandidateRewriteIssue::MissingExactInputIdentity)
                );
                assert!(identity.bindings.iter().all(|binding| {
                    binding.source_binding.is_none()
                        && binding.source_cell.is_none()
                        && binding.target.is_none()
                }));
                let retained = identity
                    .bindings
                    .iter()
                    .map(|binding| binding.specifier_index)
                    .collect::<Vec<_>>();
                let receipt = pruning_receipt(
                    &output,
                    CandidateRewriteStage::DeadImportPruning,
                    identity,
                    issue,
                    Some(index),
                    &retained,
                    Vec::new(),
                );
                assert_eq!(
                    receipt.disposition,
                    CandidateRewriteDisposition::Unsupported
                );
            }
        });
    }
}
