//! Resolves analysis source sites and imported-array spread inputs to the
//! source declarations their selectors name.

use super::{FrontendChunk, ResolvedMemberSite};
use analysis::facts::StructuralChunkSourceFacts;
use analysis_projection::{
    self, AnalysisNativeOperationInput, AnalysisSourceSites, ProjectedChunkOptions,
};
use analysis_vocabulary::inputs::{NativeOperationSelectorProvenance, SourceDeclaration};
use anyhow::{Context, Result, bail, ensure};
use artifact::{ArtifactIndexes, ChunkBundle};
use graph_provenance::{ChunkSource, GraphProvenance};
use selector_outcome::Outcome;
use selector_resolve::Chunk as SelectorChunk;
use selector_resolve::EntityIndex;
use source_identity::ChunkId;
use source_match::source_match_claim_member_selectors;
use spec::annotations::SourceMatchClaim;
use spec::input_contracts::SourceUnitSelector;
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet, HashMap};

/// The analysis entry may differ from the materialized entry. Key censuses by
/// the borrowed AST's identity so a different parse gets its own source facts.
type PreparedCensuses<'a> =
    HashMap<*const js_ast::ParsedJsModule, &'a StructuralChunkSourceFacts<'a>>;

fn source_premise_selector<'a>(
    name: &str,
    parsed: &'a js_ast::ParsedJsModule,
    prepared: &PreparedCensuses<'a>,
) -> SelectorChunk<'a> {
    match prepared.get(&std::ptr::from_ref(parsed)) {
        Some(facts) => SelectorChunk::new(name, &parsed.module, Cow::Borrowed(facts)),
        None => SelectorChunk::analyze(name, &parsed.module),
    }
}

pub(super) fn resolve_analysis_source_sites(
    artifact: &ChunkBundle,
    prepared: &[FrontendChunk<'_>],
    projected_options: &BTreeMap<String, ProjectedChunkOptions>,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    graph_provenance: Option<&GraphProvenance>,
) -> Result<(
    AnalysisSourceSites,
    BTreeMap<String, Vec<AnalysisNativeOperationInput>>,
)> {
    let indexes = ArtifactIndexes::build(artifact)?;
    let (selected_chunk_ids, prepared): (Vec<_>, PreparedCensuses<'_>) = prepared
        .iter()
        .map(|chunk| {
            let census = (
                std::ptr::from_ref(chunk.runtime_ast),
                &chunk.structural_analysis,
            );
            (chunk.chunk_id.clone(), census)
        })
        .unzip();
    let provider_closure =
        artifact::selected_static_import_closure(artifact, &indexes, &selected_chunk_ids)?;
    let mut source_sites = AnalysisSourceSites::default();
    let mut native_operation_inputs = BTreeMap::new();

    for (chunk_name, options) in projected_options {
        let has_source_premises = !options.native_operation_inputs.is_empty()
            || !options.imported_primitive_array_spread_inputs.is_empty()
            || !options.analysis.value_inputs.is_empty()
            || !options.analysis.initializer_call_effect_inputs.is_empty()
            || !options.analysis.native_call_identity_inputs.is_empty()
            || !options.analysis.argument_inputs.is_empty()
            || !options.analysis.no_accessor_install_callables.is_empty();
        if !has_source_premises {
            continue;
        }

        let prefix = format!("chunk_analysis_options.{chunk_name}");
        let chunk = artifact
            .chunk_named(chunk_name)
            .with_context(|| format!("{prefix} names no loaded provider chunk"))?;
        if !provider_closure.contains(&chunk.chunk_id) {
            if !options.analysis.value_inputs.is_empty() {
                bail!(
                    "chunk_analysis_options.{chunk_name}.value_inputs provider is outside the existing analyzed import closure"
                );
            }
            if !options.analysis.initializer_call_effect_inputs.is_empty() {
                bail!(
                    "chunk_analysis_options.{chunk_name}.initializer_call_effect_inputs importer is outside the existing analyzed import closure"
                );
            }
            if !options.native_operation_inputs.is_empty()
                || !options.imported_primitive_array_spread_inputs.is_empty()
                || !options.analysis.native_call_identity_inputs.is_empty()
            {
                bail!(
                    "chunk_analysis_options.{chunk_name}.native_operation_inputs provider is outside the existing analyzed import closure"
                );
            }
            if !options.analysis.argument_inputs.is_empty() {
                bail!(
                    "chunk_analysis_options.{chunk_name}.argument_inputs cannot target an outside-closure provider"
                );
            }
            bail!(
                "chunk_analysis_options.{chunk_name}.no_accessor_install_callables cannot target an outside-closure provider"
            );
        }
        let provenance =
            graph_provenance.context("source premise sites require parser-input provenance")?;
        let source = provenance
            .chunk_sources
            .iter()
            .find(|source| source.chunk_id == *chunk_name)
            .with_context(|| format!("{prefix} provider has no parser-input provenance"))?;
        source_sites
            .source_sha256
            .insert(chunk_name.clone(), source.sha256.clone());
        for (index, contract) in options
            .analysis
            .initializer_call_effect_inputs
            .iter()
            .enumerate()
        {
            let request = format!(
                "chunk_analysis_options.{chunk_name}.initializer_call_effect_inputs[{index}]"
            );
            let provider = artifact
                .chunks
                .iter()
                .find(|candidate| {
                    artifact.chunk_table.name(candidate.chunk_id) == contract.provider_chunk
                })
                .with_context(|| format!("{request}.provider_chunk names no loaded chunk"))?;
            ensure!(
                provider_closure.contains(&provider.chunk_id),
                "{request}.provider_chunk is outside the analyzed import closure"
            );
        }
        let entry_file = chunk
            .js
            .get_file(&chunk.analysis.entry_file)
            .with_context(|| {
                format!("{prefix} provider entry is unavailable to whole-program analysis")
            })?;

        let parsed = entry_file.ast().with_context(|| {
            format!("{prefix} provider entry has no retained source-premise AST")
        })?;
        let selector = source_premise_selector(chunk_name, parsed, &prepared);
        let resolved =
            resolve_provider_source_sites(chunk_name, &selector, options, resolved_member_sites)?;
        let ProviderSourceSites {
            value_inputs,
            native_call_identity_inputs,
            initializer_call_effect_inputs,
            argument_inputs,
            no_accessor_install_callables,
            native_operation_inputs: operations,
        } = resolved;
        let (imported_array_spreads, provider_digests) = resolve_imported_array_spread_inputs(
            artifact,
            &provider_closure,
            source,
            &selector,
            &options.imported_primitive_array_spread_inputs,
            provenance,
            &prepared,
        )?;
        source_sites.source_sha256.extend(provider_digests);
        if !imported_array_spreads.is_empty() {
            source_sites
                .imported_primitive_array_spread_inputs
                .insert(chunk_name.clone(), imported_array_spreads);
        }
        if !value_inputs.is_empty() {
            source_sites
                .value_inputs
                .insert(chunk_name.clone(), value_inputs);
        }
        if !native_call_identity_inputs.is_empty() {
            source_sites
                .native_call_identity_inputs
                .insert(chunk_name.clone(), native_call_identity_inputs);
        }
        if !initializer_call_effect_inputs.is_empty() {
            source_sites
                .initializer_call_effect_inputs
                .insert(chunk_name.clone(), initializer_call_effect_inputs);
        }
        if !argument_inputs.is_empty() {
            source_sites
                .argument_inputs
                .insert(chunk_name.clone(), argument_inputs);
        }
        if !no_accessor_install_callables.is_empty() {
            source_sites
                .no_accessor_install_callables
                .insert(chunk_name.clone(), no_accessor_install_callables);
        }
        if !operations.is_empty() {
            native_operation_inputs.insert(chunk_name.clone(), operations);
        }
    }

    Ok((source_sites, native_operation_inputs))
}

fn resolve_imported_array_spread_inputs(
    artifact: &ChunkBundle,
    provider_closure: &BTreeSet<ChunkId>,
    source: &ChunkSource,
    consumer_selector: &SelectorChunk<'_>,
    contracts: &[spec::input_contracts::ImportedPrimitiveArraySpreadInputContract],
    graph_provenance: &GraphProvenance,
    prepared: &PreparedCensuses<'_>,
) -> Result<(
    Vec<analysis_vocabulary::inputs::AnalysisImportedPrimitiveArraySpreadInput>,
    BTreeMap<String, String>,
)> {
    let mut sites = Vec::with_capacity(contracts.len());
    let mut provider_digests = BTreeMap::new();
    for (input_index, contract) in contracts.iter().enumerate() {
        let request = format!(
            "chunk_analysis_options.{}.imported_primitive_array_spread_inputs[{input_index}]",
            source.chunk_id,
        );
        anyhow::ensure!(
            !contract.operands.is_empty(),
            "{request}.operands must contain at least one direct imported operand"
        );
        anyhow::ensure!(
            contract.lifetime.identity_unchanged_through_spread
                && contract
                    .lifetime
                    .dense_own_data_primitive_contents_unchanged_through_spread
                && contract
                    .lifetime
                    .ordinary_array_prototype_chain_unchanged_through_spread
                && contract
                    .lifetime
                    .no_own_iterator_or_accessors_through_spread
                && contract.lifetime.no_rebinding_through_spread,
            "{request}.lifetime must explicitly affirm every exact-spread lifetime premise"
        );
        anyhow::ensure!(
            contract.audited_source_sha256 == source.sha256,
            "{request} has stale audited_source_sha256"
        );
        let consumer_declaration =
            select_one_source_declaration(consumer_selector, &request, &contract.source_match)?;
        let selector_list = source_match_claim_member_selectors(&request, &contract.source_match)?;
        let mut matching_free_bindings = Vec::new();
        for selector in &selector_list {
            matching_free_bindings.extend(
                consumer_selector
                    .matcher()
                    .member_candidates(&request, &selector.parsed_selector)?
                    .into_iter()
                    .filter(|candidate| {
                        candidate.body_idx == consumer_declaration.body_index
                            && candidate.binding == consumer_declaration.binding
                    })
                    .map(|candidate| candidate.free_bindings),
            );
        }
        let [free_bindings] = matching_free_bindings.as_slice() else {
            anyhow::bail!(
                "{request}.source_match must uniquely capture the selected consumer's direct operands"
            );
        };
        let mut selector_locals = BTreeSet::new();
        let mut operands = Vec::with_capacity(contract.operands.len());
        for operand in &contract.operands {
            anyhow::ensure!(
                selector_locals.insert(operand.selector_local.clone()),
                "{request}.operands repeats selector_local `{}`",
                operand.selector_local
            );
            let consumer_binding = free_bindings
                .get(&operand.selector_local)
                .with_context(|| {
                    format!(
                        "{request}.operands selector_local `{}` is not a free identifier in source_match",
                        operand.selector_local
                    )
                })?
                .clone();
            let provider_id = artifact
                .chunk_table
                .get(&operand.provider_chunk)
                .with_context(|| {
                    format!(
                        "{request} names no loaded provider chunk alias `{}`",
                        operand.provider_chunk
                    )
                })?;
            anyhow::ensure!(
                provider_closure.contains(&provider_id),
                "{request} provider {} is outside the analyzed static import closure",
                operand.provider_chunk
            );
            let provider_chunk = artifact
                .find_chunk(provider_id)
                .with_context(|| format!("{request} provider chunk is unavailable"))?;
            let provider_source = graph_provenance
                .chunk_sources
                .iter()
                .find(|source| source.chunk_id == operand.provider_chunk)
                .with_context(|| {
                    format!(
                        "{request} provider {} has no parser-input provenance",
                        operand.provider_chunk
                    )
                })?;
            anyhow::ensure!(
                operand.audited_provider_source_sha256 == provider_source.sha256,
                "{request} provider {} has stale audited_provider_source_sha256",
                operand.provider_chunk
            );
            let provider_entry = provider_chunk
                .js
                .get_file(&provider_chunk.analysis.entry_file)
                .and_then(|file| file.ast())
                .with_context(|| {
                    format!(
                        "{request} provider {} has no retained source-premise AST",
                        operand.provider_chunk
                    )
                })?;
            let provider_selector =
                source_premise_selector(&operand.provider_chunk, provider_entry, prepared);
            let provider_declaration = select_one_source_declaration(
                &provider_selector,
                &format!("{request}.operands.provider_source_match"),
                &operand.provider_source_match,
            )?;
            provider_digests.insert(
                operand.provider_chunk.clone(),
                provider_source.sha256.clone(),
            );
            operands.push(
                analysis_vocabulary::inputs::AnalysisImportedPrimitiveArrayOperand {
                    selector_local: operand.selector_local.clone(),
                    consumer_binding,
                    provider_chunk: operand.provider_chunk.clone(),
                    provider_source_match: operand.provider_source_match.clone(),
                    provider_declaration,
                    audited_provider_source_sha256: operand.audited_provider_source_sha256.clone(),
                },
            );
        }
        sites.push(
            analysis_vocabulary::inputs::AnalysisImportedPrimitiveArraySpreadInput {
                consumer_declaration,
                source_match: contract.source_match.clone(),
                audited_source_sha256: contract.audited_source_sha256.clone(),
                operands,
                lifetime: contract.lifetime.clone(),
                note: contract.note.clone(),
            },
        );
    }
    Ok((sites, provider_digests))
}

struct ProviderSourceSites {
    value_inputs: Vec<SourceDeclaration>,
    native_call_identity_inputs: Vec<SourceDeclaration>,
    initializer_call_effect_inputs: Vec<SourceDeclaration>,
    argument_inputs: Vec<SourceDeclaration>,
    no_accessor_install_callables: Vec<SourceDeclaration>,
    native_operation_inputs: Vec<AnalysisNativeOperationInput>,
}

fn resolve_provider_source_sites(
    chunk_name: &str,
    selector_chunk: &SelectorChunk<'_>,
    options: &ProjectedChunkOptions,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Result<ProviderSourceSites> {
    let mut resolved = ProviderSourceSites {
        value_inputs: Vec::with_capacity(options.analysis.value_inputs.len()),
        native_call_identity_inputs: Vec::with_capacity(
            options.analysis.native_call_identity_inputs.len(),
        ),
        initializer_call_effect_inputs: Vec::with_capacity(
            options.analysis.initializer_call_effect_inputs.len(),
        ),
        argument_inputs: Vec::with_capacity(options.analysis.argument_inputs.len()),
        no_accessor_install_callables: Vec::with_capacity(
            options.analysis.no_accessor_install_callables.len(),
        ),
        native_operation_inputs: Vec::with_capacity(options.native_operation_inputs.len()),
    };

    for (index, contract) in options.analysis.value_inputs.iter().enumerate() {
        let request_id = format!("chunk_analysis_options.{chunk_name}.value_inputs[{index}]");
        resolved.value_inputs.push(select_one_source_declaration(
            selector_chunk,
            &request_id,
            &contract.source_match,
        )?);
    }
    for (index, contract) in options
        .analysis
        .native_call_identity_inputs
        .iter()
        .enumerate()
    {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.native_call_identity_inputs[{index}]");
        resolved
            .native_call_identity_inputs
            .push(select_one_source_declaration(
                selector_chunk,
                &request_id,
                &contract.source_match,
            )?);
    }
    for (index, contract) in options
        .analysis
        .initializer_call_effect_inputs
        .iter()
        .enumerate()
    {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.initializer_call_effect_inputs[{index}]");
        // Reuse the chunk's joint logical-member claim projection rather than
        // resolving a second selector for each initializer input.
        let site = resolve_premise_member_site(
            resolved_member_sites,
            &request_id,
            chunk_name,
            &contract.resolved_member.module,
            &contract.resolved_member.name,
        )?;
        let declaration = site.original_declaration(&request_id)?.clone();
        resolved.initializer_call_effect_inputs.push(declaration);
    }
    for (index, contract) in options.analysis.argument_inputs.iter().enumerate() {
        let request_id = format!("chunk_analysis_options.{chunk_name}.argument_inputs[{index}]");
        resolved.argument_inputs.push(select_one_source_declaration(
            selector_chunk,
            &request_id,
            &contract.source_match,
        )?);
    }
    for (index, contract) in options
        .analysis
        .no_accessor_install_callables
        .iter()
        .enumerate()
    {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.no_accessor_install_callables[{index}]");
        resolved
            .no_accessor_install_callables
            .push(select_one_source_declaration(
                selector_chunk,
                &request_id,
                &contract.source_match,
            )?);
    }
    for (index, contract) in options.native_operation_inputs.iter().enumerate() {
        let request_id =
            format!("chunk_analysis_options.{chunk_name}.native_operation_inputs[{index}]");
        let (declaration, selector) = match &contract.selector {
            SourceUnitSelector::SourceMatch(source_match) => {
                let declaration =
                    select_one_source_declaration(selector_chunk, &request_id, source_match)?;
                (
                    declaration,
                    NativeOperationSelectorProvenance::SourceMatch(source_match.clone()),
                )
            }
            SourceUnitSelector::ResolvedMember(member) => {
                let site = resolve_premise_member_site(
                    resolved_member_sites,
                    &request_id,
                    chunk_name,
                    &member.module,
                    &member.name,
                )?;
                let declaration = site.original_declaration(&request_id)?.clone();
                (
                    declaration.clone(),
                    NativeOperationSelectorProvenance::Declaration(declaration),
                )
            }
        };
        resolved
            .native_operation_inputs
            .push(AnalysisNativeOperationInput {
                declaration,
                selector,
                operation: contract.operation,
                audited_source_sha256: contract.audited_source_sha256.clone(),
            });
    }

    Ok(resolved)
}

pub fn resolve_premise_member_site<'a>(
    resolved_member_sites: &'a BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    request_id: &str,
    chunk_name: &str,
    module: &str,
    name: &str,
) -> Result<&'a ResolvedMemberSite> {
    let key = (module.to_owned(), name.to_owned());
    let sites = resolved_member_sites.get(&key).with_context(|| {
        format!(
            "{request_id}.resolved_member names no logical module member in chunk {chunk_name} (module {module}, member {name}); the reference may be stale after an owner rename or move"
        )
    })?;
    let mut chunk_sites = sites
        .iter()
        .filter(|site| site.provider_chunk == chunk_name);
    let site = chunk_sites.next().with_context(|| {
        let providers: BTreeSet<_> = sites
            .iter()
            .map(|site| site.provider_chunk.as_str())
            .collect();
        format!(
            "{request_id}.resolved_member belongs to another chunk(s) {providers:?}, not requested chunk {chunk_name} (module {module}, member {name})"
        )
    })?;
    ensure!(
        chunk_sites.next().is_none(),
        "{request_id}.resolved_member is ambiguous in chunk {chunk_name} (module {module}, member {name})"
    );
    Ok(site)
}
pub fn select_one_declared_binding(
    selector_chunk: &SelectorChunk<'_>,
    request_id: &str,
    claim: &SourceMatchClaim,
) -> Result<(usize, String)> {
    ensure!(
        claim.bindings.len() == 1,
        "{request_id}.source_match must select exactly one declared binding"
    );
    let spec::annotations::SourceMatchBinding::Local(local) = &claim.bindings[0] else {
        bail!("{request_id}.source_match does not accept detailed name/rename payloads");
    };
    let mut selectors = source_match_claim_member_selectors(request_id, claim)?;
    ensure!(
        selectors.len() == 1,
        "{request_id}.source_match must produce one selector"
    );
    let selector = selectors.pop().expect("one selector was checked");
    ensure!(
        selector
            .parsed_selector
            .declared_binding_names()
            .iter()
            .any(|name| name == local),
        "{request_id}.source_match binding `{local}` must be declared by its pattern"
    );
    let parsed_selector = selector.parsed_selector.clone();
    let selector_module = spec_resolution::spec_module(
        request_id.to_owned(),
        vec![spec_resolution::member_from_source_match(
            selector.export_name,
            selector.parsed_selector,
        )],
        Vec::new(),
    );
    let resolution = selector_chunk.resolve(&[selector_module])?;
    let outcome = resolution
        .outcome(0, EntityIndex::Member(0))
        .with_context(|| format!("{request_id}.source_match produced no selector outcome"))?;
    let (representative, binding) = match &outcome.outcome {
        Outcome::Resolved {
            body_index,
            binding: Some(binding),
            ..
        } => (*body_index, binding.clone()),
        other => bail!(
            "{request_id}.source_match must select exactly one declared binding; selector result: {other:?}"
        ),
    };
    let mut sites = selector_chunk
        .matcher()
        .member_candidates(request_id, &parsed_selector)?
        .into_iter()
        .filter(|matched| matched.binding == binding)
        .map(|matched| matched.body_idx)
        .collect::<BTreeSet<_>>();
    match sites.len() {
        1 => Ok((sites.pop_first().expect("one site"), binding)),
        0 => bail!(
            "{request_id}.source_match resolved `{binding}` to statement #{representative} but its template matches none of the binding's declaration sites"
        ),
        _ => bail!(
            "{request_id}.source_match matches `{binding}` at several declaration sites (statements {}); a source-unit endpoint must pin one",
            sites
                .iter()
                .map(|site| format!("#{site}"))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn select_one_source_declaration(
    selector_chunk: &SelectorChunk<'_>,
    request_id: &str,
    claim: &SourceMatchClaim,
) -> Result<SourceDeclaration> {
    let (body_index, binding) = select_one_declared_binding(selector_chunk, request_id, claim)?;
    Ok(SourceDeclaration {
        binding,
        body_index,
    })
}
