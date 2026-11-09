//! The provider adapter: the five kernel context traits over a provider
//! body, and the one place a provider body is run. Sealing a summary runs the
//! body in a fresh `World` of the isolated provider scope; the world's scope
//! decides closure addresses and literal writability, and the adapter keeps
//! the ledger's heap cells out of any caller's effects.

use super::super::allocation_transfer::{self, AllocationJudgment, AllocationValue};
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::execution_index::ExecutionIndex;
use super::super::kernel::closure::{CapturePolicy, ClosureAdmission};
use super::super::kernel::context::{
    BindingContext, CallContext, CellPolicy, ControlContext, Disposition, HeapContext,
    NativeContext, Unproved,
};
use super::super::kernel::continuation::{self, LocalContinuation};
use super::super::kernel::driver;
use super::super::kernel::frame::{
    LocalCallRequest, LocalExecutionAttempt, LocalFrame, LocalValueResult, RemainderDisposition,
};
use super::super::local_operations::{DeferredCallableCreation, LocalBody};
use super::super::own_data_transfer::OwnDataContext;
use super::super::value_model::{
    AbstractValue, CallSite, CaptureInterner, CapturedContext, Context, LexicalCaptures,
    OwnPropertyShape, ProviderInput, ProviderScopeId, SymbolicInputId, ValueExpr, Values,
};
use super::super::world::{World, WorldScope};
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::facts::abstract_domain::builtins::{self, Invocation};
use crate::facts::abstract_domain::integrity::Integrity;
use crate::facts::abstract_domain::value::{Kind, Value};
use rustc_hash::FxHashSet;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

pub(in crate::facts::sync_effects) struct ProviderContext<'a> {
    pub(in crate::facts::sync_effects) index: &'a ExecutionIndex,
    pub(in crate::facts::sync_effects) bindings: Context,
    /// Every closure retains all arguments: the walk does not prove which
    /// argument a closure body can reach.
    pub(in crate::facts::sync_effects) argument_values: Values,
    /// Outer locals some closure reads; they get cells.
    pub(in crate::facts::sync_effects) captured_locals: FxHashSet<Id>,
    pub(in crate::facts::sync_effects) capture_contexts: CaptureInterner<LexicalCaptures>,
    /// Closures whose capture this query cannot represent; retaining one
    /// refuses the summary.
    pub(in crate::facts::sync_effects) unsupported_closures: BTreeSet<u32>,
    pub(in crate::facts::sync_effects) class_mode: bool,
    pub(in crate::facts::sync_effects) local_bindings: BTreeSet<Id>,
    pub(in crate::facts::sync_effects) shadowed: BTreeSet<&'static str>,
    pub(in crate::facts::sync_effects) native_constructions: BTreeSet<NativeIntrinsic>,
    /// The query's fresh heap: an isolated scope, so its cells never reach a
    /// caller's effects and no literal writability is proven.
    pub(in crate::facts::sync_effects) world: World,
}

/// A provider body the adapter ran to normal completion as allocation-only.
pub(in crate::facts::sync_effects) struct SealedProviderBody<'a> {
    /// The adapter after the run: its world holds the body's final heap.
    pub(in crate::facts::sync_effects) context: ProviderContext<'a>,
    /// The scope of this run's symbolic arguments and captures.
    pub(in crate::facts::sync_effects) scope: ProviderScopeId,
    /// The private provider var cells the body reads, in
    /// `ProviderInput::Capture` order.
    pub(in crate::facts::sync_effects) captures: Vec<Id>,
    /// A callable's returned values; a constructor's explicit returns.
    pub(in crate::facts::sync_effects) values: Values,
    pub(in crate::facts::sync_effects) dependencies: ProofDependencies,
}

struct ClosureCaptureProjection {
    local_reads: BTreeMap<u32, BTreeSet<Id>>,
    provider_reads: BTreeMap<u32, BTreeSet<Id>>,
}

/// A returned closure retains the values of outer locals it can reach. Nested
/// callable reads are collected from canonical callable spans so provider
/// private-var cells remain explicit retained captures after local transfer.
fn closure_capture_projection(
    index: &ExecutionIndex,
    body: &LocalBody,
) -> Option<ClosureCaptureProjection> {
    let outer_bindings: BTreeSet<_> = body.bindings.keys().cloned().collect();
    let mut projection = ClosureCaptureProjection {
        local_reads: BTreeMap::new(),
        provider_reads: BTreeMap::new(),
    };
    for &callable in body.closure_sites() {
        let span = index.callable_spans.get(&callable)?;
        let mut local_reads = BTreeSet::new();
        let mut provider_reads = BTreeSet::new();
        let descendants: Vec<_> = index.callables.range(callable..span.hi.0).collect();
        let mut closure_bindings = BTreeSet::new();
        for (_, nested) in &descendants {
            closure_bindings.extend(
                nested
                    .params
                    .iter()
                    .flat_map(|parameter| parameter.bindings.iter().cloned()),
            );
            closure_bindings.extend(nested.declared_locals.iter().cloned());
            closure_bindings.extend(nested.pattern_locals.iter().cloned());
            closure_bindings.extend(nested.local_functions.keys().cloned());
            closure_bindings.extend(nested.self_binding.iter().cloned());
        }
        let reads = index
            .prototype_callable_reads
            .get(&callable)
            .unwrap_or(&index.callables.get(&callable)?.reads);
        for binding in reads {
            if closure_bindings.contains(binding) {
                continue;
            }
            if outer_bindings.contains(binding) {
                local_reads.insert(binding.clone());
            } else if index.private_var_binding(binding) {
                provider_reads.insert(binding.clone());
            } else if !index.deferred_module_reference(binding) {
                // No final-frame cell or proved private capture represents this
                // enclosing lexical value. Creation cannot invent its contents.
                return None;
            }
        }
        projection.local_reads.insert(callable, local_reads);
        projection.provider_reads.insert(callable, provider_reads);
    }
    Some(projection)
}

impl<'a> ProviderContext<'a> {
    /// The adapter over a fresh world of the isolated provider scope, with
    /// one symbolic argument per parameter.
    fn sealing(
        index: &'a ExecutionIndex,
        shadowed: &BTreeSet<&'static str>,
        scope: ProviderScopeId,
        parameters: impl IntoIterator<Item = Id>,
        local_bindings: BTreeSet<Id>,
        class_mode: bool,
    ) -> Self {
        let mut bindings = Context::new();
        let mut argument_values = Values::new();
        for (parameter, binding) in parameters.into_iter().enumerate() {
            let value = AbstractValue::SymbolicInput(SymbolicInputId {
                scope,
                input: ProviderInput::Argument(parameter),
            });
            bindings.insert(binding, Values::from([value.clone()]));
            argument_values.insert(value);
        }
        Self {
            index,
            bindings,
            argument_values,
            captured_locals: FxHashSet::default(),
            capture_contexts: CaptureInterner::default(),
            unsupported_closures: BTreeSet::new(),
            class_mode,
            local_bindings,
            shadowed: shadowed.clone(),
            native_constructions: BTreeSet::new(),
            world: World::new(WorldScope::IsolatedProvider),
        }
    }

    /// Run a callable body for its summary. Direct returns and ordered bodies
    /// share their transfers with application execution; a refusal never
    /// retries another evaluator.
    pub(in crate::facts::sync_effects) fn seal_callable(
        index: &'a ExecutionIndex,
        callable: u32,
        shadowed: &BTreeSet<&'static str>,
    ) -> Option<SealedProviderBody<'a>> {
        let body = index.callables.get(&callable)?;
        let local_body = body.body_facts.ordered_plan();
        let direct_return = body.body_facts.value_flow().direct_allocation_return();
        if direct_return.is_none()
            && !local_body.is_some_and(|body| {
                body.allocation_only_syntax() || body.closure_allocation_syntax()
            })
        {
            return None;
        }
        let closure_mode = local_body.is_some_and(|body| body.closure_allocation_syntax());
        let closure_projection = if closure_mode {
            Some(closure_capture_projection(index, local_body?)?)
        } else {
            None
        };
        let scope = ProviderScopeId::fresh()?;
        let parameters = body
            .params
            .iter()
            .map(|param| param.binding.clone())
            .collect::<Option<Vec<_>>>()?;
        let mut context = Self::sealing(
            index,
            shadowed,
            scope,
            parameters,
            local_body
                .map(|body| body.bindings.keys().cloned().collect())
                .unwrap_or_default(),
            false,
        );
        let direct_return = direct_return.filter(|_| !closure_mode);
        let mut reads = BTreeSet::new();
        if let Some(expression) = direct_return {
            expression.bindings(&mut reads);
        } else {
            let local_body = local_body?;
            reads.extend(
                local_body
                    .free_reads()
                    .iter()
                    .filter(|binding| {
                        !local_body.requires_keyed_collection_new()
                            || !matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet")
                    })
                    .cloned(),
            );
        }
        if let Some(projection) = &closure_projection {
            reads.extend(projection.provider_reads.values().flatten().cloned());
            context.captured_locals = projection.local_reads.values().flatten().cloned().collect();
        }
        for binding in context.bindings.keys() {
            reads.remove(binding);
        }
        // Property-origin export inference is the next consumer for nested
        // callable keys. The current declaration-only export collector is not
        // widened by this body/capture query.
        let captures: Vec<_> = reads.into_iter().collect();
        for (capture, binding) in captures.iter().enumerate() {
            // This establishes lexical cell initialization, not value kind, heap
            // locality, immutability or privacy of anything the cell can contain.
            if !index.private_var_binding(binding) {
                return None;
            }
            context.bindings.insert(
                binding.clone(),
                Values::from([AbstractValue::SymbolicInput(SymbolicInputId {
                    scope,
                    input: ProviderInput::Capture(capture),
                })]),
            );
        }
        let mut frame = LocalFrame::new(std::mem::take(&mut context.bindings));
        let result = if let Some(expression) = direct_return {
            continuation::value_of(&mut context, &mut frame, expression).ok()?
        } else {
            context.drive(local_body?, frame)?
        };
        context.sealed(scope, captures, result)
    }

    /// Run a base constructor's lowered body (instance fields, then
    /// constructor statements) on a fresh instance. Class syntax admission is
    /// collected once in `ExecutionIndex`.
    pub(in crate::facts::sync_effects) fn seal_class(
        index: &'a ExecutionIndex,
        class: u32,
        shadowed: &BTreeSet<&'static str>,
    ) -> Option<SealedProviderBody<'a>> {
        let (body, parameters) = index.class_provider_bodies.get(&class)?;
        let local_bindings: BTreeSet<_> = body.bindings.keys().cloned().collect();
        if body.free_reads().iter().any(|binding| {
            !matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet")
                || shadowed.contains(binding.0.as_ref())
                || local_bindings.iter().any(|local| local.0 == binding.0)
        }) || !body.captured_writes().is_empty()
        {
            return None;
        }
        let scope = ProviderScopeId::fresh()?;
        let mut context = Self::sealing(
            index,
            shadowed,
            scope,
            parameters.iter().cloned(),
            local_bindings,
            true,
        );
        let mut frame = LocalFrame::new(std::mem::take(&mut context.bindings));
        frame.this_values = Some(Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            class,
        ))]));
        let result = context.drive(body, frame)?;
        context.sealed(scope, Vec::new(), result)
    }

    /// The body's returns when the kernel completes it; any refusal refuses
    /// the run.
    fn drive(&mut self, body: &LocalBody, frame: LocalFrame) -> Option<AllocationValue> {
        let (frame, attempt) = driver::drive(self, body, LocalContinuation::new(frame));
        let LocalExecutionAttempt::Completed(result) = attempt else {
            return None;
        };
        Some(AllocationValue {
            values: frame.returns(),
            judgment: result.ledger.allocation,
        })
    }

    fn sealed(
        self,
        scope: ProviderScopeId,
        captures: Vec<Id>,
        result: AllocationValue,
    ) -> Option<SealedProviderBody<'a>> {
        let AllocationJudgment::CoveredAllocationOnly(dependencies) = result.judgment else {
            return None;
        };
        Some(SealedProviderBody {
            context: self,
            scope,
            captures,
            values: result.values,
            dependencies: *dependencies,
        })
    }

    fn construct_empty_collection(
        &mut self,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<AllocationValue> {
        if !arguments.is_empty() {
            return None;
        }
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let (name, family) = match binding.0.as_ref() {
            "Map" => ("Map", NativeIntrinsic::Map),
            "Set" => ("Set", NativeIntrinsic::Set),
            "WeakMap" => ("WeakMap", NativeIntrinsic::WeakMap),
            "WeakSet" => ("WeakSet", NativeIntrinsic::WeakSet),
            _ => return None,
        };
        if self.shadowed.contains(name)
            || self.index.property_key_declarations.contains(binding)
            || self
                .local_bindings
                .iter()
                .any(|binding| binding.0.as_ref() == name)
        {
            return None;
        }
        let row = builtins::lookup(name, None, Invocation::Construct)?;
        let Integrity::Intact(dependencies) = self.index.integrity.require(row.requires, None)
        else {
            return None;
        };
        let result = allocation_transfer::keyed_collection_new(row, site, dependencies.clone())?;
        self.native_constructions.insert(family);
        Some(result.allocation)
    }
}

impl OwnDataContext for ProviderContext<'_> {
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        self.index.ordinary_record_shape(allocation)
    }
    fn object_properties_are_settled(&self, _allocation: u32) -> bool {
        // This isolated query refuses external mutation and publication.
        true
    }
    fn property_values(&self, allocation: u32, property: &str) -> Option<&Values> {
        self.world.surface.data_values(allocation, property)
    }
    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool {
        self.world
            .surface
            .has_accessor_property(allocation, property)
    }
    fn property_is_writable(&self, _allocation: u32, _property: &str) -> bool {
        false
    }
}

impl HeapContext for ProviderContext<'_> {
    fn index(&self) -> &ExecutionIndex {
        self.index
    }
    fn world(&self) -> &World {
        &self.world
    }
    fn world_mut(&mut self) -> &mut World {
        &mut self.world
    }
    fn current_prerequisites(&self) -> Box<ProofDependencies> {
        Box::new(self.world.prerequisites.clone())
    }
    fn admit_closure(
        &mut self,
        frame: &LocalFrame,
        creation: DeferredCallableCreation,
        _captures: &LexicalCaptures,
    ) -> Result<ClosureAdmission, RemainderDisposition> {
        let callable = creation.callable();
        let Some(body) = self.index.callables.get(&callable) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        let parameters: BTreeSet<_> = body
            .params
            .iter()
            .filter_map(|parameter| parameter.binding.as_ref())
            .collect();
        let parameter_bindings: BTreeSet<_> = body
            .params
            .iter()
            .flat_map(|parameter| parameter.bindings.iter())
            .collect();
        let class_unavailable_capture = body.reads.iter().any(|binding| {
            !body.declared_locals.contains(binding)
                && !parameters.contains(binding)
                && !self.local_bindings.contains(binding)
        });
        let unavailable_capture = body.reads.iter().any(|binding| {
            body.self_binding.as_ref() != Some(binding)
                && !body.declared_locals.contains(binding)
                && !body.pattern_locals.contains(binding)
                && !parameter_bindings.contains(binding)
                && !self.local_bindings.contains(binding)
                && !frame.environment.contains_key(binding)
                && !self.index.deferred_module_reference(binding)
        });
        if self.class_mode
            && (class_unavailable_capture
                || body.generator
                || self.index.async_callables.contains(&callable))
        {
            return Err(RemainderDisposition::NeedsSummary);
        }
        if unavailable_capture
            || ((self.class_mode || creation.home_object().is_none())
                && (body.generator || self.index.async_callables.contains(&callable)))
        {
            self.unsupported_closures.insert(callable);
        }
        if body.implicit_capture && self.class_mode {
            // Method/accessor receivers are dynamic, not the constructor's
            // lexical receiver. Keep class-mode admission conservative. The
            // lexical receiver is the constructed instance, the retention
            // walk's root.
            if creation.home_object().is_some() || frame.this_values.is_none() {
                return Err(RemainderDisposition::NeedsSummary);
            }
        } else if body.implicit_capture && matches!(creation, DeferredCallableCreation::Arrow(_)) {
            self.unsupported_closures.insert(callable);
        }
        Ok(ClosureAdmission {
            // Provider queries have no application call/execution receipt.
            effects: EffectCoverageFragment::default(),
            judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
        })
    }
    fn intern_captures(&mut self, captures: LexicalCaptures) -> CapturedContext {
        self.capture_contexts.intern(captures)
    }
}

impl BindingContext for ProviderContext<'_> {
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        // The frame starts with scoped arguments and checked private var
        // captures. Other entries come from the covered local operations.
        let Some(values) = frame.environment.get(binding) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        Ok(LocalValueResult {
            allocation: AllocationValue {
                values: values.clone(),
                judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
            },
            effects: Default::default(),
            selection: None,
        })
    }
    fn cell_policy(&self) -> CellPolicy<'_> {
        // A returned closure retains the final contents of the locals it
        // reads; a captured store would publish outside the query's heap.
        CellPolicy {
            captured: &self.captured_locals,
        }
    }
    fn capture_policy(&self) -> CapturePolicy<'_> {
        CapturePolicy::Exact
    }
    fn cell_effect(&self) -> EffectCoverageFragment {
        // Provider queries have no application execution receipt.
        EffectCoverageFragment::default()
    }
}

impl CallContext for ProviderContext<'_> {
    fn call(
        &mut self,
        _frame: &mut LocalFrame,
        _request: &LocalCallRequest,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        // An allocation-only query has no invocation certificate. Forwarding
        // a symbolic callable does not prove its execution or result shape.
        Err(RemainderDisposition::NeedsSummary)
    }
}

impl NativeContext for ProviderContext<'_> {
    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<AllocationValue> {
        self.construct_empty_collection(callee, arguments, site)
    }
}

impl ControlContext for ProviderContext<'_> {
    fn unproved(&mut self, _frame: &mut LocalFrame, _what: Unproved<'_>) -> Disposition {
        // Symbolic inputs and captures prove no descriptor, getter or coercion
        // behavior. Only the shared transfers can certify an operation, and
        // there is no application state to summarize into.
        Disposition::Refused
    }
}
