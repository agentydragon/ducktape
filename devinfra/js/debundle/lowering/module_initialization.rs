//! Check the bounded provider-cell initialization obligations against the exact
//! prepared file layout and requests that emission consumes. Source completion
//! is a prerequisite, never the candidate proof.

use std::collections::{BTreeMap, BTreeSet};

use analysis::facts::StatementFacts;
use artifact::reports::{StaticModuleRequest, StaticModuleRequestMode};
use artifact::{ArtifactIndexes, ChunkBundle, ImportReferenceKind, JsFileBody};
use gate::{CandidateModuleInitializationEvidence, ModuleInitializationRefusal};
use source_identity::{ChunkId, ModuleCellId};

use crate::lower::PreparedCandidateEmissionPlan;

type ModuleFile = (ChunkId, String);
type ModuleGraph = BTreeMap<ModuleFile, Vec<ModuleFile>>;

pub(super) struct ModuleInitializationContext<'a, 'output> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) indexes: &'a ArtifactIndexes,
    pub(super) consumer: ChunkId,
    pub(super) materialized_chunks: &'a BTreeSet<ChunkId>,
    pub(super) vendor_unchanged: bool,
    pub(super) prepared: Option<&'a PreparedCandidateEmissionPlan<'output>>,
    pub(super) source_initialized_cells: Option<&'a BTreeSet<ModuleCellId>>,
}

pub(super) fn assess_module_initialization(
    context: ModuleInitializationContext<'_, '_>,
    facts: &[StatementFacts],
) -> CandidateModuleInitializationEvidence {
    let required: BTreeSet<_> = facts
        .iter()
        .flat_map(StatementFacts::required_initialized_module_cells)
        .collect();
    if required.is_empty() {
        return CandidateModuleInitializationEvidence::default();
    }
    match preserve_initialization(&context, &required) {
        Ok(()) => CandidateModuleInitializationEvidence {
            preserved_cells: required,
            refusal: None,
        },
        Err(refusal) => CandidateModuleInitializationEvidence {
            preserved_cells: BTreeSet::new(),
            refusal: Some(refusal),
        },
    }
}

fn preserve_initialization(
    context: &ModuleInitializationContext<'_, '_>,
    required: &BTreeSet<ModuleCellId>,
) -> Result<(), ModuleInitializationRefusal> {
    use ModuleInitializationRefusal::{
        ForeignInitializationChanged, PreparedEmissionUnavailable, ProviderClosureChanged,
        ProviderIncompleteAtConsumer, UnsupportedCandidateGraph,
    };

    let prepared = context.prepared.ok_or(PreparedEmissionUnavailable)?;
    // This first stage proves one replaced chunk against retained provider
    // files. Multiple prepared chunks and vendor transforms need a joint plan.
    if context.materialized_chunks != &BTreeSet::from([context.consumer])
        || !context.vendor_unchanged
    {
        return Err(UnsupportedCandidateGraph);
    }
    if !context
        .source_initialized_cells
        .is_some_and(|cells| required.is_subset(cells))
    {
        return Err(ProviderClosureChanged);
    }

    let mut source = ModuleGraph::new();
    for chunk in &context.artifact.chunks {
        let runtime = &chunk.analysis.runtime_facts;
        if chunk.js.files.len() != 1
            || !runtime.module_kind.supports_static_esm_closure()
            || runtime.has_top_level_await
            || runtime.has_cjs_interop
        {
            return Err(UnsupportedCandidateGraph);
        }
        let file = &chunk.analysis.entry_file;
        let targets = resolve_requests(&runtime.static_requests, |specifier| {
            context.indexes.resolve_runtime_import_reference(
                specifier,
                chunk.chunk_id,
                file,
                &context.artifact.chunk_table,
            )
        })?;
        source.insert((chunk.chunk_id, file.clone()), targets);
    }
    if source
        .values()
        .flatten()
        .any(|target| !source.contains_key(target))
    {
        return Err(UnsupportedCandidateGraph);
    }
    let consumer_entry = context
        .artifact
        .find_chunk(context.consumer)
        .ok_or(UnsupportedCandidateGraph)?
        .analysis
        .entry_file
        .clone();
    let original_entry = (context.consumer, consumer_entry);
    let source_order = evaluation_order(&source, &original_entry, None)?;

    let inventories: Vec<_> = prepared.static_request_inventories().collect();
    let Some((entry_file, _)) = inventories.first() else {
        return Err(PreparedEmissionUnavailable);
    };
    let layout: Vec<_> = prepared.output_file_layout().collect();
    if inventories.len() != layout.len() {
        return Err(UnsupportedCandidateGraph);
    }
    let resolver = context
        .indexes
        .project_replaced_chunk_layout(
            context.consumer,
            entry_file,
            layout,
            &context.artifact.chunk_table,
        )
        .map_err(|_| UnsupportedCandidateGraph)?;
    let mut candidate = ModuleGraph::new();
    for chunk in &context.artifact.chunks {
        if chunk.chunk_id == context.consumer {
            continue;
        }
        let file = &chunk.analysis.entry_file;
        let original = chunk.js.get_file(file).ok_or(UnsupportedCandidateGraph)?;
        // Only retained AST entries take the late SourceCanonicalizer pass.
        // A raw pass-through source must already name actual output files.
        let canonicalized = matches!(&original.body, JsFileBody::Ast(_));
        let targets =
            resolve_requests(&chunk.analysis.runtime_facts.static_requests, |specifier| {
                resolver
                    .resolve_runtime_import_reference(specifier, chunk.chunk_id, file)
                    .filter(|target| {
                        canonicalized || target.kind == ImportReferenceKind::ArtifactPath
                    })
            })?;
        candidate.insert((chunk.chunk_id, file.clone()), targets);
    }
    for (index, (file, requests)) in inventories.iter().enumerate() {
        let targets = resolve_requests(requests, |specifier| {
            resolver
                .resolve_runtime_import_reference(specifier, context.consumer, file)
                // Materialized modules skip SourceCanonicalizer. In particular,
                // an unre-based named/star reexport is not a runtime edge merely
                // because original source-path fallback finds its provider.
                .filter(|target| index == 0 || target.kind == ImportReferenceKind::ArtifactPath)
        })?;
        if candidate
            .insert((context.consumer, (*file).to_owned()), targets)
            .is_some()
        {
            return Err(UnsupportedCandidateGraph);
        }
    }
    if candidate
        .values()
        .flatten()
        .any(|target| !candidate.contains_key(target))
    {
        return Err(UnsupportedCandidateGraph);
    }

    let mut providers = BTreeSet::new();
    let mut closure = BTreeSet::new();
    for cell in required {
        let provider = context
            .artifact
            .find_chunk(cell.chunk)
            .ok_or(ProviderClosureChanged)?;
        let entry = (cell.chunk, provider.analysis.entry_file.clone());
        if cell.chunk == context.consumer || !source_order.contains(&entry) {
            return Err(ProviderClosureChanged);
        }
        providers.insert(entry.clone());
        let mut pending = vec![entry];
        while let Some(module) = pending.pop() {
            if !closure.insert(module.clone()) {
                continue;
            }
            if context.materialized_chunks.contains(&module.0)
                || source.get(&module) != candidate.get(&module)
            {
                return Err(ProviderClosureChanged);
            }
            pending.extend(
                source
                    .get(&module)
                    .ok_or(ProviderClosureChanged)?
                    .iter()
                    .cloned(),
            );
        }
    }

    // A second foreign consumer may mutate the shared provider between calls.
    // Until linked events model that chronology, reject every such context in
    // the complete source graph, including callers through the entry chunk.
    let mut reverse = BTreeMap::<&ModuleFile, Vec<&ModuleFile>>::new();
    for (module, targets) in &source {
        for target in targets {
            reverse.entry(target).or_default().push(module);
        }
    }
    let mut reaching = closure.clone();
    let mut pending: Vec<_> = closure.iter().collect();
    while let Some(module) = pending.pop() {
        for parent in reverse.get(module).into_iter().flatten() {
            if reaching.insert((**parent).clone()) {
                pending.push(*parent);
            }
        }
    }
    if reaching
        .iter()
        .any(|module| module.0 != context.consumer && !closure.contains(module))
    {
        return Err(ForeignInitializationChanged);
    }

    let candidate_entry = (context.consumer, (*entry_file).to_owned());
    let candidate_order = evaluation_order(
        &candidate,
        &candidate_entry,
        Some((context.consumer, &providers)),
    )?;
    if source_order
        .iter()
        .filter(|module| module.0 != context.consumer)
        .ne(candidate_order
            .iter()
            .filter(|module| module.0 != context.consumer))
    {
        return Err(ForeignInitializationChanged);
    }
    if !providers
        .iter()
        .all(|provider| candidate_order.contains(provider))
    {
        return Err(ProviderIncompleteAtConsumer);
    }
    Ok(())
}

fn resolve_requests(
    requests: &[StaticModuleRequest],
    mut resolve: impl FnMut(&str) -> Option<artifact::ResolvedImportReference>,
) -> Result<Vec<ModuleFile>, ModuleInitializationRefusal> {
    requests
        .iter()
        .map(|request| {
            if request.mode != StaticModuleRequestMode::OrdinaryEsm {
                return Err(ModuleInitializationRefusal::UnsupportedCandidateGraph);
            }
            let target = resolve(&request.specifier)
                .ok_or(ModuleInitializationRefusal::UnsupportedCandidateGraph)?;
            Ok((target.target_chunk_id, target.target_file))
        })
        .collect()
}

/// DFS postorder is the synchronous ESM body order for an acyclic graph. Check
/// provider completion before every consumer file body, even one whose own
/// source units did not consume a cell summary. Duplicate requests keep their
/// first evaluation position.
fn evaluation_order(
    graph: &ModuleGraph,
    entry: &ModuleFile,
    consumer: Option<(ChunkId, &BTreeSet<ModuleFile>)>,
) -> Result<Vec<ModuleFile>, ModuleInitializationRefusal> {
    use ModuleInitializationRefusal::{ProviderIncompleteAtConsumer, UnsupportedCandidateGraph};

    let mut stack = vec![(entry.clone(), 0)];
    let mut active = BTreeSet::from([entry.clone()]);
    let mut complete = BTreeSet::new();
    let mut order = Vec::new();
    while let Some((module, next)) = stack.last_mut() {
        let requests = graph.get(module).ok_or(UnsupportedCandidateGraph)?;
        if let Some(target) = requests.get(*next) {
            *next += 1;
            if complete.contains(target) {
                continue;
            }
            if !active.insert(target.clone()) {
                return Err(UnsupportedCandidateGraph);
            }
            stack.push((target.clone(), 0));
        } else {
            if let Some((consumer_chunk, providers)) = consumer
                && module.0 == consumer_chunk
                && !providers.is_subset(&complete)
            {
                return Err(ProviderIncompleteAtConsumer);
            }
            let module = module.clone();
            stack.pop();
            active.remove(&module);
            complete.insert(module.clone());
            order.push(module);
        }
    }
    Ok(order)
}
