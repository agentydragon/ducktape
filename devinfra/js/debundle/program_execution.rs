//! Immutable executable definitions shared by local and imported analysis.
//!
//! Definitions contain source facts, never an execution world. Their source
//! coordinates are meaningful only together with their defining module.

pub(crate) mod effects;
pub mod schedule;
pub(crate) use effects::ProgramExecutionEffects;

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use crate::analysis_inputs::AnalysisInputs;
use crate::facts::sync_effects::execution_index::ExecutionIndex;
use crate::facts::{ExportReentryPolicy, PreparedExecutionModule};
use source_identity::{ChunkTable, ProgramExportOrigin, ProgramLinkIndex, ProgramLinkResolution};

/// One prepared source arena. Clones of an index retain it; an unrelated
/// standalone analysis gets a fresh one even if its module name matches.
/// This process-local identity is deliberately not serializable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RuntimeSourceScope(u64);

impl Default for RuntimeSourceScope {
    fn default() -> Self {
        Self::fresh()
    }
}

impl RuntimeSourceScope {
    pub(crate) fn fresh() -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        Self(
            NEXT.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .expect("runtime source scope identity exhausted"),
        )
    }

    pub(crate) fn allocation(self, local: u32) -> RuntimeAllocationId {
        RuntimeAllocationId {
            source: self,
            local,
            activation: None,
        }
    }

    pub(crate) fn local_origin(self, allocation: RuntimeAllocationId) -> Option<u32> {
        (allocation.source == self).then_some(allocation.local)
    }
}

/// A source allocation site interpreted in its owning prepared source.
/// An unactivated address is a source-summary site; an activated address
/// denotes one actual allocation event in the shared execution.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RuntimeAllocationId {
    pub(crate) source: RuntimeSourceScope,
    local: u32,
    activation: Option<RuntimeActivationId>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RuntimeActivationId(std::num::NonZeroU64);

impl RuntimeActivationId {
    /// Identity allocation is run-wide, including abandoned paths and replay.
    pub(crate) fn fresh() -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(1);
        let id = NEXT
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                next.checked_add(1)
            })
            .expect("runtime allocation identity exhausted");
        Self(std::num::NonZeroU64::new(id).expect("allocation identities start at one"))
    }
}

impl RuntimeAllocationId {
    pub(crate) fn instantiated(self, activation: RuntimeActivationId) -> Self {
        Self {
            activation: Some(activation),
            ..self
        }
    }
    /// Immutable source metadata is keyed by the lexical definition/site.
    pub(crate) fn source_site(self) -> Self {
        Self {
            activation: None,
            ..self
        }
    }
    pub(crate) fn activation(self) -> Option<RuntimeActivationId> {
        self.activation
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct RuntimeBindingId {
    pub(crate) source: RuntimeSourceScope,
    binding: swc_ecma_ast::Id,
}

/// An executed source unit in its defining prepared module.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RuntimeSourceUnitId {
    pub(crate) source: RuntimeSourceScope,
    pub(crate) ordinal: source_identity::SourceUnitOrdinal,
}

impl RuntimeSourceScope {
    pub(crate) fn binding(self, binding: swc_ecma_ast::Id) -> RuntimeBindingId {
        RuntimeBindingId {
            source: self,
            binding,
        }
    }

    pub(crate) fn local_binding(self, binding: &RuntimeBindingId) -> Option<&swc_ecma_ast::Id> {
        (binding.source == self).then_some(&binding.binding)
    }

    pub(crate) fn unit(self, ordinal: source_identity::SourceUnitOrdinal) -> RuntimeSourceUnitId {
        RuntimeSourceUnitId {
            source: self,
            ordinal,
        }
    }

    pub(crate) fn local_unit(
        self,
        owner: RuntimeSourceUnitId,
    ) -> Option<source_identity::SourceUnitOrdinal> {
        (owner.source == self).then_some(owner.ordinal)
    }
}

pub(crate) type RuntimeEffectCell = crate::facts::EffectCell<RuntimeAllocationId, RuntimeBindingId>;
pub(crate) type RuntimeHeapContentCells =
    crate::facts::HeapContentCells<RuntimeAllocationId, RuntimeBindingId>;
pub(crate) type RuntimeHeapContentAccesses =
    crate::facts::HeapContentAccesses<RuntimeAllocationId, RuntimeBindingId>;

impl RuntimeSourceScope {
    pub(crate) fn qualify_heap(
        self,
        cells: &crate::facts::HeapContentCells,
    ) -> RuntimeHeapContentCells {
        RuntimeHeapContentCells {
            allocations: cells
                .allocations
                .iter()
                .cloned()
                .map(|cell| self.qualify_effect(cell))
                .collect(),
            reads: cells
                .reads
                .iter()
                .cloned()
                .map(|cell| self.qualify_effect(cell))
                .collect(),
            writes: cells
                .writes
                .iter()
                .cloned()
                .map(|cell| self.qualify_effect(cell))
                .collect(),
            opaque: cells.opaque,
            observes_external_heap: cells.observes_external_heap,
        }
    }

    pub(crate) fn qualify_effect(self, cell: crate::facts::EffectCell) -> RuntimeEffectCell {
        use crate::facts::EffectCell;
        match cell {
            EffectCell::Binding(binding) => EffectCell::Binding(RuntimeBindingId {
                source: self,
                binding,
            }),
            EffectCell::GlobalProp(property) => EffectCell::GlobalProp(property),
            EffectCell::ArrayContents(id) => EffectCell::ArrayContents(self.allocation(id)),
            EffectCell::SetContents(id) => EffectCell::SetContents(self.allocation(id)),
            EffectCell::ObjectShape(id) => EffectCell::ObjectShape(self.allocation(id)),
            EffectCell::OwnProperty { origin, property } => EffectCell::OwnProperty {
                origin: self.allocation(origin),
                property,
            },
        }
    }

    pub(crate) fn project_effect(
        self,
        cell: &RuntimeEffectCell,
    ) -> Option<crate::facts::EffectCell> {
        use crate::facts::EffectCell;
        Some(match cell {
            EffectCell::Binding(binding) => {
                if binding.source != self {
                    return None;
                }
                EffectCell::Binding(binding.binding.clone())
            }
            EffectCell::GlobalProp(property) => EffectCell::GlobalProp(property.clone()),
            EffectCell::ArrayContents(id) => EffectCell::ArrayContents(self.local_origin(*id)?),
            EffectCell::SetContents(id) => EffectCell::SetContents(self.local_origin(*id)?),
            EffectCell::ObjectShape(id) => EffectCell::ObjectShape(self.local_origin(*id)?),
            EffectCell::OwnProperty { origin, property } => EffectCell::OwnProperty {
                origin: self.local_origin(*origin)?,
                property: property.clone(),
            },
        })
    }
}

/// A coordinate accepted by an owning source index. Runtime addresses are
/// checked before lookup; raw coordinates already belong to this source.
mod sealed {
    pub trait SourceCoordinate {}
    impl SourceCoordinate for u32 {}
    impl SourceCoordinate for super::RuntimeAllocationId {}
    impl<T: SourceCoordinate> SourceCoordinate for &T {}
}

pub(crate) trait SourceCoordinate: Copy + sealed::SourceCoordinate {
    fn local_in(self, source: RuntimeSourceScope) -> Option<u32>;
    fn local_in_optional(self, source: Option<RuntimeSourceScope>) -> Option<u32> {
        self.local_in(source?)
    }
}

impl SourceCoordinate for u32 {
    fn local_in(self, _source: RuntimeSourceScope) -> Option<u32> {
        Some(self)
    }
    fn local_in_optional(self, _source: Option<RuntimeSourceScope>) -> Option<u32> {
        Some(self)
    }
}

impl SourceCoordinate for RuntimeAllocationId {
    fn local_in(self, source: RuntimeSourceScope) -> Option<u32> {
        source.local_origin(self)
    }
}

impl<T: SourceCoordinate> SourceCoordinate for &T {
    fn local_in(self, source: RuntimeSourceScope) -> Option<u32> {
        (*self).local_in(source)
    }
    fn local_in_optional(self, source: Option<RuntimeSourceScope>) -> Option<u32> {
        (*self).local_in_optional(source)
    }
}

/// Source facts keep local coordinates; runtime access must prove ownership.
pub(crate) struct SourceMap<T> {
    source: Option<RuntimeSourceScope>,
    values: BTreeMap<u32, T>,
}

impl<T> Default for SourceMap<T> {
    fn default() -> Self {
        Self {
            source: None,
            values: BTreeMap::new(),
        }
    }
}

impl<T> SourceMap<T> {
    pub(crate) fn bind_source(&mut self, source: RuntimeSourceScope) {
        self.source = Some(source);
    }
    pub(crate) fn get<K: SourceCoordinate>(&self, key: &K) -> Option<&T> {
        self.values.get(&key.local_in_optional(self.source)?)
    }
    pub(crate) fn get_mut<K: SourceCoordinate>(&mut self, key: &K) -> Option<&mut T> {
        self.values.get_mut(&key.local_in_optional(self.source)?)
    }
    pub(crate) fn contains_key<K: SourceCoordinate>(&self, key: &K) -> bool {
        self.get(key).is_some()
    }
}

impl<T> std::ops::Deref for SourceMap<T> {
    type Target = BTreeMap<u32, T>;
    fn deref(&self) -> &Self::Target {
        &self.values
    }
}
impl<T> std::ops::DerefMut for SourceMap<T> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.values
    }
}
impl<T: Clone> Clone for SourceMap<T> {
    fn clone(&self) -> Self {
        Self {
            source: self.source,
            values: self.values.clone(),
        }
    }
}

impl<'a, T> IntoIterator for &'a SourceMap<T> {
    type Item = (&'a u32, &'a T);
    type IntoIter = std::collections::btree_map::Iter<'a, u32, T>;
    fn into_iter(self) -> Self::IntoIter {
        self.values.iter()
    }
}

impl<T> IntoIterator for SourceMap<T> {
    type Item = (u32, T);
    type IntoIter = std::collections::btree_map::IntoIter<u32, T>;
    fn into_iter(self) -> Self::IntoIter {
        self.values.into_iter()
    }
}

#[derive(Clone, Default)]
pub(crate) struct SourceSet {
    source: Option<RuntimeSourceScope>,
    values: std::collections::BTreeSet<u32>,
}
impl SourceSet {
    pub(crate) fn bind_source(&mut self, source: RuntimeSourceScope) {
        self.source = Some(source);
    }
    pub(crate) fn contains<K: SourceCoordinate>(&self, key: &K) -> bool {
        key.local_in_optional(self.source)
            .is_some_and(|key| self.values.contains(&key))
    }
}
impl std::ops::Deref for SourceSet {
    type Target = std::collections::BTreeSet<u32>;
    fn deref(&self) -> &Self::Target {
        &self.values
    }
}
impl std::ops::DerefMut for SourceSet {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.values
    }
}

impl<'a> IntoIterator for &'a SourceSet {
    type Item = &'a u32;
    type IntoIter = std::collections::btree_set::Iter<'a, u32>;
    fn into_iter(self) -> Self::IntoIter {
        self.values.iter()
    }
}

impl FromIterator<u32> for SourceSet {
    fn from_iter<I: IntoIterator<Item = u32>>(values: I) -> Self {
        Self {
            source: None,
            values: values.into_iter().collect(),
        }
    }
}

/// One source callable. A byte position alone is not a program identity:
/// independently parsed chunks can contain different functions at that position.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ProgramCallableId {
    pub module: String,
    pub source_origin: u32,
}

/// A source value selected through the canonical module linker. A namespace
/// and an anonymous default export keep their actual module identities;
/// neither is invented as a local binding with a guessed name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ProgramValueSource {
    Binding { module: String, local: String },
    DefaultValue { module: String, body_index: usize },
    Namespace { module: String },
}

#[derive(Clone)]
struct ProgramSourceLinks {
    chunks: ChunkTable,
    index: ProgramLinkIndex,
}

#[derive(Clone)]
pub(crate) struct ExecutableModule {
    pub(crate) index: Arc<ExecutionIndex>,
    pub(crate) exports: BTreeMap<String, String>,
}

/// One canonical source preparation retained for execution by another chunk.
/// The source hints deliberately do not point back to the enclosing program
/// registry; root execution views attach that registry when they are built.
#[derive(Clone)]
pub(crate) struct PreparedProgramSource {
    prepared: Arc<PreparedExecutionModule>,
    hints: AnalysisInputs,
    export_reentry_policy: ExportReentryPolicy,
}

impl PreparedProgramSource {
    pub(crate) fn new(
        prepared: Arc<PreparedExecutionModule>,
        mut hints: AnalysisInputs,
        export_reentry_policy: Option<ExportReentryPolicy>,
    ) -> Self {
        hints.executables = None;
        Self {
            prepared,
            hints,
            export_reentry_policy: export_reentry_policy.unwrap_or_default(),
        }
    }
}

/// The provider indexes already built by canonical source preparation. Completed
/// analysis persists its results; executable definitions stay in the current SWC
/// arena and are not serialized as portable code or binding identities.
#[derive(Clone, Default)]
pub struct ProgramExecutables {
    modules: BTreeMap<String, ExecutableModule>,
    links: Option<ProgramSourceLinks>,
    sources: BTreeMap<String, PreparedProgramSource>,
}

impl fmt::Debug for ProgramExecutables {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ProgramExecutables")
            .field("modules", &self.modules.keys().collect::<Vec<_>>())
            .field("sources", &self.sources.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl ProgramExecutables {
    pub(crate) fn source_unit_location(
        &self,
        module: &str,
        owner: source_identity::SourceUnitOrdinal,
    ) -> Option<crate::execution_evidence::SourceLocation> {
        self.sources
            .get(module)?
            .prepared
            .facts
            .get(owner.0)?
            .source_location
            .clone()
    }

    pub(crate) fn from_modules(modules: BTreeMap<String, ExecutableModule>) -> Self {
        Self {
            modules,
            links: None,
            sources: BTreeMap::new(),
        }
    }

    /// Attach the one source-order request and binding index before sharing
    /// the immutable program. This records source evaluation facts only;
    /// availability under a planned emitted graph is checked independently.
    pub fn with_source_links(mut self, chunks: ChunkTable, index: ProgramLinkIndex) -> Self {
        self.links = Some(ProgramSourceLinks { chunks, index });
        self
    }

    pub fn source_evaluation_order(&self, entry: &str) -> ProgramLinkResolution<Vec<String>> {
        let Some(links) = &self.links else {
            return ProgramLinkResolution::Unsupported;
        };
        let Some(entry) = links.chunks.get(entry) else {
            return ProgramLinkResolution::Missing;
        };
        links.index.evaluation_order(entry).map(|order| {
            order
                .into_iter()
                .map(|chunk| links.chunks.name(chunk).to_owned())
                .collect()
        })
    }

    pub(crate) fn imported_source(
        &self,
        importer: &str,
        local: &str,
        member: Option<&str>,
    ) -> ProgramLinkResolution<ProgramValueSource> {
        let Some(links) = &self.links else {
            return ProgramLinkResolution::Unsupported;
        };
        let Some(importer) = links.chunks.get(importer) else {
            return ProgramLinkResolution::Missing;
        };
        let origin = match member {
            Some(member) => links
                .index
                .resolve_namespace_member(importer, local, member)
                .map(|resolved| resolved.origin),
            None => links
                .index
                .resolve_import(importer, local)
                .map(|resolved| resolved.origin),
        };
        origin.map(|origin| match origin {
            ProgramExportOrigin::Binding(binding) => ProgramValueSource::Binding {
                module: links.chunks.name(binding.chunk).to_owned(),
                local: binding.local,
            },
            ProgramExportOrigin::DefaultValue { chunk, body_index } => {
                ProgramValueSource::DefaultValue {
                    module: links.chunks.name(chunk).to_owned(),
                    body_index,
                }
            }
            ProgramExportOrigin::Namespace { chunk } => ProgramValueSource::Namespace {
                module: links.chunks.name(chunk).to_owned(),
            },
        })
    }

    /// A namespace lookup resolves the export name before reading its live
    /// cell. Resolution itself says nothing about initialization.
    pub(crate) fn exported_source(
        &self,
        module: &str,
        name: &str,
    ) -> ProgramLinkResolution<ProgramValueSource> {
        let Some(links) = &self.links else {
            return ProgramLinkResolution::Unsupported;
        };
        let Some(chunk) = links.chunks.get(module) else {
            return ProgramLinkResolution::Missing;
        };
        links
            .index
            .resolve_export(chunk, name)
            .map(|resolved| match resolved.origin {
                ProgramExportOrigin::Binding(binding) => ProgramValueSource::Binding {
                    module: links.chunks.name(binding.chunk).to_owned(),
                    local: binding.local,
                },
                ProgramExportOrigin::DefaultValue { chunk, body_index } => {
                    ProgramValueSource::DefaultValue {
                        module: links.chunks.name(chunk).to_owned(),
                        body_index,
                    }
                }
                ProgramExportOrigin::Namespace { chunk } => ProgramValueSource::Namespace {
                    module: links.chunks.name(chunk).to_owned(),
                },
            })
    }

    pub(crate) fn namespace_exports(&self, module: &str) -> ProgramLinkResolution<Vec<String>> {
        let Some(links) = &self.links else {
            return ProgramLinkResolution::Unsupported;
        };
        let Some(chunk) = links.chunks.get(module) else {
            return ProgramLinkResolution::Missing;
        };
        links.index.export_names(chunk)
    }

    /// Attach the canonical prepared source payloads and their exact source
    /// assumptions before sharing the immutable program registry.
    pub(crate) fn with_sources(
        mut self,
        mut sources: BTreeMap<String, PreparedProgramSource>,
    ) -> Self {
        for (module, source) in &mut sources {
            source.hints.current_chunk_id = Some(module.clone());
            // The per-source hints are stored inside this registry. A pointer
            // back to the registry would create an Arc cycle once the root
            // attaches it to borrowed execution views.
            source.hints.executables = None;
            // Runtime source preparation is canonical. Reuse its exact index
            // and scope identity for callable lookup rather than retaining the
            // provider-only census that populated the summary registry.
            if let Some(executable) = self.modules.get_mut(module) {
                executable.index = source.prepared.execution_index_arc().clone();
            }
        }
        self.sources = sources;
        self
    }

    /// Attach the canonical source preparations produced by staged analysis.
    /// The returned registry owns the runtime payloads and source assumptions;
    /// it does not retain the AST-borrowing preparation wrappers.
    pub fn with_prepared_sources<'prepared, 'source>(
        self,
        sources: BTreeMap<
            String,
            (
                &'prepared crate::facts::PreparedChunkAnalysis<'source>,
                AnalysisInputs,
                Option<ExportReentryPolicy>,
            ),
        >,
    ) -> Self {
        let sources = sources
            .into_iter()
            .map(|(module, (prepared, hints, policy))| {
                (
                    module,
                    PreparedProgramSource::new(Arc::clone(&prepared.runtime), hints, policy),
                )
            })
            .collect();
        self.with_sources(sources)
    }

    /// Borrow canonical source definitions and the assumptions used to
    /// prepare them. The caller constructs execution views from these exact
    /// payloads and attaches the shared registry only to those views.
    pub(crate) fn sources(
        &self,
    ) -> impl Iterator<
        Item = (
            &str,
            &PreparedExecutionModule,
            &AnalysisInputs,
            &ExportReentryPolicy,
        ),
    > {
        self.sources.iter().map(|(module, source)| {
            (
                module.as_str(),
                source.prepared.as_ref(),
                &source.hints,
                &source.export_reentry_policy,
            )
        })
    }

    pub(crate) fn module(&self, module: &str) -> Option<&ExecutableModule> {
        self.modules.get(module)
    }

    pub(crate) fn module_for_scope(&self, scope: RuntimeSourceScope) -> Option<&ExecutableModule> {
        self.modules
            .values()
            .find(|module| module.index.source_scope() == scope)
    }

    /// Canonical source names are portable; runtime arena tokens are not.
    /// This lookup does not establish availability for execution.
    pub(crate) fn module_name_for_scope(&self, scope: RuntimeSourceScope) -> Option<&str> {
        fn unique<'a>(mut names: impl Iterator<Item = &'a str>) -> Option<&'a str> {
            let first = names.next()?;
            names.all(|name| name == first).then_some(first)
        }
        let mut prepared = self
            .sources
            .iter()
            .filter_map(|(name, source)| {
                (source
                    .prepared
                    .prepared_sync_index
                    .execution_index()
                    .source_scope()
                    == scope)
                    .then_some(name.as_str())
            })
            .peekable();
        if prepared.peek().is_some() {
            return unique(prepared);
        }
        unique(self.modules.iter().filter_map(|(name, module)| {
            (module.index.source_scope() == scope).then_some(name.as_str())
        }))
    }

    /// Resolve a summary's source identity against the prepared provider rather
    /// than interpreting a source offset in the importing chunk's index.
    pub(crate) fn exported_callable(
        &self,
        module: &str,
        export: &str,
        local: &str,
    ) -> Option<ProgramCallableId> {
        let program = self.module(module)?;
        if program.exports.get(export).map(String::as_str) != Some(local) {
            return None;
        }
        program
            .index
            .callable_for_local(local)
            .map(|source_origin| ProgramCallableId {
                module: module.to_owned(),
                source_origin,
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prepared_sources_keep_their_payload_policy_and_cycle_free_hints() {
        let module = swc_ecma_ast::Module::default();
        let structural = crate::facts::analyze_chunk_structural(&module, None);
        let prepared =
            crate::facts::prepare_chunk_analysis(structural, &AnalysisInputs::default(), None);
        let payload = prepared.runtime.clone();
        let explicit_policy = ExportReentryPolicy::NoStaticImporterReentry;
        let sources = BTreeMap::from([
            (
                "root".to_owned(),
                PreparedProgramSource::new(payload.clone(), AnalysisInputs::default(), None),
            ),
            (
                "provider".to_owned(),
                PreparedProgramSource::new(
                    payload,
                    AnalysisInputs::default(),
                    Some(explicit_policy.clone()),
                ),
            ),
        ]);
        let initial_preparation = crate::facts::prepare_chunk_analysis(
            crate::facts::analyze_chunk_structural(&module, None),
            &AnalysisInputs::default(),
            None,
        );
        let programs = ProgramExecutables::from_modules(BTreeMap::from([(
            "root".to_owned(),
            ExecutableModule {
                index: initial_preparation.runtime.execution_index_arc().clone(),
                exports: BTreeMap::new(),
            },
        )]))
        .with_sources(sources);
        assert!(
            programs
                .module_name_for_scope(
                    prepared
                        .runtime
                        .prepared_sync_index
                        .execution_index()
                        .source_scope()
                )
                .is_none(),
            "one arena under two canonical names is not a portable identity proof"
        );
        let sources = programs
            .sources()
            .map(|(module, prepared, hints, policy)| (module, (prepared, hints, policy)))
            .collect::<BTreeMap<_, _>>();

        let (source, hints, policy) = sources.get("root").expect("root source is registered");
        assert!(hints.executables.is_none());
        assert_eq!(hints.current_chunk_id.as_deref(), Some("root"));
        assert_eq!(**policy, ExportReentryPolicy::Conservative);
        assert!(std::ptr::eq(*source, prepared.runtime.as_ref()));
        assert!(Arc::ptr_eq(
            &programs.module("root").unwrap().index,
            prepared.runtime.execution_index_arc(),
        ));

        let (_, hints, policy) = sources
            .get("provider")
            .expect("provider source is registered");
        assert!(hints.executables.is_none());
        assert_eq!(hints.current_chunk_id.as_deref(), Some("provider"));
        assert_eq!(**policy, explicit_policy);
    }
}
