//! Prepares source-backed synchronous-call evidence and annotates call effects.

use super::super::execution_index::{
    Call, CallKind, ExecutionIndex, native_call_identity_key, native_own_data_read_slots,
};
use super::super::value_model::{
    AbstractValue, CallSite, CaptureInterner, MemberKey, ValueExpr, Values,
};
use super::super::world::prefix_values::{PrefixValues, TopBindings};
use super::super::world::{World, WorldScope};
use super::{
    CallbackFallbackSummary, ExecutionInputs, ExecutionState, ExportCallbackMemos, Outcome,
};
use analysis::analysis_inputs::{
    AnalysisInputs, NativeInputKind, NativeIntrinsic, ProofDependencies,
};
use analysis::facts::abstract_domain::integrity::IntrinsicSlot;
use analysis::facts::abstract_domain::value::{Kind, Value};
use analysis::facts::callable_inference::CallableInferenceInputs;
use analysis::facts::item_views::TopLevelItemView;
use analysis::facts::semantic_inference::SemanticFacts;
use analysis::facts::source_unit_read_closure::SourceUnitReadClosure;
use analysis::facts::statement_facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection, HeapContentCells, StatementFacts,
};
use analysis::facts::value_flow::{ChunkValueFlow, Values as FlowValues};
use analysis::facts::{
    DataflowOpacityCause, ExportReentryPolicy, LocalClassConstructionProof,
    LocalClassConstructionShapes, NativeIterableOperandOrigin, NativeOwnDataReadDiagnostic,
    SetIterableProof, SetIterableShapes, StableArraySpreadShapes, StatementPurityContext,
    TopLevelNativeIterableOperandSite,
};
use analysis::inference::{FactContext, IterativeInference, IterativeRules};
use analysis::purity::{ChunkCodeGraph, Effects};
use imbl::OrdMap;
use rustc_hash::{FxHashMap, FxHashSet};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::*;

pub(super) struct TopLevelEffectView<'source> {
    pub(super) reads: Vec<&'source Id>,
    pub(super) rebinds: Vec<&'source Id>,
}

pub(super) struct OwnerLazyEffectViews<'source> {
    pub(super) synchronous: Option<TopLevelEffectView<'source>>,
    pub(super) incomplete: TopLevelEffectView<'source>,
}

fn top_level_effect_view<'source>(
    reads: &'source BTreeSet<Id>,
    rebinds: &'source BTreeSet<Id>,
    top_bindings: &FxHashSet<Id>,
) -> TopLevelEffectView<'source> {
    TopLevelEffectView {
        reads: reads
            .iter()
            .filter(|id| top_bindings.contains(*id))
            .collect(),
        rebinds: rebinds
            .iter()
            .filter(|id| top_bindings.contains(*id))
            .collect(),
    }
}

/// Reuse the canonical nested identifier census, including deferred reads.
/// Borrow declaration keys for this temporary join; retain only the cells
/// that a nested callable can actually capture from an ordered activation.
fn captured_local_bindings(index: &ExecutionIndex) -> FxHashSet<Id> {
    let owners: FxHashMap<_, _> = index
        .callables
        .iter()
        .filter_map(|(key, callable)| Some((*key, callable.body_facts.ordered_plan()?)))
        .filter(|(_, body)| !body.requires_keyed_collection_new())
        .flat_map(|(key, body)| body.bindings.keys().map(move |binding| (binding, key)))
        .collect();
    let mut captured = FxHashSet::default();
    // A nested callable's write must reach its owner's frame: the binding
    // gets a cell, which the writer's ordered lane requires and stores to.
    for (key, callable) in &index.callables {
        for binding in callable.writes() {
            if let Some(owner) = owners.get(binding)
                && owner != key
            {
                captured.insert(binding.clone());
            }
        }
    }
    for (key, reads) in &index.prototype_callable_reads {
        let Some(span) = index.callable_spans.get(key) else {
            continue;
        };
        for binding in reads {
            let Some(owner) = owners.get(binding) else {
                continue;
            };
            if owner == key {
                continue;
            }
            if index
                .callable_spans
                .get(owner)
                .is_some_and(|outer| outer.contains(*span))
            {
                captured.insert(binding.clone());
            }
        }
    }
    captured
}

/// Source units with a call that may run code the source does not name
/// (SPEC.md § Synchronous execution diagnostics): a call or `new` whose callee
/// is neither a builtin row nor local callables by value flow, a protocol
/// operation on a value rooted in an import, a global or another unknown
/// binding, or an opaque body.
fn unknown_callee_owners(
    index: &ExecutionIndex,
    flow: &ChunkValueFlow,
) -> BTreeSet<SourceUnitOrdinal> {
    let local_callables = |values: FlowValues| {
        !values.is_empty()
            && values
                .iter()
                .all(|value| matches!(value, Value::Alloc(Kind::Function | Kind::Class, _)))
    };
    let names_local_callables = |callee: &ValueExpr| match callee {
        ValueExpr::Alloc(Kind::Function | Kind::Class, _) => true,
        ValueExpr::Binding(binding) => local_callables(flow.values_of_binding(binding)),
        ValueExpr::Member(receiver, Some(property), _, _) => {
            let ValueExpr::Binding(receiver) = receiver.as_ref() else {
                return false;
            };
            let receivers = flow.values_of_binding(receiver);
            !receivers.is_empty()
                && receivers.iter().all(|receiver| {
                    matches!(receiver, Value::Alloc(Kind::Object, object)
                        if flow.values_of_property(object, property)
                            .is_some_and(|values| local_callables(values.clone())))
                })
        }
        _ => false,
    };
    fn unknown_root(expression: &ValueExpr, flow: &ChunkValueFlow) -> bool {
        match expression {
            ValueExpr::Binding(binding) => {
                let values = flow.values_of_binding(binding);
                values.is_empty() || values.iter().any(|value| value == Value::Top)
            }
            ValueExpr::Member(receiver, _, _, _) => unknown_root(receiver, flow),
            _ => false,
        }
    }
    let unknown = |call: &Call| match call.kind {
        CallKind::Call | CallKind::New => {
            !native_call_identity_key(call.kind, &call.callee)
                .is_some_and(|(_, callee)| !flow.is_declared(&callee.binding))
                && !names_local_callables(&call.callee)
        }
        _ => unknown_root(&call.callee, flow),
    };
    index
        .top_calls
        .iter()
        .flatten()
        .chain(
            index
                .callables
                .values()
                .flat_map(|callable| &callable.calls),
        )
        .filter(|call| unknown(call))
        .map(|call| call.owner)
        .chain(
            index
                .callables
                .values()
                .filter(|callable| callable.opaque)
                .map(|callable| callable.owner),
        )
        .collect()
}

fn callback_source_units_for_signature(
    callback_argument_exprs: &BTreeMap<usize, ValueExpr>,
    callback_signature: &spec::RetainedCallbackSignature,
    flow: &ChunkValueFlow,
) -> Option<BTreeSet<SourceUnitOrdinal>> {
    let mut source_units = BTreeSet::new();
    for position in &callback_signature.callback_arguments {
        let expression = callback_argument_exprs.get(position)?;
        let values: BTreeSet<_> = match expression {
            ValueExpr::Binding(binding) => flow.values_of_binding(binding).into_iter().collect(),
            ValueExpr::Alloc(kind, site) => BTreeSet::from([Value::Alloc(*kind, *site)]),
            // Calls, object members and aggregate expressions can return or
            // expose callbacks through paths this local witness does not link.
            _ => return None,
        };
        if values.len() != 1 {
            return None;
        }
        for value in values {
            if !matches!(value, Value::Alloc(Kind::Function, _)) {
                return None;
            }
            source_units.insert(*flow.callable_source_unit.get(&value)?);
        }
    }
    Some(source_units)
}

/// Prove the narrow return-only wrapper shape using the same execution index
/// as call promotion. Whole-chunk flow must contain exactly that function;
/// aliases with unknown/reassigned targets and dynamic scopes remain opaque.
pub struct SynchronousCallProofs<'a> {
    pub set_iterable: &'a SetIterableProof,
    pub graph: &'a ChunkCodeGraph,
    pub purity: &'a StatementPurityContext<'a>,
    pub callable_heap_contents: &'a BTreeMap<u32, HeapContentCells>,
    pub index: PreparedSynchronousCallIndex,
}

pub struct PreparedSynchronousCallIndex {
    index: ExecutionIndex,
    set_iterable_proof: SetIterableProof,
    native_own_data_read_candidates: BTreeMap<SourceUnitOrdinal, Vec<NativeOwnDataReadCandidate>>,
}

enum NativeOwnDataReadCandidate {
    Exact {
        span: Span,
        slots: [IntrinsicSlot; 2],
    },
    Unrecognized {
        span: Span,
        detail: String,
    },
}

/// Exact native calls whose own heap action cannot observe an existing array
/// or Set. Argument evaluation remains the collector's responsibility.
pub struct FreshRecordFreezeHeapProof {
    /// Each certified call site and the `native_call_identity_inputs` entry
    /// that certified it.
    sites: BTreeMap<(u32, u32), analysis::analysis_inputs::NativeOperationInputEvidence>,
}

impl analysis::facts::heap_contents::FreshFreezeInputs for FreshRecordFreezeHeapProof {
    fn input_evidence(
        &self,
        call: &CallExpr,
    ) -> Option<&analysis::analysis_inputs::NativeOperationInputEvidence> {
        self.sites.get(&(call.span.lo.0, call.span.hi.0))
    }
}

impl analysis::facts::heap_contents::NativeHeapReads for PreparedSynchronousCallIndex {
    fn native_own_data_read(
        &self,
        span: Span,
    ) -> Option<(
        analysis::analysis_provenance::InferenceUse,
        Option<analysis::analysis_inputs::NativeOperationInputEvidence>,
        ProofDependencies,
    )> {
        PreparedSynchronousCallIndex::native_own_data_read(self, span)
    }

    /// A direct native Map consumes only private, dense entry arrays here.
    /// The complete-program census must protect every native protocol step;
    /// ordinary purity of the constructor does not establish heap locality.
    fn primitive_map_literal_heap_proof(
        &self,
        construction: &NewExpr,
        flow: &ChunkValueFlow,
        shadowed: &BTreeSet<&'static str>,
    ) -> Option<ProofDependencies> {
        let Expr::Ident(callee) = construction.callee.unwrap_parens() else {
            return None;
        };
        if callee.sym != "Map"
            || flow.is_declared(&callee.to_id())
            || shadowed.contains("Map")
            || self.index.reassigned_map
            || !self.index.dynamic_scope_owners.is_empty()
        {
            return None;
        }
        let [argument] = construction.args.as_deref()? else {
            return None;
        };
        if argument.spread.is_some() {
            return None;
        }
        let Expr::Array(entries) = argument.expr.unwrap_parens() else {
            return None;
        };
        for entry in &entries.elems {
            let Some(entry) = entry else { return None };
            if entry.spread.is_some() {
                return None;
            }
            let Expr::Array(pair) = entry.expr.unwrap_parens() else {
                return None;
            };
            let [Some(key), Some(value)] = pair.elems.as_slice() else {
                return None;
            };
            if key.spread.is_some()
                || value.spread.is_some()
                || !js_ast::is_primitive_literal(key.expr.unwrap_parens())
                || !js_ast::is_primitive_literal(value.expr.unwrap_parens())
            {
                return None;
            }
        }
        // Map construction gets its adder from the new instance, then runs
        // the Array iterator and reads each fresh pair's own 0/1 properties.
        // The conservative whole-prototype guards include symbol-key writes.
        self.index
            .integrity
            .require(
                &[
                    IntrinsicSlot::Global(NativeIntrinsic::Map),
                    IntrinsicSlot::Static(NativeIntrinsic::Map, "prototype"),
                    IntrinsicSlot::Prototype(NativeIntrinsic::Map),
                    IntrinsicSlot::Prototype(NativeIntrinsic::Array),
                    IntrinsicSlot::ArrayIteratorPrototype,
                ],
                None,
            )
            .dependencies()
    }
}

impl PreparedSynchronousCallIndex {
    pub fn native_own_data_read_diagnostics(
        &self,
        owner: SourceUnitOrdinal,
    ) -> Vec<NativeOwnDataReadDiagnostic> {
        self.native_own_data_read_candidates
            .get(&owner)
            .into_iter()
            .flatten()
            .map(|candidate| match candidate {
                NativeOwnDataReadCandidate::Exact { span, slots } => self
                    .native_own_data_read(*span)
                    .map(|(_, _, dependencies)| NativeOwnDataReadDiagnostic::Proven {
                        dependencies,
                    })
                    .unwrap_or_else(|| NativeOwnDataReadDiagnostic::MissingProof {
                        span: *span,
                        detail: self.native_own_data_read_failure_detail(*span, slots),
                    }),
                NativeOwnDataReadCandidate::Unrecognized { span, detail } => {
                    NativeOwnDataReadDiagnostic::Unrecognized {
                        span: *span,
                        detail: detail.clone(),
                    }
                }
            })
            .collect()
    }

    fn native_own_data_read_failure_detail(
        &self,
        span: Span,
        slots: &[IntrinsicSlot; 2],
    ) -> String {
        let required = slots
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join(", ");
        let dynamic_scope = !self.index.dynamic_scope_owners.is_empty();
        let missing = if dynamic_scope {
            slots.to_vec()
        } else {
            slots
                .iter()
                .copied()
                .filter(|slot| {
                    self.index
                        .integrity
                        .require(std::slice::from_ref(slot), Some(span.lo.0))
                        .dependencies()
                        .is_none()
                })
                .collect()
        };
        if dynamic_scope {
            format!(
                "Native own-data read requires intact slots {required}; dynamic scope leaves native binding identity unresolved. Use `debundle explain-integrity` with `--slot` for each listed slot to inspect the program census witnesses."
            )
        } else if !missing.is_empty() {
            let missing = missing
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join(", ");
            format!(
                "Native own-data read requires intact slots {required}; this run has no proof for {missing}. Use `debundle explain-integrity` with `--slot` for each missing slot to inspect the program native-integrity census witnesses."
            )
        } else {
            format!(
                "Native own-data read requires one proof for slots {required}; no proof was accepted for this complete slot set. Use `debundle explain-integrity` with `--slot` for each listed slot to inspect the program census side."
            )
        }
    }

    pub fn resolved_native_operation_inputs(
        &self,
    ) -> Vec<analysis::analysis_inputs::ResolvedNativeOperationInput> {
        self.index.validated_native_operation_inputs()
    }

    pub fn locate_native_integrity_witnesses(
        &mut self,
        locate: impl FnMut(Span) -> Option<analysis::execution_evidence::SourceLocation>,
    ) {
        self.index.locate_native_integrity_witnesses(locate);
    }

    pub fn set_iterable_proof(&self) -> &SetIterableProof {
        &self.set_iterable_proof
    }

    pub fn local_class_construction_proof(&self) -> &LocalClassConstructionProof {
        &self.index.local_class_construction_proof
    }

    pub fn local_class_heap_proof(&self) -> analysis::facts::LocalClassHeapProof {
        self.index.local_class_construction_proof.heap_proof()
    }

    pub fn fresh_record_freeze_heap_proof(&self) -> FreshRecordFreezeHeapProof {
        let sites = self
            .index
            .resolved_native_operation_inputs
            .iter()
            .filter(|input| {
                input.evidence.operation
                    == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            })
            .filter_map(|input| {
                let call = self
                    .index
                    .direct_freeze_return_call(&input.binding, input.selected_statement_span)?;
                let [ValueExpr::ObjectLiteral(object, _)] = call.args.as_slice() else {
                    return None;
                };
                (call.span == input.call_span && self.index.plain_data_objects.contains(object))
                    .then(|| ((call.span.lo.0, call.span.hi.0), input.evidence.clone()))
            })
            .collect();
        FreshRecordFreezeHeapProof { sites }
    }

    pub(crate) fn native_own_data_read(
        &self,
        span: Span,
    ) -> Option<(
        analysis::analysis_provenance::InferenceUse,
        Option<analysis::analysis_inputs::NativeOperationInputEvidence>,
        ProofDependencies,
    )> {
        let premise = self.index.native_own_data_read_premise(span)?;
        let evidence = if premise.census {
            None
        } else {
            Some(
                self.index
                    .native_operation_input_evidence
                    .get(&CallSite::from(span))?
                    .clone(),
            )
        };
        Some((premise.inference_use, evidence, premise.dependencies))
    }
}

/// Shape-proof refinement for purity code graphs built outside full chunk
/// analysis: prepares the synchronous call index under default inputs.
pub struct SynchronousShapeRefinement;

impl analysis::purity::ShapeProofRefinement for SynchronousShapeRefinement {
    fn refine(
        &self,
        body: &[TopLevelItemView<'_>],
        value_flow: &ChunkValueFlow,
        plain_array_bindings: &BTreeSet<String>,
        local_class_construction: &LocalClassConstructionShapes,
        set_iterable_shapes: SetIterableShapes,
        shadowed: &BTreeSet<&'static str>,
    ) -> (LocalClassConstructionProof, SetIterableProof) {
        let prepared = prepare_synchronous_call_index(
            body,
            value_flow,
            &AnalysisInputs::default(),
            plain_array_bindings,
            local_class_construction,
            set_iterable_shapes,
            shadowed,
        );
        (
            prepared.local_class_construction_proof().clone(),
            prepared.set_iterable_proof().clone(),
        )
    }
}

pub fn prepare_synchronous_call_index(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    hints: &AnalysisInputs,
    plain_array_bindings: &BTreeSet<String>,
    local_class_construction: &LocalClassConstructionShapes,
    set_iterable_shapes: SetIterableShapes,
    shadowed: &BTreeSet<&'static str>,
) -> PreparedSynchronousCallIndex {
    prepare_synchronous_call_index_with_source_facts(
        body,
        flow,
        hints,
        plain_array_bindings,
        PreparedSyncShapeInputs {
            local_class_construction,
            set_iterable_shapes,
            stable_array_spread_shapes: StableArraySpreadShapes::default(),
            shadowed,
            source_facts: None,
        },
    )
}

pub struct PreparedSyncShapeInputs<'a> {
    pub local_class_construction: &'a LocalClassConstructionShapes,
    pub set_iterable_shapes: SetIterableShapes,
    pub stable_array_spread_shapes: StableArraySpreadShapes,
    pub shadowed: &'a BTreeSet<&'static str>,
    pub source_facts: Option<&'a Arc<SemanticFacts>>,
}

pub fn prepare_synchronous_call_index_with_source_facts(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    hints: &AnalysisInputs,
    plain_array_bindings: &BTreeSet<String>,
    shapes: PreparedSyncShapeInputs<'_>,
) -> PreparedSynchronousCallIndex {
    let PreparedSyncShapeInputs {
        local_class_construction,
        set_iterable_shapes,
        stable_array_spread_shapes,
        shadowed,
        source_facts,
    } = shapes;
    let mut index = ExecutionIndex::collect(
        body,
        hints,
        Some(flow),
        plain_array_bindings,
        local_class_construction,
    );
    // The census is now complete. Publish its binding and mutation premises
    // into one immutable context shared by timing, purity and heap consumers.
    index.integrity = index.integrity_flags();
    let classes = std::mem::take(&mut index.local_class_construction_shapes);
    let facts = index.prepare_semantic_facts(
        flow,
        shadowed,
        super::super::execution_index::SemanticShapeInputs {
            classes,
            sets: set_iterable_shapes,
            array_spreads: &stable_array_spread_shapes,
            callables: CallableInferenceInputs::default(),
        },
        source_facts,
    );
    index.semantic_facts = Some(Arc::clone(&facts));
    index.local_class_construction_proof = LocalClassConstructionProof::new(Arc::clone(&facts));
    let author_set_inputs = index
        .native_operation_input_evidence
        .iter()
        .filter(|(_, evidence)| {
            evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
        })
        .map(|(site, evidence)| (site.allocation_id(), evidence.clone()))
        .collect();
    let constructor_operands: BTreeMap<_, _> = index
        .top_calls
        .iter()
        .flatten()
        .filter_map(|call| {
            if call.kind != CallKind::New
                || call.caller.is_some()
                || call.has_spread
                || !matches!(&call.callee, ValueExpr::Binding(binding) if binding.0.as_ref() == "Set")
            {
                return None;
            }
            let [ValueExpr::ObjectLiteral(allocation_site, _)] = call.args.as_slice() else {
                return None;
            };
            // ObjectLiteral is the value-flow node for arrays and objects;
            // only its array-elements index establishes the source kind.
            if !index.array_elements.contains_key(allocation_site) {
                return None;
            }
            Some((
                call.span.lo.0,
                TopLevelNativeIterableOperandSite {
                    operation_span: call.span,
                    operand: NativeIterableOperandOrigin::ImmediateArrayLiteral {
                        allocation_site: *allocation_site,
                    },
                    owner: call.owner,
                },
            ))
        })
        .collect();
    let set_iterable_proof = SetIterableProof::new(
        facts,
        author_set_inputs,
        hints,
        stable_array_spread_shapes.clone(),
        &constructor_operands,
    );
    index.local_constructors.retain(|key, constructor| {
        !constructor.checked_default_fields
            || index.local_class_construction_proof.contains_class(*key)
    });
    let mut native_own_data_read_candidates: BTreeMap<
        SourceUnitOrdinal,
        Vec<NativeOwnDataReadCandidate>,
    > = BTreeMap::new();
    let mut native_own_data_reads = BTreeMap::new();
    for call in index.top_calls.iter().flatten() {
        if call.kind != CallKind::LocalMemberRead {
            continue;
        }
        let Some(candidate) = native_own_data_read_candidate(call, flow, shadowed) else {
            continue;
        };
        if let NativeOwnDataReadCandidate::Exact { span, slots } = &candidate {
            native_own_data_reads.insert(span.lo.0, *slots);
        }
        native_own_data_read_candidates
            .entry(call.owner)
            .or_default()
            .push(candidate);
    }
    index.native_own_data_reads = native_own_data_reads;
    PreparedSynchronousCallIndex {
        index,
        set_iterable_proof,
        native_own_data_read_candidates,
    }
}

fn native_own_data_read_candidate(
    call: &Call,
    flow: &ChunkValueFlow,
    shadowed: &BTreeSet<&'static str>,
) -> Option<NativeOwnDataReadCandidate> {
    let ValueExpr::Member(receiver, property, member_key, _) = &call.callee else {
        return None;
    };
    let ValueExpr::Binding(binding) = receiver.as_ref() else {
        return None;
    };
    let family = NativeIntrinsic::for_global_binding(binding.0.as_ref())?;
    let name = binding.0.as_ref();
    if flow.is_declared(binding) || shadowed.contains(name) {
        return Some(NativeOwnDataReadCandidate::Unrecognized {
            span: call.span,
            detail: format!(
                "`{name}` resolves to a chunk binding here, so this read does not resolve to its native binding; no native slot proof applies."
            ),
        });
    }
    if !matches!(member_key, MemberKey::Static) {
        return Some(NativeOwnDataReadCandidate::Unrecognized {
            span: call.span,
            detail: format!(
                "`{name}[...]` uses computed member access, so this read does not identify an exact native own-data slot to query."
            ),
        });
    }
    let Some(slots) = native_own_data_read_slots(&call.callee, |_| false) else {
        let property = property.as_deref().unwrap_or("<unknown>");
        let global = family.global_binding().unwrap_or(name);
        return Some(NativeOwnDataReadCandidate::Unrecognized {
            span: call.span,
            detail: format!(
                "`{global}.{property}` is not a recognized native own-data slot; there is no exact slot proof to query."
            ),
        });
    };
    Some(NativeOwnDataReadCandidate::Exact {
        span: call.span,
        slots,
    })
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum PrefixNeededKey {
    Binding(Id),
    Object(Value),
}

struct PrefixNeededRules<'a> {
    dependencies: &'a BTreeMap<Id, BTreeSet<Id>>,
    member_write_receivers: &'a BTreeMap<Value, BTreeSet<Id>>,
    flow: &'a ChunkValueFlow,
}

impl IterativeRules for PrefixNeededRules<'_> {
    type Key = PrefixNeededKey;
    type Fact = ();
    type Rule = PrefixNeededKey;

    type MergeScratch = ();
    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        match rule {
            PrefixNeededKey::Binding(binding) => {
                if context.read(rule.clone()).is_none() {
                    return;
                }
                if let Some(inputs) = self.dependencies.get(binding) {
                    for input in inputs {
                        context.propose(PrefixNeededKey::Binding(input.clone()), ());
                    }
                }
                for origin in self.flow.values_of_binding(binding) {
                    if matches!(origin, Value::Alloc(Kind::Object, _))
                        && self.member_write_receivers.contains_key(&origin)
                    {
                        context.propose(PrefixNeededKey::Object(origin), ());
                    }
                }
            }
            PrefixNeededKey::Object(origin) => {
                if context.read(rule.clone()).is_none() {
                    return;
                }
                if let Some(receivers) = self.member_write_receivers.get(origin) {
                    for receiver in receivers {
                        context.propose(PrefixNeededKey::Binding(receiver.clone()), ());
                    }
                }
            }
        }
    }

    fn merge(_: &Self::Key, _: &mut Self::Fact, _: Self::Fact) -> bool {
        false
    }
}

fn close_prefix_needed_bindings(
    seeds: &BTreeSet<Id>,
    dependencies: &BTreeMap<Id, BTreeSet<Id>>,
    member_write_receivers: &BTreeMap<Value, BTreeSet<Id>>,
    flow: &ChunkValueFlow,
) -> BTreeSet<Id> {
    let mut producers = BTreeSet::new();
    for (binding, inputs) in dependencies {
        producers.insert(PrefixNeededKey::Binding(binding.clone()));
        producers.extend(inputs.iter().cloned().map(PrefixNeededKey::Binding));
    }
    for (origin, receivers) in member_write_receivers {
        producers.insert(PrefixNeededKey::Object(*origin));
        producers.extend(receivers.iter().cloned().map(PrefixNeededKey::Binding));
    }
    producers.extend(seeds.iter().cloned().map(PrefixNeededKey::Binding));

    let mut inference = IterativeInference::new(PrefixNeededRules {
        dependencies,
        member_write_receivers,
        flow,
    });
    for binding in seeds {
        inference.seed(PrefixNeededKey::Binding(binding.clone()), ());
    }
    inference
        .solve(producers)
        .into_values()
        .into_keys()
        .filter_map(|key| match key {
            PrefixNeededKey::Binding(binding) => Some(binding),
            PrefixNeededKey::Object(_) => None,
        })
        .collect()
}

pub fn annotate_synchronous_calls(
    flow: &ChunkValueFlow,
    proofs: SynchronousCallProofs<'_>,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    export_reentry_policy: &ExportReentryPolicy,
    facts: &mut [StatementFacts],
    metrics: &work_metrics::WorkMetrics,
) -> Vec<analysis::facts::CompletedPurity> {
    let index = proofs.index.index;
    for candidate in index.retained_callback_calls.values() {
        if candidate.caller.is_some() {
            continue;
        }
        let Some((namespace, member)) = &candidate.call.namespace_member else {
            continue;
        };
        if index
            .audited_factory_members
            .get(namespace)
            .is_some_and(|members| members.contains(member))
        {
            continue;
        }
        let witness = index
            .rejected_namespace_member_writes
            .get(&(namespace.clone(), member.clone()))
            .cloned();
        let dynamic_write_witness = index
            .rejected_namespace_dynamic_property_writes
            .get(&(namespace.clone(), member.clone()))
            .cloned();
        let call_argument_witness = index
            .rejected_namespace_call_arguments
            .get(namespace)
            .cloned();
        for callback_signature in candidate
            .call
            .signatures
            .iter()
            .filter(|callback_signature| {
                // `retained_callback_invocation` already applies these guards to
                // the parsed call. Keep them explicit here so a configured but
                // inapplicable overload can never be reported as the match.
                callback_signature.argument_count == candidate.call.argument_count
                    && callback_signature
                        .callback_arguments
                        .iter()
                        .all(|position| {
                            *position < candidate.call.argument_count
                                && candidate
                                    .call
                                    .callback_argument_exprs
                                    .contains_key(position)
                        })
            })
        {
            let callback_source_units = callback_source_units_for_signature(
                &candidate.call.callback_argument_exprs,
                callback_signature,
                flow,
            );
            facts[candidate.owner.0]
                .callback_factory_barriers
                .push(CallbackFactoryBarrierFact {
                    namespace_binding: namespace.0.to_string(),
                    member: member.clone(),
                    argument_count: callback_signature.argument_count,
                    callback_arguments: callback_signature.callback_arguments.clone(),
                    callback_source_units,
                    rejection: match &witness {
                        Some(witness) => CallbackFactoryBarrierRejection::NamespaceMemberWrite {
                            witness: witness.clone(),
                        },
                        None => match &dynamic_write_witness {
                            Some(witness) => {
                                CallbackFactoryBarrierRejection::NamespaceDynamicPropertyWrite {
                                    witness: witness.clone(),
                                }
                            }
                            None => match &call_argument_witness {
                                Some(witness) => {
                                    CallbackFactoryBarrierRejection::NamespaceCallArgument {
                                        witness: witness.clone(),
                                    }
                                }
                                None => CallbackFactoryBarrierRejection::UnsupportedGuard,
                            },
                        },
                    },
                });
        }
    }
    // Every top-level call is executed by `top_calls`, regardless of whether
    // its result is retained. Keep an initializer's value only if a later
    // call may read it, directly or through another initializer/function.
    // This avoids re-evaluating thousands of unused factory results.
    let mut needed: BTreeSet<Id> = index
        .callables
        .values()
        .flat_map(|callable| callable.reads.iter().cloned())
        .collect();
    for calls in &index.top_calls {
        for call in calls {
            call.callee.bindings(&mut needed);
            for arg in &call.args {
                arg.bindings(&mut needed);
            }
        }
    }
    // Property-write analysis also needs the receiver's prefix identity, even
    // when only another alias is passed to a later call. In particular, a
    // callable's custom fields/prototype are outside the object-record model.
    for write in index.top_callback_result_writes.iter().flatten() {
        write.receiver.bindings(&mut needed);
        write.value.bindings(&mut needed);
    }
    // Retain prefix values for direct heritage reachability and captured bases.
    for key in index.class_bindings.keys() {
        if let Some(super_class) = index
            .class_values
            .get(key)
            .and_then(|class| class.super_class.as_ref())
        {
            super_class.bindings(&mut needed);
        }
    }
    // A destructuring member target may run a setter on its receiver.
    for receivers in &index.top_destructuring_member_receivers {
        for receiver in receivers {
            receiver.bindings(&mut needed);
        }
    }
    needed.extend(index.exported_bindings.iter().cloned());
    for exported in &index.top_exported_values {
        for expression in exported {
            expression.bindings(&mut needed);
        }
    }
    let mut dependencies: BTreeMap<Id, BTreeSet<Id>> = BTreeMap::new();
    for assignments in &index.top_assignments {
        for (id, expression) in assignments {
            expression.bindings(dependencies.entry(id.clone()).or_default());
        }
    }
    let mut member_write_receivers = BTreeMap::<Value, BTreeSet<Id>>::new();
    for writes in &index.top_member_value_writes {
        for write in writes {
            let receiver = &write.receiver;
            let expression = &write.value;
            if index.global_object_names.contains(receiver.0.as_ref()) {
                // Global-object writes are potential callback escape roots at
                // every later opaque call, even when the receiver alias is
                // not mentioned by a statically visible callee.
                expression.bindings(&mut needed);
            } else {
                expression.bindings(dependencies.entry(receiver.clone()).or_default());
                // A later call may read an object through a different local
                // alias than the one used for this property write. Retain
                // that receiver's prefix value whenever its known object
                // identity also reaches a needed binding.
                for origin in flow.values_of_binding(receiver) {
                    if matches!(origin, Value::Alloc(Kind::Object, _)) {
                        member_write_receivers
                            .entry(origin)
                            .or_default()
                            .insert(receiver.clone());
                    }
                }
            }
        }
    }
    // A coercion resolves against the values of its operand bindings at the
    // statement's program point.
    let statement_effects: Vec<Effects> = facts
        .iter()
        .map(|fact| proofs.purity.statement_effects(fact))
        .collect();
    for effects in &statement_effects {
        effects.coerce_bindings(&mut needed);
    }
    // A statement whose reads a global-object alias may reach resolves them
    // against their settled values.
    needed.extend(
        facts
            .iter()
            .filter(|fact| {
                fact.havoc
                    .contains(&DataflowOpacityCause::GlobalObjectAliasEscape)
            })
            .flat_map(|fact| fact.reads.eager.iter())
            .cloned(),
    );
    // Member reads of a `PlainData` binding resolve against its settled
    // allocations' own properties.
    needed.extend(
        facts
            .iter()
            .flat_map(|fact| fact.declared.iter())
            .filter(|binding| proofs.graph.is_plain_data(binding.0.as_ref()))
            .cloned(),
    );
    needed = close_prefix_needed_bindings(&needed, &dependencies, &member_write_receivers, flow);
    // These indexes only build the needed-binding closure. Release them before
    // retaining the interpreter's prefix state and per-statement outcomes.
    drop(dependencies);
    drop(member_write_receivers);
    let mut writers = BTreeMap::<&Id, BTreeSet<SourceUnitOrdinal>>::new();
    for fact in facts.iter() {
        for binding in fact
            .declared
            .iter()
            .chain(&fact.rebinds.eager)
            .chain(&fact.rebinds.lazy)
        {
            writers.entry(binding).or_default().insert(fact.ordinal);
        }
    }
    let single_writer_bindings: BTreeSet<Id> = writers
        .into_iter()
        .filter(|(_, owners)| owners.len() == 1)
        .map(|(binding, _)| binding.clone())
        .collect();
    let source_unit_read_closure = SourceUnitReadClosure::new(facts);
    let prefix_effects: Vec<_> = facts
        .iter()
        .map(|fact| {
            index.owner_callable_prefix_effects[fact.ordinal.0]
                .as_ref()
                .filter(|effects| {
                    !index.dynamic_scope_owners.contains(&fact.ordinal)
                        && !index
                            .uncovered_callable_effect_owners
                            .contains(&fact.ordinal)
                        && fact.reads.first_order_lazy.is_subset(&effects.reads)
                        && fact.rebinds.first_order_lazy.is_subset(&effects.rebinds)
                })
        })
        .collect();
    let synchronous_owner_adjacency = source_unit_read_closure.synchronous_adjacency(
        facts,
        &prefix_effects
            .iter()
            .map(|effects| effects.map(|effects| &effects.reads))
            .collect::<Vec<_>>(),
    );
    for (fact, effects) in facts.iter_mut().zip(&prefix_effects) {
        fact.synchronous_prefix_effects =
            effects.map(|effects| (effects.reads.clone(), effects.rebinds.clone()));
    }
    let mut deferred_read_binding = vec![None; facts.len()];
    for (binding, owner) in flow.lexical_read_guards() {
        deferred_read_binding[owner.0] = Some(binding.clone());
    }
    let (reentry_bindings, reentry_default) = match export_reentry_policy {
        ExportReentryPolicy::Conservative => (index.exported_bindings.clone(), true),
        ExportReentryPolicy::NoStaticImporterReentry => (BTreeSet::new(), false),
        ExportReentryPolicy::ReentryThrough(names) => (
            names
                .iter()
                .filter_map(|name| index.exported_names.get(name).cloned())
                .collect(),
            names.contains("default"),
        ),
    };
    let mut exported_owners = BTreeSet::new();
    if *export_reentry_policy == ExportReentryPolicy::Conservative {
        exported_owners.extend(index.exported_declaration_owners.iter().copied());
    }
    if reentry_default {
        exported_owners.extend(index.default_export_owners.iter().copied());
    }
    exported_owners.extend(source_unit_read_closure.source_units_for_bindings(&reentry_bindings));
    let initial_export_roots = exported_owners
        .intersection(&index.hoisted_function_owners)
        .copied()
        .collect();
    let top_bindings: FxHashSet<_> = facts
        .iter()
        .flat_map(|fact| fact.declared.iter().cloned())
        .collect();
    let var_bindings: BTreeSet<_> = facts
        .iter()
        .flat_map(|fact| fact.var_declared.iter().cloned())
        .collect();
    let changed_member_roots: BTreeSet<_> = if index.top_metadata_decorations.iter().any(|call| {
        call.as_ref()
            .is_some_and(|call| !call.member_decorators.is_empty())
    }) {
        facts
            .iter()
            .flat_map(|fact| {
                fact.member_write_roots
                    .iter()
                    .chain(&fact.rebinds.eager)
                    .chain(&fact.rebinds.lazy)
                    .cloned()
            })
            .collect()
    } else {
        BTreeSet::new()
    };
    let prefix_bindings = Arc::new(TopBindings::new(
        top_bindings
            .iter()
            .chain(index.hoisted_bindings.keys())
            .cloned(),
    ));
    let mut initial_values = PrefixValues::new(prefix_bindings.clone());
    let mut initialized_bindings = BTreeSet::new();
    for (binding, key) in &index.hoisted_bindings {
        initial_values.insert(
            binding,
            Values::from([AbstractValue::Value(Value::Alloc(Kind::Function, *key))]),
        );
        initialized_bindings.insert(binding.clone());
    }
    let stale_locals: BTreeSet<Id> = index
        .callables
        .values()
        .flat_map(|callable| {
            let (readers, _) = callable.reads_before_assignment();
            callable
                .captured_writes()
                .into_iter()
                .chain(readers)
                .cloned()
                .collect::<Vec<_>>()
        })
        .filter(|binding| !top_bindings.contains(binding))
        .collect();
    let untracked_locals: BTreeSet<Id> = {
        let rebound: BTreeSet<&Id> = index
            .callables
            .values()
            .flat_map(|callable| &callable.rebinds)
            .collect();
        index
            .callables
            .values()
            .flat_map(|callable| {
                let (_, sources) = callable.reads_before_assignment();
                callable
                    .captured_writes()
                    .into_iter()
                    .cloned()
                    .chain(
                        callable
                            .captured_reads()
                            .into_iter()
                            .filter(|binding| rebound.contains(binding))
                            .cloned(),
                    )
                    .chain(sources)
                    .collect::<Vec<_>>()
            })
            .filter(|binding| !top_bindings.contains(binding))
            .collect()
    };
    let synchronous_owner_top_effects = facts
        .iter()
        .zip(&prefix_effects)
        .map(|(fact, synchronous)| OwnerLazyEffectViews {
            synchronous: synchronous.map(|effects| {
                top_level_effect_view(&effects.reads, &effects.rebinds, &top_bindings)
            }),
            incomplete: top_level_effect_view(&fact.reads.lazy, &fact.rebinds.lazy, &top_bindings),
        })
        .collect();
    let inputs = ExecutionInputs {
        metrics: metrics.clone(),
        index: &index,
        graph: proofs.graph,
        facts,
        statement_effects,
        flow,
        set_iterable_proof: proofs.set_iterable,
        shadowed,
        imported_values: &hints.imported_values,
        imported_primitive_reads: &hints.imported_primitive_reads,
        initializer_call_effect_inputs: &hints.assumptions.initializer_call_effect_inputs,
        declared_pure_initializers: &hints.assumptions.declared_pure_initializers,
        callable_heap_contents: proofs.callable_heap_contents,
        callable_prefix_reads: index
            .callables
            .iter()
            .map(|(key, callable)| {
                (
                    *key,
                    callable
                        .reads
                        .iter()
                        .filter_map(|binding| prefix_bindings.ordinal(binding))
                        .collect(),
                )
            })
            .collect(),
        prefix_binding_count: prefix_bindings.len(),
        top_bindings,
        binding_owner: facts
            .iter()
            .flat_map(|fact| fact.declared.iter().cloned().map(|id| (id, fact.ordinal)))
            .collect(),
        source_unit_read_closure,
        synchronous_owner_adjacency,
        synchronous_effect_closure: std::sync::OnceLock::new(),
        deferred_read_binding,
        synchronous_owner_top_effects,
        synchronous_owner_effects: prefix_effects,
        exported_owners,
        reentry_bindings,
        reentry_default,
        call_site_owners: index
            .top_calls
            .iter()
            .flatten()
            .chain(
                index
                    .callables
                    .values()
                    .flat_map(|callable| callable.calls.iter()),
            )
            .map(|call| (CallSite::from(call.span), (call.owner, call.span)))
            .collect(),
        reads_arguments: index
            .callable_spans
            .iter()
            .filter(|(_, span)| {
                index.callable_spans.iter().any(|(inner, inner_span)| {
                    span.contains(*inner_span)
                        && index.callables.get(inner).is_some_and(|callable| {
                            callable.reads.iter().any(|id| id.0 == "arguments")
                        })
                })
            })
            .map(|(key, _)| *key)
            .collect(),
        class_captured_reads: index
            .class_spans
            .iter()
            .map(|(class, span)| {
                let reads = index
                    .callable_spans
                    .iter()
                    .filter(|(_, callable)| span.contains(**callable))
                    .filter_map(|(key, _)| index.callables.get(key))
                    .flat_map(|callable| callable.captured_reads().into_iter().cloned())
                    .collect();
                (*class, reads)
            })
            .collect(),
        mutable_captures: index
            .callables
            .values()
            .flat_map(|callable| callable.rebinds.iter().cloned())
            .collect(),
        stale_locals,
        untracked_locals,
        captured_local_bindings: captured_local_bindings(&index),
        var_bindings,
        changed_member_roots,
        export_reentry_enabled: *export_reentry_policy
            != ExportReentryPolicy::NoStaticImporterReentry,
        needed_bindings: needed,
        single_writer_bindings,
        unknown_callee_owners: unknown_callee_owners(&index, flow),
        reaches_unknown_callee: std::sync::OnceLock::new(),
    };
    let mut world = World::new(WorldScope::Application);
    world.module.prefix_values = initial_values;
    world.module.initialized_bindings = initialized_bindings;
    world.publication.exported_runtime_values = index
        .exported_hoisted_functions
        .iter()
        .copied()
        .map(|key| AbstractValue::Value(Value::Alloc(Kind::Function, key)))
        .collect();
    let state = ExecutionState {
        world: Arc::new(world),
        native_point_effects: index.initial_native_point_effects.clone(),
        activated_native_effects: BTreeSet::new(),
        author_host_contract_uses: Vec::new(),
        active_class_member_promotions: BTreeSet::new(),
        prior_reachability_roots: Some(initial_export_roots),
        current_top_owner: None,
        proof_uses: Vec::new(),
        returned_record_proof_uses: Vec::new(),
        checking_record_factory_contract: false,
        nested_record_factory_premise: None,
        prior_effect_owners: BTreeSet::new(),
        statement_executed_owners: BTreeSet::new(),
        statement_invoked_callables: BTreeSet::new(),
        array_iteration_depth: 0,
        iteration_element_records: BTreeSet::new(),
        array_iteration_protected_objects: BTreeSet::new(),
        array_iteration_unsupported: false,
        invocation_depth: 0,
        invocation_count: 0,
        callback_fallback: CallbackFallbackSummary::default(),
        fallback_outcome: Outcome::default(),
        assumption: None,
        fallback_callback_sizes: None,
        fallback_local_owners: BTreeSet::new(),
        fallback_local_deferred: BTreeMap::new(),
        capture_contexts: CaptureInterner::default(),
        callback_sets: CaptureInterner::default(),
        active: BTreeSet::new(),
        invocation_memos: OrdMap::new(),
        export_callback_memos: ExportCallbackMemos::default(),
    };
    let results =
        super::super::runtime_inference::infer_synchronous_execution(&inputs, state, facts.len());
    drop(inputs);
    results
        .into_iter()
        .zip(facts.iter_mut())
        .map(|((calls, purity), fact)| {
            fact.synchronous_calls = Some(calls);
            purity
        })
        .collect()
}
