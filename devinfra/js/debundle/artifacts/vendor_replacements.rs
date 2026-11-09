//! Typed vendor transition application. Payloads retain their authored premise;
//! final AST/provenance reconciliation remains the independent checker's job.

use crate::reports::{
    CandidateImportRoute, CandidateImportTarget, CandidateOutputRequestKind,
    CandidatePreparedRequestBinding, CandidatePreparedRequestRoute,
    CandidateRewriteBindingIdentity, CandidateRewriteDisposition, CandidateRewriteIssue,
    CandidateRewriteReceipt, CandidateRewriteStage, CandidateSourceCorrespondence,
    CandidateSourceRequestRoute, CandidateSourceUseLocation, CandidateStaticRequestIdentity,
    CandidateVendorBindingReplacement, CandidateVendorDestination, CandidateVendorEmittedRequest,
    CandidateVendorPlanAction, CandidateVendorReplacement, CandidateVendorSourceRequestIdentity,
    CandidateVendorSourceUse, CandidateVendorUseResult, ImportSpecifierKind, OutputFileAddress,
    SourceCorrespondenceStatus, SourceStaticRequestKind, StaticModuleRequestMode,
    prepared_request_identity,
};
use swc_ecma_ast::Id;

pub(crate) fn apply_replaced_requests(
    correspondence: &mut CandidateSourceCorrespondence,
    receipts: &mut [CandidateRewriteReceipt],
    input: &[CandidatePreparedRequestRoute],
) {
    // Stage-input coordinates must not be confused with an earlier row's
    // shifted stage-output coordinates.
    let mut transitions = Vec::new();
    for (receipt_index, receipt) in receipts.iter_mut().enumerate() {
        let CandidateRewriteDisposition::Replaced(payload) = &receipt.disposition else {
            continue;
        };
        let result = validate_payload(correspondence, receipt, payload)
            .and_then(|()| request_transition(input, receipt, payload));
        match result {
            Ok(mut transition) if receipt.issue.is_none() => {
                transition.receipt_index = receipt_index;
                transitions.push(transition);
            }
            Ok(_) => correspondence.status = SourceCorrespondenceStatus::UnsupportedRewrite,
            Err(issue) => {
                receipt.issue.get_or_insert(issue);
                correspondence.status = SourceCorrespondenceStatus::UnsupportedRewrite;
            }
        }
    }
    // No other stage receipt may also claim a consumed old request.
    for transition in &transitions {
        let Some(before) = &transition.before else {
            continue;
        };
        let identity = prepared_request_identity(before);
        let matching = receipts
            .iter()
            .enumerate()
            .filter(|(_, receipt)| {
                receipt.output == transition.output && receipt.before.as_ref() == Some(&identity)
            })
            .map(|(index, _)| index)
            .collect::<Vec<_>>();
        if matching.len() != 1 {
            for index in matching {
                receipts[index].issue = Some(CandidateRewriteIssue::AmbiguousTransition);
            }
        }
    }
    let mut proposed = correspondence.prepared_requests.clone();
    // Remove every valid stage-input row before installing any stage-output
    // row. This permits exact index shifts without order-sensitive collisions.
    for transition in &transitions {
        if receipts[transition.receipt_index].issue.is_none()
            && let Some(before) = &transition.before
        {
            proposed.retain(|route| !same_prepared(route, before));
        }
    }
    let mut valid = true;
    for transition in &transitions {
        if receipts[transition.receipt_index].issue.is_some() {
            continue;
        }
        for route in transition.after.iter().chain(&transition.generated) {
            let conflicts = proposed
                .iter()
                .filter(|existing| {
                    existing.output == route.output && existing.item_index == route.item_index
                })
                .collect::<Vec<_>>();
            match conflicts.as_slice() {
                [] => proposed.push(route.clone()),
                [existing] if same_prepared(existing, route) => {}
                _ => {
                    valid = false;
                    break;
                }
            }
        }
    }
    if valid {
        proposed.sort_by(|left, right| {
            left.output
                .cmp(&right.output)
                .then_with(|| left.item_index.cmp(&right.item_index))
        });
        correspondence.prepared_requests = proposed;
        for transition in &transitions {
            if receipts[transition.receipt_index].issue.is_none()
                && let Some(before) = &transition.before
            {
                replace_old_routes(correspondence, before, transition.after.as_ref());
            }
        }
    } else {
        for transition in &transitions {
            receipts[transition.receipt_index]
                .issue
                .get_or_insert(CandidateRewriteIssue::AmbiguousTransition);
        }
    }
    // A materialized receipt may arrive before its residual-entry receipt.
    // Keep its payload intact, but withhold the source edge until the complete
    // original use census is covered by the union of exact output transitions.
    let recorded = std::mem::take(&mut correspondence.rewrite_receipts);
    let all = recorded.iter().chain(receipts.iter()).collect::<Vec<_>>();
    project_replacement_edges(correspondence, &all);
    correspondence.rewrite_receipts = recorded;
}

struct RequestTransition {
    receipt_index: usize,
    output: OutputFileAddress,
    before: Option<CandidatePreparedRequestRoute>,
    after: Option<CandidatePreparedRequestRoute>,
    generated: Vec<CandidatePreparedRequestRoute>,
}

fn same_prepared(
    left: &CandidatePreparedRequestRoute,
    right: &CandidatePreparedRequestRoute,
) -> bool {
    left.output == right.output
        && prepared_request_identity(left) == prepared_request_identity(right)
}

fn request_transition(
    input: &[CandidatePreparedRequestRoute],
    receipt: &CandidateRewriteReceipt,
    payload: &CandidateVendorReplacement,
) -> Result<RequestTransition, CandidateRewriteIssue> {
    let before = if let Some(identity) = &receipt.before {
        let matching = input
            .iter()
            .filter(|route| {
                route.output == receipt.output && prepared_request_identity(route) == *identity
            })
            .collect::<Vec<_>>();
        let [route] = matching.as_slice() else {
            return Err(CandidateRewriteIssue::MissingExactInputIdentity);
        };
        Some((*route).clone())
    } else {
        None
    };
    if receipt.before.is_none() && receipt.after.is_some() {
        return Err(CandidateRewriteIssue::MissingExactInputIdentity);
    }
    let after = receipt
        .after
        .as_ref()
        .map(|identity| route_from_identity(&receipt.output, identity))
        .transpose()?;
    if let Some(before) = &receipt.before {
        if before.source_statement != Some(payload.source_request.statement) {
            return Err(CandidateRewriteIssue::MissingExactInputIdentity);
        }
        if let Some(after) = &receipt.after
            && (before.kind != after.kind
                || before.item_span != after.item_span
                || before.source_statement != after.source_statement
                || before.request != after.request
                || before.mode != after.mode)
        {
            return Err(CandidateRewriteIssue::MissingExactOutputIdentity);
        }
        let mut remaining = before.bindings.clone();
        for binding in &payload.bindings {
            let matching = remaining
                .iter()
                .enumerate()
                .filter(|(_, candidate)| {
                    candidate.source_binding == binding.source_binding
                        && candidate.imported_name.as_deref()
                            == Some(binding.source_export.as_str())
                        && candidate.kind == Some(binding.source_kind)
                        && (binding.source_binding.is_some()
                            || (candidate.specifier_index == binding.source_specifier_index
                                && candidate.specifier_span == binding.source_specifier_span
                                && candidate.exported_name == binding.source_exported_name))
                })
                .map(|(index, _)| index)
                .collect::<Vec<_>>();
            let [index] = matching.as_slice() else {
                return Err(CandidateRewriteIssue::MissingExactInputIdentity);
            };
            remaining.remove(*index);
        }
        let mut retained = receipt
            .after
            .as_ref()
            .map(|identity| identity.bindings.clone())
            .unwrap_or_default();
        // Boundary renames keep the transformed specifier in the same request;
        // it is represented by its checked emitted identity, not a residual.
        if let Some(after) = &receipt.after {
            for binding in &payload.bindings {
                if binding
                    .emitted_requests
                    .iter()
                    .any(|request| request.output == receipt.output && request.identity == *after)
                {
                    let request = CandidateVendorEmittedRequest {
                        output: receipt.output.clone(),
                        identity: after.clone(),
                    };
                    let transformed = selected_output_binding(binding, &request)?;
                    retained.retain(|candidate| candidate != transformed);
                }
            }
        }
        // Keep residual bindings byte/identity exact apart from their actual
        // new specifier indices. Bare residual requests remain explicit rows.
        if remaining.len() != retained.len()
            || remaining.iter().zip(&retained).any(|(before, after)| {
                let mut expected = before.clone();
                expected.specifier_index = after.specifier_index;
                expected != *after
            })
        {
            return Err(CandidateRewriteIssue::UnprovedUseRewrite);
        }
    }
    let mut generated = Vec::new();
    for binding in &payload.bindings {
        for request in &binding.emitted_requests {
            let route = route_from_identity(&request.output, &request.identity)?;
            if !generated
                .iter()
                .any(|previous| same_prepared(previous, &route))
            {
                generated.push(route);
            }
        }
    }
    Ok(RequestTransition {
        receipt_index: 0,
        output: receipt.output.clone(),
        before,
        after,
        generated,
    })
}

fn route_from_identity(
    output: &OutputFileAddress,
    identity: &CandidateStaticRequestIdentity,
) -> Result<CandidatePreparedRequestRoute, CandidateRewriteIssue> {
    let (Some(request), Some(mode)) = (&identity.request, identity.mode) else {
        return Err(CandidateRewriteIssue::MissingExactOutputIdentity);
    };
    if mode != StaticModuleRequestMode::OrdinaryEsm
        || identity.targets.is_empty()
        || identity.targets.iter().any(|target| {
            matches!(
                target,
                CandidateImportTarget::Unsupported
                    | CandidateImportTarget::OriginalSourceUnknownExports { .. }
            )
        })
    {
        return Err(CandidateRewriteIssue::UnprovedTarget);
    }
    let mut bindings = Vec::new();
    for (index, binding) in identity.bindings.iter().enumerate() {
        let (Some(kind), Some(target)) = (binding.kind, &binding.target) else {
            return Err(CandidateRewriteIssue::MissingExactOutputIdentity);
        };
        if binding.specifier_index != index
            || matches!(
                target,
                CandidateImportTarget::Unsupported
                    | CandidateImportTarget::OriginalSourceUnknownExports { .. }
            )
        {
            return Err(CandidateRewriteIssue::UnprovedTarget);
        }
        bindings.push(CandidatePreparedRequestBinding {
            source_binding: binding.source_binding.clone(),
            candidate_local: binding.local.clone(),
            specifier_index: binding.specifier_index,
            specifier_span: binding.specifier_span,
            imported_name: binding.imported_name.clone(),
            kind,
            exported_name: binding.exported_name.clone(),
            source_cell: binding.source_cell,
            target: target.clone(),
        });
    }
    Ok(CandidatePreparedRequestRoute {
        output: output.clone(),
        item_index: identity.item_index,
        item_span: identity.item_span,
        request_kind: identity.kind,
        source_statement: identity.source_statement,
        emitted_request: request.clone(),
        mode,
        targets: identity.targets.clone(),
        bindings,
        status: SourceCorrespondenceStatus::Complete,
    })
}

fn validate_payload(
    correspondence: &CandidateSourceCorrespondence,
    receipt: &CandidateRewriteReceipt,
    payload: &CandidateVendorReplacement,
) -> Result<(), CandidateRewriteIssue> {
    let source = &payload.source_request;
    if receipt.source_chunk != correspondence.source_chunk
        || source.source_chunk != correspondence.source_chunk
        || source.source_file != correspondence.source_file
        || receipt.output.chunk != correspondence.source_chunk
        || receipt.stage != CandidateRewriteStage::EmissionVendorRewrite
        || payload.bindings.is_empty()
        || source.mode != StaticModuleRequestMode::OrdinaryEsm
        || !receipt.elisions.is_empty()
    {
        return Err(CandidateRewriteIssue::MissingExactInputIdentity);
    }
    let source_requests = correspondence
        .source_requests
        .iter()
        .filter(|row| {
            row.source.statement == source.statement
                && row.source.declaration_span == source.declaration_span
                && row.source.request == source.request
                && row.source.kind == source.kind
                && row.source.mode == source.mode
                && !row.source.type_only
        })
        .collect::<Vec<_>>();
    let [canonical] = source_requests.as_slice() else {
        return Err(CandidateRewriteIssue::MissingExactInputIdentity);
    };
    for (index, binding) in payload.bindings.iter().enumerate() {
        if canonical
            .source
            .specifier_spans
            .get(binding.source_specifier_index)
            != Some(&binding.source_specifier_span)
            || payload.bindings[..index]
                .iter()
                .any(|previous| previous.source_specifier_index == binding.source_specifier_index)
        {
            return Err(CandidateRewriteIssue::AmbiguousTransition);
        }
        validate_source_binding(correspondence, source, binding)?;
        let provider = match &binding.action {
            CandidateVendorPlanAction::BoundaryRename { provider_chunk, .. }
            | CandidateVendorPlanAction::PartialPackageMember { provider_chunk, .. }
            | CandidateVendorPlanAction::PartialPackageNamespace { provider_chunk, .. }
            | CandidateVendorPlanAction::PartialPackageDefault { provider_chunk, .. }
            | CandidateVendorPlanAction::PartialPackageNamed { provider_chunk, .. }
            | CandidateVendorPlanAction::BundledFacadeMember { provider_chunk, .. }
            | CandidateVendorPlanAction::BundledFacadeDefault { provider_chunk, .. } => {
                *provider_chunk
            }
        };
        if binding
            .source_cell
            .is_some_and(|cell| cell.chunk != provider)
            || matches!(&binding.destination, CandidateVendorDestination::BoundaryOutput { output, .. } if output.chunk != provider)
        {
            return Err(CandidateRewriteIssue::UnprovedTarget);
        }
        validate_binding_output(receipt, binding)?;
    }
    Ok(())
}

fn validate_source_binding(
    correspondence: &CandidateSourceCorrespondence,
    request: &CandidateVendorSourceRequestIdentity,
    binding: &CandidateVendorBindingReplacement,
) -> Result<(), CandidateRewriteIssue> {
    if let Some(local) = &binding.source_binding {
        if request.kind != SourceStaticRequestKind::Import || binding.source_exported_name.is_some()
        {
            return Err(CandidateRewriteIssue::MissingExactInputIdentity);
        }
        let rows = correspondence
            .imports
            .iter()
            .filter(|row| {
                let source = &row.source;
                source.statement == request.statement
                    && source.declaration_span == request.declaration_span
                    && source.specifier_index == binding.source_specifier_index
                    && source.local == *local
                    && source.request == request.request
                    && source.imported_name == binding.source_export
                    && source.kind == binding.source_kind
                    && !source.type_only
            })
            .collect::<Vec<_>>();
        let [row] = rows.as_slice() else {
            return Err(CandidateRewriteIssue::MissingExactInputIdentity);
        };
        let uses = row
            .source
            .use_sites
            .iter()
            .map(|site| CandidateVendorSourceUse {
                statement: site.statement,
                span: site.span,
            })
            .collect::<Vec<_>>();
        if !same_use_census(&uses, &binding.source_uses)
            || row.source.use_spans.len() != uses.len()
            || row
                .source
                .use_spans
                .iter()
                .any(|span| !uses.iter().any(|site| site.span == *span))
        {
            return Err(CandidateRewriteIssue::UnprovedUseRewrite);
        }
        if correspondence
            .source_module_cells
            .as_ref()
            .and_then(|cells| cells.get(local))
            .copied()
            != binding.source_cell
        {
            return Err(CandidateRewriteIssue::MissingExactInputIdentity);
        }
    } else {
        if request.kind != SourceStaticRequestKind::NamedReExport
            || !binding.source_uses.is_empty()
            || !binding.uses.is_empty()
            || binding.source_cell.is_some()
        {
            return Err(CandidateRewriteIssue::MissingExactInputIdentity);
        }
        let rows = correspondence
            .reexports
            .iter()
            .filter(|row| {
                let source = &row.source;
                source.statement == request.statement
                    && source.declaration_span == request.declaration_span
                    && source.specifier == Some(binding.source_specifier_index)
                    && source.specifier_span == Some(binding.source_specifier_span)
                    && source.request == request.request
                    && source.imported_name.as_deref() == Some(binding.source_export.as_str())
                    && source.exported_name == binding.source_exported_name
                    && !source.type_only
            })
            .collect::<Vec<_>>();
        if rows.len() != 1 {
            return Err(CandidateRewriteIssue::MissingExactInputIdentity);
        }
    }
    Ok(())
}

fn same_use_census(left: &[CandidateVendorSourceUse], right: &[CandidateVendorSourceUse]) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .all(|site| right.iter().filter(|candidate| *candidate == site).count() == 1)
}

fn binding_target(binding: &CandidateVendorBindingReplacement) -> Option<CandidateImportTarget> {
    use CandidateVendorDestination as D;
    use CandidateVendorPlanAction as A;
    let same_export = match &binding.action {
        A::BoundaryRename { source_export, .. }
        | A::PartialPackageMember { source_export, .. }
        | A::PartialPackageNamespace { source_export, .. }
        | A::PartialPackageDefault { source_export, .. }
        | A::PartialPackageNamed { source_export, .. }
        | A::BundledFacadeMember { source_export, .. }
        | A::BundledFacadeDefault { source_export, .. } => source_export == &binding.source_export,
    };
    if !same_export {
        return None;
    }
    match (&binding.action, &binding.destination) {
        (A::BoundaryRename { public_export, .. }, D::BoundaryOutput { output, export })
            if public_export == export =>
        {
            Some(CandidateImportTarget::CandidateOutput {
                chunk: output.chunk,
                file: output.file.clone(),
            })
        }
        (
            A::PartialPackageMember { package, .. }
            | A::PartialPackageNamespace { package, .. }
            | A::PartialPackageDefault { package, .. }
            | A::PartialPackageNamed { package, .. },
            D::Package {
                request,
                package: destination,
            },
        ) if package == destination && request == package => binding.destination.import_target(),
        (
            A::BundledFacadeMember {
                facade_app_path, ..
            }
            | A::BundledFacadeDefault {
                facade_app_path, ..
            },
            D::Facade { app_path, .. },
        ) if facade_app_path == app_path => binding.destination.import_target(),
        _ => None,
    }
}

fn expected_import(binding: &CandidateVendorBindingReplacement) -> (ImportSpecifierKind, String) {
    use CandidateVendorPlanAction as A;
    match &binding.action {
        A::PartialPackageMember { .. } => (ImportSpecifierKind::Namespace, "*".into()),
        A::PartialPackageNamespace { .. } => (ImportSpecifierKind::Namespace, "*".into()),
        A::PartialPackageDefault { .. }
        | A::BundledFacadeMember { .. }
        | A::BundledFacadeDefault { .. } => (ImportSpecifierKind::Default, "default".into()),
        A::PartialPackageNamed {
            upstream_export, ..
        } => (ImportSpecifierKind::Named, upstream_export.clone()),
        A::BoundaryRename { public_export, .. } => {
            (ImportSpecifierKind::Named, public_export.clone())
        }
    }
}

fn result_root(result: &CandidateVendorUseResult) -> &Id {
    match result {
        CandidateVendorUseResult::LocalIdentifier { local } => local,
        CandidateVendorUseResult::MemberPath { namespace, .. } => namespace,
    }
}

fn matches_action_result(
    binding: &CandidateVendorBindingReplacement,
    result: &CandidateVendorUseResult,
) -> bool {
    match (&binding.action, result) {
        (
            CandidateVendorPlanAction::PartialPackageMember {
                namespace: expected,
                upstream_export,
                ..
            },
            CandidateVendorUseResult::MemberPath { namespace, path },
        ) => {
            namespace.0.as_ref() == expected.as_str()
                && path.len() == 1
                && path[0] == *upstream_export
        }
        (
            CandidateVendorPlanAction::BundledFacadeMember {
                namespace: expected,
                property_path,
                ..
            },
            CandidateVendorUseResult::MemberPath { namespace, path },
        ) => namespace.0.as_ref() == expected.as_str() && path == property_path,
        (
            CandidateVendorPlanAction::PartialPackageMember { .. }
            | CandidateVendorPlanAction::BundledFacadeMember { .. },
            _,
        ) => false,
        (_, CandidateVendorUseResult::LocalIdentifier { .. }) => true,
        _ => false,
    }
}

fn selected_output_binding<'a>(
    binding: &CandidateVendorBindingReplacement,
    request: &'a CandidateVendorEmittedRequest,
) -> Result<&'a CandidateRewriteBindingIdentity, CandidateRewriteIssue> {
    let expected = expected_import(binding);
    let rows = request
        .identity
        .bindings
        .iter()
        .filter(|candidate| {
            if binding.source_binding.is_none() {
                let (kind, imported) = match &binding.action {
                    CandidateVendorPlanAction::PartialPackageNamespace { .. } => {
                        (ImportSpecifierKind::Namespace, None)
                    }
                    CandidateVendorPlanAction::PartialPackageDefault { .. }
                    | CandidateVendorPlanAction::BundledFacadeDefault { .. } => {
                        (ImportSpecifierKind::Named, Some("default"))
                    }
                    CandidateVendorPlanAction::PartialPackageNamed {
                        upstream_export, ..
                    } => (ImportSpecifierKind::Named, Some(upstream_export.as_str())),
                    CandidateVendorPlanAction::BoundaryRename { public_export, .. } => {
                        (ImportSpecifierKind::Named, Some(public_export.as_str()))
                    }
                    _ => return false,
                };
                candidate.exported_name == binding.source_exported_name
                    && candidate.local.is_none()
                    && candidate.kind == Some(kind)
                    && candidate.imported_name.as_deref() == imported
            } else {
                candidate.kind == Some(expected.0)
                    && candidate.imported_name.as_deref() == Some(expected.1.as_str())
                    && candidate.local.as_ref().is_some_and(|local| {
                        binding.uses.is_empty()
                            || binding
                                .uses
                                .iter()
                                .all(|use_| result_root(&use_.result) == local)
                    })
            }
        })
        .collect::<Vec<_>>();
    let [row] = rows.as_slice() else {
        return Err(CandidateRewriteIssue::MissingExactOutputIdentity);
    };
    Ok(row)
}

fn validate_binding_output(
    receipt: &CandidateRewriteReceipt,
    binding: &CandidateVendorBindingReplacement,
) -> Result<(), CandidateRewriteIssue> {
    let target = binding_target(binding).ok_or(CandidateRewriteIssue::UnprovedTarget)?;
    if binding.emitted_requests.is_empty() {
        return Err(CandidateRewriteIssue::MissingExactOutputIdentity);
    }
    for request in &binding.emitted_requests {
        if request.output != receipt.output
            || request.identity.mode != Some(StaticModuleRequestMode::OrdinaryEsm)
            || request.identity.request.as_deref()
                != Some(match &binding.destination {
                    CandidateVendorDestination::Package { request, .. } => request,
                    CandidateVendorDestination::Facade {
                        emitted_request, ..
                    } => emitted_request,
                    CandidateVendorDestination::BoundaryOutput { .. } => request
                        .identity
                        .request
                        .as_deref()
                        .ok_or(CandidateRewriteIssue::MissingExactOutputIdentity)?,
                })
            || !request.identity.targets.contains(&target)
        {
            return Err(CandidateRewriteIssue::MissingExactOutputIdentity);
        }
        if binding.destination.import_target().is_some()
            && (request
                .identity
                .targets
                .iter()
                .any(|candidate| *candidate != target)
                || request
                    .identity
                    .bindings
                    .iter()
                    .any(|candidate| candidate.target.as_ref() != Some(&target)))
        {
            return Err(CandidateRewriteIssue::UnprovedTarget);
        }
        let selected = selected_output_binding(binding, request)?;
        if selected.target.as_ref() != Some(&target)
            || (binding.destination.import_target().is_some() && selected.source_cell.is_some())
        {
            return Err(CandidateRewriteIssue::UnprovedTarget);
        }
        if binding.source_binding.is_some()
            && request.identity.kind != CandidateOutputRequestKind::Import
            || binding.source_binding.is_none()
                && request.identity.kind != CandidateOutputRequestKind::NamedReExport
        {
            return Err(CandidateRewriteIssue::MissingExactOutputIdentity);
        }
    }
    for (index, use_) in binding.uses.iter().enumerate() {
        if use_.before_output != receipt.output
            || use_.after_output != receipt.output
            || use_.candidate_before_span != use_.source_span
            || !binding.source_uses.iter().any(|source| {
                source.statement == use_.source_statement && source.span == use_.source_span
            })
            || !matches_action_result(binding, &use_.result)
            || binding.uses[..index].iter().any(|previous| {
                previous.source_statement == use_.source_statement
                    && previous.source_span == use_.source_span
            })
            || !binding.emitted_requests.iter().any(|request| {
                request
                    .identity
                    .bindings
                    .iter()
                    .any(|candidate| candidate.local.as_ref() == Some(result_root(&use_.result)))
            })
        {
            return Err(CandidateRewriteIssue::UnprovedUseRewrite);
        }
    }
    Ok(())
}

fn replace_old_routes(
    correspondence: &mut CandidateSourceCorrespondence,
    before: &CandidatePreparedRequestRoute,
    after: Option<&CandidatePreparedRequestRoute>,
) {
    for routes in correspondence
        .source_requests
        .iter_mut()
        .map(|row| &mut row.routes)
        .chain(
            correspondence
                .reexports
                .iter_mut()
                .map(|row| &mut row.routes),
        )
    {
        routes.retain_mut(|route| {
            if route.output != before.output
                || route.item_index != before.item_index
                || route.item_span != before.item_span
            {
                return true;
            }
            let Some(after) = after else { return false };
            *route = source_route(after);
            true
        });
    }
    for import in &mut correspondence.imports {
        let source_local = import.source.local.clone();
        let mut changed = false;
        import.routes.retain_mut(|route| {
            if route.output != before.output || route.item_span != before.item_span {
                return true;
            }
            let Some(after) = after else {
                changed = true;
                return false;
            };
            let candidates = after
                .bindings
                .iter()
                .filter(|binding| {
                    binding.source_binding.as_ref() == Some(&source_local)
                        && binding.candidate_local.as_ref() == Some(&route.candidate_local)
                        && binding.specifier_span == route.specifier_span
                })
                .collect::<Vec<_>>();
            let [binding] = candidates.as_slice() else {
                changed = true;
                return false;
            };
            let Some(name) = &binding.imported_name else {
                changed = true;
                return false;
            };
            let preserves_cell = route.imported_name == *name && route.target == binding.target;
            route.item_span = after.item_span;
            route.specifier_index = binding.specifier_index;
            route.specifier_span = binding.specifier_span;
            route.emitted_request = after.emitted_request.clone();
            route.imported_name = name.clone();
            route.kind = binding.kind;
            route.target = binding.target.clone();
            route.source_cell = if preserves_cell {
                binding.source_cell
            } else {
                None
            };
            changed |= !preserves_cell;
            true
        });
        if changed {
            import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
        }
    }
}

fn source_route(prepared: &CandidatePreparedRequestRoute) -> CandidateSourceRequestRoute {
    CandidateSourceRequestRoute {
        output: prepared.output.clone(),
        item_index: prepared.item_index,
        item_span: prepared.item_span,
        source_statement: prepared.source_statement,
        emitted_request: prepared.emitted_request.clone(),
        mode: prepared.mode,
        targets: prepared.targets.clone(),
        bindings: prepared.bindings.clone(),
        status: prepared.status,
    }
}

fn project_replacement_edges(
    correspondence: &mut CandidateSourceCorrespondence,
    receipts: &[&CandidateRewriteReceipt],
) {
    let valid = receipts
        .iter()
        .filter_map(|receipt| {
            let CandidateRewriteDisposition::Replaced(payload) = &receipt.disposition else {
                return None;
            };
            (receipt.issue.is_none()
                && validate_payload(correspondence, receipt, payload).is_ok()
                && payload
                    .bindings
                    .iter()
                    .flat_map(|binding| &binding.emitted_requests)
                    .all(|request| {
                        correspondence.prepared_requests.iter().any(|route| {
                            route.output == request.output
                                && prepared_request_identity(route) == request.identity
                        })
                    }))
            .then_some((receipt, payload))
        })
        .collect::<Vec<_>>();
    for import in &mut correspondence.imports {
        let statement = import.source.statement;
        let specifier_index = import.source.specifier_index;
        let source_local = import.source.local.clone();
        let source_local = &source_local;
        let rows = valid
            .iter()
            .flat_map(|(_, payload)| {
                payload.bindings.iter().filter(move |binding| {
                    payload.source_request.statement == statement
                        && binding.source_specifier_index == specifier_index
                        && binding.source_binding.as_ref() == Some(source_local)
                        && binding.destination.import_target().is_some()
                })
            })
            .collect::<Vec<_>>();
        if rows.is_empty() {
            // A boundary rename has an exact structural transition, but this
            // consumer supplies no new provider-cell or initialization policy.
            // Do not retain the old source-cell edge as replacement authority.
            if valid.iter().any(|(_, payload)| {
                payload.source_request.statement == import.source.statement
                    && payload.bindings.iter().any(|binding| {
                        binding.source_binding.as_ref() == Some(&import.source.local)
                    })
            }) {
                import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
                for route in &mut import.routes {
                    route.source_cell = None;
                }
            }
            continue;
        }
        for route in &mut import.routes {
            if rows.iter().any(|row| {
                row.emitted_requests
                    .iter()
                    .any(|request| request.output == route.output)
            }) {
                route.source_cell = None;
            }
        }
        let source = import
            .source
            .use_sites
            .iter()
            .map(|site| CandidateVendorSourceUse {
                statement: site.statement,
                span: site.span,
            })
            .collect::<Vec<_>>();
        let uses = rows.iter().flat_map(|row| &row.uses).collect::<Vec<_>>();
        if source.iter().any(|site| {
            uses.iter()
                .filter(|use_| {
                    use_.source_statement == site.statement && use_.source_span == site.span
                })
                .count()
                != 1
        }) {
            import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
            continue;
        }
        let mut new_uses = import.uses.clone();
        let mut complete = new_uses.len() == source.len()
            && source.iter().all(|site| {
                new_uses
                    .iter()
                    .filter(|route| route.source_span == site.span)
                    .count()
                    == 1
            });
        for use_route in &mut new_uses {
            let matches = uses
                .iter()
                .filter(|use_| use_.source_span == use_route.source_span)
                .collect::<Vec<_>>();
            let [use_] = matches.as_slice() else {
                complete = false;
                continue;
            };
            let locations = use_route
                .routes
                .iter()
                .filter(|location| {
                    location.output == use_.before_output
                        && (location.candidate_span == use_.candidate_before_span
                            || location.candidate_span == use_.candidate_after_span)
                        && (location.candidate_local == use_.candidate_before_local
                            || location.candidate_local == *result_root(&use_.result))
                })
                .collect::<Vec<_>>();
            let [location] = locations.as_slice() else {
                complete = false;
                continue;
            };
            // Keep the known input item position as a locator only. The
            // final checker resolves after-span/Id against the actual final
            // AST; this receipt ABI does not invent a new emitted item index.
            use_route.routes = vec![CandidateSourceUseLocation {
                output: use_.after_output.clone(),
                item_index: location.item_index,
                candidate_span: use_.candidate_after_span,
                candidate_local: result_root(&use_.result).clone(),
            }];
            use_route.status = SourceCorrespondenceStatus::Complete;
        }
        if !complete {
            import.status = SourceCorrespondenceStatus::UnsupportedRewrite;
            continue;
        }
        import.routes.retain(|route| {
            !rows.iter().any(|row| {
                row.emitted_requests
                    .iter()
                    .any(|request| request.output == route.output)
            })
        });
        for row in rows {
            for request in &row.emitted_requests {
                let Ok(binding) = selected_output_binding(row, request) else {
                    continue;
                };
                let (Some(local), Some(kind), Some(name), Some(target)) = (
                    &binding.local,
                    binding.kind,
                    &binding.imported_name,
                    &binding.target,
                ) else {
                    continue;
                };
                import.routes.push(CandidateImportRoute {
                    output: request.output.clone(),
                    item_span: request.identity.item_span,
                    candidate_local: local.clone(),
                    specifier_index: binding.specifier_index,
                    specifier_span: binding.specifier_span,
                    emitted_request: request.identity.request.clone().expect("validated request"),
                    imported_name: name.clone(),
                    kind,
                    target: target.clone(),
                    source_cell: None,
                });
            }
        }
        import.uses = new_uses;
        import.status = SourceCorrespondenceStatus::Complete;
    }
    for (receipt, payload) in &valid {
        for binding in &payload.bindings {
            if binding.destination.import_target().is_none() {
                continue;
            }
            if let Some(local) = &binding.source_binding
                && !correspondence.imports.iter().any(|row| {
                    row.source.local == *local
                        && row.source.statement == payload.source_request.statement
                        && row.status == SourceCorrespondenceStatus::Complete
                })
            {
                continue;
            }
            for request in &binding.emitted_requests {
                let Some(prepared) = correspondence.prepared_requests.iter().find(|route| {
                    route.output == request.output
                        && prepared_request_identity(route) == request.identity
                }) else {
                    continue;
                };
                for source in &mut correspondence.source_requests {
                    if source.source.statement == payload.source_request.statement
                        && !source.routes.iter().any(|route| {
                            route.output == request.output
                                && route.item_index == request.identity.item_index
                                && route.item_span == request.identity.item_span
                        })
                    {
                        source.routes.push(source_route(prepared));
                    }
                }
                for source in &mut correspondence.reexports {
                    if binding.source_binding.is_none()
                        && source.source.statement == payload.source_request.statement
                        && source.source.specifier == Some(binding.source_specifier_index)
                    {
                        source.routes.retain(|route| route.output != receipt.output);
                        source.routes.push(source_route(prepared));
                        source.status = SourceCorrespondenceStatus::Complete;
                    }
                }
            }
        }
    }
    for request in &mut correspondence.source_requests {
        let imports_complete = correspondence
            .imports
            .iter()
            .filter(|row| row.source.statement == request.source.statement)
            .all(|row| row.status == SourceCorrespondenceStatus::Complete);
        let exports_complete = correspondence
            .reexports
            .iter()
            .filter(|row| row.source.statement == request.source.statement)
            .all(|row| row.status == SourceCorrespondenceStatus::Complete);
        if !request.source.type_only
            && request.source.mode == StaticModuleRequestMode::OrdinaryEsm
            && !request.routes.is_empty()
            && request
                .routes
                .iter()
                .all(|route| route.status == SourceCorrespondenceStatus::Complete)
            && imports_complete
            && exports_complete
        {
            request.status = SourceCorrespondenceStatus::Complete;
        }
    }
}

/// Recompute only from the retained typed obligations. A replacement receipt
/// cannot promote missing source/world/cell facts or unrelated source routes.
pub(crate) fn refresh_replacement_status(correspondence: &mut CandidateSourceCorrespondence) {
    if !correspondence.rewrite_receipts.iter().any(|receipt| {
        matches!(
            receipt.disposition,
            CandidateRewriteDisposition::Replaced(_)
        )
    }) {
        return;
    }
    let complete = correspondence.source_unit_status == SourceCorrespondenceStatus::Complete
        && correspondence
            .source_units
            .values()
            .all(|row| row.status == SourceCorrespondenceStatus::Complete)
        && correspondence
            .source_requests
            .iter()
            .all(|row| row.status == SourceCorrespondenceStatus::Complete)
        && correspondence
            .imports
            .iter()
            .all(|row| row.status == SourceCorrespondenceStatus::Complete)
        && correspondence
            .reexports
            .iter()
            .all(|row| row.status == SourceCorrespondenceStatus::Complete)
        && correspondence
            .exports
            .iter()
            .all(|row| row.status == SourceCorrespondenceStatus::Complete)
        && correspondence
            .prepared_requests
            .iter()
            .all(|row| row.status == SourceCorrespondenceStatus::Complete)
        && correspondence
            .module_cell_declarations
            .iter()
            .all(|row| row.status == SourceCorrespondenceStatus::Complete)
        && correspondence.source_module_cells.is_some()
        && correspondence.source_initialized_module_cells.is_some()
        && correspondence.rewrite_receipts.iter().all(|receipt| {
            if receipt.issue.is_some()
                || receipt.disposition == CandidateRewriteDisposition::Unsupported
            {
                return false;
            }
            let CandidateRewriteDisposition::Replaced(payload) = &receipt.disposition else {
                return true;
            };
            validate_payload(correspondence, receipt, payload).is_ok()
                && payload
                    .bindings
                    .iter()
                    .flat_map(|binding| &binding.emitted_requests)
                    .all(|request| {
                        correspondence.prepared_requests.iter().any(|route| {
                            route.output == request.output
                                && prepared_request_identity(route) == request.identity
                        })
                    })
        });
    if complete {
        correspondence.status = SourceCorrespondenceStatus::Complete;
    } else {
        correspondence.status = SourceCorrespondenceStatus::UnsupportedRewrite;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use source_identity::{ChunkId, StatementId};
    use swc_common::Spanned;

    #[test]
    fn parsed_request_without_target_cannot_install_a_vendor_route() {
        js_ast::with_swc_globals(|| {
            let parsed =
                js_ast::parse_js_module("consumer.js", "import { value } from 'package';").unwrap();
            let output = OutputFileAddress {
                chunk: ChunkId(0),
                file: "entry.js".into(),
            };
            let (identity, issue) =
                crate::pruning::request_identity(None, &output, 0, &parsed.module.body[0]).unwrap();
            assert_eq!(
                issue,
                Some(CandidateRewriteIssue::MissingExactInputIdentity)
            );
            assert!(matches!(
                route_from_identity(&output, &identity),
                Err(CandidateRewriteIssue::UnprovedTarget)
            ));
        });
    }

    #[test]
    fn use_union_census_rejects_omitted_duplicated_and_wrong_statement_sites() {
        js_ast::with_swc_globals(|| {
            let parsed = js_ast::parse_js_module("consumer.js", "first(); second();").unwrap();
            let census = parsed
                .module
                .body
                .iter()
                .enumerate()
                .map(|(index, item)| CandidateVendorSourceUse {
                    statement: StatementId(index),
                    span: item.span(),
                })
                .collect::<Vec<_>>();
            assert!(same_use_census(&census, &census));
            assert!(!same_use_census(&census, &census[..1]));
            assert!(!same_use_census(
                &census,
                &[census[0].clone(), census[0].clone()]
            ));
            let mut wrong = census.clone();
            wrong[1].statement = StatementId(0);
            assert!(!same_use_census(&census, &wrong));
        });
    }
}
