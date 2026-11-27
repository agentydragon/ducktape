//! Resolves per-chunk analysis inputs against the program-wide module graph.

use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};

use analysis_vocabulary::inputs::{
    ArgumentInputEvidence, ArgumentInputRef, EffectiveInitializerCallReplacement,
    InitializerCallEffectInputRef, NativeInputKind, NativeOperationInputEvidence,
    NativeOperationInputRef, NativeOperationSelectorProvenance, NoAccessorInstallRef,
    ResolvedArgumentInput, ResolvedInitializerCallEffectInput, ResolvedNoAccessorInstall,
    SelectedBindingValueInput, SelectedNativeOperationInput, ValueInputEvidence, ValueInputRef,
};
use anyhow::{Context, Result, bail, ensure};
use artifact::ChunkBundle;
use cross_module_purity::module_facts::ModulePurityFacts;
use graph_provenance::{CurrentRoots, GraphProvenance, InputRoot};
use swc_common::{Span, Spanned};
use swc_ecma_ast::{
    ArrowExpr, CallExpr, Callee, Constructor, Decl, Function, GetterProp, MemberProp, ModuleDecl,
    ModuleItem, Pat, SetterProp,
};
use swc_ecma_visit::{Visit, VisitWith};

use analysis_projection::{AnalysisNativeOperationInput, ChunkAnalysisInputs};

use super::static_dependencies::{ResolvedStaticDependencies, resolved_closure_precedes_importer};
use super::{ProgramSummaryOptions, chunk_allowed};

pub(super) fn resolve_initializer_call_effect_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    sites: &analysis_projection::AnalysisSourceSites,
    summary_options: ProgramSummaryOptions<'_>,
    dependencies: &ResolvedStaticDependencies,
) -> Result<BTreeMap<String, Vec<ResolvedInitializerCallEffectInput>>> {
    let ProgramSummaryOptions {
        graph_provenance: provenance,
        vendor_plan,
        replacement_packages,
    } = summary_options;
    let mut resolved = BTreeMap::new();
    for (chunk, options) in options {
        if options.initializer_call_effect_inputs.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk}.initializer_call_effect_inputs");
        let module = modules
            .get(chunk)
            .with_context(|| format!("{prefix} importer has no analyzed entry"))?;
        let selected = sites
            .initializer_call_effect_inputs
            .get(chunk)
            .with_context(|| format!("{prefix} has no resolved source sites"))?;
        ensure!(
            selected.len() == options.initializer_call_effect_inputs.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let source_sha = sites
            .source_sha256
            .get(chunk)
            .with_context(|| format!("{prefix} has no parser-input digest"))?;
        let provenance =
            provenance.context("initializer call effect inputs require parser-input provenance")?;
        let mut seen = BTreeSet::new();
        let mut entries = Vec::new();
        for (index, (contract, selected)) in options
            .initializer_call_effect_inputs
            .iter()
            .zip(selected)
            .enumerate()
        {
            let request = format!("{prefix}[{index}]");
            for (field, digest) in [
                ("audited_source_sha256", &contract.audited_source_sha256),
                ("audited_provider_sha256", &contract.audited_provider_sha256),
            ] {
                ensure!(
                    digest.len() == 64 && digest.bytes().all(|b| b.is_ascii_hexdigit()),
                    "{request}.{field} must be a 64-character SHA-256 digest"
                );
            }
            ensure!(
                &contract.audited_source_sha256 == source_sha,
                "{request} has stale audited_source_sha256"
            );
            let provider_source = provenance
                .chunk_sources
                .iter()
                .find(|source| source.chunk_id == contract.provider_chunk)
                .with_context(|| {
                    format!("{request}.provider_chunk has no parser-input provenance")
                })?;
            ensure!(
                contract.audited_provider_sha256 == provider_source.sha256,
                "{request} has stale audited_provider_sha256"
            );
            let provider = modules
                .get(&contract.provider_chunk)
                .with_context(|| format!("{request}.provider_chunk has no analyzed entry"))?;
            ensure!(
                provider.exports.contains_key(&contract.provider_export),
                "{request}.provider_export has no analyzed export"
            );
            let provider_id = artifact
                .chunk_table
                .get(&contract.provider_chunk)
                .with_context(|| {
                    format!("{request}.provider_chunk has no resolved chunk identity")
                })?;
            let swapped_package = replacement_packages
                .get(&provider_id)
                .and_then(|exports| exports.get(&contract.provider_export));
            let effective_replacement = if let Some(swapped_package) = swapped_package {
                let plan = vendor_plan.with_context(|| {
                    format!("{request} has no effective vendor resolution plan")
                })?;
                let authored = contract.effective_replacement.as_ref().with_context(|| {
                    format!("{request}.effective_replacement is required for a swapped export")
                })?;
                let resolved = plan.bundled_replacement_export(provider_id, &contract.provider_export)
                    .with_context(|| format!("{request} requires an exact bundled_partial_swap export; other swap levels are unsupported"))?;
                ensure!(
                    !plan.replacement_bundle_is_open(provider_id),
                    "{request} replacement bundle has an open evaluation closure"
                );
                ensure!(
                    authored.package == *swapped_package
                        && authored.package == resolved.package
                        && authored.bundle_export == resolved.bundle_export
                        && authored.symbol_kind == resolved.symbol_kind
                        && authored.upstream_export == resolved.upstream_export,
                    "{request}.effective_replacement disagrees with the selected vendor mapping"
                );
                let bundle_path = graph_provenance::absolute(&resolved.bundle_path)?;
                let recorded_files = provenance
                    .files
                    .iter()
                    .filter(|file| {
                        matches!(&file.root, InputRoot::Source | InputRoot::Absolute)
                            && provenance
                                .resolve(&file.root, &file.path, &CurrentRoots::default())
                                .and_then(|path| graph_provenance::absolute(&path))
                                .is_ok_and(|path| path == bundle_path)
                    })
                    .collect::<Vec<_>>();
                let [recorded] = recorded_files.as_slice() else {
                    bail!("{request} selected vendor bundle has no unique captured source input");
                };
                ensure!(
                    authored.audited_bundle_sha256.len() == 64
                        && authored
                            .audited_bundle_sha256
                            .bytes()
                            .all(|b| b.is_ascii_hexdigit()),
                    "{request}.effective_replacement.audited_bundle_sha256 must be a 64-character SHA-256 digest"
                );
                ensure!(
                    authored.audited_bundle_sha256 == recorded.sha256,
                    "{request} has stale effective_replacement.audited_bundle_sha256"
                );
                Some(EffectiveInitializerCallReplacement {
                    package: resolved.package,
                    bundle_path,
                    bundle_export: resolved.bundle_export,
                    symbol_kind: resolved.symbol_kind,
                    upstream_export: resolved.upstream_export,
                    bundle_sha256: recorded.sha256.clone(),
                })
            } else {
                ensure!(
                    contract.effective_replacement.is_none(),
                    "{request}.effective_replacement names an unswapped provider export"
                );
                None
            };
            let item = module
                .body
                .get(selected.body_index)
                .with_context(|| format!("{request} selected source item is missing"))?;
            let ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(Decl::Var(declaration))) = item else {
                anyhow::bail!("{request} requires one ordinary variable declaration");
            };
            let mut declarators = declaration.decls.iter().filter(|declarator| {
                matches!(&declarator.name, Pat::Ident(binding)
                    if binding.id.sym.as_ref() == selected.binding)
            });
            let declarator = declarators.next().with_context(|| {
                format!("{request} selected binding has no variable declarator")
            })?;
            ensure!(
                declarators.next().is_none(),
                "{request} selected binding has multiple variable declarators"
            );
            let Pat::Ident(binding) = &declarator.name else {
                anyhow::bail!("{request} requires one simple binding");
            };
            ensure!(
                binding.id.sym.as_ref() == selected.binding,
                "{request} source selector binding differs from initializer binding"
            );
            let Some(initializer) = &declarator.init else {
                anyhow::bail!("{request} requires an initializer");
            };
            let swc_ecma_ast::Expr::Call(call) = initializer.as_ref().unwrap_parens() else {
                anyhow::bail!("{request} requires a direct root call initializer");
            };
            let swc_ecma_ast::Callee::Expr(callee_expression) = &call.callee else {
                anyhow::bail!("{request} requires a direct imported callee");
            };
            let swc_ecma_ast::Expr::Ident(callee) = callee_expression.as_ref().unwrap_parens()
            else {
                anyhow::bail!("{request} requires a bare imported callee");
            };
            let [argument] = call.args.as_slice() else {
                anyhow::bail!("{request} requires exactly one argument");
            };
            ensure!(
                argument.spread.is_none() && call.type_args.is_none(),
                "{request} requires a direct unspread argument"
            );
            if contract.returns_opaque_function_from_const_primitive {
                ensure!(
                    matches!(
                        argument.expr.as_ref().unwrap_parens(),
                        swc_ecma_ast::Expr::Ident(_)
                    ),
                    "{request} requires a bare const-primitive binding argument"
                );
            } else {
                let swc_ecma_ast::Expr::Fn(callback) = argument.expr.as_ref().unwrap_parens()
                else {
                    anyhow::bail!(
                        "{request} requires a fresh anonymous ordinary function argument"
                    );
                };
                ensure!(
                    callback.ident.is_none()
                        && !callback.function.is_async
                        && !callback.function.is_generator
                        && callback.function.body.is_some()
                        && callback.function.decorators.is_empty()
                        && callback.function.type_params.is_none()
                        && callback.function.return_type.is_none(),
                    "{request} requires a fresh anonymous ordinary function argument"
                );
            }
            // The callee is a resolved direct ESM import binding. Its immutable
            // module environment cell supplies the selected GetBindingValue;
            // attempted assignment throws and remains an actual source effect.
            let imported = module.imports.get(callee.sym.as_ref()).with_context(|| {
                format!("{request} callee is not a resolved direct named import")
            })?;
            ensure!(
                imported.module == contract.provider_chunk
                    && imported.export == contract.provider_export,
                "{request} provider identity disagrees with the direct import"
            );
            let closure = resolved_closure_precedes_importer(
                chunk,
                &imported.module,
                &imported.source_specifier,
                dependencies,
            )
            .with_context(|| {
                format!("{request} provider static closure does not precede importer")
            })?;
            ensure!(
                seen.insert((call.span.lo.0, call.span.hi.0)),
                "{request} duplicates an asserted initializer call"
            );
            entries.push(ResolvedInitializerCallEffectInput {
                reference: InitializerCallEffectInputRef {
                    defining_chunk: chunk.clone(),
                    input_index: index,
                },
                binding: binding.id.to_id(),
                callee: callee.to_id(),
                call_span: call.span,
                source_sha256: source_sha.clone(),
                provider_chunk: imported.module.clone(),
                provider_export: imported.export.clone(),
                provider_sha256: provider_source.sha256.clone(),
                returns_opaque_function_from_const_primitive: contract
                    .returns_opaque_function_from_const_primitive,
                effective_replacement,
                provider_dependency_closure: closure,
            });
        }
        resolved.insert(chunk.clone(), entries);
    }
    Ok(resolved)
}

pub(super) fn resolve_value_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    selected_sites: &BTreeMap<String, Vec<analysis_vocabulary::inputs::SourceDeclaration>>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<BTreeMap<String, Vec<SelectedBindingValueInput>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.value_inputs.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk_name}.value_inputs");
        let chunk = artifact.loaded_provider_chunk(&prefix, chunk_name)?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "chunk_analysis_options.{chunk_name}.value_inputs provider is outside the existing analyzed import closure"
        );
        let facts = modules.get(chunk_name).with_context(|| {
            format!(
                "chunk_analysis_options.{chunk_name}.value_inputs provider entry is unavailable to whole-program analysis"
            )
        })?;
        let entry_file = &chunk.analysis.entry_file;
        ensure!(
            chunk.js.get_file(entry_file).is_some(),
            "provider {chunk_name} is missing its analyzed entry file {entry_file}"
        );
        let provenance = graph_provenance
            .context("captured value inputs require explicit graph parser-input provenance")?;
        let source = provenance
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| {
                format!(
                    "chunk_analysis_options.{chunk_name}.value_inputs requires explicit graph parser-input provenance"
                )
            })?;
        let declarations = selected_sites
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no frontend-resolved source sites"))?;
        ensure!(
            declarations.len() == options.value_inputs.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let mut chunk_inputs = Vec::with_capacity(options.value_inputs.len());
        for (input_index, (contract, declaration)) in
            options.value_inputs.iter().zip(declarations).enumerate()
        {
            ensure!(
                contract.audited_source_sha256.len() == 64
                    && contract
                        .audited_source_sha256
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit()),
                "chunk_analysis_options.{chunk_name}.value_inputs[{input_index}].audited_source_sha256 must be a 64-character SHA-256 digest"
            );
            ensure!(
                contract.audited_source_sha256 == source.sha256,
                "chunk_analysis_options.{chunk_name}.value_inputs[{input_index}] has stale audited_source_sha256: expected parser input {}, found {}",
                source.sha256,
                contract.audited_source_sha256
            );
            let request_id =
                format!("chunk_analysis_options.{chunk_name}.value_inputs[{input_index}]");
            let body_index = declaration.body_index;
            let selected_binding = declaration.binding.clone();
            if let Some(export_name) = &contract.expected_export {
                ensure!(
                    facts
                        .exports
                        .get(export_name)
                        .is_some_and(|resolved_local| resolved_local == &selected_binding)
                        && !facts.imports.contains_key(&selected_binding),
                    "{request_id}.expected_export `{export_name}` does not resolve directly to selected local `{selected_binding}` in provider {chunk_name}"
                );
            }
            let selected_statement_span = facts
                .body
                .get(body_index)
                .with_context(|| {
                    format!("{request_id}.source_match selected a missing module item")
                })?
                .span();
            let reference = ValueInputRef {
                defining_chunk: chunk_name.clone(),
                input_index,
            };
            let evidence = ValueInputEvidence {
                reference,
                spec_location: request_id,
                source_match: contract.source_match.clone(),
                value_kind: contract.value.kind,
                source_path: source.source_path.to_string_lossy().into_owned(),
                parser_input_file: None,
                audited_source_sha256: contract.audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                declaration_location: None,
                initializer_location: None,
                expected_export: contract.expected_export.clone(),
                note: None,
            };
            chunk_inputs.push(SelectedBindingValueInput {
                evidence,
                selected_binding,
                selected_statement_span,
            });
        }
        resolved.insert(chunk_name.clone(), chunk_inputs);
    }
    Ok(resolved)
}

pub(super) fn resolve_native_operation_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
    projected_inputs: &BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
    source_sites: &analysis_projection::AnalysisSourceSites,
) -> Result<BTreeMap<String, Vec<SelectedNativeOperationInput>>> {
    let mut resolved = BTreeMap::new();
    // Native operation inputs have their own projection, so their chunks may
    // not have a source-analysis entry.
    let chunk_names: BTreeSet<&String> = options.keys().chain(projected_inputs.keys()).collect();
    for chunk_name in chunk_names {
        let analysis_options = options.get(chunk_name);
        let native_call_identity_inputs = analysis_options.map_or(&[][..], |options| {
            options.native_call_identity_inputs.as_slice()
        });
        let operation_inputs = projected_inputs
            .get(chunk_name)
            .map_or(&[][..], Vec::as_slice);
        if operation_inputs.is_empty() && native_call_identity_inputs.is_empty() {
            continue;
        }
        let chunk = artifact.loaded_provider_chunk(
            &format!("chunk_analysis_options.{chunk_name}.native_operation_inputs"),
            chunk_name,
        )?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "chunk_analysis_options.{chunk_name}.native_operation_inputs provider is outside the existing analyzed import closure"
        );
        let facts = modules.get(chunk_name).with_context(|| {
            format!(
                "chunk_analysis_options.{chunk_name}.native_operation_inputs provider entry is unavailable to whole-program analysis"
            )
        })?;
        let entry_file = &chunk.analysis.entry_file;
        ensure!(
            chunk.js.get_file(entry_file).is_some(),
            "provider {chunk_name} is missing its analyzed entry file {entry_file}"
        );
        let provenance = graph_provenance
            .context("native operation inputs require explicit graph parser-input provenance")?;
        let source = provenance
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| {
                format!(
                    "chunk_analysis_options.{chunk_name}.native_operation_inputs requires explicit graph parser-input provenance"
                )
            })?;
        let call_identity_sites = source_sites
            .native_call_identity_inputs
            .get(chunk_name)
            .map_or(&[][..], Vec::as_slice);
        ensure!(
            call_identity_sites.len() == native_call_identity_inputs.len(),
            "chunk_analysis_options.{chunk_name}.native_call_identity_inputs source-site count does not match authored inputs"
        );
        let contracts = operation_inputs
            .iter()
            .enumerate()
            .map(|(index, contract)| {
                (
                    index,
                    format!("chunk_analysis_options.{chunk_name}.native_operation_inputs[{index}]"),
                    contract.declaration.clone(),
                    contract.selector.clone(),
                    &contract.audited_source_sha256,
                    NativeInputKind::Operation(contract.operation),
                )
            })
            .chain(native_call_identity_inputs.iter().enumerate().map(
                |(index, contract)| {
                    (
                        operation_inputs.len() + index,
                        format!("chunk_analysis_options.{chunk_name}.native_call_identity_inputs[{index}]"),
                        call_identity_sites[index].clone(),
                        NativeOperationSelectorProvenance::SourceMatch(contract.source_match.clone()),
                        &contract.audited_source_sha256,
                        NativeInputKind::CallIdentity(contract.callee),
                    )
                },
            ));
        let mut chunk_inputs =
            Vec::with_capacity(operation_inputs.len() + native_call_identity_inputs.len());
        for (
            input_index,
            request_id,
            declaration,
            selector_provenance,
            audited_source_sha256,
            operation,
        ) in contracts
        {
            ensure!(
                audited_source_sha256.len() == 64
                    && audited_source_sha256
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit()),
                "{request_id}.audited_source_sha256 must be a 64-character SHA-256 digest"
            );
            ensure!(
                audited_source_sha256 == &source.sha256,
                "{request_id} has stale audited_source_sha256: expected parser input {}, found {}",
                source.sha256,
                audited_source_sha256
            );
            let body_index = declaration.body_index;
            let selected_binding = declaration.binding;
            let selected_statement_span = facts
                .body
                .get(body_index)
                .with_context(|| {
                    format!("{request_id}.source_match selected a missing module item")
                })?
                .span();
            let reference = NativeOperationInputRef {
                defining_chunk: chunk_name.clone(),
                input_index,
            };
            let evidence = NativeOperationInputEvidence {
                reference,
                spec_location: request_id,
                selector: selector_provenance,
                operation,
                source_path: source.source_path.to_string_lossy().into_owned(),
                parser_input_file: None,
                audited_source_sha256: audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                declaration_location: None,
                initializer_location: None,
                note: None,
            };
            chunk_inputs.push(SelectedNativeOperationInput {
                evidence,
                selected_binding,
                selected_statement_span,
            });
        }
        resolved.insert(chunk_name.clone(), chunk_inputs);
    }
    Ok(resolved)
}

pub(super) fn resolve_argument_inputs(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    selected_sites: &BTreeMap<String, Vec<analysis_vocabulary::inputs::SourceDeclaration>>,
    graph_provenance: Option<&GraphProvenance>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<BTreeMap<String, Vec<ResolvedArgumentInput>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.argument_inputs.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk_name}.argument_inputs");
        let chunk = artifact.loaded_provider_chunk(&prefix, chunk_name)?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "{prefix} cannot target an outside-closure provider"
        );
        let facts = modules
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no analyzed provider"))?;
        ensure!(
            chunk.js.get_file(&chunk.analysis.entry_file).is_some(),
            "{prefix} has no parser entry file"
        );
        let provenance =
            graph_provenance.context("argument inputs require graph parser-input provenance")?;
        let source = provenance
            .chunk_sources
            .iter()
            .find(|entry| entry.chunk_id == *chunk_name)
            .with_context(|| format!("{prefix} has no parser-input provenance"))?;
        let declarations = selected_sites
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no frontend-resolved source sites"))?;
        ensure!(
            declarations.len() == options.argument_inputs.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let source_facts = analysis::facts::collect_chunk_source_facts_from_items(facts.body, None);
        let mut selected = Vec::new();
        for (input_index, (contract, declaration)) in
            options.argument_inputs.iter().zip(declarations).enumerate()
        {
            let request_id = format!("{prefix}[{input_index}]");
            ensure!(
                contract.audited_source_sha256.len() == 64
                    && contract
                        .audited_source_sha256
                        .bytes()
                        .all(|b| b.is_ascii_hexdigit()),
                "{request_id}.audited_source_sha256 must be a 64-character SHA-256 digest"
            );
            ensure!(
                contract.audited_source_sha256 == source.sha256,
                "{request_id} has stale audited_source_sha256: expected parser input {}, found {}",
                source.sha256,
                contract.audited_source_sha256
            );
            ensure!(
                contract.parameter_index < contract.exact_arity,
                "{request_id} parameter_index exceeds exact_arity"
            );
            let body_index = declaration.body_index;
            let selected_binding = declaration.binding.clone();
            let item = facts
                .body
                .get(body_index)
                .context("selected source item is missing")?;
            let function = match item {
                ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(Decl::Fn(function))) => function,
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                    let Decl::Fn(function) = &export.decl else {
                        bail!("{request_id} requires a direct function declaration");
                    };
                    function
                }
                _ => bail!("{request_id} requires a direct function declaration"),
            };
            ensure!(
                function.ident.sym.as_ref() == selected_binding
                    && function.function.params.len() == contract.exact_arity
                    && function
                        .function
                        .params
                        .iter()
                        .all(|parameter| matches!(parameter.pat, Pat::Ident(_)))
                    && !function.function.is_async
                    && !function.function.is_generator
                    && function.function.body.is_some(),
                "{request_id} requires the selected stable synchronous function with exact arity"
            );
            let Pat::Ident(parameter) = &function.function.params[contract.parameter_index].pat
            else {
                bail!("{request_id} requires a simple positional parameter");
            };
            let source_facts = source_facts
                .per_statement
                .get(body_index)
                .context("selected source statement facts are missing")?;
            let contradictions = source_facts.contradicts_argument_borrow(
                &function.ident.to_id(),
                &parameter.id.to_id(),
                contract.parameter_index,
            );
            ensure!(
                !contradictions,
                "{request_id} contradicts a visible return, throw, capture, store, or mutation of its parameter"
            );
            let evidence = ArgumentInputEvidence {
                reference: ArgumentInputRef {
                    defining_chunk: chunk_name.clone(),
                    input_index,
                },
                spec_location: request_id.clone(),
                source_match: contract.source_match.clone(),
                source_path: source.source_path.to_string_lossy().into_owned(),
                audited_source_sha256: contract.audited_source_sha256.clone(),
                selected_binding: selected_binding.clone(),
                parameter_index: contract.parameter_index,
                exact_arity: contract.exact_arity,
                declaration_location: None,
            };
            selected.push(ResolvedArgumentInput {
                evidence,
                binding: function.ident.to_id(),
                function_start: function.function.span.lo.0,
            });
        }
        resolved.insert(chunk_name.clone(), selected);
    }
    Ok(resolved)
}

pub(super) fn resolve_no_accessor_install_callables(
    artifact: &ChunkBundle,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    options: &BTreeMap<String, ChunkAnalysisInputs>,
    selected_sites: &BTreeMap<String, Vec<analysis_vocabulary::inputs::SourceDeclaration>>,
    chunk_filter: Option<&BTreeSet<ChunkId>>,
) -> Result<BTreeMap<String, Vec<ResolvedNoAccessorInstall>>> {
    let mut resolved = BTreeMap::new();
    for (chunk_name, options) in options {
        if options.no_accessor_install_callables.is_empty() {
            continue;
        }
        let prefix = format!("chunk_analysis_options.{chunk_name}.no_accessor_install_callables");
        let chunk = artifact.loaded_provider_chunk(&prefix, chunk_name)?;
        ensure!(
            chunk_allowed(chunk.chunk_id, chunk_filter),
            "{prefix} cannot target an outside-closure provider"
        );
        let facts = modules
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no analyzed provider"))?;
        let declarations = selected_sites
            .get(chunk_name)
            .with_context(|| format!("{prefix} has no frontend-resolved source sites"))?;
        ensure!(
            declarations.len() == options.no_accessor_install_callables.len(),
            "{prefix} source-site count does not match authored inputs"
        );
        let mut selected = Vec::new();
        for (input_index, declaration) in declarations.iter().enumerate() {
            let request_id = format!("{prefix}[{input_index}]");
            let mut callables = SelectedCallables::default();
            facts
                .body
                .get(declaration.body_index)
                .context("selected source item is missing")?
                .visit_with(&mut callables);
            let [function_span] = callables.outermost[..] else {
                bail!(
                    "{request_id} must select a statement containing exactly one outermost function, found {}",
                    callables.outermost.len()
                );
            };
            ensure!(
                callables.computed_method_call,
                "{request_id} selects a function with no computed-key method call"
            );
            selected.push(ResolvedNoAccessorInstall {
                reference: NoAccessorInstallRef {
                    defining_chunk: chunk_name.clone(),
                    input_index,
                },
                function_span,
            });
        }
        resolved.insert(chunk_name.clone(), selected);
    }
    Ok(resolved)
}

/// The outermost functions of one selected statement, accessors included;
/// a nested function is part of the one enclosing it.
#[derive(Default)]
struct SelectedCallables {
    outermost: Vec<Span>,
    depth: usize,
    computed_method_call: bool,
}

impl SelectedCallables {
    fn enclose(&mut self, span: Span, visit: impl FnOnce(&mut Self)) {
        if self.depth == 0 {
            self.outermost.push(span);
        }
        self.depth += 1;
        visit(self);
        self.depth -= 1;
    }
}

impl Visit for SelectedCallables {
    fn visit_function(&mut self, function: &Function) {
        self.enclose(function.span, |this| function.visit_children_with(this));
    }

    fn visit_arrow_expr(&mut self, arrow: &ArrowExpr) {
        self.enclose(arrow.span, |this| arrow.visit_children_with(this));
    }

    fn visit_constructor(&mut self, constructor: &Constructor) {
        self.enclose(constructor.span, |this| {
            constructor.visit_children_with(this)
        });
    }

    fn visit_getter_prop(&mut self, getter: &GetterProp) {
        self.enclose(getter.span, |this| getter.visit_children_with(this));
    }

    fn visit_setter_prop(&mut self, setter: &SetterProp) {
        self.enclose(setter.span, |this| setter.visit_children_with(this));
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee
            && let Some(member) = callee.as_member()
            && matches!(member.prop, MemberProp::Computed(_))
        {
            self.computed_method_call = true;
        }
        call.visit_children_with(self);
    }
}
