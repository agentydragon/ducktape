//! The array iteration rows: `forEach`, `map`, `filter`, and `reduce` with an
//! initial value, executed for a method the kernel selected at GetValue
//! (`NativeContext::array_method_callee`).
//!
//! The row rechecks the receiver after the arguments, invokes the callback
//! through `Executor::invoke` once per element of the dense snapshot, rechecks
//! the receiver after every invocation, and commits a fresh result array
//! through the native `plan`/`commit` path. Every check that can refuse runs
//! before the commit; a refusal keeps the effects of the callbacks that
//! already ran and summarizes the remainder.

use super::super::adapters::application::LocalExecutionContext;
use super::super::allocation_transfer::AllocationValue;
use super::super::array_transfer::{ArrayIterationSelection, iteration_snapshot};
use super::super::effect_coverage::{EffectCoverageFragment, LocalBodyIdentity, LocalCallReceipt};
use super::super::interpreter::MAX_SYNCHRONOUS_INVOCATIONS;
use super::super::kernel::frame::{
    LocalFrame, LocalValueResult, RemainderDisposition, SelectedValue,
};
use super::super::value_model::{AbstractValue, CallSite, CapturedContext, HeapAddress, Values};
use super::operation::{self, IterationResultArray};
use crate::analysis_inputs::NativeIntrinsic;
use crate::execution_evidence::{
    ExecutionOperation, ExecutionPrecisionLossReason, OrderedCallAdmissionRefusal,
};
use crate::facts::EffectCell;
use crate::facts::abstract_domain::builtins::IterationResult;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;
use swc_common::Span;

/// The callback a row may invoke: one local callable with no live captures.
struct AdmittedCallback {
    key: u32,
    owner: source_identity::SourceUnitOrdinal,
    captured: Option<CapturedContext>,
}

impl LocalExecutionContext<'_, '_, '_> {
    pub(in crate::facts::sync_effects) fn iterate_array(
        &mut self,
        frame: &mut LocalFrame,
        selection: &ArrayIterationSelection,
        arguments: &[SelectedValue],
        span: Span,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        let receiver = Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            selection.object,
        ))]);
        let mut reached = receiver.clone();
        reached.extend(
            arguments
                .iter()
                .flat_map(|argument| &argument.values)
                .cloned(),
        );
        frame
            .borrowed_records
            .extend(self.executor.reachable_objects(&reached));
        let row = selection.row;
        let proof = self
            .executor
            .inputs
            .index
            .array_iterations
            .get(&CallSite::from(span))
            .filter(|proof| proof.row.member == row.member);
        let supported = matches!(
            (row.result, arguments.len()),
            (
                IterationResult::Undefined | IterationResult::Mapped | IterationResult::Filtered,
                1
            ) | (IterationResult::Accumulated, 2)
        );
        let (Some(proof), true) = (proof, supported) else {
            return self.refuse_iteration(
                frame,
                reached,
                OrderedCallAdmissionRefusal::ArrayIterationRowUnsupported,
                span,
            );
        };
        let result_allocation = proof.result;
        let callback = match self.admitted_callback(&arguments[0].values) {
            Ok(callback) => callback,
            Err(refusal) => return self.refuse_iteration(frame, reached, refusal, span),
        };
        let (slots, mut dependencies) = match iteration_snapshot(self.executor, selection) {
            Ok(snapshot) => snapshot,
            Err(refusal) => return self.refuse_iteration(frame, reached, refusal, span),
        };
        if slots.len() > MAX_SYNCHRONOUS_INVOCATIONS
            || (matches!(
                row.result,
                IterationResult::Mapped | IterationResult::Filtered
            ) && result_allocation.is_none())
        {
            return self.refuse_iteration(
                frame,
                reached,
                OrderedCallAdmissionRefusal::ArrayIterationReceiverUnproved,
                span,
            );
        }
        let call = self
            .calls()
            .iter()
            .find(|call| {
                call.kind == super::super::execution_index::CallKind::Call && call.span == span
            })
            .expect("a source-covered call has its canonical call record");
        let Some(caller) = self.executor.local_body_identity_for_call(call) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        let non_assumed = self.executor.state.assumption.is_none();
        let object = selection.object;
        // Length, then HasProperty/Get of each index below it.
        let mut reads = vec![
            EffectCell::ObjectShape(object),
            EffectCell::OwnProperty {
                origin: object,
                property: "length".into(),
            },
            EffectCell::ArrayContents(object),
        ];
        reads.extend((0..slots.len()).map(|index| EffectCell::OwnProperty {
            origin: object,
            property: index.to_string(),
        }));
        for cell in reads {
            self.outcome.effects.record_heap_read(cell, non_assumed);
        }
        let mut effects = EffectCoverageFragment::no_effects();
        effects.depend_on(&dependencies);
        let mut accumulator =
            (row.result == IterationResult::Accumulated).then(|| arguments[1].values.clone());
        let mut mapped = Vec::with_capacity(slots.len());
        let mut kept = Values::new();
        for element in &slots {
            let mut operands = vec![
                element.clone(),
                Values::from([AbstractValue::Value(Value::Prim(PrimKinds::NUMBER))]),
                receiver.clone(),
            ];
            if let Some(accumulator) = &accumulator {
                operands.insert(0, accumulator.clone());
            }
            let prerequisites = self.executor.boxed_settled_prefix_dependencies();
            let mut nested =
                self.executor
                    .invoke(callback.key, operands, callback.captured.clone(), false);
            let returned = nested.returns.clone();
            let complete = nested.complete
                && !nested.unknown_call
                && nested.effects.precision_losses.is_empty()
                && nested.effects.uninitialized_reads.is_empty();
            let mut coverage = nested.effect_coverage.clone();
            coverage.depend_on(&prerequisites);
            coverage.selected_local_call(LocalCallReceipt {
                caller: caller.clone(),
                call_span: span,
                target: LocalBodyIdentity::Callable {
                    key: callback.key,
                    owner: callback.owner,
                },
                dependencies: prerequisites,
            });
            self.executor.apply_rebind_effects(&nested.effects.rebinds);
            self.outcome
                .absorb_call_effects(&mut nested.effects, self.executor.state.assumption.as_ref());
            self.outcome.executed_owners.extend(nested.executed_owners);
            self.outcome.unknown_call |= nested.unknown_call;
            self.outcome
                .inherit_completeness(nested.complete, nested.effects.precision_losses);
            frame.inputs.extend(returned.iter().cloned());
            frame
                .borrowed_records
                .extend(self.executor.reachable_objects(&returned));
            reached.extend(returned.iter().cloned());
            let exact_normal = complete
                && !returned.is_empty()
                && !returned.contains(&AbstractValue::Value(Value::Unavailable))
                && !returned.contains(&AbstractValue::Value(Value::Top));
            let retained = row.result != IterationResult::Mapped
                || returned
                    .iter()
                    .all(|value| self.executor.retainable_iteration_value(value));
            if !exact_normal || !retained {
                return self.refuse_iteration(
                    frame,
                    reached,
                    OrderedCallAdmissionRefusal::ArrayIterationCallbackIncomplete,
                    span,
                );
            }
            effects.compose(&coverage);
            // The callback may have written the receiver or a slot the row
            // reads; the next element is read from the same snapshot only if
            // nothing it depends on changed.
            match iteration_snapshot(self.executor, selection) {
                Ok((current, current_dependencies)) if current == slots => {
                    dependencies.extend(&current_dependencies);
                }
                Ok(_) => {
                    return self.refuse_iteration(
                        frame,
                        reached,
                        OrderedCallAdmissionRefusal::ArrayIterationReceiverUnproved,
                        span,
                    );
                }
                Err(refusal) => return self.refuse_iteration(frame, reached, refusal, span),
            }
            match row.result {
                IterationResult::Mapped => mapped.push(returned),
                IterationResult::Filtered => kept.extend(element.iter().cloned()),
                IterationResult::Accumulated => accumulator = Some(returned),
                _ => {}
            }
        }
        let fresh = match (row.result, result_allocation) {
            (IterationResult::Mapped, Some(allocation)) => {
                Some((allocation, IterationResultArray::Slots(mapped)))
            }
            (IterationResult::Filtered, Some(allocation)) => {
                Some((allocation, IterationResultArray::Contents(kept)))
            }
            _ => None,
        };
        let values = match fresh {
            Some((allocation, result)) => {
                let plan = operation::plan_iteration_result(
                    allocation,
                    result,
                    self.executor.state.world.revision.prefix,
                );
                let Ok(Some(values)) = operation::execute(self.executor, plan) else {
                    unreachable!("an iteration result is planned against the current prefix");
                };
                for cell in [
                    EffectCell::ObjectShape(allocation),
                    EffectCell::ArrayContents(allocation),
                ] {
                    self.outcome.effects.record_heap_write(cell, non_assumed);
                }
                values
            }
            None => match accumulator {
                Some(accumulator) => accumulator,
                None => Values::from([AbstractValue::UNDEFINED]),
            },
        };
        effects.depend_on(&dependencies);
        effects.selected_local_call(LocalCallReceipt {
            caller,
            call_span: span,
            target: LocalBodyIdentity::Native {
                intrinsic: NativeIntrinsic::Array,
                property: Some(format!("prototype.{}", row.member)),
            },
            dependencies: Box::new(dependencies),
        });
        frame.inputs.extend(values.iter().cloned());
        Ok(LocalValueResult {
            allocation: AllocationValue::opaque(values),
            effects,
            selection: None,
        })
    }

    fn admitted_callback(
        &self,
        values: &Values,
    ) -> Result<AdmittedCallback, OrderedCallAdmissionRefusal> {
        let (key, captured) = match values.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Value(Value::Alloc(Kind::Function, key))] => (*key, None),
            [AbstractValue::Closure(HeapAddress { callable: key, .. }, captured)] => {
                // Call-effect coverage for a callee with live captures is
                // Wave 3 row 12; until then the row refuses it.
                if !captured.values.is_empty() {
                    return Err(OrderedCallAdmissionRefusal::ArrayIterationCapturingCallback);
                }
                (*key, Some(captured.clone()))
            }
            _ => return Err(OrderedCallAdmissionRefusal::ArrayIterationCallbackUnsupported),
        };
        let index = self.executor.inputs.index;
        let callable = index
            .callables
            .get(&key)
            .ok_or(OrderedCallAdmissionRefusal::ArrayIterationCallbackUnsupported)?;
        if callable.generator
            || callable.opaque
            || callable.implicit_capture
            || index.async_callables.contains(&key)
            || callable.reads.iter().any(|id| id.0.as_ref() == "arguments")
            || callable.params.iter().any(|param| param.binding.is_none())
        {
            return Err(OrderedCallAdmissionRefusal::ArrayIterationCallbackUnsupported);
        }
        Ok(AdmittedCallback {
            key,
            owner: callable.owner,
            captured,
        })
    }

    fn refuse_iteration(
        &mut self,
        frame: &mut LocalFrame,
        reached: Values,
        refusal: OrderedCallAdmissionRefusal,
        span: Span,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        self.fallback(
            frame,
            reached,
            ExecutionPrecisionLossReason::UnsupportedOperation {
                operation: ExecutionOperation::ArrayIteration,
                ordered_field_read_refusal: None,
                ordered_call_admission_refusal: Some(refusal),
            },
            span,
        );
        Err(RemainderDisposition::AlreadySummarized)
    }
}
