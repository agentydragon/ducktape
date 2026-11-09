//! The saved program-summary boundary. Runtime verdicts and fields containing
//! SWC bindings have explicit artifact representations. Other summary fields
//! are borrowed on write and owned on read; Id-free data keeps its existing
//! codec representation.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use analysis::analysis_inputs::saved::{
    ProviderSourceInventory, SavedCallableSummary, SavedNativeIntegrity,
};
use analysis::analysis_inputs::{
    AdmittedHostModuleDependency, ArgumentInputEvidence, ImportedPrimitiveRead,
    InferredSetConstruction, InferredSetOperation, InitializerCallEffectInputRef,
    NativeOperationInputEvidence, PrimitiveCellInvariant, ResolvedArgumentInput,
    ResolvedBindingValueInput, ResolvedInitializerCallEffectInput, ResolvedNativeOperationInput,
    ResolvedProviderDependencyClosure, StableSetIterableUse, ValueInputEvidence,
};
use analysis::facts::abstract_domain::value::Value;
use analysis_codec::{ContextOrdinals, SavedBinding, SavedChunk, SavedModuleCell};
use anyhow::{Context, Result, bail, ensure};
use js_ast::ParsedGlobalUses;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;

use super::program_summary::ProgramSummary;
use analysis::purity::saved::{PurityNodeId, PuritySnapshot, RestoredPurities};

type RetainedCallbackSignatures = BTreeMap<String, Vec<spec::RetainedCallbackSignature>>;

fn validate_provider_closure_namespace(
    closure: Option<&ResolvedProviderDependencyClosure>,
    ordinals: &ContextOrdinals,
) -> Result<()> {
    let Some(closure) = closure else {
        return Ok(());
    };
    ensure!(
        closure.is_well_formed(),
        "saved provider closure is internally inconsistent"
    );
    for module in &closure.modules {
        ordinals.restore_chunk(SavedChunk(module.clone()))?;
    }
    Ok(())
}

#[derive(Serialize, Deserialize)]
pub(super) struct SavedProgramSummary<'a> {
    bindings: BTreeMap<String, BTreeMap<String, PurityNodeId>>,
    global_uses: Vec<SavedGlobalUsesEntry>,
    values: Cow<'a, BTreeMap<String, BTreeMap<String, Value>>>,
    primitive_reads: Cow<'a, BTreeMap<String, BTreeMap<String, ImportedPrimitiveRead>>>,
    members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    fluent: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    constructors: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    imported_callables: BTreeMap<String, BTreeMap<String, SavedCallableSummary<'a>>>,
    module_cells: BTreeMap<String, Vec<SavedModuleCellEntry>>,
    module_cell_invariants: Vec<SavedModuleCellInvariant>,
    source_initialized_module_cells: BTreeMap<String, Vec<SavedModuleCell>>,
    /// Optional only for legacy summaries that predate explicit host-leaf
    /// closure evidence. Missing does not mean an empty host dependency set.
    #[serde(default)]
    source_initialized_host_dependencies:
        Option<BTreeMap<String, Vec<AdmittedHostModuleDependency>>>,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    native_integrity: Option<SavedNativeIntegrity<'a>>,
    bundle_globals: Cow<'a, analysis::analysis_inputs::BundleGlobalNames>,
    value_inputs: BTreeMap<String, Vec<SavedValueInput>>,
    native_operation_inputs: BTreeMap<String, Vec<SavedNativeOperationInput>>,
    initializer_call_effect_inputs: BTreeMap<String, Vec<SavedInitializerCallEffectInput>>,
    imported_primitive_array_spread_inputs:
        BTreeMap<String, Vec<SavedImportedPrimitiveArraySpreadInput>>,
    stable_set_spreads: BTreeMap<String, BTreeMap<u32, SavedStableSetIterableUse>>,
    inferred_set_constructions: BTreeMap<String, BTreeMap<u32, SavedInferredSetConstruction>>,
    argument_inputs: BTreeMap<String, Vec<SavedArgumentInput>>,
    inline_callback_factories: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    callback_factory_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    retained_callback_factories: Cow<'a, BTreeMap<String, RetainedCallbackSignatures>>,
    retained_callback_factory_members:
        Cow<'a, BTreeMap<String, BTreeMap<String, RetainedCallbackSignatures>>>,
    retained_callback_constructors: Cow<'a, BTreeMap<String, RetainedCallbackSignatures>>,
    no_reentry_exports: Cow<'a, BTreeMap<String, BTreeSet<String>>>,
    no_reentry_callback_exports: Cow<'a, BTreeMap<String, RetainedCallbackSignatures>>,
    no_reentry_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    no_reentry_record_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
    namespace_independent_members: Cow<'a, BTreeMap<String, BTreeMap<String, BTreeSet<String>>>>,
}

#[derive(Serialize, Deserialize)]
struct SavedModuleCellEntry {
    binding: SavedBinding,
    cell: SavedModuleCell,
}

#[derive(Serialize, Deserialize)]
struct SavedModuleCellInvariant {
    cell: SavedModuleCell,
    invariant: PrimitiveCellInvariant,
}

#[derive(Serialize, Deserialize)]
struct SavedGlobalUsesEntry {
    chunk: SavedChunk,
    manifest: SavedGlobalUses,
}

#[derive(Serialize, Deserialize)]
struct SavedGlobalUses {
    source_file: String,
    uses: Vec<SavedGlobalUse>,
    dynamic_scope: bool,
}

#[derive(Serialize, Deserialize)]
struct SavedGlobalUse {
    binding: SavedBinding,
    spans: Vec<Span>,
}

impl SavedGlobalUses {
    fn snapshot(manifest: &ParsedGlobalUses, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            source_file: manifest.source_file.clone(),
            uses: manifest
                .uses
                .iter()
                .map(|(binding, spans)| {
                    Ok(SavedGlobalUse {
                        binding: ordinals.snapshot_binding(binding)?,
                        spans: spans.clone(),
                    })
                })
                .collect::<Result<_>>()?,
            dynamic_scope: manifest.dynamic_scope,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ParsedGlobalUses> {
        let mut uses = BTreeMap::new();
        for entry in self.uses {
            let binding = ordinals.restore_binding(entry.binding)?;
            if uses.insert(binding.clone(), entry.spans).is_some() {
                bail!("duplicate saved global-use binding {binding:?}");
            }
        }
        Ok(ParsedGlobalUses {
            source_file: self.source_file,
            uses,
            dynamic_scope: self.dynamic_scope,
        })
    }
}

/// Check that restored inferred global-use data still describes the current
/// parser output. The saved form preserves address ordinals, but this check
/// also binds the complete inventory, spans, source path and dynamic-scope
/// bit to the fresh shallow analysis.
pub(super) fn validate_global_uses(
    restored: &BTreeMap<String, Arc<ParsedGlobalUses>>,
    fresh: &BTreeMap<String, Option<&ParsedGlobalUses>>,
    expected_chunks: &BTreeSet<String>,
) -> Result<()> {
    let restored_chunks: BTreeSet<_> = restored.keys().cloned().collect();
    ensure!(
        &restored_chunks == expected_chunks,
        "saved global-use manifest inventory differs from prepared program chunks: expected {expected_chunks:?}, got {restored_chunks:?}"
    );
    let fresh_chunks: BTreeSet<_> = fresh.keys().cloned().collect();
    ensure!(
        &fresh_chunks == expected_chunks,
        "fresh global-use context inventory differs from prepared program chunks: expected {expected_chunks:?}, got {fresh_chunks:?}"
    );
    for chunk in expected_chunks {
        let saved = restored
            .get(chunk)
            .with_context(|| format!("missing saved global-use manifest for {chunk}"))?;
        let current = fresh
            .get(chunk)
            .with_context(|| format!("missing fresh global-use context for {chunk}"))?
            .with_context(|| format!("fresh source context is unavailable for {chunk}"))?;
        ensure!(
            saved.source_file == current.source_file,
            "saved global-use source path differs from current source for {chunk}"
        );
        ensure!(
            saved.uses == current.uses,
            "saved global-use binding spans differ from current source for {chunk}"
        );
        ensure!(
            saved.dynamic_scope == current.dynamic_scope,
            "saved global-use dynamic-scope classification differs from current source for {chunk}"
        );
    }
    Ok(())
}

impl<'a> SavedProgramSummary<'a> {
    pub(super) fn snapshot(
        summary: &'a ProgramSummary,
        ordinals: &ContextOrdinals,
        purities: &mut PuritySnapshot<'a>,
    ) -> Result<Self> {
        let source_inventories = summary.provider_programs.source_inventories();
        let prepared_chunks: BTreeSet<_> = summary.bindings.keys().cloned().collect();
        let global_use_chunks: BTreeSet<_> = summary.global_uses.keys().cloned().collect();
        if prepared_chunks != global_use_chunks {
            bail!(
                "global-use manifest inventory differs from prepared program chunks: expected {prepared_chunks:?}, got {global_use_chunks:?}"
            );
        }
        let bindings = summary
            .bindings
            .iter()
            .map(|(chunk, bindings)| {
                (
                    chunk.clone(),
                    bindings
                        .iter()
                        .map(|(name, purity)| (name.clone(), purities.snapshot(purity)))
                        .collect(),
                )
            })
            .collect();
        let global_uses = summary
            .global_uses
            .iter()
            .map(|(chunk, manifest)| {
                let chunk_id = ordinals.restore_chunk(SavedChunk(chunk.clone()))?;
                Ok(SavedGlobalUsesEntry {
                    chunk: ordinals.snapshot_chunk(chunk_id)?,
                    manifest: SavedGlobalUses::snapshot(manifest, ordinals)?,
                })
            })
            .collect::<Result<_>>()?;
        for read in summary.primitive_reads.values().flat_map(BTreeMap::values) {
            validate_provider_closure_namespace(read.provider_closure.as_ref(), ordinals)?;
        }
        let value_inputs = summary
            .value_inputs
            .iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(|input| SavedValueInput::snapshot(input, ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let imported_callables = summary
            .imported_callables
            .iter()
            .map(|(chunk, callables)| {
                Ok((
                    chunk.clone(),
                    callables
                        .iter()
                        .map(|(name, callable)| {
                            Ok((
                                name.clone(),
                                SavedCallableSummary::snapshot(
                                    callable,
                                    ordinals,
                                    &source_inventories,
                                )?,
                            ))
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let module_cells = summary
            .module_cells
            .iter()
            .map(|(chunk, cells)| {
                ordinals.restore_chunk(SavedChunk(chunk.clone()))?;
                Ok((
                    chunk.clone(),
                    cells
                        .iter()
                        .map(|(binding, cell)| {
                            Ok(SavedModuleCellEntry {
                                binding: ordinals.snapshot_binding(binding)?,
                                cell: ordinals.snapshot_module_cell(*cell)?,
                            })
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let module_cell_invariants = summary
            .module_cell_invariants
            .iter()
            .map(|(cell, invariant)| {
                Ok(SavedModuleCellInvariant {
                    cell: ordinals.snapshot_module_cell(*cell)?,
                    invariant: invariant.clone(),
                })
            })
            .collect::<Result<_>>()?;
        let source_initialized_module_cells = summary
            .source_initialized_module_cells
            .iter()
            .map(|(importer, cells)| {
                ordinals
                    .restore_chunk(SavedChunk(importer.clone()))
                    .map(|_| ())?;
                Ok((
                    importer.clone(),
                    cells
                        .iter()
                        .map(|cell| ordinals.snapshot_module_cell(*cell))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let source_initialized_host_dependencies = summary
            .source_initialized_host_dependencies
            .as_ref()
            .map(|dependencies_by_importer| {
                dependencies_by_importer
                    .iter()
                    .map(|(importer, dependencies)| {
                        ordinals.restore_chunk(SavedChunk(importer.clone()))?;
                        for dependency in dependencies {
                            ordinals.restore_chunk(SavedChunk(dependency.source_module.clone()))?;
                            ensure!(
                                dependency.is_well_formed(),
                                "saved source-initialized host dependency is malformed for importer {importer}"
                            );
                        }
                        Ok((importer.clone(), dependencies.iter().cloned().collect()))
                    })
                    .collect::<Result<_>>()
            })
            .transpose()?;
        let native_operation_inputs = summary
            .native_operation_inputs
            .iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(|input| SavedNativeOperationInput::snapshot(input, ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let initializer_call_effect_inputs = summary
            .initializer_call_effect_inputs
            .iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(|input| SavedInitializerCallEffectInput::snapshot(input, ordinals))
                        .collect::<Result<Vec<_>>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let imported_primitive_array_spread_inputs = summary
            .imported_primitive_array_spread_inputs
            .iter()
            .map(|(chunk, inputs)| {
                (
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(SavedImportedPrimitiveArraySpreadInput::snapshot)
                        .collect(),
                )
            })
            .collect();
        let stable_set_spreads = summary
            .stable_set_spreads
            .iter()
            .map(|(chunk, spreads)| {
                Ok((
                    chunk.clone(),
                    spreads
                        .iter()
                        .map(|(site, spread)| {
                            Ok((
                                *site,
                                SavedStableSetIterableUse::snapshot(spread, ordinals)?,
                            ))
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let inferred_set_constructions = summary
            .inferred_set_constructions
            .iter()
            .map(|(chunk, constructions)| {
                Ok((
                    chunk.clone(),
                    constructions
                        .iter()
                        .map(|(site, construction)| {
                            Ok((
                                *site,
                                SavedInferredSetConstruction::snapshot(construction, ordinals)?,
                            ))
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let argument_inputs = summary
            .argument_inputs
            .iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk.clone(),
                    inputs
                        .iter()
                        .map(|input| SavedArgumentInput::snapshot(input, ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        Ok(Self {
            bindings,
            global_uses,
            value_inputs,
            values: Cow::Borrowed(&summary.values),
            primitive_reads: Cow::Borrowed(&summary.primitive_reads),
            members: Cow::Borrowed(&summary.members),
            fluent: Cow::Borrowed(&summary.fluent),
            constructors: Cow::Borrowed(&summary.constructors),
            imported_callables,
            module_cells,
            module_cell_invariants,
            source_initialized_module_cells,
            source_initialized_host_dependencies,
            native_integrity: summary
                .native_integrity
                .as_ref()
                .map(SavedNativeIntegrity::snapshot),
            bundle_globals: Cow::Borrowed(&summary.bundle_globals),
            native_operation_inputs,
            initializer_call_effect_inputs,
            imported_primitive_array_spread_inputs,
            stable_set_spreads,
            inferred_set_constructions,
            argument_inputs,
            inline_callback_factories: Cow::Borrowed(&summary.inline_callback_factories),
            callback_factory_members: Cow::Borrowed(&summary.callback_factory_members),
            retained_callback_factories: Cow::Borrowed(&summary.retained_callback_factories),
            retained_callback_factory_members: Cow::Borrowed(
                &summary.retained_callback_factory_members,
            ),
            retained_callback_constructors: Cow::Borrowed(&summary.retained_callback_constructors),
            no_reentry_exports: Cow::Borrowed(&summary.no_reentry_exports),
            no_reentry_callback_exports: Cow::Borrowed(&summary.no_reentry_callback_exports),
            no_reentry_members: Cow::Borrowed(&summary.no_reentry_members),
            no_reentry_record_members: Cow::Borrowed(&summary.no_reentry_record_members),
            namespace_independent_members: Cow::Borrowed(&summary.namespace_independent_members),
        })
    }

    pub(super) fn restore(
        self,
        ordinals: &ContextOrdinals,
        purities: &RestoredPurities,
        source_inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<ProgramSummary> {
        let prepared_chunks: BTreeSet<_> = self.bindings.keys().cloned().collect();
        for chunk in &prepared_chunks {
            ordinals.restore_chunk(SavedChunk(chunk.clone()))?;
        }
        let mut global_uses = BTreeMap::new();
        for entry in self.global_uses {
            let SavedChunk(chunk) = entry.chunk;
            ordinals.restore_chunk(SavedChunk(chunk.clone()))?;
            let manifest = Arc::new(entry.manifest.restore(ordinals)?);
            if global_uses.insert(chunk.clone(), manifest).is_some() {
                bail!("duplicate saved global-use chunk {chunk}");
            }
        }
        let global_use_chunks: BTreeSet<_> = global_uses.keys().cloned().collect();
        if prepared_chunks != global_use_chunks {
            bail!(
                "saved global-use manifest inventory differs from prepared program chunks: expected {prepared_chunks:?}, got {global_use_chunks:?}"
            );
        }
        for read in self
            .primitive_reads
            .as_ref()
            .values()
            .flat_map(BTreeMap::values)
        {
            validate_provider_closure_namespace(read.provider_closure.as_ref(), ordinals)?;
        }
        let bindings = self
            .bindings
            .into_iter()
            .map(|(chunk, bindings)| {
                Ok((
                    chunk,
                    bindings
                        .into_iter()
                        .map(|(name, id)| Ok((name, purities.get(id)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let value_inputs = self
            .value_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk,
                    inputs
                        .into_iter()
                        .map(|input| input.restore(ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let imported_callables = self
            .imported_callables
            .into_iter()
            .map(|(chunk, callables)| {
                Ok((
                    chunk,
                    callables
                        .into_iter()
                        .map(|(name, callable)| {
                            Ok((name, callable.restore(ordinals, source_inventories)?))
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let module_cells = self
            .module_cells
            .into_iter()
            .map(|(chunk, entries)| {
                ordinals.restore_chunk(SavedChunk(chunk.clone()))?;
                let mut cells = BTreeMap::new();
                for entry in entries {
                    let binding = ordinals.restore_binding(entry.binding)?;
                    let cell = ordinals.restore_module_cell(entry.cell)?;
                    if cells.insert(binding, cell).is_some() {
                        bail!("duplicate saved module-cell binding in chunk {chunk}");
                    }
                }
                Ok((chunk, cells))
            })
            .collect::<Result<_>>()?;
        let mut module_cell_invariants = BTreeMap::new();
        for entry in self.module_cell_invariants {
            let cell = ordinals.restore_module_cell(entry.cell)?;
            if module_cell_invariants
                .insert(cell, entry.invariant)
                .is_some()
            {
                bail!("duplicate saved module-cell invariant for {cell:?}");
            }
        }
        let source_initialized_module_cells = self
            .source_initialized_module_cells
            .into_iter()
            .map(|(importer, saved_cells)| {
                ordinals.restore_chunk(SavedChunk(importer.clone()))?;
                let mut cells = BTreeSet::new();
                for saved_cell in saved_cells {
                    let cell = ordinals.restore_module_cell(saved_cell)?;
                    if !cells.insert(cell) {
                        bail!(
                            "duplicate saved source-initialized module cell {cell:?} for importer {importer}"
                        );
                    }
                }
                Ok((importer, cells))
            })
            .collect::<Result<_>>()?;
        let source_initialized_host_dependencies = self
            .source_initialized_host_dependencies
            .map(|dependencies_by_importer| {
                dependencies_by_importer
                    .into_iter()
                    .map(|(importer, dependencies)| {
                        ordinals.restore_chunk(SavedChunk(importer.clone()))?;
                        let mut restored = BTreeSet::new();
                        for dependency in dependencies {
                            ordinals.restore_chunk(SavedChunk(dependency.source_module.clone()))?;
                            if !restored.insert(dependency) {
                                bail!("duplicate saved host dependency for importer {importer}");
                            }
                        }
                        Ok((importer, restored))
                    })
                    .collect::<Result<_>>()
            })
            .transpose()?;
        let native_operation_inputs = self
            .native_operation_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk,
                    inputs
                        .into_iter()
                        .map(|input| input.restore(ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let initializer_call_effect_inputs = self
            .initializer_call_effect_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk,
                    inputs
                        .into_iter()
                        .map(|input| input.restore(ordinals))
                        .collect::<Result<Vec<_>>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let imported_primitive_array_spread_inputs = self
            .imported_primitive_array_spread_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                (
                    chunk,
                    inputs
                        .into_iter()
                        .map(SavedImportedPrimitiveArraySpreadInput::restore)
                        .collect(),
                )
            })
            .collect();
        let stable_set_spreads = self
            .stable_set_spreads
            .into_iter()
            .map(|(chunk, spreads)| {
                Ok((
                    chunk,
                    spreads
                        .into_iter()
                        .map(|(site, spread)| Ok((site, spread.restore(ordinals)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let inferred_set_constructions = self
            .inferred_set_constructions
            .into_iter()
            .map(|(chunk, constructions)| {
                Ok((
                    chunk,
                    constructions
                        .into_iter()
                        .map(|(site, construction)| Ok((site, construction.restore(ordinals)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        let argument_inputs = self
            .argument_inputs
            .into_iter()
            .map(|(chunk, inputs)| {
                Ok((
                    chunk,
                    inputs
                        .into_iter()
                        .map(|input| input.restore(ordinals))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?;
        Ok(ProgramSummary {
            provider_programs: Default::default(),
            bindings,
            global_uses,
            value_inputs,
            values: self.values.into_owned(),
            primitive_reads: self.primitive_reads.into_owned(),
            members: self.members.into_owned(),
            fluent: self.fluent.into_owned(),
            constructors: self.constructors.into_owned(),
            imported_callables,
            module_cells,
            module_cell_invariants,
            source_initialized_module_cells,
            source_initialized_host_dependencies,
            native_integrity: self
                .native_integrity
                .map(SavedNativeIntegrity::restore)
                .transpose()?,
            bundle_globals: self.bundle_globals.into_owned(),
            native_operation_inputs,
            initializer_call_effect_inputs,
            imported_primitive_array_spread_inputs,
            stable_set_spreads,
            inferred_set_constructions,
            argument_inputs,
            inline_callback_factories: self.inline_callback_factories.into_owned(),
            callback_factory_members: self.callback_factory_members.into_owned(),
            retained_callback_factories: self.retained_callback_factories.into_owned(),
            retained_callback_factory_members: self.retained_callback_factory_members.into_owned(),
            retained_callback_constructors: self.retained_callback_constructors.into_owned(),
            no_reentry_exports: self.no_reentry_exports.into_owned(),
            no_reentry_callback_exports: self.no_reentry_callback_exports.into_owned(),
            no_reentry_members: self.no_reentry_members.into_owned(),
            no_reentry_record_members: self.no_reentry_record_members.into_owned(),
            namespace_independent_members: self.namespace_independent_members.into_owned(),
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedValueInput {
    evidence: ValueInputEvidence,
    binding: SavedBinding,
    declaration_span: Span,
    initializer_span: Span,
    initializer_ordinal: SourceUnitOrdinal,
}

impl SavedValueInput {
    fn snapshot(input: &ResolvedBindingValueInput, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            evidence: input.evidence.clone(),
            binding: ordinals.snapshot_binding(&input.binding)?,
            declaration_span: input.declaration_span,
            initializer_span: input.initializer_span,
            initializer_ordinal: input.initializer_ordinal,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ResolvedBindingValueInput> {
        Ok(ResolvedBindingValueInput {
            evidence: self.evidence,
            binding: ordinals.restore_binding(self.binding)?,
            declaration_span: self.declaration_span,
            initializer_span: self.initializer_span,
            initializer_ordinal: self.initializer_ordinal,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedArgumentInput {
    evidence: ArgumentInputEvidence,
    binding: SavedBinding,
    function_start: u32,
}

impl SavedArgumentInput {
    fn snapshot(input: &ResolvedArgumentInput, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            evidence: input.evidence.clone(),
            binding: ordinals.snapshot_binding(&input.binding)?,
            function_start: input.function_start,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ResolvedArgumentInput> {
        Ok(ResolvedArgumentInput {
            evidence: self.evidence,
            binding: ordinals.restore_binding(self.binding)?,
            function_start: self.function_start,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedInitializerCallEffectInput {
    reference: InitializerCallEffectInputRef,
    binding: SavedBinding,
    callee: SavedBinding,
    call_span: Span,
    source_sha256: String,
    provider_chunk: String,
    provider_export: String,
    provider_sha256: String,
    effective_replacement: Option<analysis::analysis_inputs::EffectiveInitializerCallReplacement>,
    provider_dependency_closure: BTreeSet<String>,
    /// Optional for legacy saved evidence; it must not decode as empty.
    #[serde(default)]
    provider_closure: Option<ResolvedProviderDependencyClosure>,
}

impl SavedInitializerCallEffectInput {
    fn snapshot(
        input: &ResolvedInitializerCallEffectInput,
        ordinals: &ContextOrdinals,
    ) -> Result<Self> {
        validate_provider_closure_namespace(input.provider_closure.as_ref(), ordinals)?;
        Ok(Self {
            reference: input.reference.clone(),
            binding: ordinals.snapshot_binding(&input.binding)?,
            callee: ordinals.snapshot_binding(&input.callee)?,
            call_span: input.call_span,
            source_sha256: input.source_sha256.clone(),
            provider_chunk: input.provider_chunk.clone(),
            provider_export: input.provider_export.clone(),
            provider_sha256: input.provider_sha256.clone(),
            effective_replacement: input.effective_replacement.clone(),
            provider_dependency_closure: input.provider_dependency_closure.clone(),
            provider_closure: input.provider_closure.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ResolvedInitializerCallEffectInput> {
        validate_provider_closure_namespace(self.provider_closure.as_ref(), ordinals)?;
        Ok(ResolvedInitializerCallEffectInput {
            reference: self.reference,
            binding: ordinals.restore_binding(self.binding)?,
            callee: ordinals.restore_binding(self.callee)?,
            call_span: self.call_span,
            source_sha256: self.source_sha256,
            provider_chunk: self.provider_chunk,
            provider_export: self.provider_export,
            provider_sha256: self.provider_sha256,
            effective_replacement: self.effective_replacement,
            provider_dependency_closure: self.provider_dependency_closure,
            provider_closure: self.provider_closure,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedNativeOperationInput {
    evidence: NativeOperationInputEvidence,
    binding: SavedBinding,
    selected_statement_span: Span,
    declaration_span: Span,
    initializer_span: Span,
    call_span: Span,
    initializer_ordinal: SourceUnitOrdinal,
    spread_source: Option<SavedBinding>,
    spread_source_span: Option<Span>,
    spread_token_span: Option<Span>,
}

#[derive(Serialize, Deserialize)]
struct SavedImportedPrimitiveArraySpreadInput {
    evidence: analysis::analysis_inputs::ImportedPrimitiveArraySpreadInputEvidence,
    consumer_initializer_span: Span,
    spread_token_spans: Vec<Span>,
}

impl SavedImportedPrimitiveArraySpreadInput {
    fn snapshot(
        input: &analysis::analysis_inputs::ResolvedImportedPrimitiveArraySpreadInput,
    ) -> Self {
        Self {
            evidence: input.evidence.clone(),
            consumer_initializer_span: input.consumer_initializer_span,
            spread_token_spans: input.spread_token_spans.clone(),
        }
    }

    fn restore(self) -> analysis::analysis_inputs::ResolvedImportedPrimitiveArraySpreadInput {
        analysis::analysis_inputs::ResolvedImportedPrimitiveArraySpreadInput {
            evidence: self.evidence,
            consumer_initializer_span: self.consumer_initializer_span,
            spread_token_spans: self.spread_token_spans,
        }
    }
}

impl SavedNativeOperationInput {
    fn snapshot(input: &ResolvedNativeOperationInput, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            evidence: input.evidence.clone(),
            binding: ordinals.snapshot_binding(&input.binding)?,
            selected_statement_span: input.selected_statement_span,
            declaration_span: input.declaration_span,
            initializer_span: input.initializer_span,
            call_span: input.call_span,
            initializer_ordinal: input.initializer_ordinal,
            spread_source: input
                .spread_source
                .as_ref()
                .map(|binding| ordinals.snapshot_binding(binding))
                .transpose()?,
            spread_source_span: input.spread_source_span,
            spread_token_span: input.spread_token_span,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ResolvedNativeOperationInput> {
        Ok(ResolvedNativeOperationInput {
            evidence: self.evidence,
            binding: ordinals.restore_binding(self.binding)?,
            selected_statement_span: self.selected_statement_span,
            declaration_span: self.declaration_span,
            initializer_span: self.initializer_span,
            call_span: self.call_span,
            initializer_ordinal: self.initializer_ordinal,
            spread_source: self
                .spread_source
                .map(|binding| ordinals.restore_binding(binding))
                .transpose()?,
            spread_source_span: self.spread_source_span,
            spread_token_span: self.spread_token_span,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedStableSetIterableUse {
    source_span: Span,
    spread_token_span: Span,
    interval_bindings: Vec<SavedBinding>,
    dependencies: analysis::analysis_inputs::ProofDependencies,
}

impl SavedStableSetIterableUse {
    fn snapshot(use_fact: &StableSetIterableUse, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            source_span: use_fact.source_span,
            spread_token_span: use_fact.spread_token_span,
            interval_bindings: use_fact
                .interval_bindings
                .iter()
                .map(|binding| ordinals.snapshot_binding(binding))
                .collect::<Result<_>>()?,
            dependencies: use_fact.dependencies.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<StableSetIterableUse> {
        Ok(StableSetIterableUse {
            source_span: self.source_span,
            spread_token_span: self.spread_token_span,
            interval_bindings: self
                .interval_bindings
                .into_iter()
                .map(|binding| ordinals.restore_binding(binding))
                .collect::<Result<_>>()?,
            dependencies: self.dependencies,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedInferredSetConstruction {
    binding: SavedBinding,
    operation: SavedInferredSetOperation,
    dependencies: analysis::analysis_inputs::ProofDependencies,
    inference: analysis::analysis_provenance::InferenceUse,
}

impl SavedInferredSetConstruction {
    fn snapshot(
        construction: &InferredSetConstruction,
        ordinals: &ContextOrdinals,
    ) -> Result<Self> {
        Ok(Self {
            binding: ordinals.snapshot_binding(&construction.binding)?,
            operation: SavedInferredSetOperation::snapshot(&construction.operation, ordinals)?,
            dependencies: construction.dependencies.clone(),
            inference: construction.inference.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<InferredSetConstruction> {
        Ok(InferredSetConstruction {
            binding: ordinals.restore_binding(self.binding)?,
            operation: self.operation.restore(ordinals)?,
            dependencies: self.dependencies,
            inference: self.inference,
        })
    }
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum SavedInferredSetOperation {
    PrimitiveArray,
    ProtectedSpread {
        source: SavedBinding,
        interval: Box<SavedStableSetIterableUse>,
    },
}

impl SavedInferredSetOperation {
    fn snapshot(operation: &InferredSetOperation, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(match operation {
            InferredSetOperation::PrimitiveArray => Self::PrimitiveArray,
            InferredSetOperation::ProtectedSpread { source, interval } => Self::ProtectedSpread {
                source: ordinals.snapshot_binding(source)?,
                interval: Box::new(SavedStableSetIterableUse::snapshot(interval, ordinals)?),
            },
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<InferredSetOperation> {
        Ok(match self {
            Self::PrimitiveArray => InferredSetOperation::PrimitiveArray,
            Self::ProtectedSpread { source, interval } => InferredSetOperation::ProtectedSpread {
                source: ordinals.restore_binding(source)?,
                interval: Box::new(interval.restore(ordinals)?),
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use source_identity::{ChunkTable, ModuleCellId, SourceUnitOrdinal};
    use swc_common::{BytePos, DUMMY_SP, Span, SyntaxContext};

    #[test]
    fn required_saved_summary_fields_cannot_be_omitted() {
        let primitive_reads = BTreeMap::from([(
            "app".to_string(),
            BTreeMap::from([(
                "limit".to_string(),
                ImportedPrimitiveRead {
                    kinds: analysis_model::PrimKinds::NUMBER,
                    source_specifier: "./provider.js".to_owned(),
                    imported_name: "limit".to_owned(),
                    provider_module: "provider".to_string(),
                    dependency_closure: BTreeSet::from(["provider".to_string()]),
                    provider_closure: Some(ResolvedProviderDependencyClosure {
                        modules: BTreeSet::from(["provider".to_string()]),
                        admitted_host_modules: BTreeSet::new(),
                    }),
                    source_cells: BTreeSet::from([
                        analysis::analysis_inputs::PrimitiveExportCell {
                            module: "provider".to_string(),
                            export: "limit".to_string(),
                        },
                    ]),
                },
            )]),
        )]);
        let binding: swc_ecma_ast::Id = ("effect".into(), SyntaxContext::empty());
        let mut writer_chunks = ChunkTable::default();
        let writer_provider = writer_chunks.intern("provider".to_owned());
        writer_chunks.intern("app".to_owned());
        writer_chunks.intern("other".to_owned());
        let cell = ModuleCellId {
            chunk: writer_provider,
            declaration_origin: 41,
        };
        let owner = serde_json::to_value(SourceUnitOrdinal(3)).expect("owner serializes");
        let span = serde_json::to_value(DUMMY_SP).expect("span serializes");
        let invariant: PrimitiveCellInvariant = serde_json::from_value(serde_json::json!({
            "declaration": {
                "owner": owner.clone(),
                "activation_callable": null,
                "binding_span": span.clone(),
                "initializer_span": span.clone(),
            },
            "post_init_kinds": serde_json::to_value(analysis_model::PrimKinds::STRING)
                .expect("primitive kinds serialize"),
            "stores": [{
                "owner": owner,
                "activation_callable": 7,
                "span": span,
                "kind": "assignment",
            }],
        }))
        .expect("public primitive cell proof deserializes");
        let module_cells =
            BTreeMap::from([("app".to_string(), BTreeMap::from([(binding.clone(), cell)]))]);
        let source_initialized_module_cells =
            BTreeMap::from([("app".to_string(), BTreeSet::from([cell]))]);
        let app_span = Span::new(BytePos(4), BytePos(17));
        let global_uses = BTreeMap::from([
            (
                "app".to_string(),
                Arc::new(ParsedGlobalUses {
                    source_file: "static/app.js".to_string(),
                    uses: BTreeMap::from([(binding.clone(), vec![app_span])]),
                    dynamic_scope: true,
                }),
            ),
            (
                "other".to_string(),
                Arc::new(ParsedGlobalUses {
                    source_file: "static/other.js".to_string(),
                    uses: BTreeMap::new(),
                    dynamic_scope: false,
                }),
            ),
            (
                "provider".to_string(),
                Arc::new(ParsedGlobalUses {
                    source_file: "static/provider.js".to_string(),
                    uses: BTreeMap::new(),
                    dynamic_scope: false,
                }),
            ),
        ]);
        let summary = ProgramSummary {
            provider_programs: Default::default(),
            bindings: ["app", "other", "provider"]
                .into_iter()
                .map(|chunk| (chunk.to_string(), BTreeMap::new()))
                .collect(),
            global_uses,
            values: BTreeMap::new(),
            primitive_reads: primitive_reads.clone(),
            members: BTreeMap::new(),
            fluent: BTreeMap::new(),
            constructors: BTreeMap::new(),
            imported_callables: BTreeMap::new(),
            module_cells,
            module_cell_invariants: BTreeMap::from([(cell, invariant.clone())]),
            source_initialized_module_cells,
            source_initialized_host_dependencies: Some(BTreeMap::from([(
                "app".to_string(),
                BTreeSet::new(),
            )])),
            native_integrity: None,
            bundle_globals: Default::default(),
            value_inputs: BTreeMap::new(),
            native_operation_inputs: BTreeMap::new(),
            initializer_call_effect_inputs: BTreeMap::new(),
            imported_primitive_array_spread_inputs: BTreeMap::new(),
            stable_set_spreads: BTreeMap::new(),
            inferred_set_constructions: BTreeMap::new(),
            argument_inputs: BTreeMap::new(),
            inline_callback_factories: BTreeMap::new(),
            callback_factory_members: BTreeMap::new(),
            retained_callback_factories: BTreeMap::new(),
            retained_callback_factory_members: BTreeMap::new(),
            retained_callback_constructors: BTreeMap::new(),
            no_reentry_exports: BTreeMap::new(),
            no_reentry_callback_exports: BTreeMap::new(),
            no_reentry_members: BTreeMap::new(),
            no_reentry_record_members: BTreeMap::new(),
            namespace_independent_members: BTreeMap::new(),
        };
        let ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
        let mut purities = PuritySnapshot::default();
        let saved = SavedProgramSummary::snapshot(&summary, &ordinals, &mut purities)
            .expect("empty program summary snapshots");
        let saved_json = serde_json::to_value(&saved).expect("saved summary serializes");
        assert!(saved_json["native_integrity"].is_null());
        assert_eq!(
            saved_json["module_cells"]["app"][0]["binding"]["symbol"],
            "effect"
        );
        assert_eq!(
            saved_json["module_cells"]["app"][0]["cell"]["chunk"],
            "provider"
        );
        assert_eq!(saved_json["global_uses"].as_array().unwrap().len(), 3);

        let mut legacy_host_summary = saved_json.clone();
        legacy_host_summary
            .as_object_mut()
            .expect("saved summary is a map")
            .remove("source_initialized_host_dependencies");
        let decoded: SavedProgramSummary<'static> =
            serde_json::from_value(legacy_host_summary).expect("legacy host field is optional");
        let restored_purities = PuritySnapshot::default()
            .finish()
            .restore()
            .expect("empty purity graph restores");
        let mut legacy_chunks = ChunkTable::default();
        legacy_chunks.intern("app".to_owned());
        legacy_chunks.intern("other".to_owned());
        legacy_chunks.intern("provider".to_owned());
        let legacy_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &legacy_chunks);
        let legacy_restored = decoded
            .restore(&legacy_ordinals, &restored_purities, &BTreeMap::new())
            .expect("legacy host summary restores as unknown");
        assert!(
            legacy_restored
                .source_initialized_host_dependencies
                .is_none()
        );
        assert!(
            legacy_restored.primitive_reads["app"]["limit"]
                .provider_closure
                .is_some()
        );

        let mut legacy_read = saved_json.clone();
        legacy_read["primitive_reads"]["app"]["limit"]
            .as_object_mut()
            .expect("primitive read is a map")
            .remove("provider_closure");
        let decoded: SavedProgramSummary<'static> =
            serde_json::from_value(legacy_read).expect("legacy primitive read decodes");
        let restored_purities = PuritySnapshot::default()
            .finish()
            .restore()
            .expect("empty purity graph restores");
        let read_restored = decoded
            .restore(&legacy_ordinals, &restored_purities, &BTreeMap::new())
            .expect("legacy primitive read restores");
        assert!(
            read_restored.primitive_reads["app"]["limit"]
                .provider_closure
                .is_none()
        );

        for field in [
            "native_integrity",
            "primitive_reads",
            "imported_primitive_array_spread_inputs",
            "module_cells",
            "module_cell_invariants",
            "source_initialized_module_cells",
            "global_uses",
        ] {
            let mut missing_field = saved_json.clone();
            missing_field
                .as_object_mut()
                .expect("saved summary is a map")
                .remove(field);
            assert!(
                serde_json::from_value::<SavedProgramSummary<'static>>(missing_field).is_err(),
                "omitted {field} must not restore as empty proof metadata"
            );
        }

        let decoded: SavedProgramSummary<'static> = serde_json::from_value(saved_json.clone())
            .expect("explicit None native census decodes");
        let restored_purities = PuritySnapshot::default()
            .finish()
            .restore()
            .expect("empty purity graph restores");
        let mut reader_chunks = ChunkTable::default();
        reader_chunks.intern("other".to_owned());
        reader_chunks.intern("app".to_owned());
        let reader_provider = reader_chunks.intern("provider".to_owned());
        assert_ne!(writer_provider, reader_provider);
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        // This fixture has no imported callable summaries, so it has no
        // provider source inventories to restore.
        let source_inventories = BTreeMap::new();
        let restored = decoded
            .restore(&reader_ordinals, &restored_purities, &source_inventories)
            .expect("absent native census restores");
        assert!(restored.native_integrity.is_none());
        assert_eq!(restored.primitive_reads, primitive_reads);
        let restored_app_uses = &restored.global_uses["app"];
        assert_eq!(restored_app_uses.source_file, "static/app.js");
        assert_eq!(
            restored_app_uses.uses,
            BTreeMap::from([(binding.clone(), vec![app_span])])
        );
        assert!(restored_app_uses.dynamic_scope);
        assert!(restored.global_uses["other"].uses.is_empty());
        assert!(!restored.global_uses["other"].dynamic_scope);
        assert_eq!(restored.global_uses.len(), 3);
        let absent_source_context = analysis::AnalysisInputs::default();
        let explicit_empty_manifest = analysis::AnalysisInputs {
            global_uses: Some(restored.global_uses["other"].clone()),
            ..analysis::AnalysisInputs::default()
        };
        assert!(absent_source_context.global_uses.is_none());
        assert!(
            explicit_empty_manifest
                .global_uses
                .as_ref()
                .is_some_and(|manifest| manifest.uses.is_empty())
        );
        assert_eq!(
            restored.source_initialized_module_cells,
            BTreeMap::from([(
                "app".to_string(),
                BTreeSet::from([ModuleCellId {
                    chunk: reader_provider,
                    declaration_origin: 41,
                }]),
            )]),
        );
        assert_eq!(
            restored.source_initialized_host_dependencies,
            Some(BTreeMap::from([("app".to_string(), BTreeSet::new())]))
        );
        assert_eq!(
            restored.module_cell_invariants,
            BTreeMap::from([(
                ModuleCellId {
                    chunk: reader_provider,
                    declaration_origin: 41,
                },
                invariant.clone(),
            )]),
        );
        assert_eq!(
            restored.module_cells,
            BTreeMap::from([(
                "app".to_string(),
                BTreeMap::from([(
                    binding.clone(),
                    ModuleCellId {
                        chunk: reader_provider,
                        declaration_origin: 41,
                    },
                )]),
            )]),
        );

        let mut duplicate_proof = saved_json.clone();
        let proofs = duplicate_proof["module_cell_invariants"]
            .as_array_mut()
            .expect("module-cell invariants are a sequence");
        let duplicate_entry = proofs[0].clone();
        proofs.push(duplicate_entry);
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(duplicate_proof)
            .expect("duplicate proof cell decodes structurally");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities, &source_inventories)
                .is_err(),
            "duplicate module-cell proof keys must refuse restore"
        );

        let mut unknown_importer = saved_json.clone();
        let source_initialized = unknown_importer["source_initialized_module_cells"]
            .as_object_mut()
            .expect("source initialized cells are a map");
        let app_cells = source_initialized
            .remove("app")
            .expect("app context exists");
        source_initialized.insert("missing-importer".to_owned(), app_cells);
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(unknown_importer)
            .expect("unknown importer decodes structurally");
        let restored_purities = PuritySnapshot::default()
            .finish()
            .restore()
            .expect("empty purity graph restores");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities, &source_inventories)
                .is_err(),
            "unknown importer names must refuse source-initialization restore"
        );

        let mut incomplete_global_uses = saved_json.clone();
        incomplete_global_uses["global_uses"]
            .as_array_mut()
            .expect("global-use inventory is a sequence")
            .pop();
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(incomplete_global_uses)
            .expect("incomplete global-use inventory decodes structurally");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities, &source_inventories)
                .is_err(),
            "incomplete prepared-chunk global-use inventory must refuse restore"
        );

        let mut unknown_global_chunk = saved_json.clone();
        unknown_global_chunk["global_uses"][0]["chunk"] = serde_json::json!("missing-chunk");
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(unknown_global_chunk)
            .expect("unknown global-use chunk decodes structurally");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities, &source_inventories)
                .is_err(),
            "unknown global-use source chunk names must refuse restore"
        );

        let mut unknown_global_binding = saved_json.clone();
        unknown_global_binding["global_uses"][0]["manifest"]["uses"][0]["binding"]["context_ordinal"] =
            serde_json::json!(99);
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(unknown_global_binding)
            .expect("unknown global-use binding ordinal decodes structurally");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities, &source_inventories)
                .is_err(),
            "unknown global-use binding ordinals must refuse restore"
        );

        let mut duplicate_global_chunk = saved_json.clone();
        let global_entries = duplicate_global_chunk["global_uses"]
            .as_array_mut()
            .expect("global-use inventory is a sequence");
        let duplicate_entry = global_entries[0].clone();
        global_entries.push(duplicate_entry);
        let decoded: SavedProgramSummary<'static> = serde_json::from_value(duplicate_global_chunk)
            .expect("duplicate global-use chunk decodes structurally");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities, &source_inventories)
                .is_err(),
            "duplicate global-use source chunk names must refuse restore"
        );

        let mut duplicate_global_binding = saved_json;
        let app_uses = duplicate_global_binding["global_uses"]
            .as_array_mut()
            .expect("global-use inventory is a sequence")
            .iter_mut()
            .find(|entry| entry["chunk"] == "app")
            .expect("app manifest exists");
        let uses = app_uses["manifest"]["uses"]
            .as_array_mut()
            .expect("global uses are a sequence");
        let duplicate_use = uses[0].clone();
        uses.push(duplicate_use);
        let decoded: SavedProgramSummary<'static> =
            serde_json::from_value(duplicate_global_binding)
                .expect("duplicate global-use binding decodes structurally");
        assert!(
            decoded
                .restore(&reader_ordinals, &restored_purities, &source_inventories)
                .is_err(),
            "duplicate global-use bindings must refuse restore"
        );
    }

    #[test]
    fn restored_global_uses_must_match_fresh_source_manifest() {
        let binding: swc_ecma_ast::Id = ("ambient".into(), SyntaxContext::empty());
        let uses = |source_file: &str, span: Span, dynamic_scope| ParsedGlobalUses {
            source_file: source_file.to_owned(),
            uses: BTreeMap::from([(binding.clone(), vec![span])]),
            dynamic_scope,
        };
        let saved = Arc::new(uses("src/app.js", Span::new(BytePos(4), BytePos(8)), false));
        let restored = BTreeMap::from([("app".to_owned(), saved)]);
        let expected = BTreeSet::from(["app".to_owned()]);
        let current = uses("src/app.js", Span::new(BytePos(4), BytePos(8)), false);
        let fresh = BTreeMap::from([("app".to_owned(), Some(&current))]);
        assert!(validate_global_uses(&restored, &fresh, &expected).is_ok());

        let changed_spans = uses("src/app.js", Span::new(BytePos(4), BytePos(9)), false);
        assert!(
            validate_global_uses(
                &restored,
                &BTreeMap::from([("app".to_owned(), Some(&changed_spans))]),
                &expected,
            )
            .is_err(),
            "changed source-use spans must not retain saved completeness"
        );

        let changed_source = uses("other/app.js", Span::new(BytePos(4), BytePos(8)), false);
        assert!(
            validate_global_uses(
                &restored,
                &BTreeMap::from([("app".to_owned(), Some(&changed_source))]),
                &expected,
            )
            .is_err(),
            "a changed source path must refuse the saved manifest"
        );

        let changed_scope = uses("src/app.js", Span::new(BytePos(4), BytePos(8)), true);
        assert!(
            validate_global_uses(
                &restored,
                &BTreeMap::from([("app".to_owned(), Some(&changed_scope))]),
                &expected,
            )
            .is_err(),
            "a changed dynamic-scope classification must refuse the saved manifest"
        );

        assert!(
            validate_global_uses(
                &restored,
                &BTreeMap::from([("app".to_owned(), None)]),
                &expected,
            )
            .is_err(),
            "missing fresh source context must remain unproved"
        );

        let expanded = BTreeSet::from(["app".to_owned(), "provider".to_owned()]);
        assert!(validate_global_uses(&restored, &fresh, &expanded).is_err());
    }
}
