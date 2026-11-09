//! Materialized vendor receipts join canonical source imports to actual prepared output.

use std::collections::{BTreeMap, BTreeSet};

use artifact::pruning::request_identity;
use artifact::reports::{
    CandidateImportTarget, CandidatePreparedRequestBinding, CandidatePreparedRequestRoute,
    CandidateRewriteDisposition, CandidateRewriteIssue, CandidateRewriteReceipt,
    CandidateRewriteStage, CandidateSourceCorrespondence, CandidateStaticRequestIdentity,
    CandidateVendorBindingReplacement, CandidateVendorDestination, CandidateVendorEmittedRequest,
    CandidateVendorPlanAction, CandidateVendorReplacement, CandidateVendorSourceRequestIdentity,
    CandidateVendorSourceUse, CandidateVendorUseResult, CandidateVendorUseRewrite,
    ImportSpecifierKind, ParsedSourceImportBinding, ParsedSourceImports,
    SourceCorrespondenceStatus, SourceStaticRequestKind, StaticModuleRequestMode,
    prepared_request_identity,
};
use source_identity::ModuleCellId;
use swc_common::Span;
use swc_ecma_ast::{Id, ModuleItem};

use crate::imports::MaterializedVendorIntent;
use crate::lower::PreparedCandidateEmissionPlan;
use crate::module_output::MaterializedVendorOutput;

struct PendingReplacement {
    replacement: CandidateVendorReplacement,
    issue: Option<CandidateRewriteIssue>,
}

struct SelectedBinding<'a> {
    intent: &'a MaterializedVendorIntent,
    source: &'a ParsedSourceImportBinding,
    action: CandidateVendorPlanAction,
    destination: CandidateVendorDestination,
    requests: Vec<usize>,
}

/// Record construction-time replacements before the later emission/pruning
/// stages consume the same prepared-request inventory. Original source route
/// status is not promoted: the final checker must discharge each replacement.
pub(super) fn record_materialized_vendor_replacements(
    correspondence: &mut CandidateSourceCorrespondence,
    parsed: &ParsedSourceImports,
    prepared: &PreparedCandidateEmissionPlan<'_>,
    source_cells: &BTreeMap<Id, ModuleCellId>,
) {
    let mut receipts = Vec::new();
    for (output, body) in prepared.materialized_vendor_outputs() {
        let mut pending = BTreeMap::<usize, PendingReplacement>::new();
        let mut selected = Vec::new();
        for intent in &output.intents {
            let Some((source_request, source)) = exact_source_import(parsed, intent) else {
                receipts.push(unsupported_receipt(
                    output,
                    CandidateRewriteIssue::MissingExactInputIdentity,
                ));
                continue;
            };
            let entry = pending
                .entry(intent.statement.0)
                .or_insert_with(|| PendingReplacement {
                    replacement: CandidateVendorReplacement {
                        source_request: CandidateVendorSourceRequestIdentity {
                            source_chunk: correspondence.source_chunk,
                            source_file: correspondence.source_file.clone(),
                            statement: source_request.statement,
                            declaration_span: source_request.declaration_span,
                            request: source_request.request.clone(),
                            mode: source_request.mode,
                            kind: source_request.kind,
                        },
                        bindings: Vec::new(),
                    },
                    issue: None,
                });
            let Some((action, destination)) = &intent.checked else {
                entry.issue = Some(CandidateRewriteIssue::UnprovedTarget);
                continue;
            };
            let roots = replacement_roots(
                &output.observations,
                &intent.candidate_before,
                &source.use_spans,
                action,
            );
            let requests = if intent.external {
                register_generated_requests(correspondence, output, body, destination, &roots)
            } else {
                register_boundary_request(correspondence, output, intent, destination)
            };
            let Some(requests) = requests.filter(|requests| !requests.is_empty()) else {
                entry.issue = Some(CandidateRewriteIssue::MissingExactOutputIdentity);
                continue;
            };
            selected.push(SelectedBinding {
                intent,
                source,
                action: action.clone(),
                destination: destination.clone(),
                requests,
            });
        }
        // Request identities are captured after every binding action in the
        // output has updated its exact prepared row, including mixed requests.
        for selected in selected {
            let entry = pending
                .get_mut(&selected.intent.statement.0)
                .expect("source request was selected");
            let source_uses = selected
                .source
                .use_sites
                .iter()
                .map(|site| CandidateVendorSourceUse {
                    statement: site.statement,
                    span: site.span,
                })
                .collect::<Vec<_>>();
            if selected.source.use_spans.len() != source_uses.len()
                || selected
                    .source
                    .use_spans
                    .iter()
                    .any(|span| !source_uses.iter().any(|site| site.span == *span))
            {
                entry.issue = Some(CandidateRewriteIssue::UnprovedUseRewrite);
            }
            let emitted_requests = selected
                .requests
                .iter()
                .map(|index| CandidateVendorEmittedRequest {
                    output: output.output.clone(),
                    identity: prepared_request_identity(&correspondence.prepared_requests[*index]),
                })
                .collect::<Vec<_>>();
            let uses = replacement_uses(prepared, output, &selected, &emitted_requests);
            for site in &selected.source.use_sites {
                let locally_present = output.observations.iter().any(|observation| {
                    observation.source_span == site.span
                        && observation.before_local == selected.intent.candidate_before
                }) || prepared
                    .candidate_identifier_routes(output.output.chunk, site.span)
                    .iter()
                    .any(|route| route.output == output.output);
                let mapped = uses
                    .iter()
                    .filter(|use_| {
                        use_.source_statement == site.statement && use_.source_span == site.span
                    })
                    .count();
                if locally_present && mapped == 0 {
                    entry.issue = Some(CandidateRewriteIssue::UnprovedUseRewrite);
                } else if mapped > 1 {
                    entry.issue = Some(CandidateRewriteIssue::AmbiguousTransition);
                }
            }
            if entry
                .replacement
                .bindings
                .iter()
                .any(|binding| binding.source_specifier_index == selected.intent.specifier_index)
            {
                entry.issue = Some(CandidateRewriteIssue::AmbiguousTransition);
                continue;
            }
            entry
                .replacement
                .bindings
                .push(CandidateVendorBindingReplacement {
                    source_specifier_index: selected.intent.specifier_index,
                    source_specifier_span: selected.intent.specifier_span,
                    source_binding: Some(selected.source.local.clone()),
                    source_cell: source_cells.get(&selected.source.local).copied(),
                    source_export: selected.source.imported_name.clone(),
                    source_exported_name: None,
                    source_kind: selected.source.kind,
                    source_uses,
                    action: selected.action,
                    destination: selected.destination,
                    emitted_requests,
                    uses,
                });
        }
        for mut pending in pending.into_values() {
            pending
                .replacement
                .bindings
                .sort_by_key(|binding| binding.source_specifier_index);
            receipts.push(CandidateRewriteReceipt {
                source_chunk: correspondence.source_chunk,
                output: output.output.clone(),
                stage: CandidateRewriteStage::EmissionVendorRewrite,
                before: None,
                after: None,
                disposition: CandidateRewriteDisposition::Replaced(pending.replacement),
                elisions: Vec::new(),
                issue: pending.issue,
            });
        }
    }
    correspondence.apply_emission_rewrite_receipts(&mut receipts);
}

fn exact_source_import<'a>(
    parsed: &'a ParsedSourceImports,
    intent: &MaterializedVendorIntent,
) -> Option<(
    &'a artifact::reports::ParsedSourceRequest,
    &'a ParsedSourceImportBinding,
)> {
    let requests = parsed
        .requests
        .iter()
        .filter(|request| {
            request.statement == intent.statement
                && request.declaration_span == intent.declaration_span
                && request.request == intent.source_request
                && request.kind == SourceStaticRequestKind::Import
                && request.mode == StaticModuleRequestMode::OrdinaryEsm
                && !request.type_only
                && request.specifier_spans.get(intent.specifier_index)
                    == Some(&intent.specifier_span)
        })
        .collect::<Vec<_>>();
    let [request] = requests.as_slice() else {
        return None;
    };
    let bindings = parsed
        .bindings
        .iter()
        .filter(|binding| {
            binding.statement == intent.statement
                && binding.declaration_span == intent.declaration_span
                && binding.specifier_index == intent.specifier_index
                && binding.local == intent.source_local
                && binding.request == intent.source_request
                && binding.imported_name == intent.source_export
                && binding.kind == ImportSpecifierKind::Named
                && !binding.type_only
        })
        .collect::<Vec<_>>();
    let [binding] = bindings.as_slice() else {
        return None;
    };
    Some((request, binding))
}

fn direct_action(action: &CandidateVendorPlanAction) -> bool {
    !matches!(
        action,
        CandidateVendorPlanAction::PartialPackageMember { .. }
            | CandidateVendorPlanAction::BundledFacadeMember { .. }
    )
}

fn replacement_roots(
    observations: &[vendor::VendorUseRewriteObservation],
    candidate_before: &Id,
    source_use_spans: &[Span],
    action: &CandidateVendorPlanAction,
) -> BTreeSet<Id> {
    let mut roots = observations
        .iter()
        .filter(|observation| {
            observation.before_local == *candidate_before
                && source_use_spans.contains(&observation.source_span)
        })
        .map(|observation| match &observation.result {
            vendor::VendorUseRewriteTarget::LocalIdentifier { local } => local.clone(),
            vendor::VendorUseRewriteTarget::MemberPath { namespace, .. } => namespace.clone(),
        })
        .collect::<BTreeSet<_>>();
    if roots.is_empty() && direct_action(action) {
        roots.insert(candidate_before.clone());
    }
    roots
}

fn target(destination: &CandidateVendorDestination) -> CandidateImportTarget {
    match destination {
        CandidateVendorDestination::BoundaryOutput { output, .. } => {
            CandidateImportTarget::CandidateOutput {
                chunk: output.chunk,
                file: output.file.clone(),
            }
        }
        _ => destination
            .import_target()
            .expect("external destination has a typed import target"),
    }
}

fn register_generated_requests(
    correspondence: &mut CandidateSourceCorrespondence,
    output: &MaterializedVendorOutput,
    body: &[ModuleItem],
    destination: &CandidateVendorDestination,
    roots: &BTreeSet<Id>,
) -> Option<Vec<usize>> {
    // One selected action for one source binding has one actual output root.
    // Multiple declarations of that root cannot establish an exact replacement.
    if roots.len() != 1 {
        return None;
    }
    let expected_target = target(destination);
    let expected_request = match destination {
        CandidateVendorDestination::Package { request, .. } => request,
        CandidateVendorDestination::Facade {
            emitted_request, ..
        } => emitted_request,
        CandidateVendorDestination::BoundaryOutput { .. } => return None,
    };
    let mut selected = Vec::new();
    for item_index in &output.generated_request_indices {
        let item = body.get(*item_index)?;
        let (mut identity, _) = request_identity(None, &output.output, *item_index, item)?;
        if identity.request.as_ref() != Some(expected_request)
            || identity.mode != Some(StaticModuleRequestMode::OrdinaryEsm)
            || !identity
                .bindings
                .iter()
                .any(|binding| binding.local.as_ref().is_some_and(|id| roots.contains(id)))
        {
            continue;
        }
        identity.targets = vec![expected_target.clone()];
        for binding in &mut identity.bindings {
            binding.target = Some(expected_target.clone());
        }
        let matches = correspondence
            .prepared_requests
            .iter()
            .enumerate()
            .filter(|(_, route)| {
                route.output == output.output
                    && route.item_index == *item_index
                    && route.item_span == identity.item_span
                    && route.emitted_request == *expected_request
            })
            .map(|(index, _)| index)
            .collect::<Vec<_>>();
        let [index] = matches.as_slice() else {
            return None;
        };
        let previous = &correspondence.prepared_requests[*index];
        if previous.targets.iter().any(|target| {
            !matches!(target, CandidateImportTarget::Unsupported) && *target != expected_target
        }) {
            return None;
        }
        correspondence.prepared_requests[*index] = prepared_route(output, &identity)?;
        selected.push(*index);
    }
    (selected.len() == 1).then_some(selected)
}

fn prepared_route(
    output: &MaterializedVendorOutput,
    identity: &CandidateStaticRequestIdentity,
) -> Option<CandidatePreparedRequestRoute> {
    let bindings = identity
        .bindings
        .iter()
        .map(|binding| {
            Some(CandidatePreparedRequestBinding {
                source_binding: binding.source_binding.clone(),
                candidate_local: Some(binding.local.clone()?),
                specifier_index: binding.specifier_index,
                specifier_span: binding.specifier_span,
                imported_name: binding.imported_name.clone(),
                kind: binding.kind?,
                exported_name: binding.exported_name.clone(),
                source_cell: None,
                target: binding.target.clone()?,
            })
        })
        .collect::<Option<Vec<_>>>()?;
    Some(CandidatePreparedRequestRoute {
        output: output.output.clone(),
        item_index: identity.item_index,
        item_span: identity.item_span,
        request_kind: identity.kind,
        source_statement: None,
        emitted_request: identity.request.clone()?,
        mode: identity.mode?,
        targets: identity.targets.clone(),
        bindings,
        status: SourceCorrespondenceStatus::Complete,
    })
}

fn register_boundary_request(
    correspondence: &mut CandidateSourceCorrespondence,
    output: &MaterializedVendorOutput,
    intent: &MaterializedVendorIntent,
    destination: &CandidateVendorDestination,
) -> Option<Vec<usize>> {
    let CandidateVendorDestination::BoundaryOutput { export, .. } = destination else {
        return None;
    };
    let matches = correspondence
        .prepared_requests
        .iter()
        .enumerate()
        .flat_map(|(request_index, route)| {
            route
                .bindings
                .iter()
                .enumerate()
                .filter_map(move |(binding_index, binding)| {
                    (route.output == output.output
                        && binding.source_binding.as_ref() == Some(&intent.source_local)
                        && binding.candidate_local.as_ref() == Some(&intent.candidate_before)
                        && binding.imported_name.as_ref() == Some(export))
                    .then_some((request_index, binding_index))
                })
        })
        .collect::<Vec<_>>();
    let [(request_index, binding_index)] = matches.as_slice() else {
        return None;
    };
    let route = &mut correspondence.prepared_requests[*request_index];
    let binding = &mut route.bindings[*binding_index];
    binding.target = target(destination);
    binding.source_cell = None;
    // Grouped residual specifiers keep their own original typed targets.
    route.targets = route
        .bindings
        .iter()
        .map(|binding| binding.target.clone())
        .collect();
    route.status = if route.bindings.iter().any(|binding| {
        matches!(
            binding.target,
            CandidateImportTarget::Unsupported
                | CandidateImportTarget::OriginalSourceUnknownExports { .. }
        )
    }) {
        SourceCorrespondenceStatus::UnsupportedRewrite
    } else {
        SourceCorrespondenceStatus::Complete
    };
    Some(vec![*request_index])
}

fn replacement_uses(
    prepared: &PreparedCandidateEmissionPlan<'_>,
    output: &MaterializedVendorOutput,
    selected: &SelectedBinding<'_>,
    requests: &[CandidateVendorEmittedRequest],
) -> Vec<CandidateVendorUseRewrite> {
    let roots = requests
        .iter()
        .flat_map(|request| &request.identity.bindings)
        .filter_map(|binding| binding.local.clone())
        .collect::<BTreeSet<_>>();
    let mut uses = Vec::new();
    for site in &selected.source.use_sites {
        let observations = output
            .observations
            .iter()
            .filter(|observation| {
                observation.source_span == site.span
                    && observation.before_local == selected.intent.candidate_before
            })
            .collect::<Vec<_>>();
        for observation in &observations {
            let result = match &observation.result {
                vendor::VendorUseRewriteTarget::LocalIdentifier { local }
                    if roots.contains(local) =>
                {
                    CandidateVendorUseResult::LocalIdentifier {
                        local: local.clone(),
                    }
                }
                vendor::VendorUseRewriteTarget::MemberPath { namespace, path }
                    if roots.contains(namespace) =>
                {
                    CandidateVendorUseResult::MemberPath {
                        namespace: namespace.clone(),
                        path: path.clone(),
                    }
                }
                _ => continue,
            };
            uses.push(CandidateVendorUseRewrite {
                source_statement: site.statement,
                source_span: site.span,
                before_output: output.output.clone(),
                candidate_before_span: observation.source_span,
                candidate_before_local: observation.before_local.clone(),
                after_output: output.output.clone(),
                candidate_after_span: observation.after_span,
                result,
            });
        }
        if observations.is_empty() && direct_action(&selected.action) {
            for route in prepared.candidate_identifier_routes(output.output.chunk, site.span) {
                if route.output != output.output
                    || route.candidate_local != selected.intent.candidate_before
                    || !roots.contains(&route.candidate_local)
                {
                    continue;
                }
                uses.push(CandidateVendorUseRewrite {
                    source_statement: site.statement,
                    source_span: site.span,
                    before_output: output.output.clone(),
                    candidate_before_span: route.candidate_span,
                    candidate_before_local: route.candidate_local.clone(),
                    after_output: output.output.clone(),
                    candidate_after_span: route.candidate_span,
                    result: CandidateVendorUseResult::LocalIdentifier {
                        local: route.candidate_local,
                    },
                });
            }
        }
    }
    uses
}

fn unsupported_receipt(
    output: &MaterializedVendorOutput,
    issue: CandidateRewriteIssue,
) -> CandidateRewriteReceipt {
    CandidateRewriteReceipt {
        source_chunk: output.output.chunk,
        output: output.output.clone(),
        stage: CandidateRewriteStage::EmissionVendorRewrite,
        before: None,
        after: None,
        disposition: CandidateRewriteDisposition::Unsupported,
        elisions: Vec::new(),
        issue: Some(issue),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use source_identity::ChunkId;
    use swc_ecma_ast::{ImportSpecifier, ModuleDecl};
    use swc_ecma_visit::VisitMutWith;

    #[test]
    fn member_root_requires_an_actual_observation_at_the_exact_source_use() {
        js_ast::with_swc_globals(|| {
            let mut parsed = js_ast::parse_js_module(
                "consumer.js",
                "import { member as original } from './provider.js'; const value = original(1);",
            )
            .expect("real input parses");
            let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = &parsed.module.body[0] else {
                panic!("source import");
            };
            let ImportSpecifier::Named(binding) = &import.specifiers[0] else {
                panic!("named source binding");
            };
            let original = binding.local.to_id();
            let declaration_span = binding.local.span;
            let rewrites = BTreeMap::from([(
                original.clone(),
                vendor::IdentRewriteTarget::Member {
                    namespace: "namespace".into(),
                    property_path: vec!["run".into()],
                    chunk_id: ChunkId(0),
                    chunk_export: "member".into(),
                },
            )]);
            let mut observations = Vec::new();
            let mut counts = BTreeMap::new();
            parsed
                .module
                .visit_mut_with(&mut vendor::PartialSwapIdentRewriter {
                    bindings: &rewrites,
                    references_by_symbol: &mut counts,
                    observations: Some(&mut observations),
                });
            let [observation] = observations.as_slice() else {
                panic!("one actual use rewrite")
            };
            let vendor::VendorUseRewriteTarget::MemberPath { namespace, .. } = &observation.result
            else {
                panic!("actual member result");
            };
            let action = CandidateVendorPlanAction::PartialPackageMember {
                provider_chunk: ChunkId(0),
                source_export: "member".into(),
                package: "package".into(),
                version: "1.0.0".into(),
                subpath: "index.js".into(),
                namespace: "namespace".into(),
                upstream_export: "run".into(),
            };
            assert_eq!(
                replacement_roots(
                    &observations,
                    &original,
                    &[observation.source_span],
                    &action
                ),
                BTreeSet::from([namespace.clone()])
            );
            assert!(
                replacement_roots(&[], &original, &[observation.source_span], &action).is_empty()
            );
            assert!(
                replacement_roots(&observations, &original, &[declaration_span], &action)
                    .is_empty()
            );
        });
    }
}
