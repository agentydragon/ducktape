//! The allocation-only provider consumer of the shared value transfers.

use super::allocation_transfer::{self, AllocationJudgment, AllocationValue};
use super::effect_coverage::EffectCoverageFragment;
use super::execution_index::ExecutionIndex;
use super::kernel::closure::{CapturePolicy, ClosureAdmission};
use super::kernel::context::{
    BindingContext, CallContext, CellPolicy, ControlContext, Disposition, HeapContext,
    NativeContext, Unproved,
};
use super::kernel::continuation::{self, LocalContinuation};
use super::kernel::driver;
use super::kernel::frame::{
    LocalCallRequest, LocalExecutionAttempt, LocalFrame, LocalValueResult, RemainderDisposition,
};
use super::local_operations::DeferredCallableCreation;
use super::own_data_transfer::OwnDataContext;
use super::value_model::{
    AbstractValue, CallSite, CaptureInterner, CapturedContext, Context, LexicalCaptures,
    OwnPropertyShape, ProviderInput, ProviderScopeId, SymbolicInputId, ValueExpr, Values,
};
use super::world::{World, WorldScope};
use crate::analysis_inputs::{
    ForeignInvocation, ForeignReturn, NativeIntrinsic, ProofDependencies,
};
use crate::facts::abstract_domain::builtins::{self, Invocation};
use crate::facts::abstract_domain::foreign_call::DerivedForeignCall;
use crate::facts::abstract_domain::integrity::Integrity;
use crate::facts::abstract_domain::value::{Kind, Value};
use rustc_hash::FxHashSet;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

struct ProviderContext<'a> {
    index: &'a ExecutionIndex,
    bindings: Context,
    /// Every closure retains all arguments: the walk does not prove which
    /// argument a closure body can reach.
    argument_values: Values,
    /// Outer locals some closure reads; they get cells.
    captured_locals: FxHashSet<Id>,
    capture_contexts: CaptureInterner<LexicalCaptures>,
    /// Closures whose capture this query cannot represent; retaining one
    /// refuses the summary.
    unsupported_closures: BTreeSet<u32>,
    class_mode: bool,
    local_bindings: BTreeSet<Id>,
    shadowed: BTreeSet<&'static str>,
    native_constructions: BTreeSet<NativeIntrinsic>,
    /// The query's fresh heap: an isolated scope, so its cells never reach a
    /// caller's effects and no literal writability is proven.
    world: World,
}

impl ProviderContext<'_> {
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

struct ClosureCaptureProjection {
    local_reads: BTreeMap<u32, BTreeSet<Id>>,
    provider_reads: BTreeMap<u32, BTreeSet<Id>>,
}

/// A returned closure retains the values of outer locals it can reach. Nested
/// callable reads are collected from canonical callable spans so provider
/// private-var cells remain explicit retained captures after local transfer.
fn closure_capture_projection(
    index: &ExecutionIndex,
    body: &super::local_operations::LocalBody,
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

fn retain_provider_values(
    context: &ProviderContext<'_>,
    scope: ProviderScopeId,
    captures: &[Id],
    roots: impl IntoIterator<Item = AbstractValue>,
    retained_arguments: &mut BTreeSet<usize>,
    retained_provider_captures: &mut BTreeSet<String>,
) -> Option<()> {
    let mut pending: Vec<_> = roots.into_iter().collect();
    let mut visited_objects = BTreeSet::new();
    let mut visited_closures = BTreeSet::new();
    while let Some(value) = pending.pop() {
        match value {
            AbstractValue::SymbolicInput(input) if input.scope == scope => match input.input {
                ProviderInput::Argument(index) => {
                    retained_arguments.insert(index);
                }
                ProviderInput::Capture(index) => {
                    retained_provider_captures.insert(captures.get(index)?.0.to_string());
                }
            },
            AbstractValue::Value(Value::Alloc(Kind::Object, allocation)) => {
                if visited_objects.insert(allocation) {
                    pending.extend(
                        context
                            .world
                            .surface
                            .own_properties(allocation)?
                            .values()
                            .flatten()
                            .cloned(),
                    );
                }
            }
            AbstractValue::Closure(address, captured) => {
                if !visited_closures.insert((address.callable, captured.cache_identity())) {
                    continue;
                }
                if context.unsupported_closures.contains(&address.callable) {
                    return None;
                }
                pending.extend(context.argument_values.iter().cloned());
                if let Some(home_object) = captured.values.home_object {
                    pending.push(AbstractValue::Value(Value::Alloc(
                        Kind::Object,
                        home_object,
                    )));
                }
                let body = context.index.callables.get(&address.callable)?;
                let reads = context
                    .index
                    .prototype_callable_reads
                    .get(&address.callable)
                    .unwrap_or(&body.reads);
                for binding in reads {
                    if let Some(cell) = captured.values.cells.get(binding) {
                        // A cell holds the local's final contents.
                        pending.extend(context.world.cells.get(*cell)?.values.iter().cloned());
                    } else if let Some(values) = captured.values.snapshots.get(binding) {
                        pending.extend(values.iter().cloned());
                    } else if context.local_bindings.contains(binding) || captures.contains(binding)
                    {
                        // Creation captured nothing for an enclosing value
                        // the closure reads; its contents are unknown.
                        return None;
                    }
                }
            }
            AbstractValue::Value(Value::Alloc(kind, _))
                if kind.is_keyed_collection() || kind == Kind::RegExp => {}
            AbstractValue::Value(Value::Prim(_)) => {}
            _ => return None,
        }
    }
    Some(())
}

/// The canonical source collector retained whole-body syntax coverage before
/// value lowering. Direct returns and ordered bodies share their transfers
/// with application execution; refusal never retries the legacy AST evaluator.
pub(super) fn derive(
    index: &ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
) -> Option<DerivedForeignCall> {
    let body = index.callables.get(&callable)?;
    let local_body = body.body_facts.ordered_plan();
    if body
        .body_facts
        .value_flow()
        .direct_allocation_return()
        .is_none()
        && !local_body
            .is_some_and(|body| body.allocation_only_syntax() || body.closure_allocation_syntax())
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
    let mut bindings = Context::new();
    let mut argument_values = Values::new();
    for (parameter, param) in body.params.iter().enumerate() {
        let value = AbstractValue::SymbolicInput(SymbolicInputId {
            scope,
            input: ProviderInput::Argument(parameter),
        });
        bindings.insert(param.binding.clone()?, Values::from([value.clone()]));
        argument_values.insert(value);
    }
    let mut reads = BTreeSet::new();
    if let Some(expression) = body.body_facts.value_flow().direct_allocation_return()
        && !closure_mode
    {
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
    }
    for binding in bindings.keys() {
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
        bindings.insert(
            binding.clone(),
            Values::from([AbstractValue::SymbolicInput(SymbolicInputId {
                scope,
                input: ProviderInput::Capture(capture),
            })]),
        );
    }
    let mut context = ProviderContext {
        index,
        bindings,
        argument_values,
        captured_locals: closure_projection
            .as_ref()
            .map(|projection| projection.local_reads.values().flatten().cloned().collect())
            .unwrap_or_default(),
        capture_contexts: CaptureInterner::default(),
        unsupported_closures: BTreeSet::new(),
        class_mode: false,
        local_bindings: body
            .body_facts
            .ordered_plan()
            .map(|body| body.bindings.keys().cloned().collect())
            .unwrap_or_default(),
        shadowed: shadowed.clone(),
        native_constructions: BTreeSet::new(),
        world: World::new(WorldScope::IsolatedProvider),
    };
    let frame = LocalFrame::new(std::mem::take(&mut context.bindings));
    let result = if let Some(expression) = body.body_facts.value_flow().direct_allocation_return()
        && !closure_mode
    {
        let mut frame = frame;
        continuation::value_of(&mut context, &mut frame, expression).ok()?
    } else {
        let (frame, attempt) =
            driver::drive(&mut context, local_body?, LocalContinuation::new(frame));
        let LocalExecutionAttempt::Completed(result) = attempt else {
            return None;
        };
        AllocationValue {
            values: frame.returns(),
            judgment: result.ledger.allocation,
        }
    };
    let AllocationJudgment::CoveredAllocationOnly(dependencies) = result.judgment else {
        return None;
    };
    // Native constructor identity is conditional on the exact census proof;
    // transport every consumed row dependency to the summary's importer.
    let mut returns = BTreeSet::new();
    let mut retained_arguments = BTreeSet::new();
    let mut retained_provider_captures = BTreeSet::new();
    let closure = match result.values.iter().collect::<Vec<_>>().as_slice() {
        [AbstractValue::Closure(address, _)] => {
            let span = index.callable_spans.get(&address.callable)?;
            Some(span.lo.0..span.hi.0)
        }
        _ => None,
    };
    let record = if body.params.is_empty() {
        match result.values.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Value(Value::Alloc(Kind::Object, allocation))] => {
                index.method_record_members.get(allocation).cloned()
            }
            _ => None,
        }
    } else {
        None
    };
    for value in result.values {
        match value {
            AbstractValue::SymbolicInput(input) if input.scope == scope => {
                let ProviderInput::Argument(index) = input.input else {
                    // A capture itself is not a fresh return allocation. Its
                    // exact borrowed result contract has not been modeled.
                    return None;
                };
                returns.insert(ForeignReturn::Argument { index });
            }
            AbstractValue::Value(Value::Prim(_)) => {
                returns.insert(ForeignReturn::Primitive);
            }
            AbstractValue::Value(Value::Alloc(Kind::RegExp, _)) => {
                // Regex literals are fresh opaque allocations here: their
                // prototype, internal slots, methods, and lastIndex stay
                // outside the provider's retention projection.
                returns.insert(ForeignReturn::FreshAllocation);
            }
            AbstractValue::Value(Value::Alloc(kind, _)) if kind.is_keyed_collection() => {
                // Empty native construction has no user argument references
                // to traverse. Preserve its typed identity as a fresh leaf.
                returns.insert(ForeignReturn::FreshAllocation);
            }
            AbstractValue::Value(Value::Alloc(Kind::Object, allocation)) => {
                returns.insert(ForeignReturn::FreshAllocation);
                let properties = context.world.surface.own_properties(allocation)?;
                retain_provider_values(
                    &context,
                    scope,
                    &captures,
                    properties.values().flatten().cloned(),
                    &mut retained_arguments,
                    &mut retained_provider_captures,
                )?;
            }
            closure @ AbstractValue::Closure(..) => {
                returns.insert(ForeignReturn::FreshAllocation);
                retain_provider_values(
                    &context,
                    scope,
                    &captures,
                    [closure],
                    &mut retained_arguments,
                    &mut retained_provider_captures,
                )?;
            }
            _ => return None,
        }
    }
    Some(DerivedForeignCall {
        invocation: ForeignInvocation::Call,
        returns,
        retained_arguments,
        retained_provider_captures,
        native_constructions: context.native_constructions,
        dependencies: *dependencies,
        record,
        closure,
    })
}

/// Derive the constructor result with the same ordered allocation operations
/// used by ordinary local execution. Class syntax admission is collected once
/// in `ExecutionIndex`; a refused query never retries the private AST evaluator.
pub(super) fn derive_class(
    index: &ExecutionIndex,
    class: u32,
    shadowed: &BTreeSet<&'static str>,
) -> Option<DerivedForeignCall> {
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
    let mut bindings = Context::new();
    let mut argument_values = Values::new();
    for (parameter, binding) in parameters.iter().enumerate() {
        let value = AbstractValue::SymbolicInput(SymbolicInputId {
            scope,
            input: ProviderInput::Argument(parameter),
        });
        bindings.insert(binding.clone(), Values::from([value.clone()]));
        argument_values.insert(value);
    }
    let mut context = ProviderContext {
        index,
        bindings,
        argument_values,
        captured_locals: FxHashSet::default(),
        capture_contexts: CaptureInterner::default(),
        unsupported_closures: BTreeSet::new(),
        class_mode: true,
        local_bindings: body.bindings.keys().cloned().collect(),
        shadowed: shadowed.clone(),
        native_constructions: BTreeSet::new(),
        world: World::new(WorldScope::IsolatedProvider),
    };
    let mut frame = LocalFrame::new(std::mem::take(&mut context.bindings));
    frame.this_values = Some(Values::from([AbstractValue::Value(Value::Alloc(
        Kind::Object,
        class,
    ))]));
    let (frame, attempt) = driver::drive(&mut context, body, LocalContinuation::new(frame));
    let LocalExecutionAttempt::Completed(result) = attempt else {
        return None;
    };
    if !frame
        .returns()
        .iter()
        .all(|value| matches!(value, AbstractValue::Value(Value::Prim(_))))
    {
        return None;
    }
    let AllocationJudgment::CoveredAllocationOnly(dependencies) = result.ledger.allocation else {
        return None;
    };
    let mut retained_arguments = BTreeSet::new();
    let mut pending_values = vec![AbstractValue::Value(Value::Alloc(Kind::Object, class))];
    let mut visited_objects = BTreeSet::new();
    let mut visited_closures = BTreeSet::new();
    while let Some(value) = pending_values.pop() {
        match value {
            AbstractValue::SymbolicInput(input) if input.scope == scope => match input.input {
                ProviderInput::Argument(index) => {
                    retained_arguments.insert(index);
                }
                ProviderInput::Capture(_) => return None,
            },
            AbstractValue::Value(Value::Alloc(Kind::Object, allocation)) => {
                if !visited_objects.insert(allocation) {
                    continue;
                }
                if let Some(properties) = context.world.surface.own_properties(allocation) {
                    pending_values.extend(properties.values().flatten().cloned());
                }
            }
            AbstractValue::Closure(address, captured) => {
                // A constructor closure's lexical receiver is the instance,
                // this walk's root; it retains every argument.
                if visited_closures.insert((address.callable, captured.cache_identity())) {
                    pending_values.extend(context.argument_values.iter().cloned());
                }
            }
            // The shared keyed-collection constructor transfer admits only
            // empty, zero-argument constructors here. Their native storage
            // has no user argument references to traverse; keep their typed
            // identity as a leaf rather than treating them as records.
            AbstractValue::Value(Value::Alloc(kind, _)) if kind.is_keyed_collection() => {}
            AbstractValue::Value(Value::Prim(_)) => {}
            _ => return None,
        }
    }
    Some(DerivedForeignCall {
        invocation: ForeignInvocation::Construct,
        returns: BTreeSet::from([ForeignReturn::FreshAllocation]),
        retained_arguments,
        retained_provider_captures: BTreeSet::new(),
        native_constructions: context.native_constructions,
        dependencies: *dependencies,
        record: None,
        closure: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::StableArraySpreadShapes;
    use crate::facts::item_views::top_level_item_views;
    use swc_ecma_ast::{Decl, Expr, FnDecl, ModuleItem, Stmt};
    use swc_ecma_visit::{Visit, VisitWith};

    #[derive(Default)]
    struct MakeFunctionKey(Option<u32>);

    impl Visit for MakeFunctionKey {
        fn visit_fn_decl(&mut self, function: &FnDecl) {
            if function.ident.sym == "make" {
                self.0 = Some(function.function.span.lo.0);
            }
            function.visit_children_with(self);
        }
    }

    fn query(source: &str, syntax_covered: bool) -> Option<DerivedForeignCall> {
        let module = js_ast::parse_js_module("provider.js", source)
            .unwrap()
            .module;
        let key = match &module.body[0] {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => function.function.span.lo.0,
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(variable))) => {
                let Expr::Arrow(arrow) = variable.decls[0].init.as_deref().unwrap() else {
                    panic!("arrow fixture");
                };
                arrow.span.lo.0
            }
            _ => panic!("provider fixture"),
        };
        let views = top_level_item_views(&module.body);
        let prepared = super::super::execution_index::prepare_callable_capabilities(
            &views,
            StableArraySpreadShapes::default(),
            BTreeSet::from(["make".into()]),
            Vec::new(),
            Vec::new(),
            Default::default(),
            Default::default(),
        );
        assert_eq!(
            prepared.index.callables[&key]
                .body_facts
                .value_flow()
                .direct_allocation_return()
                .is_some(),
            syntax_covered,
            "{source}"
        );
        derive(&prepared.index, key, &BTreeSet::new())
    }

    #[test]
    fn strict_operators_and_terminal_branches_use_the_central_query() {
        js_ast::with_swc_globals(|| {
            for (expression, retained) in [
                (
                    "{ type: input, compare: compare === void 0 ? null : compare }",
                    BTreeSet::from([0, 1]),
                ),
                ("{ value: input !== compare }", BTreeSet::new()),
                ("{ value: void input }", BTreeSet::new()),
                ("{ value: input ? null : compare }", BTreeSet::from([1])),
            ] {
                for source in [
                    format!("function make(input, compare) {{ return {expression}; }}"),
                    format!("const make = (input, compare) => ({expression});"),
                ] {
                    let summary =
                        query(&source, true).expect("shared strict transfer, no legacy evaluator");
                    assert_eq!(
                        summary.returns,
                        BTreeSet::from([ForeignReturn::FreshAllocation])
                    );
                    assert_eq!(summary.retained_arguments, retained, "{source}");
                }
            }
            for expression in ["void input", "input === compare", "input !== compare"] {
                let summary = query(
                    &format!("function make(input, compare) {{ return {expression}; }}"),
                    true,
                )
                .unwrap();
                assert_eq!(summary.returns, BTreeSet::from([ForeignReturn::Primitive]));
                assert!(summary.retained_arguments.is_empty());
            }
            // Covered syntax can still exceed the terminal-arm transfer. It
            // must decline centrally, not retry the unmigrated evaluator.
            assert!(
                query(
                    "function make(input) { return input ? { input } : {}; }",
                    true
                )
                .is_none()
            );
            assert!(query("function make(input) { return input === missing; }", true).is_none());
            for expression in [
                "input == 1",
                "void input()",
                "input.value === 1",
                "input() ? null : input",
                "true ? input : input()",
                "void (input(), input)",
            ] {
                assert!(
                    query(
                        &format!("function make(input) {{ return {expression}; }}"),
                        false
                    )
                    .is_none()
                );
            }
        });
    }

    #[test]
    fn function_and_const_arrow_keys_reach_the_central_query() {
        js_ast::with_swc_globals(|| {
            for source in [
                "function make(input) { return { input }; }",
                "const make = input => ({ input });",
            ] {
                let summary = query(source, true).expect("central query, without legacy fallback");
                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation])
                );
                assert_eq!(summary.retained_arguments, BTreeSet::from([0]));
            }
        });
    }

    #[test]
    fn returned_closure_retains_capture_reached_through_final_local_heap() {
        js_ast::with_swc_globals(|| {
            for returned in ["wrapped", "{ async run() { await 0; return local; } }"] {
                let source = format!(
                    "function outer() {{ var captured = () => 42; function make() {{ const local = {{ callback: captured }}; const wrapped = () => local; return {returned}; }} }}"
                );
                let module = js_ast::parse_js_module("provider.js", &source)
                    .unwrap()
                    .module;
                let mut make = MakeFunctionKey::default();
                module.visit_with(&mut make);
                let key = make.0.expect("canonical nested make callable");
                let views = top_level_item_views(&module.body);
                let prepared = super::super::execution_index::prepare_callable_capabilities(
                    &views,
                    StableArraySpreadShapes::default(),
                    BTreeSet::from(["make".into()]),
                    Vec::new(),
                    Vec::new(),
                    Default::default(),
                    Default::default(),
                );
                let summary = derive(&prepared.index, key, &BTreeSet::new())
                    .expect("local transfer retains the checked provider capture");

                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation])
                );
                assert!(summary.retained_arguments.is_empty());
                assert_eq!(
                    summary.retained_provider_captures,
                    BTreeSet::from(["captured".to_owned()]),
                    "the returned closure reaches the capture through its final local record, including after await"
                );
            }
        });
    }

    #[test]
    fn returned_closure_rejects_unrepresented_enclosing_lexical_capture() {
        js_ast::with_swc_globals(|| {
            for declaration in [
                "const enclosing = 42;",
                "const { value: enclosing } = { value: 42 };",
                "const [enclosing] = [42];",
            ] {
                let source = format!(
                    "function outer() {{ {declaration} function make(input) {{ return () => enclosing + input; }} }}"
                );
                let module = js_ast::parse_js_module("provider.js", &source)
                    .unwrap()
                    .module;
                let mut make = MakeFunctionKey::default();
                module.visit_with(&mut make);
                let key = make.0.expect("canonical nested make callable");
                let views = top_level_item_views(&module.body);
                let prepared = super::super::execution_index::prepare_callable_capabilities(
                    &views,
                    StableArraySpreadShapes::default(),
                    BTreeSet::from(["make".into()]),
                    Vec::new(),
                    Vec::new(),
                    Default::default(),
                    Default::default(),
                );

                assert!(
                    prepared.index.callables[&key]
                        .body_facts
                        .ordered_plan()
                        .is_some_and(|body| body.closure_allocation_syntax()),
                    "the source is admitted to the shared closure transfer"
                );
                assert!(
                    derive(&prepared.index, key, &BTreeSet::new()).is_none(),
                    "an enclosing const that is absent from the final local frame is not a deferred module binding"
                );
            }
        });
    }
}
