//! Instantiate a checked conditional provider family in the caller's world.
//!
//! Every alternative is prepared before any world changes. Its completion,
//! payload and premises then travel in one runtime leaf through the caller's
//! existing continuation, including catch and normal-only suffix execution.

use super::*;
use crate::analysis_inputs::{
    ConditionalProviderAlternative, ConditionalProviderCompletion, ConditionalProviderTransfer,
    ConditionalProviderValue, ConditionalResultAlternative, ParameterHeapRead, ProviderGraphInput,
};
use crate::facts::abstract_domain::value::HeapModuleScope;
use crate::facts::sync_effects::property_inference::{
    CapturedPropertyDescriptor, OwnPropertyDescriptorProjection,
};
use std::num::NonZeroUsize;

struct PreparedProviderAlternative {
    completion: PreparedProviderCompletion,
    effects: EffectCoverageFragment,
    heap: HeapContentCells,
    target: crate::facts::statement_facts::ConditionalProviderTargetRequirement,
    required_initialized_module_cells: BTreeSet<ModuleCellId>,
    native_global_reads: BTreeSet<&'static str>,
    inference: crate::analysis_provenance::InferenceUse,
    dependencies: ProofDependencies,
}

enum PreparedProviderValue {
    Projection(Values),
    Graph(Box<provider_graph::PreparedProviderGraph>),
}

enum PreparedProviderCompletion {
    Normal(PreparedProviderValue),
    Throw(Box<PreparedProviderThrow>),
}

struct PreparedProviderThrow {
    origin: QualifiedThrowSite,
    value: PreparedProviderValue,
    frontier: SourceOperationSite,
}

struct ProviderInput<'a> {
    call: &'a Call,
    arguments: &'a [Values],
    projection: &'a OwnPropertyDescriptorProjection,
    dependencies: &'a ProofDependencies,
    origin: HeapAddress,
}

impl Executor<'_, '_> {
    pub(super) fn conditional_provider_family(
        &mut self,
        call: &Call,
        operands: &CallOperands,
        prefix: &Outcome,
    ) -> Option<(CallTransfer, RuntimeTransferFamily)> {
        if call.has_spread
            || operands.callee.targets().is_empty()
            || operands
                .callee
                .targets()
                .contains(&AbstractValue::Value(Value::Unavailable))
        {
            return None;
        }
        if self
            .covered_foreign_call_summary(call.kind, &call.callee)
            .is_some()
        {
            let conflicting = self
                .conditional_provider_transfers(call.kind, &call.callee)
                .is_some_and(|transfers| {
                    transfers.iter().any(|transfer| {
                        self.provider_transfer_is_usable(transfer)
                            && transfer.alternatives.iter().any(|alternative| {
                                matches!(
                                    alternative.completion,
                                    ConditionalProviderCompletion::Throw { .. }
                                )
                            })
                    })
                });
            if conflicting {
                // A generic proof claims normal completion for every input.
                // A usable throw specialization contradicts that claim. Do
                // not select either summary or quietly prefer the scalar one;
                // the owning continuation summarizes this reached call once.
                let mut refused = prefix.clone();
                refused.complete = false;
                refused.unknown_call = true;
                refused.mark_uncertain_completion();
                refused.ledger.effects = EffectCoverageFragment::default();
                refused.add_reached(operands.callee.targets().clone());
                refused.add_reached(
                    operands
                        .arguments
                        .iter()
                        .flat_map(SelectedValue::retained_values),
                );
                return Some((
                    CallTransfer::UnsupportedOperands(
                        OrderedCallAdmissionRefusal::InconsistentProviderCompletion,
                    ),
                    RuntimeTransferFamily::capture(self, refused),
                ));
            }
            return None;
        }
        let arguments: Vec<_> = operands
            .arguments
            .iter()
            .map(|value| value.values.clone())
            .collect();
        if arguments.iter().any(|values| {
            values.is_empty() || values.contains(&AbstractValue::Value(Value::Unavailable))
        }) {
            // An uncertain operand is not a provider completion alternative.
            // Leave it to the existing reached-prefix refusal, without
            // splitting it into an invented normal or throw branch.
            return None;
        }
        let alternatives = self.prepare_provider_alternatives(call, &arguments)?;
        let count = NonZeroUsize::new(alternatives.len())?;
        let has_normal = alternatives.iter().any(|alternative| {
            matches!(
                &alternative.completion,
                PreparedProviderCompletion::Normal(_)
            )
        });
        let mut prefix = prefix.clone();
        prefix.add_reached(operands.callee.targets().clone());
        prefix.add_reached(
            operands
                .arguments
                .iter()
                .flat_map(SelectedValue::retained_values),
        );
        let family = RuntimeTransferFamily::fork_count(self, prefix, count);
        let mut alternatives = alternatives.into_iter();
        let family = family.map(self, |executor, mut outcome| {
            let alternative = alternatives
                .next()
                .expect("one checked transfer per runtime leaf");
            executor.apply_provider_alternative(call, alternative, &mut outcome);
            outcome
        });
        Some((
            if has_normal {
                CallTransfer::ResultAvailable
            } else {
                CallTransfer::EffectsOnly
            },
            family,
        ))
    }

    fn prepare_provider_alternatives(
        &self,
        call: &Call,
        arguments: &[Values],
    ) -> Option<Vec<PreparedProviderAlternative>> {
        let transfers = self.conditional_provider_transfers(call.kind, &call.callee)?;
        let projection =
            self.own_property_projection(arguments.first()?, OwnPropertyRequest::DescriptorLayout)?;
        let OwnPropertyProjection::Descriptors(descriptors) = &projection.projection else {
            return None;
        };
        let DescriptorSource::Object(origin) = descriptors.source_object else {
            return None;
        };
        let mut matching = transfers.iter().filter(|transfer| {
            self.provider_transfer_is_usable(transfer)
                && transfer.input.matches_projection(descriptors)
        });
        let transfer = matching.next()?;
        if matching.next().is_some() || transfer.replacement_package.is_some() {
            // Result identity and an exact throw require the source execution.
            // Replacement equivalence alone does not instantiate those values.
            return None;
        }
        let program = self.inputs.provider_programs.program(&transfer.identity)?;
        let input = ProviderInput {
            call,
            arguments,
            projection: descriptors,
            dependencies: &projection.dependencies,
            origin,
        };
        // Verify the complete family once. The returned vector preserves
        // serialized alternative order and includes current-source origins.
        let verified = super::super::strict_provider::verify_provider_family(program, transfer)?;
        if verified.len() != transfer.alternatives.len() {
            return None;
        }
        transfer
            .alternatives
            .iter()
            .zip(verified)
            .map(|(alternative, verified)| {
                self.prepare_provider_alternative(
                    &input,
                    transfer,
                    alternative,
                    program.source.scope,
                    verified,
                )
            })
            .collect()
    }

    fn prepare_provider_alternative(
        &self,
        input: &ProviderInput<'_>,
        transfer: &ConditionalProviderTransfer,
        alternative: &ConditionalProviderAlternative,
        provider_scope: HeapModuleScope,
        verified: super::super::strict_provider::VerifiedProviderAlternative,
    ) -> Option<PreparedProviderAlternative> {
        let mut heap = HeapContentCells::default();
        for read in &alternative.reads {
            heap.reads.insert(match read {
                ParameterHeapRead::ObjectShape { argument: 0 } => {
                    EffectCell::ObjectShape(input.origin)
                }
                ParameterHeapRead::OwnProperty {
                    argument: 0,
                    property,
                } => EffectCell::OwnProperty {
                    origin: input.origin,
                    property: property.clone(),
                },
                _ => return None,
            });
        }
        let mut dependencies = alternative.dependencies.clone();
        dependencies.extend(input.dependencies);
        dependencies.extend(&verified.dependencies);
        let (payload, throw_origin) = match verified.completion {
            ConditionalProviderCompletion::Normal { value } => (value, None),
            ConditionalProviderCompletion::Throw { origin, value } => {
                let origin = QualifiedThrowSite {
                    scope: provider_scope,
                    site: origin,
                };
                if !self.throw_origin_is_current(&origin) {
                    return None;
                }
                (value, Some(origin))
            }
        };
        let value = match payload {
            ConditionalProviderValue::Projection(projection) => {
                let mut values = Values::new();
                for alternative in projection.alternatives {
                    match alternative {
                        ConditionalResultAlternative::Argument { index: 0 } => {
                            values.extend(input.arguments.first()?.iter().cloned())
                        }
                        ConditionalResultAlternative::Property {
                            argument: 0,
                            property,
                        } => {
                            let read =
                                self.present_own_data_read(input.arguments.first()?, &property)?;
                            if read.cell
                                != (EffectCell::OwnProperty {
                                    origin: input.origin,
                                    property,
                                })
                            {
                                return None;
                            }
                            heap.reads.insert(read.cell);
                            values.extend(read.values);
                        }
                        ConditionalResultAlternative::Primitive { kinds } => {
                            values.insert(AbstractValue::Value(Value::Prim(kinds)));
                        }
                        _ => return None,
                    }
                }
                if values.is_empty() || values.contains(&AbstractValue::Value(Value::Unavailable)) {
                    return None;
                }
                PreparedProviderValue::Projection(values)
            }
            ConditionalProviderValue::RetainedGraph(graph) => {
                let mut substitutions = BTreeMap::new();
                for parameter in graph.inputs() {
                    let values = match &parameter {
                        ProviderGraphInput::Argument { index: 0 } => {
                            input.arguments.first()?.clone()
                        }
                        ProviderGraphInput::Property {
                            argument: 0,
                            property,
                        } => {
                            let CapturedPropertyDescriptor::Data(values) =
                                input.projection.properties.get(property)?
                            else {
                                return None;
                            };
                            heap.reads.insert(EffectCell::OwnProperty {
                                origin: input.origin,
                                property: property.clone(),
                            });
                            values.clone()
                        }
                        ProviderGraphInput::Getter {
                            argument: 0,
                            property,
                        } => {
                            let CapturedPropertyDescriptor::Accessor { getter, .. } =
                                input.projection.properties.get(property)?
                            else {
                                return None;
                            };
                            getter.clone()
                        }
                        ProviderGraphInput::Setter {
                            argument: 0,
                            property,
                        } => {
                            let CapturedPropertyDescriptor::Accessor { setter, .. } =
                                input.projection.properties.get(property)?
                            else {
                                return None;
                            };
                            setter.clone()
                        }
                        _ => return None,
                    };
                    if values.is_empty()
                        || values.contains(&AbstractValue::Value(Value::Unavailable))
                    {
                        return None;
                    }
                    substitutions.insert(parameter, values);
                }
                PreparedProviderValue::Graph(Box::new(provider_graph::PreparedProviderGraph {
                    provider: transfer.identity.clone(),
                    graph,
                    inputs: substitutions,
                    verified: Box::new(verified.graph?),
                }))
            }
        };
        heap.reads.extend(
            alternative
                .module_cells
                .reads
                .iter()
                .copied()
                .map(EffectCell::ModuleCell),
        );
        heap.writes.extend(
            alternative
                .module_cells
                .writes
                .iter()
                .copied()
                .map(EffectCell::ModuleCell),
        );
        let mut effects = alternative.effects.clone();
        if !effects.instantiate_operation_support(&[input.origin])
            || effects.has_uninstantiated_operation_support()
        {
            return None;
        }
        for receipt in effects.operation_support_receipts() {
            if let Some(cell) = receipt.projected_cell() {
                heap.reads.insert(cell);
            }
        }
        let ValueExpr::Binding(source_binding) = &input.call.callee else {
            return None;
        };
        let native_global_reads = alternative
            .native_operations
            .iter()
            .map(|operation| operation.intrinsic.global_binding())
            .collect::<Option<BTreeSet<_>>>()?;
        let identity = &transfer.identity;
        let provider_closure = transfer.provider_closure.clone()?;
        let source_initialized_host_dependencies = if alternative.module_cells.invariants.is_empty()
        {
            Some(BTreeSet::new())
        } else {
            self.inputs
                .source_initialized_host_dependencies
                .as_ref()
                .filter(|dependencies| {
                    provider_closure
                        .admitted_host_modules
                        .is_subset(dependencies)
                })
                .map(|_| provider_closure.admitted_host_modules.clone())
        };
        let target = crate::facts::statement_facts::ConditionalProviderTargetRequirement {
            owner: input.call.owner,
            call_span: input.call.span,
            source_binding: source_binding.clone(),
            defining_module: identity.defining_module.clone(),
            export: identity.export.clone(),
            local: identity.local.clone(),
            callable_key: identity.callable_key,
            provider_closure: Some(provider_closure),
            source_initialized_host_dependencies,
        };
        let body_effects = self.imported_body_effects(
            input.call,
            ImportedBodyProof {
                identity: super::super::super::effect_coverage::ImportedCallableIdentity {
                    defining_module: identity.defining_module.clone(),
                    export: identity.export.clone(),
                    local: identity.local.clone(),
                    member: None,
                    key: identity.callable_key,
                },
                effects: &effects,
                dependencies: &dependencies,
                replacement_package: None,
            },
            false,
        );
        let inference = crate::analysis_provenance::InferenceUse {
            conclusion: crate::analysis_provenance::InferenceRule::ConditionalForeignBody,
            derivation_rules: BTreeSet::from([
                crate::analysis_provenance::InferenceRule::ConditionalForeignBody,
                crate::analysis_provenance::InferenceRule::NativeIntegrity,
                crate::analysis_provenance::InferenceRule::OwnPropertyPrefixState,
            ]),
            value_input_refs: dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: BTreeSet::new(),
            argument_input_refs: dependencies.argument_inputs.clone(),
            native_operation_input_refs: dependencies.native_operation_inputs.clone(),
            imported_primitive_array_spread_input_refs: dependencies
                .imported_primitive_array_spread_inputs
                .clone(),
        };
        Some(PreparedProviderAlternative {
            completion: match throw_origin {
                Some(origin) => {
                    PreparedProviderCompletion::Throw(Box::new(PreparedProviderThrow {
                        origin,
                        value,
                        frontier: self.source_operation_site_for_call(input.call)?,
                    }))
                }
                None => PreparedProviderCompletion::Normal(value),
            },
            effects: body_effects,
            heap,
            target,
            required_initialized_module_cells: alternative
                .module_cells
                .invariants
                .keys()
                .copied()
                .collect(),
            native_global_reads,
            inference,
            dependencies,
        })
    }

    fn apply_provider_alternative(
        &mut self,
        call: &Call,
        transfer: PreparedProviderAlternative,
        outcome: &mut Outcome,
    ) {
        outcome
            .effects
            .conditional_provider_targets
            .push(transfer.target);
        outcome.ledger.effects = transfer.effects;
        outcome.ledger.dependencies.extend(&transfer.dependencies);
        for receipt in outcome.ledger.effects.operation_support_receipts() {
            outcome.effects.record_operation_support(receipt.clone());
        }
        outcome
            .effects
            .record_heap_contents(&transfer.heap, self.state.heap_access_origin);
        outcome
            .effects
            .required_initialized_module_cells
            .extend(transfer.required_initialized_module_cells);
        for property in transfer.native_global_reads {
            self.evaluation_effects
                .record(call.owner, call.span, GlobalAccessKind::Read, property);
        }
        self.record_inference_use(
            NativeIntegrityGuard::ForeignCallSummary,
            transfer.inference,
            &transfer.dependencies,
        );
        let (value, throw_origin) = match transfer.completion {
            PreparedProviderCompletion::Normal(value) => (value, None),
            PreparedProviderCompletion::Throw(thrown) => {
                let PreparedProviderThrow {
                    origin,
                    value,
                    frontier,
                } = *thrown;
                (value, Some((origin, frontier)))
            }
        };
        let selected = match value {
            PreparedProviderValue::Projection(values) => SelectedValue {
                selection: CallableSelection::observed(
                    &values,
                    Box::new(transfer.dependencies.clone()),
                ),
                values,
                postcondition: None,
            },
            PreparedProviderValue::Graph(prepared) => {
                let provider_graph::PreparedProviderGraph {
                    provider,
                    graph,
                    inputs,
                    verified,
                } = *prepared;
                outcome.ledger.allocation = AllocationJudgment::Opaque;
                let mut verified = verified;
                verified.dependencies.extend(&transfer.dependencies);
                if let Some(values) =
                    self.instantiate_verified_provider_graph(call, &verified, &inputs, outcome)
                {
                    SelectedValue {
                        selection: CallableSelection::observed(
                            &values,
                            Box::new(verified.dependencies.clone()),
                        ),
                        values,
                        postcondition: None,
                    }
                } else {
                    // The completion is source-proved, while this value graph
                    // lacks an executable caller store representation. Keep
                    // every alias/capture root in its explicit opaque carrier.
                    SelectedValue::raw(Values::from([
                        self.construct_provider_private_graph(provider, graph, inputs)
                    ]))
                }
            }
        };
        outcome.add_reached(selected.retained_values());
        outcome.ledger.completion = match throw_origin {
            None => Completion::Normal(selected),
            Some((origin, frontier)) => {
                // The origin is the actual checked producer operation. The
                // caller owns only the continuation frontier, not that origin.
                Completion::Abrupt(Box::new(KnownThrow {
                    value: selected,
                    origin,
                    frontier,
                }))
            }
        };
    }
}
