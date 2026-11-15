//! Immutable executable definitions shared by local and imported analysis.
//!
//! Definitions contain source facts, never an execution world. Their source
//! coordinates are meaningful only together with their defining module.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use crate::facts::sync_effects::execution_index::ExecutionIndex;

/// One prepared source arena. Clones of an index retain it; an unrelated
/// standalone analysis gets a fresh one even if its module name matches.
/// This process-local identity is deliberately not serializable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RuntimeSourceScope(u64);

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
/// Repeated allocations at one site keep the existing may-set multiplicity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RuntimeAllocationId {
    pub(crate) source: RuntimeSourceScope,
    local: u32,
    activation: Option<RuntimeActivationId>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RuntimeActivationId(std::num::NonZeroU64);

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct RuntimeBindingId {
    pub(crate) source: RuntimeSourceScope,
    binding: swc_ecma_ast::Id,
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

pub(crate) struct ExecutableModule {
    pub(crate) index: Arc<ExecutionIndex>,
    pub(crate) exports: BTreeMap<String, String>,
}

/// The provider indexes already built by canonical source preparation. Completed
/// analysis persists its results; executable definitions stay in the current SWC
/// arena and are not serialized as portable code or binding identities.
#[derive(Default)]
pub struct ProgramExecutables {
    modules: BTreeMap<String, ExecutableModule>,
}

impl fmt::Debug for ProgramExecutables {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ProgramExecutables")
            .field("modules", &self.modules.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl ProgramExecutables {
    pub(crate) fn from_modules(modules: BTreeMap<String, ExecutableModule>) -> Self {
        Self { modules }
    }

    pub(crate) fn module(&self, module: &str) -> Option<&ExecutableModule> {
        self.modules.get(module)
    }

    pub(crate) fn module_for_scope(&self, scope: RuntimeSourceScope) -> Option<&ExecutableModule> {
        self.modules
            .values()
            .find(|module| module.index.source_scope() == scope)
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
