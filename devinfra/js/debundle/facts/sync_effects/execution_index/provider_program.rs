//! Immutable executable source retained from the canonical preparation pass.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::Arc;

use anyhow::{Result, ensure};
use source_identity::ModuleCellId;
use swc_ecma_ast::Id;
#[cfg(test)]
use swc_ecma_ast::ModuleItem;

#[cfg(test)]
use super::prepare_callable_capabilities;
use super::{ExecutionIndex, PrimitiveCellInvariantQuery, compute_primitive_cell_invariant};
#[cfg(test)]
use crate::analysis_inputs::AnalysisInputs;
use crate::analysis_inputs::{
    CallableSummary, ConditionalProviderIdentity, PrimitiveCellInvariant, ProviderCallableSource,
};
use crate::facts::abstract_domain::value::HeapModuleScope;
#[cfg(test)]
use crate::facts::abstract_domain::value::SourceHeapResolver;
#[cfg(test)]
use crate::facts::{StableArraySpreadShapes, top_level_item_views};

/// Parser input identity and the current parse's coordinate translation. The
/// digest belongs to prepared source provenance; executable bodies are not saved.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ProviderProgramSource {
    pub module: String,
    pub scope: HeapModuleScope,
    pub sha256: String,
    pub source_start: u32,
    pub source_end: u32,
}

impl ProviderProgramSource {
    pub fn source_offset(&self, position: u32) -> Option<u32> {
        (position < self.source_end)
            .then(|| position.checked_sub(self.source_start))
            .flatten()
    }

    pub fn current_position(&self, offset: u32) -> Option<u32> {
        self.source_start
            .checked_add(offset)
            .filter(|position| *position < self.source_end)
    }
}

// Definitions own no execution world, capture interner or SWC SourceMap.
// Ids remain in the preparing GLOBALS arena; parallel consumers enter that
// same arena before using this immutable index.
pub(crate) struct FrozenProviderProgram {
    pub(super) index: Arc<ExecutionIndex>,
    pub(super) shadowed: BTreeSet<&'static str>,
}

impl FrozenProviderProgram {
    pub(super) fn new(index: ExecutionIndex, shadowed: BTreeSet<&'static str>) -> Self {
        Self {
            index: Arc::new(index),
            shadowed,
        }
    }

    pub(crate) fn bind_source(
        self,
        source: ProviderProgramSource,
        module_cells: BTreeMap<Id, ModuleCellId>,
        exports: BTreeMap<String, String>,
    ) -> Result<ProviderProgram> {
        ensure!(
            self.index.heap_resolver.scope() == source.scope,
            "provider source scope differs from prepared execution index"
        );
        ensure!(
            source.source_start <= source.source_end,
            "provider source has an invalid coordinate range"
        );
        ensure!(
            source.sha256.len() == 64 && source.sha256.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "provider source digest is invalid"
        );
        ensure!(
            self.index.callable_spans.values().all(|span| {
                source.source_offset(span.lo.0).is_some() && span.hi.0 <= source.source_end
            }),
            "provider callable is outside its prepared source"
        );
        Ok(ProviderProgram {
            source,
            executable: self,
            module_cells,
            exports,
        })
    }
}

/// An activation may select this immutable program while its semantic world
/// remains caller-owned. Provider lexical/module cells are not caller Ids.
pub(crate) struct ProviderProgram {
    pub(crate) source: ProviderProgramSource,
    pub(super) executable: FrozenProviderProgram,
    pub(crate) module_cells: BTreeMap<Id, ModuleCellId>,
    exports: BTreeMap<String, String>,
}

impl ProviderProgram {
    pub(crate) fn primitive_cell_invariant(&self, binding: &Id) -> Option<PrimitiveCellInvariant> {
        let cell = self.module_cells.get(binding)?;
        let HeapModuleScope::Chunk(chunk) = self.source.scope else {
            return None;
        };
        if cell.chunk != chunk {
            return None;
        }
        let PrimitiveCellInvariantQuery::Known(invariant) = compute_primitive_cell_invariant(
            &self.executable.index,
            &self.executable.shadowed,
            binding,
        ) else {
            return None;
        };
        (invariant.declaration.binding_span.lo.0 == cell.declaration_origin).then_some(invariant)
    }

    pub(crate) fn callable_position(&self, origin: u32) -> Option<u32> {
        let key = self.source.current_position(origin)?;
        self.executable
            .index
            .callables
            .contains_key(&key)
            .then_some(key)
    }

    fn callable_source(&self, key: u32) -> Option<ProviderCallableSource> {
        let span = self.executable.index.callable_spans.get(&key)?;
        self.executable.index.callables.get(&key)?;
        Some(ProviderCallableSource {
            sha256: self.source.sha256.clone(),
            source_start: self.source.source_start,
            callable_start: self.source.source_offset(span.lo.0)?,
            callable_end: span.hi.0.checked_sub(self.source.source_start)?,
        })
    }

    pub(crate) fn resolve_current_root(
        &self,
        identity: &ConditionalProviderIdentity,
    ) -> Option<u32> {
        if !self.executable.index.dynamic_scope_owners.is_empty()
            || identity.defining_module != self.source.module
            || self.exports.get(&identity.export) != Some(&identity.local)
        {
            return None;
        }
        let key = match &identity.source {
            Some(receipt) => {
                if receipt.sha256 != self.source.sha256
                    || receipt.callable_start >= receipt.callable_end
                    || receipt.source_start.checked_add(receipt.callable_start)
                        != Some(identity.callable_key)
                {
                    return None;
                }
                let key = self.callable_position(receipt.callable_start)?;
                let current = self.callable_source(key)?;
                if current.callable_start != receipt.callable_start
                    || current.callable_end != receipt.callable_end
                {
                    return None;
                }
                key
            }
            None => {
                let origin = self.source.source_offset(identity.callable_key)?;
                self.callable_position(origin)?
            }
        };
        self.executable
            .index
            .hoisted_bindings
            .iter()
            .any(|(binding, candidate)| {
                binding.0.as_ref() == identity.local
                    && *candidate == key
                    && self
                        .executable
                        .index
                        .direct_top_level_functions
                        .contains(binding)
                    && !self.executable.index.reassigned_bindings.contains(binding)
            })
            .then_some(key)
    }

    /// Translate only after the root's digest, relative declaration bounds,
    /// export and binding identity resolve in the current canonical program.
    pub(crate) fn resolve_saved_position(
        &self,
        identity: &ConditionalProviderIdentity,
        position: u32,
    ) -> Option<u32> {
        self.resolve_current_root(identity)?;
        let producing_start = identity
            .source
            .as_ref()
            .map_or(self.source.source_start, |source| source.source_start);
        self.source
            .current_position(position.checked_sub(producing_start)?)
    }

    pub(crate) fn resolve_saved_callable(
        &self,
        identity: &ConditionalProviderIdentity,
        key: u32,
    ) -> Option<u32> {
        let current = self.resolve_saved_position(identity, key)?;
        self.callable_source(current)?;
        Some(current)
    }
}

#[derive(Clone, Default)]
pub struct FrozenProviderPrograms {
    programs: Arc<BTreeMap<String, ProviderProgram>>,
}

impl fmt::Debug for FrozenProviderPrograms {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_map()
            .entries(
                self.programs
                    .iter()
                    .map(|(module, program)| (module, &program.source)),
            )
            .finish()
    }
}

#[cfg(test)]
struct ProviderProgramPreparation<'a> {
    pub source: ProviderProgramSource,
    pub body: &'a [ModuleItem],
    pub exports: BTreeMap<String, String>,
    pub inputs: AnalysisInputs,
}

impl FrozenProviderPrograms {
    pub(crate) fn from_programs(programs: BTreeMap<String, ProviderProgram>) -> Result<Self> {
        let mut scopes = BTreeSet::new();
        for (module, program) in &programs {
            ensure!(
                module == &program.source.module && scopes.insert(program.source.scope),
                "provider program namespace is inconsistent"
            );
        }
        Ok(Self {
            programs: Arc::new(programs),
        })
    }

    /// Return source bounds, digest, and canonical operation sites from the
    /// same retained programs used to execute provider evidence. This does not
    /// parse or scan source again.
    pub fn source_inventories(
        &self,
    ) -> BTreeMap<String, crate::analysis_inputs::saved::ProviderSourceInventory> {
        self.programs
            .iter()
            .map(|(module, program)| {
                (
                    module.clone(),
                    crate::analysis_inputs::saved::ProviderSourceInventory {
                        source: program.source.clone(),
                        source_sites: program.executable.index.source_operation_sites.clone(),
                        callable_spans: program.executable.index.callable_spans.clone(),
                        class_spans: program.executable.index.class_spans.clone(),
                    },
                )
            })
            .collect()
    }

    #[cfg(test)]
    fn prepare_current(sources: Vec<ProviderProgramPreparation<'_>>) -> Result<Self> {
        let mut programs = BTreeMap::new();
        for source in sources {
            let ProviderProgramPreparation {
                source,
                body,
                exports,
                mut inputs,
            } = source;
            inputs.heap_resolver = Some(SourceHeapResolver::new(source.scope));
            inputs.current_chunk_id = Some(source.module.clone());
            let module_cells = inputs.module_cells.clone();
            let integrity = inputs.native_integrity.clone();
            let views = top_level_item_views(body);
            let array_spreads = StableArraySpreadShapes::collect(body, &views);
            let mut prepared = prepare_callable_capabilities(
                &views,
                array_spreads,
                exports.values().cloned().collect(),
                inputs,
            );
            if let Some(integrity) = integrity {
                prepared.index.apply_native_integrity(&integrity);
            }
            let module = source.module.clone();
            let program = FrozenProviderProgram::new(prepared.index, prepared.shadowed)
                .bind_source(source, module_cells, exports)?;
            ensure!(
                programs.insert(module, program).is_none(),
                "duplicate current provider program"
            );
        }
        Self::from_programs(programs)
    }

    /// Runtime source sites are checked in their own current program. This
    /// does not authenticate a serialized site: restore first reexecutes and
    /// relocates provider evidence through the checked source identity.
    pub(crate) fn contains_source_operation_site(
        &self,
        scope: HeapModuleScope,
        site: &crate::facts::sync_effects::SourceOperationSite,
    ) -> bool {
        self.programs.values().any(|program| {
            program.source.scope == scope
                && program
                    .executable
                    .index
                    .source_operation_sites
                    .contains(site)
        })
    }

    pub(crate) fn callable_source(&self, module: &str, key: u32) -> Option<ProviderCallableSource> {
        self.programs.get(module)?.callable_source(key)
    }

    pub(crate) fn callable_program(
        &self,
        scope: HeapModuleScope,
        sha256: &str,
        origin: u32,
    ) -> Option<(&ProviderProgram, u32)> {
        let program = self
            .programs
            .values()
            .find(|program| program.source.scope == scope)?;
        if program.source.sha256 != sha256 {
            return None;
        }
        Some((program, program.callable_position(origin)?))
    }

    pub(crate) fn program(
        &self,
        identity: &ConditionalProviderIdentity,
    ) -> Option<&ProviderProgram> {
        let program = self.programs.get(&identity.defining_module)?;
        program.resolve_current_root(identity).map(|_| program)
    }

    /// Source identity only: graph execution and capture/birth verification
    /// remain prerequisites of runtime admission.
    pub fn validate_transfers(
        &self,
        imported: &BTreeMap<String, BTreeMap<String, CallableSummary>>,
    ) -> Result<()> {
        for transfer in imported
            .values()
            .flat_map(BTreeMap::values)
            .flat_map(|summary| &summary.provider_transfers)
        {
            let program = self.program(&transfer.identity);
            ensure!(
                program.is_some(),
                "saved provider transfer does not resolve in current prepared source: {}:{}",
                transfer.identity.defining_module,
                transfer.identity.export
            );
            let program = program.expect("provider identity was checked");
            for alternative in &transfer.alternatives {
                for (cell, expected) in &alternative.module_cells.invariants {
                    let current = program
                        .module_cells
                        .iter()
                        .find_map(|(binding, candidate)| {
                            (candidate == cell)
                                .then(|| program.primitive_cell_invariant(binding))
                                .flatten()
                        });
                    ensure!(
                        current.as_ref() == Some(expected),
                        "saved provider module cell does not match current source declaration and writers"
                    );
                }
            }
        }
        Ok(())
    }

    /// Validate provider body identities retained inside saved execution
    /// receipts after the current projection has admitted its provider source
    /// closure. The serialized target is only a coordinate: the current
    /// prepared registry must resolve its module, digest, callable start,
    /// callable span, and callable-kind inventory entry.
    pub fn validate_saved_provider_call_targets(
        &self,
        imported: &BTreeMap<String, BTreeMap<String, CallableSummary>>,
        chunks: &BTreeMap<String, crate::facts::ChunkFactAnalysis>,
    ) -> Result<()> {
        let mut current_callable = |module: &str, sha256: &str, source_origin: u32| {
            self.resolves_current_provider_callable(module, sha256, source_origin)
        };

        for (chunk, analysis) in chunks {
            ensure!(
                analysis.provider_callables_are_current(&mut current_callable),
                "saved execution receipt in {chunk} names a provider callable outside the current prepared inventory"
            );
        }

        for summary in imported.values().flat_map(BTreeMap::values) {
            if let Some(body) = summary
                .foreign_call
                .as_ref()
                .and_then(|call| call.body_effects.as_deref())
            {
                ensure!(
                    body.effects
                        .provider_callables_are_current(&mut current_callable),
                    "saved foreign-call receipt names a provider callable outside the current prepared inventory"
                );
            }
            for body in summary
                .foreign_members
                .values()
                .filter_map(|call| call.body_effects.as_deref())
            {
                ensure!(
                    body.effects
                        .provider_callables_are_current(&mut current_callable),
                    "saved foreign-member receipt names a provider callable outside the current prepared inventory"
                );
            }
            for transfer in &summary.provider_transfers {
                for alternative in &transfer.alternatives {
                    ensure!(
                        alternative
                            .effects
                            .provider_callables_are_current(&mut current_callable),
                        "saved conditional-provider receipt names a provider callable outside the current prepared inventory"
                    );
                }
            }
        }
        Ok(())
    }

    fn resolves_current_provider_callable(
        &self,
        module: &str,
        sha256: &str,
        source_origin: u32,
    ) -> bool {
        let Some(program) = self.programs.get(module) else {
            return false;
        };
        if program.source.module != module
            || program.source.sha256 != sha256
            || !matches!(program.source.scope, HeapModuleScope::Chunk(_))
        {
            return false;
        }
        let Some(key) = program.callable_position(source_origin) else {
            return false;
        };
        let Some(current) = program.callable_source(key) else {
            return false;
        };
        current.sha256 == sha256
            && current.callable_start == source_origin
            && current.callable_start < current.callable_end
            && program
                .executable
                .index
                .supports_synchronous_provider_activation(key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use js_ast::{parse_js_module, with_swc_globals};
    use source_identity::ChunkTable;

    #[test]
    fn module_capture_requires_the_current_declaration_and_writer_proof() {
        with_swc_globals(|| {
            let mut chunks = ChunkTable::default();
            let chunk = chunks.intern("provider".into());
            let parsed = parse_js_module(
                "provider.js",
                "let count = 0; export function factory() { return () => count; }",
            )
            .unwrap();
            let views = top_level_item_views(&parsed.module.body);
            let prepared = prepare_callable_capabilities(
                &views,
                StableArraySpreadShapes::default(),
                BTreeSet::new(),
                AnalysisInputs {
                    heap_resolver: Some(SourceHeapResolver::new(HeapModuleScope::Chunk(chunk))),
                    ..Default::default()
                },
            );
            let cells: BTreeMap<_, _> = prepared
                .module_cell_declarations()
                .into_iter()
                .map(|(binding, declaration_origin)| {
                    (
                        binding,
                        ModuleCellId {
                            chunk,
                            declaration_origin,
                        },
                    )
                })
                .collect();
            let files = parsed.cm.files();
            let source = ProviderProgramSource {
                module: "provider".into(),
                scope: HeapModuleScope::Chunk(chunk),
                sha256: "a".repeat(64),
                source_start: files[0].start_pos.0,
                source_end: files[0].end_pos.0,
            };
            let mut program = FrozenProviderProgram::new(prepared.index, prepared.shadowed)
                .bind_source(
                    source,
                    cells,
                    BTreeMap::from([("factory".into(), "factory".into())]),
                )
                .unwrap();
            let binding = program
                .module_cells
                .keys()
                .find(|binding| binding.0.as_ref() == "count")
                .unwrap()
                .clone();
            assert!(program.primitive_cell_invariant(&binding).is_some());
            program
                .module_cells
                .get_mut(&binding)
                .unwrap()
                .declaration_origin += 1;
            assert!(program.primitive_cell_invariant(&binding).is_none());
        });
    }

    #[test]
    fn same_spelling_providers_require_their_own_program_and_current_callable() {
        with_swc_globals(|| {
            let mut chunks = ChunkTable::default();
            let left = chunks.intern("left".into());
            let right = chunks.intern("right".into());
            let parsed_left =
                parse_js_module("left.js", "export function factory() { return () => 1; }")
                    .unwrap();
            let parsed_right =
                parse_js_module("right.js", "export function factory() { return () => 2; }")
                    .unwrap();
            let sources = [
                ("left", left, &parsed_left, "a".repeat(64)),
                ("right", right, &parsed_right, "b".repeat(64)),
            ];
            let prepare = || {
                sources
                    .iter()
                    .map(|(name, chunk, parsed, digest)| {
                        let files = parsed.cm.files();
                        ProviderProgramPreparation {
                            source: ProviderProgramSource {
                                module: (*name).into(),
                                scope: HeapModuleScope::Chunk(*chunk),
                                sha256: digest.clone(),
                                source_start: files[0].start_pos.0,
                                source_end: files[0].end_pos.0,
                            },
                            body: &parsed.module.body,
                            exports: BTreeMap::from([("factory".into(), "factory".into())]),
                            inputs: AnalysisInputs::default(),
                        }
                    })
                    .collect()
            };
            let programs = FrozenProviderPrograms::prepare_current(prepare()).unwrap();
            let independent = FrozenProviderPrograms::prepare_current(prepare()).unwrap();
            let left_program = &programs.programs["left"];
            let key = *left_program
                .executable
                .index
                .hoisted_bindings
                .values()
                .next()
                .unwrap();
            let origin = left_program.source.source_offset(key).unwrap();
            let site = left_program
                .executable
                .index
                .source_operation_sites
                .iter()
                .next()
                .unwrap();
            assert!(independent.contains_source_operation_site(HeapModuleScope::Chunk(left), site));
            assert!(!independent.contains_source_operation_site(
                HeapModuleScope::LocalAnalysis(
                    crate::facts::abstract_domain::value::LocalAnalysisScopeId::fresh()
                ),
                site
            ));
            let right_program = &programs.programs["right"];
            assert!(right_program.executable.index.callables.contains_key(&key));
            let (resolved_right, right_key) = independent
                .callable_program(HeapModuleScope::Chunk(right), &"b".repeat(64), origin)
                .unwrap();
            assert_eq!(resolved_right.source.scope, HeapModuleScope::Chunk(right));
            assert_eq!(right_key, key);
            assert!(
                independent
                    .callable_program(HeapModuleScope::Chunk(left), &"a".repeat(64), origin)
                    .is_some()
            );
            assert!(
                independent
                    .callable_program(HeapModuleScope::Chunk(right), &"a".repeat(64), origin)
                    .is_none()
            );
            assert!(
                independent
                    .callable_program(HeapModuleScope::Chunk(left), &"b".repeat(64), origin)
                    .is_none()
            );
            assert!(
                independent
                    .callable_program(HeapModuleScope::Chunk(left), &"a".repeat(64), origin + 1)
                    .is_none()
            );
            let identity = ConditionalProviderIdentity {
                defining_module: "left".into(),
                export: "factory".into(),
                local: "factory".into(),
                callable_key: key,
                source: programs.callable_source("left", key),
            };
            assert!(independent.program(&identity).is_some());
            let program = independent.program(&identity).unwrap();
            let nested = *program
                .executable
                .index
                .callables
                .keys()
                .find(|candidate| **candidate != key)
                .unwrap();
            let mut relocated = identity.clone();
            let receipt = relocated.source.as_mut().unwrap();
            receipt.source_start += 1000;
            relocated.callable_key += 1000;
            assert_eq!(program.resolve_current_root(&relocated), Some(key));
            assert_eq!(
                program.resolve_saved_callable(&relocated, nested + 1000),
                Some(nested)
            );
            assert!(
                program
                    .resolve_saved_callable(&relocated, nested + 1001)
                    .is_none()
            );
            let mut wrong_digest = relocated.clone();
            wrong_digest.source.as_mut().unwrap().sha256 = "b".repeat(64);
            assert!(program.resolve_current_root(&wrong_digest).is_none());
            let mut wrong_bounds = relocated.clone();
            wrong_bounds.source.as_mut().unwrap().callable_end += 1;
            assert!(program.resolve_current_root(&wrong_bounds).is_none());
            relocated.source = None;
            assert!(program.resolve_current_root(&relocated).is_none());
            assert!(
                independent
                    .program(&ConditionalProviderIdentity {
                        defining_module: "missing".into(),
                        ..identity.clone()
                    })
                    .is_none()
            );
            assert!(
                independent
                    .program(&ConditionalProviderIdentity {
                        local: "missing".into(),
                        ..identity.clone()
                    })
                    .is_none()
            );
            assert!(
                independent
                    .program(&ConditionalProviderIdentity {
                        callable_key: key + 1,
                        ..identity
                    })
                    .is_none()
            );
        });
    }

    #[test]
    fn saved_provider_callable_coordinates_resolve_only_in_current_callable_inventory() {
        with_swc_globals(|| {
            let source_text = "const notCallable = 1; export function factory() { return 1; } const arrowFactory = () => 1; async function asyncFactory() {} function* generatorFactory() {} class Box { method() {} }";
            let parsed = parse_js_module("provider.js", source_text).unwrap();
            let mut chunks = ChunkTable::default();
            let chunk = chunks.intern("static/provider".into());
            let files = parsed.cm.files();
            let sha256 = "a".repeat(64);
            let programs =
                FrozenProviderPrograms::prepare_current(vec![ProviderProgramPreparation {
                    source: ProviderProgramSource {
                        module: "static/provider".into(),
                        scope: HeapModuleScope::Chunk(chunk),
                        sha256: sha256.clone(),
                        source_start: files[0].start_pos.0,
                        source_end: files[0].end_pos.0,
                    },
                    body: &parsed.module.body,
                    exports: BTreeMap::from([("factory".into(), "factory".into())]),
                    inputs: AnalysisInputs::default(),
                }])
                .unwrap();
            let program = &programs.programs["static/provider"];
            let origin_for = |needle: &str| {
                program
                    .executable
                    .index
                    .callables
                    .keys()
                    .find_map(|key| {
                        let span = program.executable.index.callable_spans.get(key)?;
                        let start = key.checked_sub(program.source.source_start)? as usize;
                        let end = span.hi.0.checked_sub(program.source.source_start)? as usize;
                        source_text
                            .get(start..end)
                            .is_some_and(|source| source.contains(needle))
                            .then(|| program.source.source_offset(*key))?
                    })
                    .expect("callable source marker is in the canonical callable inventory")
            };
            let factory_origin = origin_for("factory");
            let arrow_origin = origin_for("=>");
            let async_origin = origin_for("asyncFactory");
            let generator_origin = origin_for("generatorFactory");
            let class_method_origin = program
                .executable
                .index
                .class_method_callables
                .iter()
                .next()
                .and_then(|key| program.source.source_offset(*key))
                .expect("class method is marked in the canonical callable inventory");

            assert!(programs.resolves_current_provider_callable(
                "static/provider",
                &sha256,
                factory_origin
            ));
            assert!(programs.resolves_current_provider_callable(
                "static/provider",
                &sha256,
                arrow_origin
            ));
            assert!(!programs.resolves_current_provider_callable(
                "static/missing",
                &sha256,
                factory_origin
            ));
            assert!(!programs.resolves_current_provider_callable(
                "static/provider",
                &"b".repeat(64),
                factory_origin
            ));
            assert!(!programs.resolves_current_provider_callable(
                "static/provider",
                &sha256,
                factory_origin + 1
            ));
            for ineligible_origin in [async_origin, generator_origin, class_method_origin] {
                assert!(
                    !programs.resolves_current_provider_callable(
                        "static/provider",
                        &sha256,
                        ineligible_origin
                    ),
                    "only current synchronous function/arrow bodies support provider activation"
                );
            }
            assert!(
                !programs.resolves_current_provider_callable("static/provider", &sha256, 0),
                "a top-level non-callable declaration is not a provider callable kind"
            );
        });
    }
}
