//! Explicit saved projections for imported capabilities and native census data.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use analysis_codec::{ContextOrdinals, SavedChunk, SavedModuleCell};
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};

use super::{
    BindingCellDeclarationIdentity, BindingCellStoreEvidence, CallableSummary,
    ConditionalProviderCompletion, ConditionalProviderIdentity, ConditionalProviderTransfer,
    ConditionalProviderValue, ForeignBodyEffects, ForeignNativeOperation, HostBehaviorKey,
    ModuleCellAccesses, ModuleCellId, NativeIntegrity, NativeIntrinsic, ParameterHeapRead,
    PrimitiveCellInvariant, ProofDependencies, ProviderCallableSource, RecordInputShape,
    ResolvedProviderDependencyClosure, SourceUnitOrdinal, ValueInputEvidence, ValueInputRef,
};
use crate::facts::abstract_domain::value::{ClassDefinitionId, HeapAddress, HeapModuleScope};
use crate::facts::sync_effects::{
    Completion, EffectCoverageFragment, LocalBodyIdentity, SavedEffectCoverageFragment,
    SourceOperationSite,
};
use crate::reports::saved::{SavedNativeMutationWitness, SavedNativePrototypeMutation};
use crate::reports::schema::NeverWrittenOverride;
use swc_common::{BytePos, Span};

/// Source metadata and canonical operation inventory retained by one already
/// prepared provider program. This runtime-only value is the sole coordinate
/// authority for saved provider evidence.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ProviderSourceInventory {
    pub source: super::ProviderProgramSource,
    pub source_sites: BTreeSet<SourceOperationSite>,
    /// Canonical callable keys and declaration spans from the same prepared
    /// execution index. Keys in saved evidence are validated against this
    /// table before and after rebinding.
    pub callable_spans: BTreeMap<u32, Span>,
    /// Class declaration keys are separate from callable keys. Ordinary
    /// class-constructor body effects can be rooted at this source position.
    pub class_spans: BTreeMap<u32, Span>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
struct SavedProviderSourceAnchor {
    module: String,
    sha256: String,
    source_len: u32,
}

impl SavedProviderSourceAnchor {
    fn snapshot(module: &str, inventory: &ProviderSourceInventory) -> Result<Self> {
        validate_inventory(module, inventory)?;
        Ok(Self {
            module: module.to_owned(),
            sha256: inventory.source.sha256.clone(),
            source_len: inventory.source.source_end - inventory.source.source_start,
        })
    }

    fn current<'a>(
        &self,
        inventories: &'a BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<&'a ProviderSourceInventory> {
        let inventory = inventory(&self.module, inventories)?;
        validate_inventory(&self.module, inventory)?;
        ensure!(
            inventory.source.sha256 == self.sha256
                && inventory.source.source_end - inventory.source.source_start == self.source_len,
            "saved provider source anchor differs from the canonical current source"
        );
        Ok(inventory)
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
struct SavedProviderCallableSource {
    sha256: String,
    callable_start: u32,
    callable_end: u32,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
struct SavedConditionalProviderIdentity {
    defining_module: String,
    export: String,
    local: String,
    callable_key: u32,
    source: SavedProviderCallableSource,
}

fn snapshot_provider_identity(
    identity: &ConditionalProviderIdentity,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<(SavedProviderSourceAnchor, SavedConditionalProviderIdentity)> {
    let current = inventory(&identity.defining_module, inventories)?;
    let source = identity
        .source
        .as_ref()
        .context("conditional provider identity has no canonical source anchor")?;
    ensure!(
        source.sha256 == current.source.sha256
            && source.source_start == current.source.source_start
            && source.callable_start < source.callable_end
            && source.callable_end <= current.source.source_end - current.source.source_start,
        "conditional provider callable source differs from its canonical source inventory"
    );
    let key = map_callable_key(
        &identity.defining_module,
        identity.callable_key,
        inventories,
        CoordinateDirection::ToSaved,
    )?;
    let span = current
        .callable_spans
        .get(&identity.callable_key)
        .expect("map_callable_key validated the key");
    ensure!(
        span.lo.0.checked_sub(current.source.source_start) == Some(source.callable_start)
            && span.hi.0.checked_sub(current.source.source_start) == Some(source.callable_end)
            && key == source.callable_start,
        "conditional provider callable range does not match its canonical callable entry"
    );
    Ok((
        SavedProviderSourceAnchor::snapshot(&identity.defining_module, current)?,
        SavedConditionalProviderIdentity {
            defining_module: identity.defining_module.clone(),
            export: identity.export.clone(),
            local: identity.local.clone(),
            callable_key: key,
            source: SavedProviderCallableSource {
                sha256: source.sha256.clone(),
                callable_start: source.callable_start,
                callable_end: source.callable_end,
            },
        },
    ))
}

fn normalize_provider_identity(
    identity: &ConditionalProviderIdentity,
    anchor: &SavedProviderSourceAnchor,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<SavedConditionalProviderIdentity> {
    ensure!(
        identity.defining_module == anchor.module,
        "conditional provider identity module differs from its saved source anchor"
    );
    let current = anchor.current(inventories)?;
    let source = identity
        .source
        .as_ref()
        .context("callable summary has no provider source identity")?;
    ensure!(
        source.sha256 == anchor.sha256
            && source.callable_start < source.callable_end
            && source.callable_end <= anchor.source_len,
        "callable summary provider source differs from the saved source anchor"
    );
    let relative_key = identity
        .callable_key
        .checked_sub(source.source_start)
        .context("callable summary key precedes its source base")?;
    let key = map_callable_key(
        &identity.defining_module,
        relative_key,
        inventories,
        CoordinateDirection::ToCurrent,
    )?;
    let span = current.callable_spans.get(&key).expect("key was validated");
    ensure!(
        span.lo.0.checked_sub(current.source.source_start) == Some(source.callable_start)
            && span.hi.0.checked_sub(current.source.source_start) == Some(source.callable_end)
            && relative_key == source.callable_start,
        "callable summary source range does not match the canonical callable entry"
    );
    Ok(SavedConditionalProviderIdentity {
        defining_module: identity.defining_module.clone(),
        export: identity.export.clone(),
        local: identity.local.clone(),
        callable_key: relative_key,
        source: SavedProviderCallableSource {
            sha256: source.sha256.clone(),
            callable_start: source.callable_start,
            callable_end: source.callable_end,
        },
    })
}

fn restore_provider_identity(
    identity: SavedConditionalProviderIdentity,
    anchor: &SavedProviderSourceAnchor,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<ConditionalProviderIdentity> {
    ensure!(
        identity.defining_module == anchor.module
            && identity.source.sha256 == anchor.sha256
            && identity.source.callable_start == identity.callable_key
            && identity.source.callable_start < identity.source.callable_end
            && identity.source.callable_end <= anchor.source_len,
        "saved conditional provider identity is not anchored to its source"
    );
    let current = anchor.current(inventories)?;
    let callable_key = map_callable_key(
        &identity.defining_module,
        identity.callable_key,
        inventories,
        CoordinateDirection::ToCurrent,
    )?;
    let span = current
        .callable_spans
        .get(&callable_key)
        .expect("map_callable_key validated the key");
    ensure!(
        span.lo.0.checked_sub(current.source.source_start) == Some(identity.source.callable_start)
            && span.hi.0.checked_sub(current.source.source_start)
                == Some(identity.source.callable_end),
        "saved provider callable range differs from the current canonical callable"
    );
    Ok(ConditionalProviderIdentity {
        defining_module: identity.defining_module,
        export: identity.export,
        local: identity.local,
        callable_key,
        source: Some(ProviderCallableSource {
            sha256: identity.source.sha256,
            source_start: current.source.source_start,
            callable_start: identity.source.callable_start,
            callable_end: identity.source.callable_end,
        }),
    })
}

fn validate_inventory(module: &str, inventory: &ProviderSourceInventory) -> Result<()> {
    ensure!(
        inventory.source.module == module
            && inventory.source.source_start <= inventory.source.source_end
            && inventory.source.sha256.len() == 64
            && inventory
                .source
                .sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit()),
        "prepared provider source inventory is malformed"
    );
    ensure!(
        inventory.source_sites.iter().all(|site| {
            site.span.lo.0 >= inventory.source.source_start
                && site.span.hi.0 <= inventory.source.source_end
                && site.span.lo.0 <= site.span.hi.0
        }),
        "prepared provider source site lies outside its source file"
    );
    ensure!(
        inventory.callable_spans.iter().all(|(key, span)| {
            *key >= inventory.source.source_start
                && *key < inventory.source.source_end
                && span.lo.0 >= inventory.source.source_start
                && span.hi.0 <= inventory.source.source_end
                && span.lo.0 <= span.hi.0
        }),
        "prepared provider callable span lies outside its source file"
    );
    ensure!(
        inventory.class_spans.iter().all(|(key, span)| {
            *key >= inventory.source.source_start
                && *key < inventory.source.source_end
                && span.lo.0 >= inventory.source.source_start
                && span.hi.0 <= inventory.source.source_end
                && span.lo.0 <= span.hi.0
        }),
        "prepared provider class span lies outside its source file"
    );
    Ok(())
}

fn inventory<'a>(
    module: &str,
    inventories: &'a BTreeMap<String, ProviderSourceInventory>,
) -> Result<&'a ProviderSourceInventory> {
    let inventory = inventories
        .get(module)
        .with_context(|| format!("prepared provider source inventory is missing for {module:?}"))?;
    ensure!(
        inventory.source.module == module
            && inventory.source.source_start <= inventory.source.source_end,
        "prepared provider source inventory header is malformed"
    );
    Ok(inventory)
}

#[derive(Clone, Copy)]
enum CoordinateDirection {
    ToSaved,
    ToCurrent,
}

fn map_position(
    module: &str,
    position: u32,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<u32> {
    let source = &inventory(module, inventories)?.source;
    let source_len = source.source_end - source.source_start;
    match direction {
        CoordinateDirection::ToSaved => {
            ensure!(
                position >= source.source_start && position < source.source_end,
                "provider source position is outside its anchored input"
            );
            Ok(position - source.source_start)
        }
        CoordinateDirection::ToCurrent => {
            ensure!(
                position < source_len,
                "saved provider source position is out of bounds"
            );
            source
                .source_start
                .checked_add(position)
                .context("provider source position overflow")
        }
    }
}

fn map_callable_key(
    module: &str,
    key: u32,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<u32> {
    let mapped = map_position(module, key, inventories, direction)?;
    ensure!(
        inventory(module, inventories)?
            .callable_spans
            .contains_key(match direction {
                CoordinateDirection::ToSaved => &key,
                CoordinateDirection::ToCurrent => &mapped,
            }),
        "provider callable key is absent from its canonical callable inventory"
    );
    Ok(mapped)
}

fn map_class_key(
    module: &str,
    key: u32,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<u32> {
    let mapped = map_position(module, key, inventories, direction)?;
    ensure!(
        inventory(module, inventories)?
            .class_spans
            .contains_key(match direction {
                CoordinateDirection::ToSaved => &key,
                CoordinateDirection::ToCurrent => &mapped,
            }),
        "provider class key is absent from its canonical class inventory"
    );
    Ok(mapped)
}

fn map_callable_or_class_key(
    module: &str,
    key: u32,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<u32> {
    let mapped = map_position(module, key, inventories, direction)?;
    let inventory = inventory(module, inventories)?;
    ensure!(
        match direction {
            CoordinateDirection::ToSaved => {
                inventory.callable_spans.contains_key(&key)
                    || inventory.class_spans.contains_key(&key)
            }
            CoordinateDirection::ToCurrent => {
                inventory.callable_spans.contains_key(&mapped)
                    || inventory.class_spans.contains_key(&mapped)
            }
        },
        "provider body key is absent from the canonical callable and class inventories"
    );
    Ok(mapped)
}

fn map_span(
    module: &str,
    span: Span,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<Span> {
    let source = &inventory(module, inventories)?.source;
    let source_len = source.source_end - source.source_start;
    let (lo, hi) = match direction {
        CoordinateDirection::ToSaved => {
            ensure!(
                span.lo.0 >= source.source_start
                    && span.hi.0 <= source.source_end
                    && span.lo.0 <= span.hi.0,
                "provider source span is outside its anchored input"
            );
            (
                span.lo.0 - source.source_start,
                span.hi.0 - source.source_start,
            )
        }
        CoordinateDirection::ToCurrent => {
            ensure!(
                span.lo.0 <= source_len && span.hi.0 <= source_len && span.lo.0 <= span.hi.0,
                "saved provider source span is out of bounds"
            );
            (
                source
                    .source_start
                    .checked_add(span.lo.0)
                    .context("provider source span overflow")?,
                source
                    .source_start
                    .checked_add(span.hi.0)
                    .context("provider source span overflow")?,
            )
        }
    };
    Ok(Span::new(BytePos(lo), BytePos(hi)))
}

fn map_body(
    body: &LocalBodyIdentity,
    default_module: &str,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<LocalBodyIdentity> {
    match body {
        LocalBodyIdentity::Callable { key, .. } => {
            map_callable_key(default_module, *key, inventories, direction)?;
        }
        LocalBodyIdentity::Construction { class, .. } => {
            map_class_key(default_module, *class, inventories, direction)?;
        }
        LocalBodyIdentity::ImportedCallable(identity) => {
            map_callable_or_class_key(
                identity.defining_module(),
                identity.body_key(),
                inventories,
                direction,
            )?;
        }
        _ => {}
    }
    body.map_provider_source_coordinates(
        default_module,
        |module, position| map_position(module, position, inventories, direction),
        |module, span| map_span(module, span, inventories, direction),
        |module, sha256, offset| {
            let source = &inventory(module, inventories)?.source;
            ensure!(
                source.sha256 == sha256 && offset < source.source_end - source.source_start,
                "provider callable source identity is outside its canonical source"
            );
            let current = source
                .current_position(offset)
                .context("provider callable origin overflow")?;
            ensure!(
                inventory(module, inventories)?
                    .callable_spans
                    .contains_key(&current),
                "provider callable origin is absent from its canonical callable inventory"
            );
            Ok(())
        },
    )
}

fn body_module<'a>(body: &'a LocalBodyIdentity, default_module: &'a str) -> &'a str {
    match body {
        LocalBodyIdentity::ImportedCallable(identity) => identity.defining_module(),
        LocalBodyIdentity::ProviderCallable {
            defining_module, ..
        } => defining_module,
        _ => default_module,
    }
}

fn add_heap_module(
    scope: HeapModuleScope,
    ordinals: &ContextOrdinals<'_>,
    modules: &mut BTreeSet<String>,
) -> Result<()> {
    match scope {
        HeapModuleScope::Chunk(chunk) => {
            modules.insert(ordinals.snapshot_chunk(chunk)?.0);
        }
        HeapModuleScope::LocalAnalysis(_) => {
            bail!("local-analysis heap identity cannot cross saved provider boundary")
        }
    }
    Ok(())
}

fn effect_source_modules(
    effects: &EffectCoverageFragment,
    default_module: &str,
    ordinals: &ContextOrdinals<'_>,
) -> Result<BTreeSet<String>> {
    let mut body_modules = BTreeSet::new();
    let mut site_modules = BTreeSet::new();
    let mut heap_modules = BTreeSet::new();
    let mut loss_modules = BTreeSet::new();
    let mut identity = effects.clone();
    identity.map_source_coordinates(
        |body| {
            body_modules.insert(body_module(body, default_module).to_owned());
            Ok(body.clone())
        },
        |site| {
            site_modules.insert(body_module(&site.body, default_module).to_owned());
            Ok(site.clone())
        },
        |address| {
            match address {
                HeapAddress::GlobalObject => {}
                HeapAddress::AllocationFamily { module, .. } => {
                    add_heap_module(module, ordinals, &mut heap_modules)?;
                }
                HeapAddress::ClassObject { class }
                | HeapAddress::ConstructorInstances { class } => {
                    add_heap_module(class.module, ordinals, &mut heap_modules)?;
                }
                HeapAddress::ProviderAllocationFamily {
                    consumer, producer, ..
                } => {
                    add_heap_module(consumer, ordinals, &mut heap_modules)?;
                    add_heap_module(producer.module, ordinals, &mut heap_modules)?;
                }
            }
            Ok(address)
        },
        |span| {
            // Precision-loss spans carry no module tag, so their checked
            // source is the provider body that owns this fragment.
            loss_modules.insert(default_module.to_owned());
            Ok(span)
        },
    )?;
    let mut modules = BTreeSet::from([default_module.to_owned()]);
    modules.extend(body_modules);
    modules.extend(site_modules);
    modules.extend(heap_modules);
    modules.extend(loss_modules);
    Ok(modules)
}

fn module_cell_source_modules(
    accesses: &ModuleCellAccesses,
    ordinals: &ContextOrdinals<'_>,
) -> Result<BTreeSet<String>> {
    accesses
        .reads
        .iter()
        .chain(&accesses.writes)
        .chain(accesses.invariants.keys())
        .map(|cell| Ok(ordinals.snapshot_module_cell(*cell)?.chunk.0))
        .collect()
}

fn saved_module_cell_source_modules(accesses: &SavedModuleCellAccesses) -> BTreeSet<String> {
    accesses
        .reads
        .iter()
        .chain(&accesses.writes)
        .chain(accesses.invariants.iter().map(|invariant| &invariant.cell))
        .map(|cell| cell.chunk.0.clone())
        .collect()
}

fn snapshot_source_anchors(
    modules: &BTreeSet<String>,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<BTreeMap<String, SavedProviderSourceAnchor>> {
    modules
        .iter()
        .map(|module| {
            let current = inventory(module, inventories)?;
            Ok((
                module.clone(),
                SavedProviderSourceAnchor::snapshot(module, current)?,
            ))
        })
        .collect()
}

fn validate_source_anchors(
    source: &SavedProviderSourceAnchor,
    related: &BTreeMap<String, SavedProviderSourceAnchor>,
    required_modules: &BTreeSet<String>,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<()> {
    source.current(inventories)?;
    for (module, anchor) in related {
        ensure!(
            module == &anchor.module,
            "saved provider source-anchor key differs from its module"
        );
        ensure!(
            module != &source.module,
            "saved provider source anchor is duplicated"
        );
        anchor.current(inventories)?;
    }
    let provided_modules = std::iter::once(source.module.clone())
        .chain(related.keys().cloned())
        .collect::<BTreeSet<_>>();
    ensure!(
        &provided_modules == required_modules,
        "saved provider source-anchor set does not exactly cover referenced modules"
    );
    Ok(())
}

fn related_source_anchors(
    modules: &BTreeSet<String>,
    primary_module: &str,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<BTreeMap<String, SavedProviderSourceAnchor>> {
    let mut related = modules.clone();
    related.remove(primary_module);
    snapshot_source_anchors(&related, inventories)
}

fn map_site(
    site: &SourceOperationSite,
    default_module: &str,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<SourceOperationSite> {
    let module = body_module(&site.body, default_module);
    if matches!(direction, CoordinateDirection::ToSaved) {
        ensure!(
            inventory(module, inventories)?.source_sites.contains(site),
            "provider receipt source site is not in its canonical source inventory"
        );
    }
    let mapped = SourceOperationSite {
        owner: site.owner,
        body: map_body(&site.body, default_module, inventories, direction)?,
        span: map_span(module, site.span, inventories, direction)?,
        ordinal: site.ordinal,
        step: site.step,
        control_test_ordinal: site.control_test_ordinal,
    };
    if matches!(direction, CoordinateDirection::ToCurrent) {
        ensure!(
            inventory(module, inventories)?
                .source_sites
                .contains(&mapped),
            "saved provider receipt does not match a full canonical source operation site"
        );
    }
    Ok(mapped)
}

fn chunk_module(scope: HeapModuleScope, ordinals: &ContextOrdinals<'_>) -> Result<String> {
    match scope {
        HeapModuleScope::Chunk(chunk) => Ok(ordinals.snapshot_chunk(chunk)?.0),
        HeapModuleScope::LocalAnalysis(_) => {
            bail!("local-analysis source identity cannot cross saved provider boundary")
        }
    }
}

fn map_class(
    class: ClassDefinitionId,
    ordinals: &ContextOrdinals<'_>,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<ClassDefinitionId> {
    let module = chunk_module(class.module, ordinals)?;
    Ok(ClassDefinitionId {
        module: class.module,
        origin: map_class_key(&module, class.origin, inventories, direction)?,
    })
}

fn map_heap_address(
    address: HeapAddress,
    ordinals: &ContextOrdinals<'_>,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<HeapAddress> {
    Ok(match address {
        HeapAddress::GlobalObject => HeapAddress::GlobalObject,
        HeapAddress::AllocationFamily {
            module,
            origin,
            kind,
        } => HeapAddress::AllocationFamily {
            origin: map_position(
                &chunk_module(module, ordinals)?,
                origin,
                inventories,
                direction,
            )?,
            module,
            kind,
        },
        HeapAddress::ClassObject { class } => HeapAddress::ClassObject {
            class: map_class(class, ordinals, inventories, direction)?,
        },
        HeapAddress::ConstructorInstances { class } => HeapAddress::ConstructorInstances {
            class: map_class(class, ordinals, inventories, direction)?,
        },
        HeapAddress::ProviderAllocationFamily {
            consumer,
            call_origin,
            producer,
            producer_birth,
            slot,
            kind,
        } => {
            ensure!(
                producer_birth.is_none(),
                "runtime-only provider allocation birth cannot be saved as effect support"
            );
            let producer_module = chunk_module(producer.module, ordinals)?;
            let producer_inventory = inventory(&producer_module, inventories)?;
            let producer_key = producer_inventory
                .source
                .current_position(producer.origin)
                .context("provider allocation producer origin is outside its canonical source")?;
            ensure!(
                producer_inventory
                    .callable_spans
                    .contains_key(&producer_key),
                "provider allocation producer is absent from its canonical callable inventory"
            );
            HeapAddress::ProviderAllocationFamily {
                consumer,
                call_origin: map_position(
                    &chunk_module(consumer, ordinals)?,
                    call_origin,
                    inventories,
                    direction,
                )?,
                producer,
                producer_birth: None,
                slot,
                kind,
            }
        }
    })
}

fn map_effects(
    effects: &EffectCoverageFragment,
    default_module: &str,
    ordinals: &ContextOrdinals<'_>,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<EffectCoverageFragment> {
    let mut mapped = effects.clone();
    mapped.map_source_coordinates(
        |body| map_body(body, default_module, inventories, direction),
        |site| map_site(site, default_module, inventories, direction),
        |address| map_heap_address(address, ordinals, inventories, direction),
        |span| map_span(default_module, span, inventories, direction),
    )?;
    Ok(mapped)
}

fn map_provider_completion(
    completion: &ConditionalProviderCompletion,
    default_module: &str,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<ConditionalProviderCompletion> {
    Ok(match completion {
        ConditionalProviderCompletion::Normal { value } => ConditionalProviderCompletion::Normal {
            value: map_provider_value(value, default_module, inventories, direction)?,
        },
        ConditionalProviderCompletion::Throw { origin, value } => {
            ConditionalProviderCompletion::Throw {
                origin: map_site(origin, default_module, inventories, direction)?,
                value: map_provider_value(value, default_module, inventories, direction)?,
            }
        }
    })
}

fn map_provider_value(
    value: &ConditionalProviderValue,
    default_module: &str,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<ConditionalProviderValue> {
    Ok(match value {
        ConditionalProviderValue::Projection(projection) => {
            ConditionalProviderValue::Projection(projection.clone())
        }
        ConditionalProviderValue::RetainedGraph(graph) => {
            let mut graph = graph.clone();
            for node in &mut graph.nodes {
                if let crate::analysis_inputs::ProviderGraphNodeKind::Closure { source_key } =
                    &mut node.kind
                {
                    *source_key =
                        map_callable_key(default_module, *source_key, inventories, direction)?;
                }
            }
            ConditionalProviderValue::RetainedGraph(graph)
        }
    })
}

fn normalize_report_value(
    value: &ConditionalProviderValue,
    module: &str,
    old_source_start: u32,
    anchor: &SavedProviderSourceAnchor,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<ConditionalProviderValue> {
    Ok(match value {
        ConditionalProviderValue::Projection(projection) => {
            ConditionalProviderValue::Projection(projection.clone())
        }
        ConditionalProviderValue::RetainedGraph(graph) => {
            ensure!(
                module == anchor.module,
                "saved provider graph has no matching source anchor"
            );
            let mut graph = graph.clone();
            for node in &mut graph.nodes {
                if let crate::analysis_inputs::ProviderGraphNodeKind::Closure { source_key } =
                    &mut node.kind
                {
                    let relative = source_key
                        .checked_sub(old_source_start)
                        .context("saved provider closure key precedes its source base")?;
                    map_callable_key(
                        module,
                        relative,
                        inventories,
                        CoordinateDirection::ToCurrent,
                    )?;
                    *source_key = relative;
                }
            }
            ConditionalProviderValue::RetainedGraph(graph)
        }
    })
}

fn normalize_report_site(
    site: &SourceOperationSite,
    module: &str,
    old_source_start: u32,
    source_len: u32,
    sha256: &str,
) -> Result<SourceOperationSite> {
    let source_end = old_source_start
        .checked_add(source_len)
        .context("saved provider source bounds overflow")?;
    let map_position = |site_module: &str, position: u32| -> Result<u32> {
        ensure!(
            site_module == module && position >= old_source_start && position < source_end,
            "saved throw site has no matching source-coordinate anchor"
        );
        Ok(position - old_source_start)
    };
    let map_span = |site_module: &str, span: Span| -> Result<Span> {
        ensure!(
            site_module == module
                && span.lo.0 >= old_source_start
                && span.hi.0 <= source_end
                && span.lo.0 <= span.hi.0,
            "saved throw span has no matching source-coordinate anchor"
        );
        Ok(Span::new(
            BytePos(span.lo.0 - old_source_start),
            BytePos(span.hi.0 - old_source_start),
        ))
    };
    let body = site.body.map_provider_source_coordinates(
        module,
        map_position,
        map_span,
        |site_module, site_sha, offset| {
            ensure!(
                site_module == module && site_sha == sha256 && offset < source_len,
                "saved throw body source identity differs from its source anchor"
            );
            Ok(())
        },
    )?;
    ensure!(
        body_module(&site.body, module) == module,
        "saved throw site belongs to a different source module"
    );
    Ok(SourceOperationSite {
        owner: site.owner,
        body,
        span: map_span(module, site.span)?,
        ordinal: site.ordinal,
        step: site.step,
        control_test_ordinal: site.control_test_ordinal,
    })
}

fn normalize_report_completion(
    completion: &ConditionalProviderCompletion,
    module: &str,
    old_source_start: u32,
    anchor: &SavedProviderSourceAnchor,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<ConditionalProviderCompletion> {
    let normalized = match completion {
        ConditionalProviderCompletion::Normal { value } => ConditionalProviderCompletion::Normal {
            value: normalize_report_value(value, module, old_source_start, anchor, inventories)?,
        },
        ConditionalProviderCompletion::Throw { origin, value } => {
            ConditionalProviderCompletion::Throw {
                origin: normalize_report_site(
                    origin,
                    module,
                    old_source_start,
                    anchor.source_len,
                    &anchor.sha256,
                )?,
                value: normalize_report_value(
                    value,
                    module,
                    old_source_start,
                    anchor,
                    inventories,
                )?,
            }
        }
    };
    // The summary itself stores parser-session positions. Authenticate the
    // normalized offset against the same retained canonical inventory before
    // comparing it with the required sidecar.
    match &normalized {
        ConditionalProviderCompletion::Normal { .. } => {}
        ConditionalProviderCompletion::Throw { origin, .. } => {
            map_site(origin, module, inventories, CoordinateDirection::ToCurrent)?;
        }
    }
    Ok(normalized)
}

/// Ordinary foreign-body effect summaries admit only exact normal completion.
/// Keep this closed codec separate from the runtime completion enum, whose
/// other variants can carry abstract values that are not durable identities.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum SavedProviderCompletion {
    Normal(()),
}

impl SavedProviderCompletion {
    fn snapshot(completion: &Completion<(), ()>, message: &str) -> Result<Self> {
        ensure!(completion.is_exact_normal(), "{message}");
        Ok(Self::Normal(()))
    }

    fn restore(self) -> Completion<(), ()> {
        match self {
            Self::Normal(()) => Completion::Normal(()),
        }
    }
}

/// A callable summary whose trusted proof dependencies are explicit artifact
/// data while remaining absent from its ordinary report-facing Serde shape.
#[derive(Serialize, Deserialize)]
pub struct SavedCallableSummary<'a> {
    summary: Cow<'a, CallableSummary>,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    namespace_wrapper_dependencies: Option<ProofDependencies>,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    foreign_call_dependencies: Option<ProofDependencies>,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    foreign_call_body_effects: Option<SavedForeignBodyEffects>,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    foreign_call_initialized_class: Option<ProofDependencies>,
    foreign_member_evidence: BTreeMap<String, SavedForeignMemberEvidence>,
    conditional_bodies: Vec<SavedConditionalProviderTransfer>,
}

#[derive(Serialize, Deserialize)]
struct SavedForeignBodyEffects {
    source: SavedProviderSourceAnchor,
    related_sources: BTreeMap<String, SavedProviderSourceAnchor>,
    callable_key: u32,
    completion: SavedProviderCompletion,
    effects: SavedEffectCoverageFragment<'static>,
}

impl SavedForeignBodyEffects {
    fn snapshot(
        body: &ForeignBodyEffects,
        defining_module: &str,
        ordinals: &ContextOrdinals<'_>,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<Self> {
        let source_inventory = inventory(defining_module, inventories)?;
        let source = SavedProviderSourceAnchor::snapshot(defining_module, source_inventory)?;
        let source_modules = effect_source_modules(&body.effects, defining_module, ordinals)?;
        let related_sources =
            related_source_anchors(&source_modules, defining_module, inventories)?;
        Ok(Self {
            source,
            related_sources,
            callable_key: map_callable_or_class_key(
                defining_module,
                body.callable_key,
                inventories,
                CoordinateDirection::ToSaved,
            )?,
            completion: SavedProviderCompletion::snapshot(
                &body.completion,
                "provider body effects require exact normal completion",
            )?,
            effects: SavedEffectCoverageFragment::snapshot_owned(
                map_effects(
                    &body.effects,
                    defining_module,
                    ordinals,
                    inventories,
                    CoordinateDirection::ToSaved,
                )?,
                ordinals,
            )?,
        })
    }

    fn restore(
        self,
        ordinals: &ContextOrdinals<'_>,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
        defining_module: &str,
        existing: Option<&ForeignBodyEffects>,
        summary_was_borrowed: bool,
    ) -> Result<ForeignBodyEffects> {
        ensure!(
            self.source.module == defining_module,
            "saved provider body source anchor does not match its callable"
        );
        ensure!(
            summary_was_borrowed == existing.is_some(),
            "in-memory provider body effects do not match their saved sidecar presence"
        );
        let callable_key = map_callable_or_class_key(
            defining_module,
            self.callable_key,
            inventories,
            CoordinateDirection::ToCurrent,
        )?;
        let relative_effects = self.effects.restore(ordinals)?;
        let required_modules = effect_source_modules(&relative_effects, defining_module, ordinals)?;
        validate_source_anchors(
            &self.source,
            &self.related_sources,
            &required_modules,
            inventories,
        )?;
        let effects = map_effects(
            &relative_effects,
            defining_module,
            ordinals,
            inventories,
            CoordinateDirection::ToCurrent,
        )?;
        let completion = self.completion.clone().restore();
        if let Some(existing) = existing {
            ensure!(
                map_callable_or_class_key(
                    defining_module,
                    existing.callable_key,
                    inventories,
                    CoordinateDirection::ToSaved,
                )? == self.callable_key
                    && SavedProviderCompletion::snapshot(
                        &existing.completion,
                        "provider body effects require exact normal completion",
                    )? == self.completion
                    && map_effects(
                        &existing.effects,
                        defining_module,
                        ordinals,
                        inventories,
                        CoordinateDirection::ToSaved,
                    )? == relative_effects,
                "in-memory provider body effects do not match their saved evidence"
            );
        }
        Ok(ForeignBodyEffects {
            callable_key,
            completion,
            effects,
        })
    }
}

#[derive(Serialize, Deserialize)]
struct SavedForeignMemberEvidence {
    dependencies: ProofDependencies,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    body_effects: Option<SavedForeignBodyEffects>,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    initialized_class: Option<ProofDependencies>,
}

#[derive(Serialize, Deserialize)]
struct SavedConditionalProviderTransfer {
    identity: SavedConditionalProviderIdentity,
    source: SavedProviderSourceAnchor,
    related_sources: BTreeMap<String, SavedProviderSourceAnchor>,
    input: RecordInputShape,
    alternatives: Vec<SavedConditionalProviderAlternative>,
    /// Old saved summaries omit this and therefore cannot use the transfer.
    #[serde(default)]
    provider_closure: Option<ResolvedProviderDependencyClosure>,
    dependencies: ProofDependencies,
    /// Required nullable sidecar; absence is a distinct saved value.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    replacement_package: Option<String>,
}

#[derive(Serialize, Deserialize)]
struct SavedConditionalProviderAlternative {
    completion: ConditionalProviderCompletion,
    reads: BTreeSet<ParameterHeapRead>,
    native_operations: BTreeSet<ForeignNativeOperation>,
    dependencies: ProofDependencies,
    effects: SavedEffectCoverageFragment<'static>,
    module_cells: SavedModuleCellAccesses,
}

struct DecodedConditionalProviderAlternative {
    completion: ConditionalProviderCompletion,
    reads: BTreeSet<ParameterHeapRead>,
    native_operations: BTreeSet<ForeignNativeOperation>,
    dependencies: ProofDependencies,
    relative_effects: EffectCoverageFragment,
    module_cells: SavedModuleCellAccesses,
}

fn validate_provider_closure_namespace(
    closure: Option<&ResolvedProviderDependencyClosure>,
    ordinals: &ContextOrdinals<'_>,
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
struct SavedModuleCellAccesses {
    reads: Vec<SavedModuleCell>,
    writes: Vec<SavedModuleCell>,
    invariants: Vec<SavedModuleCellInvariant>,
}

#[derive(Clone, Serialize, Deserialize)]
struct SavedModuleCellInvariant {
    cell: SavedModuleCell,
    invariant: SavedPrimitiveCellInvariant,
}

#[derive(Clone, Serialize, Deserialize)]
struct SavedPrimitiveCellInvariant {
    declaration: SavedBindingCellDeclarationIdentity,
    post_init_kinds: analysis_model::PrimKinds,
    stores: Vec<SavedBindingCellStoreEvidence>,
}

#[derive(Clone, Copy, Serialize, Deserialize)]
struct SavedBindingCellDeclarationIdentity {
    owner: SourceUnitOrdinal,
    activation_callable: Option<u32>,
    binding_span: Span,
    initializer_span: Option<Span>,
}

#[derive(Clone, Copy, Serialize, Deserialize)]
struct SavedBindingCellStoreEvidence {
    owner: SourceUnitOrdinal,
    activation_callable: Option<u32>,
    span: Span,
    kind: super::BindingStoreKind,
}

fn map_saved_module_cell_origin(
    cell: &mut SavedModuleCell,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<()> {
    cell.declaration_origin = map_position(
        &cell.chunk.0,
        cell.declaration_origin,
        inventories,
        direction,
    )?;
    Ok(())
}

fn map_invariant(
    invariant: &PrimitiveCellInvariant,
    module: &str,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
    direction: CoordinateDirection,
) -> Result<SavedPrimitiveCellInvariant> {
    let map_callable = |callable: Option<u32>| -> Result<Option<u32>> {
        callable
            .map(|position| map_callable_or_class_key(module, position, inventories, direction))
            .transpose()
    };
    let map_span_option = |span: Option<Span>| -> Result<Option<Span>> {
        span.map(|span| map_span(module, span, inventories, direction))
            .transpose()
    };
    Ok(SavedPrimitiveCellInvariant {
        declaration: SavedBindingCellDeclarationIdentity {
            owner: invariant.declaration.owner,
            activation_callable: map_callable(invariant.declaration.activation_callable)?,
            binding_span: map_span(
                module,
                invariant.declaration.binding_span,
                inventories,
                direction,
            )?,
            initializer_span: map_span_option(invariant.declaration.initializer_span)?,
        },
        post_init_kinds: invariant.post_init_kinds,
        stores: invariant
            .stores
            .iter()
            .map(|store| {
                Ok(SavedBindingCellStoreEvidence {
                    owner: store.owner,
                    activation_callable: map_callable(store.activation_callable)?,
                    span: map_span(module, store.span, inventories, direction)?,
                    kind: store.kind,
                })
            })
            .collect::<Result<_>>()?,
    })
}

fn restore_invariant(
    invariant: SavedPrimitiveCellInvariant,
    module: &str,
    inventories: &BTreeMap<String, ProviderSourceInventory>,
) -> Result<PrimitiveCellInvariant> {
    let map_callable = |callable: Option<u32>| -> Result<Option<u32>> {
        callable
            .map(|position| {
                map_callable_or_class_key(
                    module,
                    position,
                    inventories,
                    CoordinateDirection::ToCurrent,
                )
            })
            .transpose()
    };
    let map_span_option = |span: Option<Span>| -> Result<Option<Span>> {
        span.map(|span| map_span(module, span, inventories, CoordinateDirection::ToCurrent))
            .transpose()
    };
    Ok(PrimitiveCellInvariant {
        declaration: BindingCellDeclarationIdentity {
            owner: invariant.declaration.owner,
            activation_callable: map_callable(invariant.declaration.activation_callable)?,
            binding_span: map_span(
                module,
                invariant.declaration.binding_span,
                inventories,
                CoordinateDirection::ToCurrent,
            )?,
            initializer_span: map_span_option(invariant.declaration.initializer_span)?,
        },
        post_init_kinds: invariant.post_init_kinds,
        stores: invariant
            .stores
            .into_iter()
            .map(|store| {
                Ok(BindingCellStoreEvidence {
                    owner: store.owner,
                    activation_callable: map_callable(store.activation_callable)?,
                    span: map_span(
                        module,
                        store.span,
                        inventories,
                        CoordinateDirection::ToCurrent,
                    )?,
                    kind: store.kind,
                })
            })
            .collect::<Result<_>>()?,
    })
}

impl SavedModuleCellAccesses {
    fn snapshot(
        accesses: &ModuleCellAccesses,
        ordinals: &ContextOrdinals<'_>,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<Self> {
        let save_cell = |cell: ModuleCellId| -> Result<SavedModuleCell> {
            let mut saved = ordinals.snapshot_module_cell(cell)?;
            map_saved_module_cell_origin(&mut saved, inventories, CoordinateDirection::ToSaved)?;
            Ok(saved)
        };
        Ok(Self {
            reads: accesses
                .reads
                .iter()
                .map(|cell| save_cell(*cell))
                .collect::<Result<_>>()?,
            writes: accesses
                .writes
                .iter()
                .map(|cell| save_cell(*cell))
                .collect::<Result<_>>()?,
            invariants: accesses
                .invariants
                .iter()
                .map(|(cell, invariant)| {
                    let saved_cell = save_cell(*cell)?;
                    let module = saved_cell.chunk.0.clone();
                    let saved_invariant = map_invariant(
                        invariant,
                        &module,
                        inventories,
                        CoordinateDirection::ToSaved,
                    )?;
                    Ok(SavedModuleCellInvariant {
                        cell: saved_cell,
                        invariant: saved_invariant,
                    })
                })
                .collect::<Result<_>>()?,
        })
    }

    fn restore(
        self,
        ordinals: &ContextOrdinals<'_>,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<ModuleCellAccesses> {
        let restore_cell = |mut saved: SavedModuleCell| -> Result<ModuleCellId> {
            map_saved_module_cell_origin(&mut saved, inventories, CoordinateDirection::ToCurrent)?;
            ordinals.restore_module_cell(saved)
        };
        let mut reads = BTreeSet::new();
        for saved in self.reads {
            let cell = restore_cell(saved)?;
            ensure!(
                reads.insert(cell),
                "duplicate saved module-cell read {cell:?}"
            );
        }
        let mut writes = BTreeSet::new();
        for saved in self.writes {
            let cell = restore_cell(saved)?;
            ensure!(
                writes.insert(cell),
                "duplicate saved module-cell write {cell:?}"
            );
        }
        let mut invariants = BTreeMap::new();
        for saved in self.invariants {
            let module = saved.cell.chunk.0.clone();
            let cell = restore_cell(saved.cell)?;
            let invariant = restore_invariant(saved.invariant, &module, inventories)?;
            if invariants.insert(cell, invariant).is_some() {
                bail!("duplicate saved module-cell invariant {cell:?}");
            }
        }
        Ok(ModuleCellAccesses {
            reads,
            writes,
            invariants,
        })
    }
}

impl SavedConditionalProviderTransfer {
    fn snapshot(
        body: &ConditionalProviderTransfer,
        ordinals: &ContextOrdinals<'_>,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<Self> {
        ensure!(
            body.is_well_formed(),
            "conditional provider transfer is internally inconsistent"
        );
        validate_provider_closure_namespace(body.provider_closure.as_ref(), ordinals)?;
        let (source, identity) = snapshot_provider_identity(&body.identity, inventories)?;
        let mut source_modules = BTreeSet::from([body.identity.defining_module.clone()]);
        if let Some(closure) = body.provider_closure.as_ref() {
            source_modules.extend(closure.modules.iter().cloned());
        }
        let alternatives = body
            .alternatives
            .iter()
            .map(|alternative| {
                source_modules.extend(effect_source_modules(
                    &alternative.effects,
                    &body.identity.defining_module,
                    ordinals,
                )?);
                source_modules.extend(module_cell_source_modules(
                    &alternative.module_cells,
                    ordinals,
                )?);
                if let ConditionalProviderCompletion::Throw { origin, .. } = &alternative.completion
                {
                    source_modules.insert(
                        body_module(&origin.body, &body.identity.defining_module).to_owned(),
                    );
                }
                let effects = map_effects(
                    &alternative.effects,
                    &body.identity.defining_module,
                    ordinals,
                    inventories,
                    CoordinateDirection::ToSaved,
                )?;
                Ok(SavedConditionalProviderAlternative {
                    completion: map_provider_completion(
                        &alternative.completion,
                        &body.identity.defining_module,
                        inventories,
                        CoordinateDirection::ToSaved,
                    )?,
                    reads: alternative.reads.clone(),
                    native_operations: alternative.native_operations.clone(),
                    dependencies: alternative.dependencies.clone(),
                    effects: SavedEffectCoverageFragment::snapshot_owned(effects, ordinals)?,
                    module_cells: SavedModuleCellAccesses::snapshot(
                        &alternative.module_cells,
                        ordinals,
                        inventories,
                    )?,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let related_sources =
            related_source_anchors(&source_modules, &body.identity.defining_module, inventories)?;
        Ok(Self {
            identity,
            source,
            related_sources,
            input: body.input.clone(),
            alternatives,
            provider_closure: body.provider_closure.clone(),
            dependencies: body.dependencies.clone(),
            replacement_package: body.replacement_package.clone(),
        })
    }

    fn restore(
        self,
        body: &mut ConditionalProviderTransfer,
        ordinals: &ContextOrdinals<'_>,
        summary_was_borrowed: bool,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<()> {
        validate_provider_closure_namespace(self.provider_closure.as_ref(), ordinals)?;
        let source = inventory(&self.source.module, inventories)?;
        let old_source_start = body
            .identity
            .source
            .as_ref()
            .context("callable summary has no original provider source base")?
            .source_start;
        ensure!(
            normalize_provider_identity(&body.identity, &self.source, inventories)?
                == self.identity,
            "saved conditional-provider-transfer identity does not match its callable summary"
        );
        ensure!(
            body.input == self.input
                && body.dependencies == self.dependencies
                && body.replacement_package == self.replacement_package
                && body.provider_closure == self.provider_closure
                && body.alternatives.len() == self.alternatives.len(),
            "saved provider transfer does not match its callable summary"
        );
        let mut required_modules = BTreeSet::from([self.source.module.clone()]);
        if let Some(closure) = self.provider_closure.as_ref() {
            required_modules.extend(closure.modules.iter().cloned());
        }
        let mut saved_alternatives = Vec::with_capacity(self.alternatives.len());
        for saved in self.alternatives {
            let SavedConditionalProviderAlternative {
                completion,
                reads,
                native_operations,
                dependencies,
                effects,
                module_cells,
            } = saved;
            let relative_effects = effects.restore(ordinals)?;
            required_modules.extend(effect_source_modules(
                &relative_effects,
                &self.source.module,
                ordinals,
            )?);
            required_modules.extend(saved_module_cell_source_modules(&module_cells));
            if let ConditionalProviderCompletion::Throw { origin, .. } = &completion {
                required_modules.insert(body_module(&origin.body, &self.source.module).to_owned());
            }
            saved_alternatives.push(DecodedConditionalProviderAlternative {
                completion,
                reads,
                native_operations,
                dependencies,
                relative_effects,
                module_cells,
            });
        }
        validate_source_anchors(
            &self.source,
            &self.related_sources,
            &required_modules,
            inventories,
        )?;
        for (alternative, saved) in body.alternatives.iter_mut().zip(saved_alternatives) {
            let normalized_completion = normalize_report_completion(
                &alternative.completion,
                &self.source.module,
                old_source_start,
                &self.source,
                inventories,
            )?;
            ensure!(
                normalized_completion == saved.completion
                    && alternative.reads == saved.reads
                    && alternative.native_operations == saved.native_operations
                    && alternative.dependencies == saved.dependencies,
                "saved provider alternative does not match its callable summary"
            );
            if summary_was_borrowed {
                let normalized_effects = map_effects(
                    &alternative.effects,
                    &self.source.module,
                    ordinals,
                    inventories,
                    CoordinateDirection::ToSaved,
                )?;
                ensure!(
                    normalized_effects == saved.relative_effects,
                    "in-memory provider effects do not match their saved evidence"
                );
            } else {
                ensure!(
                    alternative.effects
                        == crate::facts::sync_effects::EffectCoverageFragment::default(),
                    "decoded callable summary contains unchecked provider effects"
                );
            }
            let effects = map_effects(
                &saved.relative_effects,
                &self.source.module,
                ordinals,
                inventories,
                CoordinateDirection::ToCurrent,
            )?;
            ensure!(
                effects.references_source_sites(&source.source_sites),
                "saved provider alternative references a site outside its prepared source table"
            );

            let module_cells = saved.module_cells.restore(ordinals, inventories)?;
            if summary_was_borrowed {
                ensure!(
                    alternative.module_cells == module_cells,
                    "in-memory provider module cells do not match their saved evidence"
                );
            } else {
                ensure!(
                    alternative.module_cells == ModuleCellAccesses::default(),
                    "decoded callable summary contains unchecked provider module cells"
                );
            }
            alternative.completion = map_provider_completion(
                &saved.completion,
                &self.source.module,
                inventories,
                CoordinateDirection::ToCurrent,
            )?;
            alternative.effects = effects;
            alternative.module_cells = module_cells;
        }
        body.identity = restore_provider_identity(self.identity, &self.source, inventories)?;
        body.provider_closure = self.provider_closure;
        ensure!(
            body.is_well_formed(),
            "saved conditional provider transfer is internally inconsistent"
        );
        Ok(())
    }
}

impl<'a> SavedCallableSummary<'a> {
    pub fn snapshot(
        summary: &'a CallableSummary,
        ordinals: &ContextOrdinals<'_>,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<Self> {
        let foreign_call_body_effects = summary
            .foreign_call
            .as_ref()
            .map(|call| {
                call.body_effects
                    .as_deref()
                    .map(|body| {
                        SavedForeignBodyEffects::snapshot(
                            body,
                            &call.defining_module,
                            ordinals,
                            inventories,
                        )
                    })
                    .transpose()
            })
            .transpose()?
            .flatten();
        Ok(Self {
            summary: Cow::Borrowed(summary),
            namespace_wrapper_dependencies: summary
                .namespace_wrapper
                .as_ref()
                .map(|wrapper| wrapper.dependencies.clone()),
            foreign_call_dependencies: summary
                .foreign_call
                .as_ref()
                .map(|foreign_call| foreign_call.dependencies.clone()),
            foreign_call_body_effects,
            foreign_call_initialized_class: summary
                .foreign_call
                .as_ref()
                .and_then(|call| call.initialized_class.clone()),
            foreign_member_evidence: summary
                .foreign_members
                .iter()
                .map(|(name, call)| {
                    Ok((
                        name.clone(),
                        SavedForeignMemberEvidence {
                            dependencies: call.dependencies.clone(),
                            initialized_class: call.initialized_class.clone(),
                            body_effects: call
                                .body_effects
                                .as_deref()
                                .map(|body| {
                                    SavedForeignBodyEffects::snapshot(
                                        body,
                                        &call.defining_module,
                                        ordinals,
                                        inventories,
                                    )
                                })
                                .transpose()?,
                        },
                    ))
                })
                .collect::<Result<_>>()?,
            conditional_bodies: summary
                .provider_transfers
                .iter()
                .map(|body| SavedConditionalProviderTransfer::snapshot(body, ordinals, inventories))
                .collect::<Result<_>>()?,
        })
    }

    pub fn restore(
        self,
        ordinals: &ContextOrdinals<'_>,
        inventories: &BTreeMap<String, ProviderSourceInventory>,
    ) -> Result<CallableSummary> {
        let summary_was_borrowed = matches!(&self.summary, Cow::Borrowed(_));
        let mut summary = self.summary.into_owned();
        ensure!(
            summary.namespace_wrapper.is_some() == self.namespace_wrapper_dependencies.is_some(),
            "saved namespace-wrapper dependency does not match its callable summary"
        );
        ensure!(
            summary.foreign_call.is_some() == self.foreign_call_dependencies.is_some(),
            "saved foreign-call dependency does not match its callable summary"
        );
        ensure!(
            summary.foreign_call.is_some()
                || (self.foreign_call_body_effects.is_none()
                    && self.foreign_call_initialized_class.is_none()),
            "saved foreign-call body has no callable summary"
        );
        ensure!(
            summary
                .foreign_members
                .keys()
                .eq(self.foreign_member_evidence.keys()),
            "saved foreign-member evidence does not match its callable summaries"
        );
        ensure!(
            summary.provider_transfers.len() == self.conditional_bodies.len(),
            "saved provider transfers do not match their callable summaries"
        );
        if let (Some(wrapper), Some(dependencies)) = (
            summary.namespace_wrapper.as_mut(),
            self.namespace_wrapper_dependencies,
        ) {
            wrapper.dependencies = dependencies;
        }
        if let (Some(foreign_call), Some(dependencies)) = (
            summary.foreign_call.as_mut(),
            self.foreign_call_dependencies,
        ) {
            foreign_call.dependencies = dependencies;
            foreign_call.initialized_class = self.foreign_call_initialized_class;
            ensure!(
                summary_was_borrowed || foreign_call.body_effects.is_none(),
                "decoded foreign-call summary contains unchecked provider body effects"
            );
            let restored_body_effects = self
                .foreign_call_body_effects
                .map(|body| {
                    body.restore(
                        ordinals,
                        inventories,
                        &foreign_call.defining_module,
                        foreign_call.body_effects.as_deref(),
                        summary_was_borrowed,
                    )
                    .map(Box::new)
                })
                .transpose()?;
            ensure!(
                !summary_was_borrowed
                    || restored_body_effects.is_some() == foreign_call.body_effects.is_some(),
                "in-memory foreign-call body effects are missing their saved sidecar"
            );
            foreign_call.body_effects = restored_body_effects;
        }
        for (name, evidence) in self.foreign_member_evidence {
            let member = summary
                .foreign_members
                .get_mut(&name)
                .expect("member keys verified");
            member.dependencies = evidence.dependencies;
            member.initialized_class = evidence.initialized_class;
            ensure!(
                summary_was_borrowed || member.body_effects.is_none(),
                "decoded foreign-member summary contains unchecked provider body effects"
            );
            let restored_body_effects = evidence
                .body_effects
                .map(|body| {
                    body.restore(
                        ordinals,
                        inventories,
                        &member.defining_module,
                        member.body_effects.as_deref(),
                        summary_was_borrowed,
                    )
                    .map(Box::new)
                })
                .transpose()?;
            ensure!(
                !summary_was_borrowed
                    || restored_body_effects.is_some() == member.body_effects.is_some(),
                "in-memory foreign-member body effects are missing their saved sidecar"
            );
            member.body_effects = restored_body_effects;
        }
        for (body, saved) in summary
            .provider_transfers
            .iter_mut()
            .zip(self.conditional_bodies)
        {
            saved.restore(body, ordinals, summary_was_borrowed, inventories)?;
        }
        Ok(summary)
    }
}

/// Native census data with source-site evidence selected explicitly.
#[derive(Serialize, Deserialize)]
pub struct SavedNativeIntegrity<'a> {
    mutated: Cow<'a, BTreeSet<NativeIntrinsic>>,
    prototype_mutations: Vec<SavedNativePrototypeMutation<'a>>,
    witnesses: Vec<SavedNativeMutationWitness<'a>>,
    value_input_evidence: Cow<'a, BTreeMap<ValueInputRef, ValueInputEvidence>>,
    consumed_value_inputs: Cow<'a, BTreeSet<ValueInputRef>>,
    consumed_host_behavior: Cow<'a, BTreeSet<HostBehaviorKey>>,
    never_written: Cow<'a, BTreeSet<NativeIntrinsic>>,
    never_written_overrides: Cow<'a, [NeverWrittenOverride]>,
}

impl<'a> SavedNativeIntegrity<'a> {
    pub fn snapshot(integrity: &'a NativeIntegrity) -> Self {
        Self {
            mutated: Cow::Borrowed(&integrity.mutated),
            prototype_mutations: integrity
                .prototype_mutations
                .iter()
                .map(SavedNativePrototypeMutation::snapshot)
                .collect(),
            witnesses: integrity
                .witnesses
                .iter()
                .map(SavedNativeMutationWitness::snapshot)
                .collect(),
            value_input_evidence: Cow::Borrowed(&integrity.value_input_evidence),
            consumed_value_inputs: Cow::Borrowed(&integrity.consumed_value_inputs),
            consumed_host_behavior: Cow::Borrowed(&integrity.consumed_host_behavior),
            never_written: Cow::Borrowed(&integrity.never_written),
            never_written_overrides: Cow::Borrowed(integrity.never_written_overrides.as_slice()),
        }
    }

    pub fn restore(self) -> Result<NativeIntegrity> {
        Ok(NativeIntegrity {
            mutated: self.mutated.into_owned(),
            prototype_mutations: Arc::new(
                self.prototype_mutations
                    .into_iter()
                    .map(SavedNativePrototypeMutation::restore)
                    .collect(),
            ),
            witnesses: Arc::new(
                self.witnesses
                    .into_iter()
                    .map(SavedNativeMutationWitness::restore)
                    .collect::<Result<_>>()?,
            ),
            value_input_evidence: self.value_input_evidence.into_owned(),
            consumed_value_inputs: self.consumed_value_inputs.into_owned(),
            consumed_host_behavior: self.consumed_host_behavior.into_owned(),
            never_written: self.never_written.into_owned(),
            never_written_overrides: self.never_written_overrides.into_owned(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{
        ArgumentInputRef, BindingCellDeclarationIdentity, BindingCellStoreEvidence,
        BindingStoreKind, CallableSummary, ConditionalProviderAlternative,
        ConditionalProviderCompletion, ConditionalProviderIdentity, ConditionalProviderTransfer,
        ConditionalProviderValue, ConditionalResultAlternative, ConditionalResultProjection,
        ForeignBodyEffects, ForeignCallSummary, ForeignInvocation, ForeignReturn,
        ModuleCellAccesses, NamespaceWrapperSummary, ParameterHeapRead, ProviderCallableSource,
        ProviderProgramSource, ProviderRetainedGraph, RecordInputProperty, RecordInputShape,
        ValueInputRef,
    };
    use crate::analysis_provenance::{InferenceRule, InferenceUse};
    use crate::facts::abstract_domain::value::HeapModuleScope;
    use crate::facts::sync_effects::OperationStep;
    use analysis_codec::{ContextOrdinals, SavedChunk};
    use source_identity::{ChunkId, ChunkTable, ModuleCellId, SourceUnitOrdinal};
    use std::collections::{BTreeMap, BTreeSet};
    use swc_common::{Span, Spanned};
    use swc_ecma_ast::{
        AssignExpr, AssignTarget, Decl, Module, ModuleDecl, ModuleItem, Pat, SimpleAssignTarget,
        Stmt,
    };
    use swc_ecma_visit::{Visit, VisitWith};

    struct SourceFixture {
        parsed: js_ast::ParsedJsModule,
        inventory: ProviderSourceInventory,
    }

    fn intern_chunks(order: &[&str]) -> ChunkTable {
        let mut chunks = ChunkTable::default();
        for name in order {
            chunks.intern((*name).to_owned());
        }
        chunks
    }

    fn source_fixtures(
        chunks: &ChunkTable,
        sources: &[(&str, &str)],
    ) -> BTreeMap<String, SourceFixture> {
        sources
            .iter()
            .map(|(module_name, source)| {
                let chunk = chunks
                    .get(module_name)
                    .expect("each parsed source has a chunk identity");
                let source_file = format!("{module_name}.js");
                let parsed = js_ast::parse_js_module(&source_file, source)
                    .expect("fixture source parses and resolves");
                let (source_start, source_end) = {
                    let files = parsed.cm.files();
                    let file = files.first().expect("parse owns its source file");
                    (file.start_pos.0, file.end_pos.0)
                };
                let line_index = parsed.line_index();
                let structural = crate::facts::analyze_chunk_structural_in_chunk(
                    &parsed.module,
                    chunk,
                    Some(&source_file),
                    |span| line_index.line_range_and_start_column_for_span(span),
                );
                let canonical = crate::facts::canonical_source_inventory(&structural);
                let inventory = ProviderSourceInventory {
                    source: ProviderProgramSource {
                        module: (*module_name).to_owned(),
                        scope: HeapModuleScope::Chunk(chunk),
                        sha256: graph_provenance::sha256(source.as_bytes()),
                        source_start,
                        source_end,
                    },
                    source_sites: canonical.source_sites,
                    callable_spans: canonical.callable_spans,
                    class_spans: canonical.class_spans,
                };
                (
                    (*module_name).to_owned(),
                    SourceFixture { parsed, inventory },
                )
            })
            .collect()
    }

    fn inventories(
        fixtures: &BTreeMap<String, SourceFixture>,
    ) -> BTreeMap<String, ProviderSourceInventory> {
        fixtures
            .iter()
            .map(|(module, fixture)| (module.clone(), fixture.inventory.clone()))
            .collect()
    }

    fn function_declaration(item: &ModuleItem) -> Option<&swc_ecma_ast::FnDecl> {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(declaration))) => Some(declaration),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                Decl::Fn(declaration) => Some(declaration),
                _ => None,
            },
            _ => None,
        }
    }

    fn function_identity(fixture: &SourceFixture, name: &str) -> (SourceUnitOrdinal, u32) {
        for (owner, item) in fixture.parsed.module.body.iter().enumerate() {
            let Some(declaration) = function_declaration(item) else {
                continue;
            };
            if declaration.ident.sym.as_ref() == name {
                let key = declaration.function.span.lo.0;
                assert!(fixture.inventory.callable_spans.contains_key(&key));
                return (SourceUnitOrdinal(owner), key);
            }
        }
        panic!("fixture has no function declaration named {name}");
    }

    fn class_key(fixture: &SourceFixture, name: &str) -> u32 {
        for item in &fixture.parsed.module.body {
            let declaration = match item {
                ModuleItem::Stmt(Stmt::Decl(Decl::Class(declaration))) => Some(declaration),
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                    Decl::Class(declaration) => Some(declaration),
                    _ => None,
                },
                _ => None,
            };
            if let Some(declaration) = declaration
                && declaration.ident.sym.as_ref() == name
            {
                let key = declaration.class.span.lo.0;
                assert!(fixture.inventory.class_spans.contains_key(&key));
                return key;
            }
        }
        panic!("fixture has no class declaration named {name}");
    }

    fn var_declaration(item: &ModuleItem) -> Option<&swc_ecma_ast::VarDecl> {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) => Some(declaration),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                Decl::Var(declaration) => Some(declaration),
                _ => None,
            },
            _ => None,
        }
    }

    fn variable_coordinates(
        module: &Module,
        name: &str,
    ) -> (SourceUnitOrdinal, Span, Option<Span>) {
        for (owner, item) in module.body.iter().enumerate() {
            let Some(declaration) = var_declaration(item) else {
                continue;
            };
            for declarator in &declaration.decls {
                let Pat::Ident(binding) = &declarator.name else {
                    continue;
                };
                if binding.id.sym.as_ref() == name {
                    return (
                        SourceUnitOrdinal(owner),
                        binding.id.span,
                        declarator
                            .init
                            .as_ref()
                            .map(|initializer| initializer.span()),
                    );
                }
            }
        }
        panic!("fixture has no variable declaration named {name}");
    }

    #[derive(Default)]
    struct AssignmentSpanFinder {
        binding: String,
        spans: Vec<Span>,
    }

    impl Visit for AssignmentSpanFinder {
        fn visit_assign_expr(&mut self, assignment: &AssignExpr) {
            if let AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) = &assignment.left
                && binding.id.sym.as_ref() == self.binding.as_str()
            {
                self.spans.push(assignment.span);
            }
            assignment.visit_children_with(self);
        }
    }

    fn assignment_span(module: &Module, binding: &str) -> Span {
        let mut finder = AssignmentSpanFinder {
            binding: binding.to_owned(),
            spans: Vec::new(),
        };
        module.visit_with(&mut finder);
        let [span] = finder.spans.as_slice() else {
            panic!("fixture must contain one assignment to {binding}");
        };
        *span
    }

    fn provider_callable_source(fixture: &SourceFixture, key: u32) -> ProviderCallableSource {
        let span = fixture
            .inventory
            .callable_spans
            .get(&key)
            .expect("callable key comes from the parsed declaration");
        ProviderCallableSource {
            sha256: fixture.inventory.source.sha256.clone(),
            source_start: fixture.inventory.source.source_start,
            callable_start: key
                .checked_sub(fixture.inventory.source.source_start)
                .expect("callable begins in its source file"),
            callable_end: span
                .hi
                .0
                .checked_sub(fixture.inventory.source.source_start)
                .expect("callable ends in its source file"),
        }
    }

    fn value_dependencies(input_index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider".to_owned(),
                input_index,
            }]),
            ..ProofDependencies::default()
        }
    }

    #[test]
    fn callable_dependencies_round_trip_outside_codec_and_stay_out_of_report_json() {
        js_ast::with_swc_globals(|| {
            let provider_source = "export function wrap(value) { return { value }; }\nexport class Invocable { constructor(value) { this.value = value; } }\n";
            let helper_source = "export const helperValue = 1;\n";
            let other_source =
                "export let otherCell = 1;\nexport function updateOther() { otherCell = 2; }\n";
            let sources = [
                ("provider", provider_source),
                ("helper", helper_source),
                ("other", other_source),
            ];
            let writer_chunks = intern_chunks(&["provider", "helper", "other"]);
            let writer_sources = source_fixtures(&writer_chunks, &sources);
            let writer_inventories = inventories(&writer_sources);
            let writer_ordinals = ContextOrdinals::new(
                writer_sources
                    .values()
                    .map(|fixture| &fixture.parsed.module),
                &writer_chunks,
            );
            let provider = &writer_sources["provider"];
            let constructed_class = class_key(provider, "Invocable");

            let namespace_dependencies = value_dependencies(2);
            let foreign_dependencies = value_dependencies(4);
            let inference = InferenceUse {
                conclusion: InferenceRule::NamespaceWrapper,
                derivation_rules: BTreeSet::from([InferenceRule::NamespaceWrapper]),
                value_input_refs: BTreeSet::new(),
                descriptor_copy_author_refs: BTreeSet::new(),
                argument_input_refs: BTreeSet::<ArgumentInputRef>::new(),
                native_operation_input_refs: BTreeSet::new(),
                imported_primitive_array_spread_input_refs: BTreeSet::new(),
            };
            let mut summary = CallableSummary::namespace_wrapper(NamespaceWrapperSummary {
                defining_module: "provider".to_owned(),
                export: "wrap".to_owned(),
                local: "wrap".to_owned(),
                source_location: None,
                inference,
                dependencies: namespace_dependencies,
            });
            summary.foreign_call = Some(ForeignCallSummary {
                defining_module: "provider".to_owned(),
                export: "Invocable".to_owned(),
                local: "Invocable".to_owned(),
                member: None,
                source_location: None,
                invocation: ForeignInvocation::Construct,
                returns: BTreeSet::from([ForeignReturn::FreshAllocation]),
                retained_arguments: BTreeSet::new(),
                retained_provider_captures: BTreeSet::new(),
                native_operations: BTreeSet::new(),
                replacement_package: None,
                dependencies: foreign_dependencies.clone(),
                initialized_class: Some(foreign_dependencies),
                body_effects: Some(Box::new(ForeignBodyEffects {
                    callable_key: constructed_class,
                    completion: Completion::Normal(()),
                    effects: Default::default(),
                })),
            });

            let report = serde_json::to_value(&summary).expect("callable report serializes");
            assert!(report["namespace_wrapper"].get("dependencies").is_none());
            assert!(report["foreign_call"].get("dependencies").is_none());
            assert!(report["foreign_call"].get("initialized_class").is_none());
            assert!(report["foreign_call"].get("body_effects").is_none());

            let saved =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("callable summary snapshots");
            let bytes = rmp_serde::to_vec_named(&saved).expect("ordinary saved encode");
            let decoded: SavedCallableSummary<'static> =
                rmp_serde::from_slice(&bytes).expect("ordinary saved decode");

            let reader_chunks = intern_chunks(&["other", "helper", "provider"]);
            let reader_sources = source_fixtures(&reader_chunks, &sources);
            let reader_inventories = inventories(&reader_sources);
            let reader_ordinals = ContextOrdinals::new(
                reader_sources
                    .values()
                    .map(|fixture| &fixture.parsed.module),
                &reader_chunks,
            );
            let restored = decoded
                .restore(&reader_ordinals, &reader_inventories)
                .expect("class-root body effects and callable dependencies restore");
            assert_eq!(
                restored
                    .foreign_call
                    .as_ref()
                    .unwrap()
                    .body_effects
                    .as_ref()
                    .unwrap()
                    .callable_key,
                class_key(&reader_sources["provider"], "Invocable")
            );
            assert_eq!(restored, summary);

            let mut saved_json = serde_json::to_value(
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("callable snapshots"),
            )
            .expect("saved callable serializes as ordinary Serde");
            assert_eq!(
                saved_json["foreign_call_body_effects"]["callable_key"],
                serde_json::json!(constructed_class - provider.inventory.source.source_start)
            );
            assert_eq!(
                saved_json["foreign_call_body_effects"]["completion"],
                serde_json::json!({"normal": null})
            );
            assert!(
                saved_json["foreign_call_body_effects"]["effects"]
                    .get("completion")
                    .is_none()
            );

            let mut missing_body_completion = saved_json.clone();
            missing_body_completion["foreign_call_body_effects"]
                .as_object_mut()
                .expect("saved body effects are a map")
                .remove("completion");
            assert!(
                serde_json::from_value::<SavedCallableSummary<'static>>(missing_body_completion)
                    .is_err()
            );
            let mut opaque_body_completion = saved_json.clone();
            opaque_body_completion["foreign_call_body_effects"]["completion"] =
                serde_json::json!({"opaque_may_normal": null});
            assert!(
                serde_json::from_value::<SavedCallableSummary<'static>>(opaque_body_completion)
                    .is_err()
            );
            saved_json
                .as_object_mut()
                .expect("saved callable is a map")
                .remove("namespace_wrapper_dependencies");
            assert!(serde_json::from_value::<SavedCallableSummary<'static>>(saved_json).is_err());

            let mut missing_initialization = serde_json::to_value(
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .unwrap(),
            )
            .unwrap();
            missing_initialization
                .as_object_mut()
                .unwrap()
                .remove("foreign_call_initialized_class");
            assert!(
                serde_json::from_value::<SavedCallableSummary<'static>>(missing_initialization)
                    .is_err()
            );

            let mut mismatched_json = serde_json::to_value(
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .unwrap(),
            )
            .unwrap();
            mismatched_json
                .as_object_mut()
                .expect("saved callable is a map")
                .insert(
                    "namespace_wrapper_dependencies".to_owned(),
                    serde_json::Value::Null,
                );
            let decoded: SavedCallableSummary<'static> =
                serde_json::from_value(mismatched_json).expect("explicit None dependencies decode");
            assert!(
                decoded
                    .restore(&writer_ordinals, &writer_inventories)
                    .is_err()
            );

            let absent_capability = CallableSummary::default();
            let saved_json = serde_json::to_value(
                SavedCallableSummary::snapshot(
                    &absent_capability,
                    &writer_ordinals,
                    &writer_inventories,
                )
                .unwrap(),
            )
            .expect("saved callable serializes with explicit absent dependencies");
            let decoded: SavedCallableSummary<'static> =
                serde_json::from_value(saved_json).expect("explicit None dependencies decode");
            assert_eq!(
                decoded
                    .restore(&writer_ordinals, &writer_inventories)
                    .expect("absent capabilities match"),
                absent_capability
            );
        });
    }

    #[test]
    fn conditional_module_cells_and_completion_payloads_use_checked_saved_addresses() {
        js_ast::with_swc_globals(|| {
            let provider_source = "import { helperValue } from \"helper\";\nexport let providerCell = \"ready\";\nexport function wrap(input) { if (input.flag) { void helperValue; throw input; } return providerCell; }\n";
            let helper_source = "export const helperValue = 1;\n";
            let other_source =
                "export let otherCell = 1;\nexport function updateOther() { otherCell = 2; }\n";
            let sources = [
                ("provider", provider_source),
                ("helper", helper_source),
                ("other", other_source),
            ];
            let writer_chunks = intern_chunks(&["provider", "helper", "other"]);
            let writer_provider_chunk = writer_chunks.get("provider").unwrap();
            let writer_other_chunk = writer_chunks.get("other").unwrap();
            let writer_sources = source_fixtures(&writer_chunks, &sources);
            let writer_inventories = inventories(&writer_sources);
            let writer_ordinals = ContextOrdinals::new(
                writer_sources
                    .values()
                    .map(|fixture| &fixture.parsed.module),
                &writer_chunks,
            );
            let provider = &writer_sources["provider"];
            let other = &writer_sources["other"];
            let (provider_owner, provider_binding, provider_initializer) =
                variable_coordinates(&provider.parsed.module, "providerCell");
            let (other_owner, other_binding, other_initializer) =
                variable_coordinates(&other.parsed.module, "otherCell");
            let (update_owner, update_key) = function_identity(other, "updateOther");
            let wrap_key = function_identity(provider, "wrap").1;
            let provider_cell = ModuleCellId {
                chunk: writer_provider_chunk,
                declaration_origin: provider_binding.lo.0,
            };
            let other_cell = ModuleCellId {
                chunk: writer_other_chunk,
                declaration_origin: other_binding.lo.0,
            };
            let provider_invariant = PrimitiveCellInvariant {
                declaration: BindingCellDeclarationIdentity {
                    owner: provider_owner,
                    activation_callable: None,
                    binding_span: provider_binding,
                    initializer_span: provider_initializer,
                },
                post_init_kinds: analysis_model::PrimKinds::STRING,
                stores: Vec::new(),
            };
            let other_invariant = PrimitiveCellInvariant {
                declaration: BindingCellDeclarationIdentity {
                    owner: other_owner,
                    activation_callable: None,
                    binding_span: other_binding,
                    initializer_span: other_initializer,
                },
                post_init_kinds: analysis_model::PrimKinds::NUMBER,
                stores: vec![BindingCellStoreEvidence {
                    owner: update_owner,
                    activation_callable: Some(update_key),
                    span: assignment_span(&other.parsed.module, "otherCell"),
                    kind: BindingStoreKind::Assignment,
                }],
            };
            let throw_origin = provider
                .inventory
                .source_sites
                .iter()
                .find(|site| site.step == OperationStep::Throw)
                .cloned()
                .expect("parsed provider throw has a canonical operation site");
            assert!(provider.inventory.source_sites.contains(&throw_origin));
            assert!(matches!(
                &throw_origin.body,
                crate::facts::sync_effects::LocalBodyIdentity::Callable { key, .. }
                    if *key == wrap_key
            ));

            let input = RecordInputShape {
                properties: BTreeMap::from([("flag".to_owned(), RecordInputProperty::Data)]),
                creation_order: vec!["flag".to_owned()],
            };
            let reads = BTreeSet::from([
                ParameterHeapRead::ObjectShape { argument: 0 },
                ParameterHeapRead::OwnProperty {
                    argument: 0,
                    property: "flag".to_owned(),
                },
            ]);
            let graph = ProviderRetainedGraph {
                roots: BTreeSet::from([super::super::ProviderGraphValue::Slot { slot: 0 }]),
                nodes: vec![
                    super::super::ProviderGraphNode {
                        kind: super::super::ProviderGraphNodeKind::Record,
                        edges: BTreeSet::from([
                            super::super::ProviderGraphEdge {
                                role: super::super::ProviderGraphRole::OwnData {
                                    property: "kept".to_owned(),
                                },
                                value: super::super::ProviderGraphValue::Input {
                                    input: super::super::ProviderGraphInput::Argument { index: 0 },
                                },
                            },
                            super::super::ProviderGraphEdge {
                                role: super::super::ProviderGraphRole::OwnData {
                                    property: "closure".to_owned(),
                                },
                                value: super::super::ProviderGraphValue::Slot { slot: 1 },
                            },
                        ]),
                    },
                    super::super::ProviderGraphNode {
                        kind: super::super::ProviderGraphNodeKind::Closure {
                            source_key: wrap_key,
                        },
                        edges: BTreeSet::new(),
                    },
                ],
            };
            let normal_dependencies = value_dependencies(2);
            let throw_dependencies = value_dependencies(4);
            let mut dependencies = normal_dependencies.clone();
            dependencies.extend(&throw_dependencies);
            let normal_module_cells = ModuleCellAccesses {
                reads: BTreeSet::from([provider_cell]),
                writes: BTreeSet::new(),
                invariants: BTreeMap::from([(provider_cell, provider_invariant.clone())]),
            };
            let throw_module_cells = ModuleCellAccesses {
                reads: BTreeSet::new(),
                writes: BTreeSet::from([other_cell]),
                invariants: BTreeMap::from([(other_cell, other_invariant.clone())]),
            };
            let transfer = ConditionalProviderTransfer {
                identity: ConditionalProviderIdentity {
                    defining_module: "provider".to_owned(),
                    export: "wrap".to_owned(),
                    local: "wrap".to_owned(),
                    callable_key: wrap_key,
                    source: Some(provider_callable_source(provider, wrap_key)),
                },
                input,
                alternatives: vec![
                    ConditionalProviderAlternative {
                        completion: ConditionalProviderCompletion::Normal {
                            value: ConditionalProviderValue::Projection(
                                ConditionalResultProjection {
                                    alternatives: BTreeSet::from([
                                        ConditionalResultAlternative::Primitive {
                                            kinds: analysis_model::PrimKinds::STRING,
                                        },
                                    ]),
                                },
                            ),
                        },
                        reads: reads.clone(),
                        native_operations: BTreeSet::new(),
                        effects: Default::default(),
                        module_cells: normal_module_cells,
                        dependencies: normal_dependencies,
                    },
                    ConditionalProviderAlternative {
                        completion: ConditionalProviderCompletion::Throw {
                            origin: throw_origin.clone(),
                            value: ConditionalProviderValue::RetainedGraph(graph.clone()),
                        },
                        reads,
                        native_operations: BTreeSet::new(),
                        effects: Default::default(),
                        module_cells: throw_module_cells,
                        dependencies: throw_dependencies,
                    },
                ],
                provider_closure: Some(super::ResolvedProviderDependencyClosure {
                    modules: BTreeSet::from([
                        "provider".to_owned(),
                        "helper".to_owned(),
                        "other".to_owned(),
                    ]),
                    admitted_host_modules: BTreeSet::from([
                        crate::analysis_inputs::AdmittedHostModuleDependency {
                            source_module: "provider".to_owned(),
                            specifier: "path".to_owned(),
                            module: spec::HostModule::Node(spec::NodeBuiltinModule::Path),
                            consumed_premise: HostBehaviorKey::reentry_free_module(
                                spec::HostModule::Node(spec::NodeBuiltinModule::Path),
                            ),
                        },
                    ]),
                }),
                dependencies,
                replacement_package: None,
            };
            assert!(transfer.is_well_formed());
            let mut summary = CallableSummary::default();
            summary.provider_transfers.push(transfer);

            let saved =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("per-alternative transfer sidecars snapshot");
            let saved_json = serde_json::to_value(&saved).expect("saved callable serializes");
            let report = serde_json::to_value(&summary).expect("runtime summary serializes");
            assert!(report["conditional_bodies"][0].get("completion").is_none());
            assert!(
                report["conditional_bodies"][0]["alternatives"][0]
                    .get("effects")
                    .is_none()
            );
            assert!(
                report["conditional_bodies"][0]["alternatives"][0]
                    .get("module_cells")
                    .is_none()
            );
            assert_eq!(
                saved_json["conditional_bodies"][0]["alternatives"][0]["completion"]["kind"],
                "normal"
            );
            assert_eq!(
                saved_json["conditional_bodies"][0]["alternatives"][0]["completion"]["value"]["kind"],
                "projection"
            );
            assert_eq!(
                saved_json["conditional_bodies"][0]["alternatives"][1]["completion"]["kind"],
                "throw"
            );
            assert_eq!(
                saved_json["conditional_bodies"][0]["alternatives"][1]["completion"]["value"]["kind"],
                "retained_graph"
            );
            assert_eq!(
                saved_json["conditional_bodies"][0]["alternatives"][0]["module_cells"]["reads"][0]
                    ["chunk"],
                "provider"
            );
            assert_eq!(
                saved_json["conditional_bodies"][0]["alternatives"][1]["module_cells"]["writes"][0]
                    ["chunk"],
                "other"
            );
            assert!(
                saved_json["conditional_bodies"][0]["alternatives"][0]["effects"]
                    .get("completion")
                    .is_none()
            );

            let mut missing = saved_json.clone();
            missing
                .as_object_mut()
                .expect("saved callable is a map")
                .remove("conditional_bodies");
            assert!(serde_json::from_value::<SavedCallableSummary<'static>>(missing).is_err());

            let mut missing_completion = saved_json.clone();
            missing_completion["conditional_bodies"][0]["alternatives"][0]
                .as_object_mut()
                .expect("saved alternative is a map")
                .remove("completion");
            assert!(
                serde_json::from_value::<SavedCallableSummary<'static>>(missing_completion)
                    .is_err()
            );

            for completion in [
                serde_json::json!({"kind": "abrupt", "value": null}),
                serde_json::json!({"kind": "opaque_may_normal", "value": null}),
            ] {
                let mut unsupported = saved_json.clone();
                unsupported["conditional_bodies"][0]["alternatives"][0]["completion"] = completion;
                assert!(
                    serde_json::from_value::<SavedCallableSummary<'static>>(unsupported).is_err()
                );
            }

            let mut missing_graph = saved_json.clone();
            missing_graph["conditional_bodies"][0]["alternatives"][1]["completion"]["value"]
                .as_object_mut()
                .expect("retained graph completion value is tagged")
                .remove("value");
            assert!(
                serde_json::from_value::<SavedCallableSummary<'static>>(missing_graph).is_err()
            );

            let normal_payload =
                saved_json["conditional_bodies"][0]["alternatives"][0]["completion"]["value"]
                    .clone();
            let mut mismatched_payload_json = saved_json.clone();
            mismatched_payload_json["conditional_bodies"][0]["alternatives"][1]["completion"]["value"] =
                normal_payload;
            let mismatched_payload: SavedCallableSummary<'static> =
                serde_json::from_value(mismatched_payload_json)
                    .expect("a different typed payload still decodes");
            assert!(
                mismatched_payload
                    .restore(&writer_ordinals, &writer_inventories)
                    .is_err()
            );

            let mut mismatched_identity =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("transfer snapshots");
            mismatched_identity.conditional_bodies[0].identity.export = "other".to_owned();
            assert!(
                mismatched_identity
                    .restore(&writer_ordinals, &writer_inventories)
                    .is_err()
            );

            let mut mismatched_input =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("transfer snapshots");
            mismatched_input.conditional_bodies[0]
                .input
                .properties
                .insert("extra".to_owned(), RecordInputProperty::Data);
            assert!(
                mismatched_input
                    .restore(&writer_ordinals, &writer_inventories)
                    .is_err()
            );

            let mut malformed_input =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("transfer snapshots");
            malformed_input.summary.to_mut().provider_transfers[0]
                .input
                .properties
                .insert("extra".to_owned(), RecordInputProperty::Data);
            malformed_input.conditional_bodies[0]
                .input
                .properties
                .insert("extra".to_owned(), RecordInputProperty::Data);
            assert!(
                malformed_input
                    .restore(&writer_ordinals, &writer_inventories)
                    .is_err(),
                "matching summary and sidecar cannot admit an input with missing creation order"
            );

            let mut duplicate =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("transfer snapshots");
            let duplicate_proof = duplicate.conditional_bodies[0].alternatives[0]
                .module_cells
                .invariants[0]
                .clone();
            duplicate.conditional_bodies[0].alternatives[0]
                .module_cells
                .invariants
                .push(duplicate_proof);
            assert!(
                duplicate
                    .restore(&writer_ordinals, &writer_inventories)
                    .is_err()
            );

            let mut unknown =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("transfer snapshots");
            unknown.conditional_bodies[0].alternatives[0]
                .module_cells
                .reads[0]
                .chunk = SavedChunk("missing".to_owned());
            assert!(
                unknown
                    .restore(&writer_ordinals, &writer_inventories)
                    .is_err()
            );

            let mut missing_other_anchor =
                SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("transfer snapshots");
            missing_other_anchor.conditional_bodies[0]
                .related_sources
                .remove("other")
                .expect("the written transfer anchors its other-cell module");
            let anchor_error = missing_other_anchor
                .restore(&writer_ordinals, &writer_inventories)
                .expect_err("the referenced other module must remain source-anchored");
            assert!(
                anchor_error
                    .to_string()
                    .contains("source-anchor set does not exactly cover referenced modules")
            );

            let mut unknown_id = summary.clone();
            let unknown_cell = ModuleCellId {
                chunk: ChunkId(writer_chunks.len()),
                declaration_origin: provider_binding.lo.0,
            };
            let normal_cells = &mut unknown_id.provider_transfers[0].alternatives[0].module_cells;
            normal_cells.reads.insert(unknown_cell);
            normal_cells
                .invariants
                .insert(unknown_cell, provider_invariant.clone());
            assert!(
                SavedCallableSummary::snapshot(&unknown_id, &writer_ordinals, &writer_inventories,)
                    .is_err()
            );

            let bytes = rmp_serde::to_vec_named(&saved).expect("saved callable encodes");
            let decoded: SavedCallableSummary<'static> =
                rmp_serde::from_slice(&bytes).expect("saved callable decodes");
            let reader_chunks = intern_chunks(&["other", "helper", "provider"]);
            let reader_provider_chunk = reader_chunks.get("provider").unwrap();
            let reader_other_chunk = reader_chunks.get("other").unwrap();
            assert_ne!(writer_provider_chunk, reader_provider_chunk);
            assert_ne!(writer_other_chunk, reader_other_chunk);
            let reader_sources = source_fixtures(&reader_chunks, &sources);
            let reader_inventories = inventories(&reader_sources);
            let reader_ordinals = ContextOrdinals::new(
                reader_sources
                    .values()
                    .map(|fixture| &fixture.parsed.module),
                &reader_chunks,
            );
            let reader_provider = &reader_sources["provider"];
            let reader_other = &reader_sources["other"];
            let (_, reader_provider_binding, reader_provider_initializer) =
                variable_coordinates(&reader_provider.parsed.module, "providerCell");
            let (reader_other_owner, reader_other_binding, reader_other_initializer) =
                variable_coordinates(&reader_other.parsed.module, "otherCell");
            let (reader_update_owner, reader_update_key) =
                function_identity(reader_other, "updateOther");
            let reader_wrap_key = function_identity(reader_provider, "wrap").1;
            let reader_provider_cell = ModuleCellId {
                chunk: reader_provider_chunk,
                declaration_origin: reader_provider_binding.lo.0,
            };
            let reader_other_cell = ModuleCellId {
                chunk: reader_other_chunk,
                declaration_origin: reader_other_binding.lo.0,
            };
            let reader_provider_invariant = PrimitiveCellInvariant {
                declaration: BindingCellDeclarationIdentity {
                    owner: variable_coordinates(&reader_provider.parsed.module, "providerCell").0,
                    activation_callable: None,
                    binding_span: reader_provider_binding,
                    initializer_span: reader_provider_initializer,
                },
                post_init_kinds: analysis_model::PrimKinds::STRING,
                stores: Vec::new(),
            };
            let reader_other_invariant = PrimitiveCellInvariant {
                declaration: BindingCellDeclarationIdentity {
                    owner: reader_other_owner,
                    activation_callable: None,
                    binding_span: reader_other_binding,
                    initializer_span: reader_other_initializer,
                },
                post_init_kinds: analysis_model::PrimKinds::NUMBER,
                stores: vec![BindingCellStoreEvidence {
                    owner: reader_update_owner,
                    activation_callable: Some(reader_update_key),
                    span: assignment_span(&reader_other.parsed.module, "otherCell"),
                    kind: BindingStoreKind::Assignment,
                }],
            };
            let reader_throw_origin = reader_provider
                .inventory
                .source_sites
                .iter()
                .find(|site| site.step == OperationStep::Throw)
                .cloned()
                .expect("reader source has its canonical throw site");
            assert!(
                reader_provider
                    .inventory
                    .source_sites
                    .contains(&reader_throw_origin)
            );
            let restored = decoded
                .restore(&reader_ordinals, &reader_inventories)
                .expect("checked chunk names and source anchors restore");
            let mut expected = summary.clone();
            let expected_transfer = &mut expected.provider_transfers[0];
            expected_transfer.identity = ConditionalProviderIdentity {
                defining_module: "provider".to_owned(),
                export: "wrap".to_owned(),
                local: "wrap".to_owned(),
                callable_key: reader_wrap_key,
                source: Some(provider_callable_source(reader_provider, reader_wrap_key)),
            };
            expected_transfer.alternatives[0].module_cells = ModuleCellAccesses {
                reads: BTreeSet::from([reader_provider_cell]),
                writes: BTreeSet::new(),
                invariants: BTreeMap::from([(reader_provider_cell, reader_provider_invariant)]),
            };
            expected_transfer.alternatives[1].completion = ConditionalProviderCompletion::Throw {
                origin: reader_throw_origin,
                value: ConditionalProviderValue::RetainedGraph(graph),
            };
            expected_transfer.alternatives[1].module_cells = ModuleCellAccesses {
                reads: BTreeSet::new(),
                writes: BTreeSet::from([reader_other_cell]),
                invariants: BTreeMap::from([(reader_other_cell, reader_other_invariant)]),
            };
            assert_eq!(restored, expected);
            assert_eq!(
                restored.provider_transfers[0].alternatives[1].completion,
                summary.provider_transfers[0].alternatives[1].completion
            );

            let shifted_frame_bytes = rmp_serde::to_vec_named(
                &SavedCallableSummary::snapshot(&summary, &writer_ordinals, &writer_inventories)
                    .expect("transfer snapshots for old parser-frame check"),
            )
            .expect("old parser-frame saved value encodes");
            let mut shifted_frame: SavedCallableSummary<'static> =
                rmp_serde::from_slice(&shifted_frame_bytes)
                    .expect("old parser-frame saved value decodes with report-only fields erased");
            let delta = 4096;
            {
                let report_transfer = &mut shifted_frame.summary.to_mut().provider_transfers[0];
                report_transfer.identity.callable_key = report_transfer
                    .identity
                    .callable_key
                    .checked_add(delta)
                    .expect("test frame shift stays within source-coordinate bounds");
                let source = report_transfer
                    .identity
                    .source
                    .as_mut()
                    .expect("conditional transfer has a source identity");
                source.source_start = source
                    .source_start
                    .checked_add(delta)
                    .expect("test frame shift stays within source-coordinate bounds");
                let ConditionalProviderCompletion::Throw { origin, value } =
                    &mut report_transfer.alternatives[1].completion
                else {
                    panic!("fixture retains a throw alternative");
                };
                origin.span.lo.0 = origin
                    .span
                    .lo
                    .0
                    .checked_add(delta)
                    .expect("test frame shift stays within source-coordinate bounds");
                origin.span.hi.0 = origin
                    .span
                    .hi
                    .0
                    .checked_add(delta)
                    .expect("test frame shift stays within source-coordinate bounds");
                match &mut origin.body {
                    crate::facts::sync_effects::LocalBodyIdentity::Callable { key, .. } => {
                        *key = key
                            .checked_add(delta)
                            .expect("test frame shift stays within source-coordinate bounds");
                    }
                    other => panic!("fixture throw body has callable identity, found {other:?}"),
                }
                let ConditionalProviderValue::RetainedGraph(graph) = value else {
                    panic!("throw alternative retains its graph payload");
                };
                for node in &mut graph.nodes {
                    if let super::super::ProviderGraphNodeKind::Closure { source_key } =
                        &mut node.kind
                    {
                        *source_key = source_key
                            .checked_add(delta)
                            .expect("test frame shift stays within source-coordinate bounds");
                    }
                }
            }
            assert_eq!(
                shifted_frame
                    .restore(&writer_ordinals, &writer_inventories)
                    .expect("relative sidecar restores across an old absolute parser frame"),
                summary
            );
        });
    }
}
